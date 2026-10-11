"""Zet de dienstregelingen van Trenitalia en Italo om naar JSON per station (zelfde opbouw als Belgrado).

Bronnen (CC BY 4.0, © Clément Desouche): GTFS-omzettingen van de officiële NeTEx-data van het Italiaanse
nationale toegangspunt (NAP), wekelijks bijgewerkt:
  https://github.com/deryclem/trenitalia-gtfs
  https://github.com/deryclem/italo-gtfs
Live data is er niet (open): dit is alleen de dienstregeling.

Uitvoer (standaard ./public/italia):
  stops.json          [[station_id, code, naam, richting-hint], ...]
  calendar.json       {"weekly": {patroon: {...}}, "added": {...}, "removed": {...}}
  dep/<station>.json  {"l": [[lijn, modus, bestemming, 0, kleur], ...], "<patroon>": [[minuten, lijn-index], ...]}

Gebruik:
  python3 scripts/build_italia.py
  python3 scripts/build_italia.py --ti ti.zip --it it.zip
"""
import argparse
import csv
import io
import json
import math
import os
import re
import shutil
import urllib.request
import zipfile
import zlib
from collections import Counter, defaultdict

FEEDS = {
    "ti": "https://raw.githubusercontent.com/deryclem/trenitalia-gtfs/main/gtfs-trenitalia.zip",
    "it": "https://raw.githubusercontent.com/deryclem/italo-gtfs/main/gtfs-italo.zip",
}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MATCH_METERS = 400  # Italo-station binnen deze afstand van een Trenitalia-station = hetzelfde station

# Korte namen (de MatrixBox toont standaard 3 tekens) en kleuren. Italo-rood komt uit de GTFS;
# de Trenitalia-kleuren zijn eigen keuzes in de stijl van de merken (geen officiële codes gevonden).
NAMES = {"Italo": "ITA", "ItaloBus": "BUS"}
COLORS = {
    "FR": "C8102E", "FA": "A7A9AC", "FB": "E6E6E6", "IC": "1F5FA8", "ICN": "1F5FA8", "EC": "1F5FA8",
    "EN": "1F5FA8", "EXP": "1F5FA8", "REG": "2E9E4F", "RV": "2E9E4F", "SFM": "2E9E4F", "FL": "2E9E4F",
    "MET": "2E9E4F", "BUS": "888888", "ITA": "E30037",
}


def read_csv(zf, name):
    if name not in zf.namelist():
        return
    with zf.open(name) as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            yield {k.strip(): (v or "").strip() for k, v in row.items() if k}


def to_minutes(hms):
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)


def nice(name):
    """Trenitalia schrijft namen in hoofdletters ("ROMA TERMINI", "S.CANDIDO"): maak er "Roma Termini" van."""
    if name != name.upper():
        return name
    return re.sub(r"[A-Za-zÀ-ÿ]+", lambda m: m.group(0).capitalize(), name.lower())


def distance(a, b):
    lat = math.radians((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * 111320, (a[1] - b[1]) * 111320 * math.cos(lat))


def build(feeds, out):
    # ---- stations: Trenitalia-StopPlaces als basis, Italo op afstand eraan gekoppeld
    station_of = {}   # (feed, stop_id) -> station-id
    stations = {}     # station-id -> {"name", "code", "pos"}
    for s in read_csv(feeds["ti"], "stops.txt"):
        if s["location_type"] == "1":
            sid = s["stop_code"] or s["stop_id"].rsplit(":", 1)[-1]
            stations[sid] = {"name": nice(s["stop_name"]), "code": sid, "pos": (float(s["stop_lat"]), float(s["stop_lon"]))}
            station_of[("ti", s["stop_id"])] = sid
    for s in read_csv(feeds["ti"], "stops.txt"):
        if s["location_type"] != "1" and s.get("parent_station"):
            station_of[("ti", s["stop_id"])] = station_of.get(("ti", s["parent_station"]))

    ti_list = [(sid, st["pos"]) for sid, st in stations.items()]
    italo_parent = {}
    for s in read_csv(feeds["it"], "stops.txt"):
        if s.get("location_type") == "1":
            pos = (float(s["stop_lat"]), float(s["stop_lon"]))
            best = min(ti_list, key=lambda t: distance(pos, t[1]))
            if distance(pos, best[1]) <= MATCH_METERS:
                sid = best[0]
                stations[sid]["name"] = s["stop_name"]  # Italo-namen zijn netter geschreven
            else:
                sid = "9" + str(zlib.crc32(s["stop_id"].encode()))  # vast per station en alleen cijfers (de Worker accepteert alleen \d+)
                stations[sid] = {"name": s["stop_name"], "code": s["stop_code"], "pos": pos}
            station_of[("it", s["stop_id"])] = sid
            italo_parent[s["stop_id"]] = sid
    for s in read_csv(feeds["it"], "stops.txt"):
        if s.get("location_type") != "1" and s.get("parent_station"):
            station_of[("it", s["stop_id"])] = italo_parent.get(s["parent_station"])

    # ---- kalender: identieke patronen samenvoegen (Trenitalia heeft één service per rit)
    patterns = {}     # sleutel -> patroon-id
    service_pat = {}  # (feed, service_id) -> patroon-id
    exceptions = defaultdict(lambda: ([], []))
    for kind, zf in feeds.items():
        for cd in read_csv(zf, "calendar_dates.txt"):
            exceptions[(kind, cd["service_id"])][0 if cd["exception_type"] == "1" else 1].append(cd["date"])
    weekly, added, removed = {}, defaultdict(list), defaultdict(list)
    for kind, zf in feeds.items():
        for c in read_csv(zf, "calendar.txt"):
            days = tuple(i for i, d in enumerate(DAYS) if c[d] == "1")
            add, rem = exceptions.get((kind, c["service_id"]), ([], []))
            key = (days, c["start_date"], c["end_date"], tuple(sorted(add)), tuple(sorted(rem)))
            if key not in patterns:
                pid = "p" + str(len(patterns))
                patterns[key] = pid
                weekly[pid] = {"days": list(days), "start": c["start_date"], "end": c["end_date"]}
                for d in add: added[d].append(pid)
                for d in rem: removed[d].append(pid)
            service_pat[(kind, c["service_id"])] = patterns[key]

    # ---- ritten
    per_stop = defaultdict(lambda: defaultdict(set))
    for kind, zf in feeds.items():
        routes = {}
        for r in read_csv(zf, "routes.txt"):
            short = NAMES.get(r["route_short_name"], r["route_short_name"])
            routes[r["route_id"]] = (short, "BUS" if r["route_type"] == "3" else "TRAIN",
                                     COLORS.get(short, r.get("route_color", "")))
        trips = {}
        for t in read_csv(zf, "trips.txt"):
            pid = service_pat.get((kind, t["service_id"]))
            if pid:
                trips[t["trip_id"]] = (routes[t["route_id"]], t.get("trip_headsign", ""), pid)
        stop_times = defaultdict(list)
        for st in read_csv(zf, "stop_times.txt"):
            if st["trip_id"] in trips:
                stop_times[st["trip_id"]].append((int(st["stop_sequence"]), station_of.get((kind, st["stop_id"])),
                                                  st["departure_time"], st.get("pickup_type", "")))
        for trip_id, stops in stop_times.items():
            (line, mode, color), headsign, pid = trips[trip_id]
            stops.sort()
            dest = headsign or stations.get(stops[-1][1], {}).get("name", "")
            info = (line, mode, dest, 0, color)
            for _, sid, dep, pickup in stops[:-1]:
                if sid and pickup != "1" and sid != stops[-1][1]:
                    per_stop[sid][pid].add((to_minutes(dep), info))
        print(kind, len(trips), "ritten")

    dep_dir = os.path.join(out, "dep")
    os.makedirs(dep_dir, exist_ok=True)
    stops_out = []
    for sid, by_pat in per_stop.items():
        lines, index, data, dests = [], {}, {}, Counter()
        for pid, deps in by_pat.items():
            rows = []
            for minutes, info in sorted(deps):
                if info not in index:
                    index[info] = len(lines)
                    lines.append(list(info))
                rows.append([minutes, index[info]])
                dests[info[2]] += 1
            data[pid] = rows
        data["l"] = lines
        with open(os.path.join(dep_dir, sid + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        st = stations[sid]
        stops_out.append([sid, st["code"], st["name"], ", ".join(h for h, _ in dests.most_common(2))])

    stops_out.sort(key=lambda s: s[2])
    for name, obj in [("stops.json", stops_out), ("calendar.json", {"weekly": weekly, "added": added, "removed": removed})]:
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(stops_out)} stations, {len(weekly)} kalenderpatronen, geschreven naar {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ti", help="lokaal Trenitalia-zipbestand")
    parser.add_argument("--it", help="lokaal Italo-zipbestand")
    parser.add_argument("out", nargs="?", default="public/italia")
    args = parser.parse_args()
    feeds = {}
    for kind, url in FEEDS.items():
        local = getattr(args, kind)
        if local:
            raw = open(local, "rb").read()
        else:
            print("download", url)
            with urllib.request.urlopen(url, timeout=600) as r:
                raw = r.read()
        feeds[kind] = zipfile.ZipFile(io.BytesIO(raw))
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)
    build(feeds, args.out)


if __name__ == "__main__":
    main()
