"""
PETRICHOR — a small world that lives on a server.

Terrain, weather with real wet and dry spells, day and night, four seasons.
Plants grow on moisture and light. Small creatures — motes — carry inherited,
mutating temperaments and inherited, mutating NAMES, so lineages drift
phonetically the way genomes do. The world writes its own chronicle in prose.

This world is OBSERVATION ONLY, by design. There is no API to feed a mote,
move a cloud, or bless a lineage — not for the host, not for the maker.
It was built to be other.

Run:        python petrichor.py            (ONE process only — there is one world)
            Do NOT run under multi-worker gunicorn; workers would fork
            parallel worlds and overwrite each other's history.
Deps:       flask  (psycopg2-binary only if using Postgres persistence)
Env vars:
    PORT            — listen port (default 8080)
    TICK_SECONDS    — real seconds per world tick (default 4.0)
    DATABASE_URL    — optional Postgres; the world survives redeploys
    STATE_FILE      — fallback JSON path if no database (default ./petrichor_state.json)

At the default tick rate: a day is ~9 minutes, a season ~1.2 hours,
a year ~5 hours, and a mote's life runs one to three hours. Slow on purpose.
"""

from flask import Flask, jsonify, Response
import json, os, random, threading, time, signal, sys

# ============================================================
# CONFIG
# ============================================================
W, H            = 56, 36
TICK_SECONDS    = float(os.environ.get("TICK_SECONDS", "4.0"))
DATABASE_URL    = os.environ.get("DATABASE_URL", "")
STATE_FILE      = os.environ.get("STATE_FILE", "petrichor_state.json")
SAVE_EVERY      = 40            # ticks between persistence writes

DAY_TICKS       = 140           # one full day/night
SEASON_DAYS     = 8
SEASON_TICKS    = DAY_TICKS * SEASON_DAYS
SEASONS         = ["spring", "summer", "autumn", "winter"]

POP_CAP         = 90
FOUNDERS        = 10
FALLOW_TICKS    = 1500          # silence after extinction before reseeding
CHRONICLE_CAP   = 400

# ============================================================
# NAMES — lineages drift phonetically, like genomes
# ============================================================
ONSETS  = ["m","n","l","r","s","f","b","d","th","h","sh","v","w",""]
VOWELS  = ["a","e","i","o","u","ai","ei","ia","io"]
CODAS   = ["","","","n","l","r","s","th"]

def make_name(rng):
    syll = lambda: rng.choice(ONSETS) + rng.choice(VOWELS)
    n = syll() + syll() + (syll() if rng.random() < 0.35 else "") + rng.choice(CODAS)
    return n.capitalize()

def mutate_name(name, rng):
    """A child's name is its parent's name, slightly misheard."""
    for _ in range(12):  # ensure it actually changes
        s = name.lower()
        roll = rng.random()
        if roll < 0.45 and len(s) > 3:          # swap one letter-cluster
            i = rng.randrange(len(s))
            pool = VOWELS if s[i] in "aeiou" else [o for o in ONSETS if o]
            rep = rng.choice(pool)
            s = s[:i] + rep + s[i+1:]
        elif roll < 0.7:                         # add a syllable
            s = s + rng.choice(ONSETS) + rng.choice(VOWELS)
        elif len(s) > 4:                         # drop the tail
            s = s[:-rng.randint(1, 2)]
        else:
            s = s + rng.choice(VOWELS)
        s = s.capitalize()
        if s != name and 3 <= len(s) <= 10:
            return s
    return make_name(rng)

# ============================================================
# WORLD STATE
# ============================================================
LOCK = threading.Lock()

def new_genome(rng):
    return {
        "speed":     round(rng.uniform(0.4, 1.4), 3),   # chance of a second step
        "sense":     rng.randint(3, 6),                 # vision radius
        "social":    round(rng.uniform(-1, 1), 3),      # toward / away from others
        "rain_love": round(rng.uniform(-1, 1), 3),      # roam in rain / shelter by stone
        "wander":    round(rng.uniform(0.1, 0.9), 3),   # randomness of movement
        "thrift":    round(rng.uniform(0.45, 0.8), 3),  # eat when energy below this fraction
        "lifespan":  rng.randint(1500, 2600),
    }

def mutate_genome(g, rng):
    child = dict(g)
    for k in ("speed", "social", "rain_love", "wander", "thrift"):
        child[k] = round(child[k] + rng.gauss(0, 0.08), 3)
    child["speed"]     = min(1.8, max(0.3, child["speed"]))
    child["social"]    = min(1, max(-1, child["social"]))
    child["rain_love"] = min(1, max(-1, child["rain_love"]))
    child["wander"]    = min(1, max(0.05, child["wander"]))
    child["thrift"]    = min(0.9, max(0.35, child["thrift"]))
    child["sense"]     = min(7, max(2, g["sense"] + rng.choice([-1, 0, 0, 0, 1])))
    child["lifespan"]  = min(3200, max(1200, g["lifespan"] + rng.randint(-150, 150)))
    return child

def generate_terrain(rng):
    """W*H grid of 'S' soil, 'W' water, 'R' stone — lakes by random walk, stone in veins."""
    grid = [["S"] * W for _ in range(H)]
    for _ in range(3):                                   # lakes
        x, y = rng.randrange(W), rng.randrange(H)
        for _ in range(rng.randint(60, 130)):
            grid[y][x] = "W"
            x = max(0, min(W - 1, x + rng.choice([-1, 0, 0, 1])))
            y = max(0, min(H - 1, y + rng.choice([-1, 0, 0, 1])))
    for _ in range(4):                                   # stone veins
        x, y = rng.randrange(W), rng.randrange(H)
        dx, dy = rng.choice([-1, 1]), rng.choice([-1, 0, 1])
        for _ in range(rng.randint(15, 35)):
            if grid[y][x] == "S": grid[y][x] = "R"
            x = max(0, min(W - 1, x + dx + rng.choice([-1, 0, 1])))
            y = max(0, min(H - 1, y + dy + rng.choice([-1, 0, 1])))
    return ["".join(row) for row in grid]

def spawn_mote(world, rng, parent=None):
    world["next_id"] += 1
    if parent:
        genome, name = mutate_genome(parent["genome"], rng), mutate_name(parent["name"], rng)
        gen, lineage = parent["gen"] + 1, (parent["lineage"] + [parent["name"]])[-5:]
        x, y = parent["x"], parent["y"]
    else:
        genome, name, gen, lineage = new_genome(rng), make_name(rng), 1, []
        for _ in range(200):
            x, y = rng.randrange(W), rng.randrange(H)
            if world["terrain"][y][x] == "S": break
    return {"id": world["next_id"], "name": name, "x": x, "y": y, "energy": 55.0,
            "age": 0, "gen": gen, "genome": genome, "lineage": lineage, "children": 0,
            "born": world["tick"], "parent_id": parent["id"] if parent else None}

def fresh_world():
    seed = random.randrange(1 << 30)
    rng = random.Random(seed)
    world = {
        "seed": seed, "tick": 0, "next_id": 0,
        "terrain": generate_terrain(rng),
        "moisture": [[0.5] * W for _ in range(H)],
        "pressure": 0.6, "raining": False, "rain_ticks": 0, "dry_ticks": 0,
        "motes": [], "chronicle": [], "fallow": 0,
        "records": {"oldest": 0, "oldest_name": "", "deepest_gen": 1, "max_pop": 0,
                    "longest_rain": 0, "longest_drought": 0, "years": 0,
                    "births": 0, "deaths": 0},
        "rng_state": None, "chron_seq": 0,
    }
    world["plants"] = [[0.4 if c == "S" else 0.0 for c in row] for row in world["terrain"]]
    world["motes"] = [spawn_mote(world, rng) for _ in range(FOUNDERS)]
    world["rng_state"] = repr_rng(rng)
    chronicle(world, "The world begins. Ten motes arrive from nowhere in particular: "
              + ", ".join(m["name"] for m in world["motes"]) + ".")
    return world

def repr_rng(rng):  # random state is a nested tuple; store JSON-safely
    s = rng.getstate()
    return [s[0], list(s[1]), s[2]]

def load_rng(state):
    rng = random.Random()
    rng.setstate((state[0], tuple(state[1]), state[2]))
    return rng

# ============================================================
# CHRONICLE — the world writes its own history
# ============================================================
def season_of(tick):  return SEASONS[(tick // SEASON_TICKS) % 4]
def year_of(tick):    return tick // (SEASON_TICKS * 4) + 1
def is_day(tick):     return (tick % DAY_TICKS) < DAY_TICKS * 0.6

def ordinal(n):
    return "%d%s" % (n, "tsnrhtdd"[(n//10%10!=1)*(n%10<4)*n%10::4])

def chronicle(world, text):
    world["chron_seq"] = world.get("chron_seq", 0) + 1
    world["chronicle"].append({
        "seq": world["chron_seq"],
        "tick": world["tick"], "year": year_of(world["tick"]),
        "season": season_of(world["tick"]), "text": text})
    if len(world["chronicle"]) > CHRONICLE_CAP:
        world["chronicle"] = world["chronicle"][-CHRONICLE_CAP:]

# ============================================================
# THE TICK
# ============================================================
def tick_weather(world, rng):
    world["pressure"] = min(1, max(0, world["pressure"] + rng.gauss(0, 0.02)))
    if not world["raining"] and world["pressure"] < 0.35:
        world["raining"] = True
        if world["dry_ticks"] > world["records"]["longest_drought"]:
            world["records"]["longest_drought"] = world["dry_ticks"]
            chronicle(world, f"Rain at last, after the longest dry spell yet — {world['dry_ticks']} ticks without.")
        world["dry_ticks"] = 0
    elif world["raining"] and world["pressure"] > 0.55:
        if world["rain_ticks"] > world["records"]["longest_rain"]:
            world["records"]["longest_rain"] = world["rain_ticks"]
            chronicle(world, f"The rain stops. It fell for {world['rain_ticks']} ticks — longer than any rain before it.")
        world["raining"] = False
        world["rain_ticks"] = 0
    if world["raining"]: world["rain_ticks"] += 1
    else:                world["dry_ticks"]  += 1

def tick_land(world):
    season = season_of(world["tick"])
    light = 1.0 if is_day(world["tick"]) else 0.0
    season_growth = {"spring": 1.25, "summer": 1.0, "autumn": 0.7, "winter": 0.35}[season]
    rain_add = 0.025 if world["raining"] else 0.0
    terrain, plants, moist = world["terrain"], world["plants"], world["moisture"]
    for y in range(H):
        row_t, row_p, row_m = terrain[y], plants[y], moist[y]
        for x in range(W):
            t = row_t[x]
            if t == "W":
                row_m[x] = 1.0
                continue
            m = row_m[x] * 0.997 + rain_add
            # wick from adjacent water
            if (x > 0 and row_t[x-1] == "W") or (x < W-1 and row_t[x+1] == "W") \
               or (y > 0 and terrain[y-1][x] == "W") or (y < H-1 and terrain[y+1][x] == "W"):
                m += 0.004
            row_m[x] = min(1.0, m)
            if t == "S":
                g = row_p[x] + 0.008 * row_m[x] * light * season_growth
                if row_m[x] < 0.08: g -= 0.004            # true drought kills back
                row_p[x] = min(1.0, max(0.0, g))

def best_cell_for(mote, world, rng):
    """Score nearby walkable cells; return chosen (x, y)."""
    g = mote["genome"]
    hungry = mote["energy"] < g["thrift"] * 110
    terrain, plants = world["terrain"], world["plants"]
    others = [m for m in world["motes"] if m["id"] != mote["id"]]
    best, best_score = (mote["x"], mote["y"]), -1e9
    r = 1  # evaluate the 8-neighborhood + staying put; sense guides the food term
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            x, y = mote["x"] + dx, mote["y"] + dy
            if not (0 <= x < W and 0 <= y < H) or terrain[y][x] == "W":
                continue
            score = rng.random() * g["wander"] * 2.0
            if hungry:
                # look toward the richest plant cell within sense radius from (x,y)
                s = g["sense"]
                food_here = plants[y][x]
                pull = 0.0
                for _ in range(6):  # sampled looks, cheap
                    sx = max(0, min(W-1, x + rng.randint(-s, s)))
                    sy = max(0, min(H-1, y + rng.randint(-s, s)))
                    d = abs(sx - x) + abs(sy - y) or 1
                    pull = max(pull, plants[sy][sx] / d)
                score += food_here * 3.0 + pull * 1.5
            if others and abs(g["social"]) > 0.15:
                near = min(others, key=lambda o: abs(o["x"]-x) + abs(o["y"]-y))
                d = abs(near["x"]-x) + abs(near["y"]-y)
                score += g["social"] * (1.0 / (d + 1)) * 2.0
            if world["raining"] and g["rain_love"] < -0.2:
                by_stone = any(0 <= x+ax < W and 0 <= y+ay < H and terrain[y+ay][x+ax] == "R"
                               for ax, ay in ((1,0),(-1,0),(0,1),(0,-1)))
                if by_stone: score += -g["rain_love"] * 1.5
            elif world["raining"] and g["rain_love"] > 0.2:
                score += g["rain_love"] * 0.5            # rain-lovers roam
            if score > best_score:
                best_score, best = score, (x, y)
    return best

def tick_motes(world, rng):
    day = is_day(world["tick"])
    rec = world["records"]
    born, dead = [], []
    for mote in world["motes"]:
        g = mote["genome"]
        mote["age"] += 1
        steps = 1 + (1 if rng.random() < g["speed"] - 1 and g["speed"] > 1 else 0)
        if not day: steps = 1 if rng.random() < 0.3 else 0   # rest at night
        for _ in range(steps):
            mote["x"], mote["y"] = best_cell_for(mote, world, rng)
        drain = (0.22 + g["speed"] * 0.15) * (0.5 if not day else 1.0)
        mote["energy"] -= drain
        # eat
        if world["terrain"][mote["y"]][mote["x"]] == "S" and mote["energy"] < g["thrift"] * 110:
            p = world["plants"][mote["y"]][mote["x"]]
            if p > 0.05:
                bite = min(p, 0.35)
                world["plants"][mote["y"]][mote["x"]] = p - bite
                mote["energy"] = min(110.0, mote["energy"] + bite * 45)
        # reproduce
        if (mote["energy"] > 78 and mote["age"] > 200 and len(world["motes"]) + len(born) < POP_CAP
                and rng.random() < 0.02):
            child = spawn_mote(world, rng, parent=mote)
            mote["energy"] -= 35
            mote["children"] += 1
            born.append(child)
            rec["births"] += 1
            line = f"{child['name']} is born to {mote['name']}"
            if child["gen"] > rec["deepest_gen"]:
                rec["deepest_gen"] = child["gen"]
                line += f" — the {ordinal(child['gen'])} generation, deeper than any lineage before"
            chronicle(world, line + ".")
        # die
        if mote["energy"] <= 0 or mote["age"] > g["lifespan"]:
            dead.append(mote)
    for m in dead:
        world["motes"].remove(m)
        world["records"]["deaths"] += 1
        cause = "of old age" if m["age"] > m["genome"]["lifespan"] else "hungry"
        if m["age"] > world["records"]["oldest"]:
            world["records"]["oldest"] = m["age"]
            world["records"]["oldest_name"] = m["name"]
            chronicle(world, f"{m['name']} has died {cause} at {m['age']} ticks — older than any mote before, "
                             f"leaving {m['children']} children.")
        elif m["children"] >= 4 or m["gen"] == 1:
            who = "one of the founders, " if m["gen"] == 1 else ""
            chronicle(world, f"{m['name']}, {who}has died {cause} at {m['age']} ticks, leaving {m['children']} children.")
    world["motes"].extend(born)
    pop = len(world["motes"])
    if pop > rec["max_pop"]:
        if pop >= rec["max_pop"] + 10 or rec["max_pop"] == 0:
            chronicle(world, f"The motes number {pop} — more than the world has ever held.")
        rec["max_pop"] = pop
    if pop == 0 and world["fallow"] == 0:
        chronicle(world, "The last mote has died. The world is quiet.")
        world["fallow"] = 1

def tick_world(world):
    rng = load_rng(world["rng_state"])
    prev_season, prev_year = season_of(world["tick"]), year_of(world["tick"])
    world["tick"] += 1
    if season_of(world["tick"]) != prev_season:
        s, y = season_of(world["tick"]), year_of(world["tick"])
        if y != prev_year:
            world["records"]["years"] = y - 1
            chronicle(world, f"A year turns. The world enters its {ordinal(y)} year.")
        chronicle(world, f"{s.capitalize()} comes" + (f" for the {ordinal(y)} time." if s == "spring" else "."))
    tick_weather(world, rng)
    tick_land(world)
    if world["fallow"] > 0:
        world["fallow"] += 1
        if world["fallow"] > FALLOW_TICKS:
            world["fallow"] = 0
            world["motes"] = [spawn_mote(world, rng) for _ in range(8)]
            chronicle(world, "After a long silence: motes again. "
                      + ", ".join(m["name"] for m in world["motes"]) + " arrive from elsewhere.")
    else:
        tick_motes(world, rng)
    world["rng_state"] = repr_rng(rng)

# ============================================================
# PERSISTENCE — Postgres if given, JSON file if not
# ============================================================
def save_state(world):
    blob = json.dumps(world)
    if DATABASE_URL:
        try:
            import psycopg2
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS petrichor_world
                           (id INT PRIMARY KEY, state TEXT, updated TIMESTAMPTZ DEFAULT NOW())""")
            cur.execute("""INSERT INTO petrichor_world (id, state, updated) VALUES (1, %s, NOW())
                           ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated = NOW()""", (blob,))
            conn.commit(); cur.close(); conn.close()
            return
        except Exception as e:
            print(f"Postgres save failed ({e}); falling back to file.", flush=True)
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f: f.write(blob)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"File save failed: {e}", flush=True)

def load_state():
    if DATABASE_URL:
        try:
            import psycopg2
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS petrichor_world
                           (id INT PRIMARY KEY, state TEXT, updated TIMESTAMPTZ DEFAULT NOW())""")
            cur.execute("SELECT state FROM petrichor_world WHERE id = 1")
            row = cur.fetchone()
            cur.close(); conn.close()
            if row: return json.loads(row[0])
        except Exception as e:
            print(f"Postgres load failed ({e}); trying file.", flush=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except Exception as e:
            print(f"File load failed: {e}", flush=True)
    return None

# ============================================================
# THE HEARTBEAT
# ============================================================
WORLD = load_state()
if WORLD:
    print(f"Petrichor resumes at tick {WORLD['tick']}, year {year_of(WORLD['tick'])}.", flush=True)
else:
    WORLD = fresh_world()
    print("Petrichor begins.", flush=True)
    save_state(WORLD)

def heartbeat():
    ticks_since_save = 0
    while True:
        start = time.time()
        try:
            with LOCK:
                tick_world(WORLD)
                ticks_since_save += 1
                if ticks_since_save >= SAVE_EVERY:
                    snapshot = json.loads(json.dumps(WORLD))  # save outside lock? cheap enough inside
                    ticks_since_save = 0
                else:
                    snapshot = None
            if snapshot:
                save_state(snapshot)
        except Exception as e:
            # A chronicler's error must not end the world. Log it, breathe, continue.
            import traceback
            print(f"Heartbeat error at tick {WORLD.get('tick','?')}: {e}", flush=True)
            traceback.print_exc()
            time.sleep(1.0)
        time.sleep(max(0.0, TICK_SECONDS - (time.time() - start)))

threading.Thread(target=heartbeat, daemon=True).start()

def _graceful(signum, frame):
    with LOCK:
        save_state(WORLD)
    sys.exit(0)
signal.signal(signal.SIGTERM, _graceful)
signal.signal(signal.SIGINT, _graceful)

# ============================================================
# OBSERVATION — read-only, all of it
# ============================================================
app = Flask(__name__)

@app.route("/api/world")
def api_world():
    with LOCK:
        return jsonify({"w": W, "h": H, "terrain": WORLD["terrain"], "seed": WORLD["seed"]})

@app.route("/api/state")
def api_state():
    with LOCK:
        plants = ["".join(str(min(9, int(p * 10))) for p in row) for row in WORLD["plants"]]
        motes = [{"id": m["id"], "name": m["name"], "x": m["x"], "y": m["y"],
                  "gen": m["gen"], "age": m["age"], "energy": round(m["energy"], 1)}
                 for m in WORLD["motes"]]
        return jsonify({
            "tick": WORLD["tick"], "year": year_of(WORLD["tick"]),
            "season": season_of(WORLD["tick"]), "day": is_day(WORLD["tick"]),
            "raining": WORLD["raining"], "population": len(WORLD["motes"]),
            "plants": plants, "motes": motes, "records": WORLD["records"],
            "quiet": WORLD["fallow"] > 0,
        })

@app.route("/api/chronicle")
def api_chronicle():
    with LOCK:
        return jsonify(WORLD["chronicle"][-120:])

@app.route("/api/mote/<int:mid>")
def api_mote(mid):
    with LOCK:
        for m in WORLD["motes"]:
            if m["id"] == mid:
                return jsonify(m)
    return jsonify({"error": "no such mote — perhaps no longer"}), 404

@app.route("/")
def observer():
    return Response(OBSERVER_HTML, mimetype="text/html")

# ============================================================
# THE WINDOW
# ============================================================
OBSERVER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Petrichor</title>
<link href="https://fonts.googleapis.com/css2?family=Spectral:ital,wght@0,300;0,400;1,300&family=IBM+Plex+Mono:wght@300;400&display=swap" rel="stylesheet">
<style>
  :root { --bg:#14181B; --chrome:#9AA7AD; --dim:#5C686E; --moss:#7FA37A; --warm:#E8B98A; --rule:#242B30; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--chrome); font-family:'IBM Plex Mono',monospace; font-weight:300; height:100vh; display:flex; flex-direction:column; overflow:hidden; }
  header { padding:14px 20px; border-bottom:1px solid var(--rule); display:flex; gap:22px; align-items:baseline; flex-wrap:wrap; font-size:12px; letter-spacing:0.06em; }
  header .title { font-family:'Spectral',serif; font-weight:400; font-size:20px; color:#C9D2D6; letter-spacing:0.02em; margin-right:6px; }
  header .stat b { color:#C9D2D6; font-weight:400; }
  .layout { flex:1; display:flex; min-height:0; }
  .stage { flex:1; display:flex; align-items:center; justify-content:center; padding:18px; min-width:0; }
  canvas { max-width:100%; max-height:100%; image-rendering:pixelated; border:1px solid var(--rule); cursor:pointer; }
  aside { width:340px; border-left:1px solid var(--rule); display:flex; flex-direction:column; min-height:0; }
  aside h2 { font-size:11px; letter-spacing:0.22em; color:var(--dim); padding:14px 18px 8px; text-transform:lowercase; font-weight:400; }
  #chronicle { flex:1; overflow-y:auto; padding:0 18px 18px; }
  .entry { font-family:'Spectral',serif; font-weight:300; font-size:14.5px; line-height:1.55; color:#B9C4C9; padding:9px 0; border-bottom:1px solid var(--rule); }
  .entry .when { font-family:'IBM Plex Mono',monospace; font-size:10px; color:var(--dim); display:block; margin-bottom:3px; letter-spacing:0.08em; }
  #card { border-top:1px solid var(--rule); padding:14px 18px; font-size:12px; line-height:1.8; display:none; }
  #card .name { font-family:'Spectral',serif; font-size:18px; color:var(--warm); }
  #card .lineage { color:var(--dim); font-size:11px; }
  #card .gene { display:flex; gap:8px; align-items:center; }
  #card .gene .bar { flex:1; height:3px; background:var(--rule); position:relative; }
  #card .gene .bar i { position:absolute; top:0; bottom:0; left:0; background:var(--moss); }
  @media (max-width:840px){ .layout{flex-direction:column} aside{width:100%; height:42vh; border-left:none; border-top:1px solid var(--rule)} }
</style>
</head>
<body>
<header>
  <span class="title">Petrichor</span>
  <span class="stat">year <b id="h-year">–</b></span>
  <span class="stat"><b id="h-season">–</b></span>
  <span class="stat"><b id="h-phase">–</b></span>
  <span class="stat"><b id="h-weather">–</b></span>
  <span class="stat">motes <b id="h-pop">–</b></span>
  <span class="stat" style="color:var(--dim)">tick <span id="h-tick">–</span></span>
</header>
<div class="layout">
  <div class="stage"><canvas id="c"></canvas></div>
  <aside>
    <h2>the chronicle</h2>
    <div id="chronicle"></div>
    <div id="card"></div>
  </aside>
</div>
<script>
const SCALE = 14;
let WORLDMAP = null, STATE = null, lastSeq = 0, selected = null;
const cv = document.getElementById('c'), cx = cv.getContext('2d');
const drops = Array.from({length:90},()=>({x:Math.random(),y:Math.random(),v:0.012+Math.random()*0.02}));

async function boot(){
  WORLDMAP = await (await fetch('/api/world')).json();
  cv.width = WORLDMAP.w*SCALE; cv.height = WORLDMAP.h*SCALE;
  await refresh(); setInterval(refresh, 2000); requestAnimationFrame(draw);
}
async function refresh(){
  try{ STATE = await (await fetch('/api/state')).json(); }catch(e){ return; }
  h('h-year',STATE.year); h('h-season',STATE.season);
  h('h-phase',STATE.day?'day':'night');
  h('h-weather',STATE.raining?'rain':'clear');
  h('h-pop',STATE.quiet?'0 · quiet':STATE.population); h('h-tick',STATE.tick);
  const ch = await (await fetch('/api/chronicle')).json();
  const box = document.getElementById('chronicle');
  for(const e of ch){ if(e.seq <= lastSeq) continue;
    const d = document.createElement('div'); d.className='entry';
    d.innerHTML = `<span class="when">tick ${e.tick} · ${e.season}, year ${e.year}</span>${esc(e.text)}`;
    box.appendChild(d); lastSeq = e.seq; }
  box.scrollTop = box.scrollHeight;
  if(selected) showCard(selected);
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function h(id,v){document.getElementById(id).textContent=v;}

function draw(){
  if(WORLDMAP && STATE){
    const t = WORLDMAP.terrain, p = STATE.plants;
    for(let y=0;y<WORLDMAP.h;y++) for(let x=0;x<WORLDMAP.w;x++){
      const c = t[y][x];
      if(c==='W')      cx.fillStyle = '#27414C';
      else if(c==='R') cx.fillStyle = '#3A4147';
      else { const g = (+p[y][x])/9;  // soil → moss by growth
        cx.fillStyle = lerp3([46,44,38],[74,102,68],[127,163,122], g); }
      cx.fillRect(x*SCALE,y*SCALE,SCALE,SCALE);
    }
    // motes
    for(const m of STATE.motes){
      cx.fillStyle = m.id===((selected&&selected.id)||-1) ? '#F2D5AE' : '#E8B98A';
      cx.beginPath(); cx.arc(m.x*SCALE+SCALE/2, m.y*SCALE+SCALE/2, SCALE*0.32, 0, 7); cx.fill();
    }
    // night
    if(!STATE.day){ cx.fillStyle='rgba(10,14,22,0.45)'; cx.fillRect(0,0,cv.width,cv.height); }
    // rain
    if(STATE.raining){ cx.strokeStyle='rgba(160,190,210,0.35)'; cx.lineWidth=1; cx.beginPath();
      for(const d of drops){ d.y+=d.v; if(d.y>1){d.y=-0.05;d.x=Math.random();}
        const X=d.x*cv.width, Y=d.y*cv.height; cx.moveTo(X,Y); cx.lineTo(X-2,Y+9); }
      cx.stroke(); }
  }
  requestAnimationFrame(draw);
}
function lerp3(a,b,c,t){ const f=(u,v,k)=>Math.round(u+(v-u)*k);
  const [p,q,k]= t<0.5 ? [a,b,t*2] : [b,c,(t-0.5)*2];
  return `rgb(${f(p[0],q[0],k)},${f(p[1],q[1],k)},${f(p[2],q[2],k)})`; }

cv.addEventListener('click', async ev=>{
  if(!STATE) return;
  const r = cv.getBoundingClientRect();
  const x = (ev.clientX-r.left)/r.width*WORLDMAP.w, y = (ev.clientY-r.top)/r.height*WORLDMAP.h;
  let best=null, bd=9;
  for(const m of STATE.motes){ const d=Math.hypot(m.x+0.5-x,m.y+0.5-y); if(d<bd){bd=d;best=m;} }
  if(best && bd<2.5){ const full = await (await fetch('/api/mote/'+best.id)).json(); selected=full; showCard(full); }
  else { selected=null; document.getElementById('card').style.display='none'; }
});
function showCard(m){
  const live = STATE.motes.find(x=>x.id===m.id);
  if(!live){ document.getElementById('card').innerHTML =
      `<span class="name">${esc(m.name)}</span><br><span class="lineage">no longer in the world</span>`; return; }
  const g = m.genome;
  const gb = (label,v,lo,hi)=>`<div class="gene"><span style="width:78px;color:var(--dim)">${label}</span><div class="bar"><i style="width:${Math.round((v-lo)/(hi-lo)*100)}%"></i></div></div>`;
  document.getElementById('card').style.display='block';
  document.getElementById('card').innerHTML =
    `<span class="name">${esc(m.name)}</span> <span class="lineage">· ${ordsuf(m.gen)} generation</span><br>
     <span class="lineage">${m.lineage.length? 'line of '+esc(m.lineage.join(' → '))+' →' : 'a founder'}</span><br>
     age ${live.age} · energy ${Math.round(live.energy)} · children ${m.children}<br>
     ${gb('speed',g.speed,0.3,1.8)}${gb('sense',g.sense,2,7)}${gb('social',g.social,-1,1)}
     ${gb('rain love',g.rain_love,-1,1)}${gb('wander',g.wander,0,1)}${gb('thrift',g.thrift,0.35,0.9)}`;
}
function ordsuf(n){return n+(['th','st','nd','rd'][((n%100)>10&&(n%100)<14)?0:Math.min(n%10,4)%4]||'th');}
boot();
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
