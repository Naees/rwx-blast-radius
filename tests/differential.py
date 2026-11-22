"""Compare a local pipeline executable with a small, deliberately exhaustive oracle.

Run: python tests/differential.py .local/shop --seeds 30
Requires pyarrow to read the executable's output.
"""

import argparse
import json
import os
import random
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq


DAY = 86_400_000
GAP = 1_800_000
START = 1_835_136_000_000  # 2028-02-26 UTC
END = START + 14 * DAY
HEADERS = {
    "events": "event_id,user_id,ts,ingest_ts,event_type,product_id,price_cents",
    "clicks": "click_id,user_id,campaign_id,ts",
    "links": "user_id_a,user_id_b",
    "products": "product_id,category",
    "campaigns": "campaign_id,channel",
}


def stamp(ms, offset=0):
    zone = timezone(timedelta(minutes=offset))
    dt = datetime.fromtimestamp(ms // 1000, zone)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def millis(value):
    if re.fullmatch("[0-9]+", value):
        return int(value)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])", value):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()) * 1000
    except ValueError:
        return None


def generate(root, seed):
    rng = random.Random(seed)
    records = defaultdict(list)
    end = END + (100 * DAY if seed % 4 == 0 else 0)

    def add(table, *values):
        fields = [f'"{v}"' if rng.random() < .3 else str(v) for v in values]
        records[table].append(",".join(fields))

    def event(eid, uid, ms, product="p1", price="1", kind="purchase", ingest=None):
        ts = str(ms) if isinstance(ms, str) or ms % 1000 else rng.choice(
            [str(ms), "000" + str(ms), stamp(ms), stamp(ms, rng.choice([-719, 1380]))])
        add("events", eid, uid, ts, stamp(ingest if ingest is not None else ms), kind, product, price)

    users = ["name  ", "Name", " 名", "w" * 255, "a\rb", "ع", "ß", "member", " ", "x\x00", "x\x00y"]
    for a, b in zip(users[:3], users[1:4]):
        add("links", a, b)
    add("links", users[3], " canonical")
    for user in users[4:]:
        if rng.random() < .4:
            add("links", user, rng.choice(users))
    add("links", " ignored", "")
    records["links"].append('bad,q"x')
    for i, cat in enumerate(["Z", "a ", "Ù"]):
        add("products", f"p{i}", cat)
    for c in range(5):
        add("campaigns", f"c_{c}", ["ad", "ref"][c % 2])

    # A linked cross-midnight session with an exact category tie and two assisting campaigns.
    t = START + DAY - 60_000
    for a, b in [("fixture-A", "fixture-B"), ("fixture-B", "Fixture")]:
        add("links", a, b)
    event("fixture-1", "fixture-A", t, "p0", "0007")
    event("fixture-2", "fixture-B", t + 60_000, "p2", "7")
    event("fixture-3", "fixture-B", t + 60_000 + GAP, "p2", "７", "view")
    event("fixture-4", "fixture-B", t + 60_000 + 2 * GAP + 1, "?", "7")
    for key, c, when in [("early", 0, t - 7 * DAY), ("!winner", 1, t), ("tie", 2, t),
                         ("late", 3, t + 1), ("expired", 4, t - 7 * DAY - 1)]:
        add("clicks", key, "fixture-B", f"c_{c}", str(when))
    event("duplicate", "fixture-A", "bad", ingest=t)
    event("duplicate", "fixture-B", t, ingest=t + 1000)

    long_prefix = "opaque/" + "f" * 400
    for suffix in ("1", "2"):
        event(long_prefix + suffix, "nul" + "\x00" * int(suffix), t, price=suffix)
    event(long_prefix + "1", "losing-user", t, ingest=t + 1000)
    if end != END:
        event("wide", "fixture-A", end - DAY, "p0", "3")
        event("wide-view", "fixture-B", end - DAY + 1, "p1", "", "view")
        add("clicks", "wide-click", "Fixture", "c_1", str(end - DAY))

    for i in range(130):
        u = rng.choice(users)
        ms = START + rng.randrange(0, 14) * DAY + rng.randrange(0, 48) * GAP + rng.randrange(3)
        kind = rng.choice(["purchase", "cart", "purchase", "view", "x"])
        ts = rng.choice(["9999999999999999999999", "2028-02-30T00:00:00Z", "", "１２"]) if i % 11 == 0 else ms
        p = rng.choice(["p0", "p1", "p2", "not-listed", ""])
        price = rng.choice(["1", "000001", "10000000", "0", "10000001", "２", "1\r"])
        ing = ms + rng.choice([-1000, DAY, DAY + 1000, 0])
        event("x" + str(i), u, ts, p, price, kind, ing)
        if rng.random() < .3:
            event("x" + str(i), rng.choice(users), ms, ingest=ing + 1000)
        if rng.random() < .4:
            click = ("c" + str(i), u, f"c_{rng.randrange(6)}",
                     str(ms + rng.choice([-7 * DAY, -1, 0, 1, DAY])))
            add("clicks", *click)
            if rng.random() < .4:
                add("clicks", *click)
    records["events"] += ["", "too,few", f'bad,x,{t},{stamp(t)},view,p0,br"oken']
    records["clicks"] += [f"cr,user,c_1,{stamp(t)}\r", "too,many,fields,x,y"]

    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"window_start": stamp(START), "window_end": stamp(end)}))
    for table, header in HEADERS.items():
        rows = records[table]
        rng.shuffle(rows)
        if table in ("products", "campaigns"):
            parts = [(root / f"{table}.csv", rows)]
        else:
            (root / table).mkdir()
            parts = [(root / table / f"{i}.csv", rows[i::3] if i < 3 else []) for i in range(4)]
        for file, part in parts:
            file.write_bytes(("\n".join([header, *part]) + "\n").encode())


def rows(root, table):
    files = [root / f"{table}.csv"] if (root / f"{table}.csv").exists() else (root / table).glob("*.csv")
    for file in files:
        for line in file.read_bytes().decode().split("\n")[1:]:
            fields = line.split(",")
            if len(fields) == HEADERS[table].count(",") + 1 and all(
                re.fullmatch(r'[^"]*|"[^"]*"', field) for field in fields
            ):
                yield tuple(f[1:-1] if f.startswith('"') else f for f in fields)


def oracle(root):
    end = millis(json.loads((root / "manifest.json").read_text())["window_end"])
    neighbors = defaultdict(set)
    for a, b in rows(root, "links"):
        if a and b:
            neighbors[a].add(b)
            neighbors[b].add(a)
    names = {}
    for u in neighbors:
        if u in names:
            continue
        todo, component = [u], {u}
        while todo:
            adjacent = neighbors[todo.pop()] - component
            component.update(adjacent)
            todo.extend(adjacent)
        names.update(dict.fromkeys(component, min(component)))

    products = dict(rows(root, "products"))
    campaigns = dict(rows(root, "campaigns"))
    unique = {}
    for eid, u, t, ing, kind, p, price in rows(root, "events"):
        if eid and (eid not in unique or ing < unique[eid][0]):
            unique[eid] = ing, u, t, kind, p, price
    events = defaultdict(list)
    for ing, u, t, kind, p, price in unique.values():
        ms = millis(t)
        if not (u and p and kind in ("view", "purchase", "cart") and ms is not None and
                START <= ms < end and millis(ing) <= ms + DAY):
            continue
        if kind == "purchase" and not (re.fullmatch("[0-9]+", price) and 1 <= int(price) <= 10_000_000):
            continue
        events[names.get(u, u)].append((ms, products.get(p, "unknown"), int(price) if kind == "purchase" else 0))
    clicks = defaultdict(list)
    for eid, u, c, t in set(rows(root, "clicks")):
        ms = millis(t)
        if eid and u and c in campaigns and ms is not None and START - 7 * DAY <= ms < end:
            clicks[names.get(u, u)].append((ms, eid, c))

    user_rows, campaign_rows = {}, {}
    categories = defaultdict(Counter)

    def campaign(c, day):
        return campaign_rows.setdefault((c, day), dict(campaign_id=c, day=day,
            channel=campaigns.get(c, "organic"), sessions=0, revenue_cents=0,
            purchasing_sessions=0, assisted_sessions=0))

    for person, log in events.items():
        sessions = []
        for entry in sorted(log):
            if not sessions or entry[0] - sessions[-1][-1][0] > GAP:
                sessions.append([])
            sessions[-1].append(entry)
        for session in sessions:
            start = session[0][0]
            day = date(1970, 1, 1) + timedelta(days=start // DAY)
            revenue = sum(price for _, _, price in session)
            row = user_rows.setdefault((person, day), dict(user_id=person, day=day, sessions=0,
                events=0, revenue_cents=0, top_category=None))
            row["sessions"] += 1
            row["events"] += len(session)
            row["revenue_cents"] += revenue
            cats = categories[person, day]
            for _, cat, price in session:
                if price:
                    cats[cat] += price
            if cats:
                row["top_category"] = min(cats, key=lambda c: (-cats[c], c))
            eligible = [c for c in clicks[person] if start - 7 * DAY <= c[0] <= start]
            winner = min(eligible, key=lambda c: (-c[0], c[1]))[2] if eligible else "organic"
            credited = campaign(winner, day)
            credited["sessions"] += 1
            credited["purchasing_sessions"] += int(revenue > 0)
            credited["revenue_cents"] += revenue
            for c in {c[2] for c in eligible}:
                campaign(c, day)["assisted_sessions"] += 1
    return user_rows, campaign_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--seeds", type=int, default=30)
    parser.add_argument("--first-seed", type=int, default=0)
    args = parser.parse_args()
    for seed in range(args.first_seed, args.first_seed + args.seeds):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "input", root / "output"
            output.mkdir()
            scratch = root / "scratch"
            scratch.mkdir()
            generate(source, seed)
            expected = oracle(source)
            subprocess.run([str(args.binary.resolve())], check=True, env=dict(os.environ,
                DATA_DIR=str(source), OUTPUT_DIR=str(output), SCRATCH_DIR=str(scratch),
                NCPU=str(1 + seed % 8)))
            for name, key, want in zip(("user_daily", "campaign_daily"),
                                       ("user_id", "campaign_id"), expected):
                rows_out = pq.read_table(output / name).to_pylist()
                got = {(row[key], row["day"]): row for row in rows_out}
                if len(rows_out) != len(got):
                    raise AssertionError(f"seed {seed}, {name}: duplicate rows")
                for k in got.keys() | want.keys():
                    if got.get(k) != want.get(k):
                        raise AssertionError(f"seed {seed}, {name}, {k}: {got.get(k)} != {want.get(k)}")
    print(f"{args.seeds} randomized end-to-end comparisons passed")


if __name__ == "__main__":
    main()
