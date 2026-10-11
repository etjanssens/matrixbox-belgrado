"""Zet de dienstregelingen van de Italiaanse spoorbedrijven om naar JSON per station (zelfde opbouw als Belgrado).

Bronnen (alleen dienstregeling; open live data is er niet):
  Trenitalia, Italo  GTFS-omzettingen van de officiële NeTEx-data van het Italiaanse NAP (CC BY 4.0,
                     © Clément Desouche): github.com/deryclem/trenitalia-gtfs en github.com/deryclem/italo-gtfs
  Trenord            Regione Lombardia, dati.lombardia.it
  EAV                EAV srl (Circumvesuviana, Cumana, Circumflegrea, …), eavsrl.it/open-data
  ARST               ARST Sardegna (alleen de treinlijnen, niet de bussen)
Stations van verschillende bedrijven die vlak bij elkaar liggen worden samengevoegd, zodat bijvoorbeeld
Milano Centrale zowel Trenitalia, Italo als Trenord toont.

Uitvoer (standaard ./public/italia):
  stops.json          [[station_id, code, naam, richting-hint], ...]
  calendar.json       {"weekly": {patroon: {...}}, "added": {...}, "removed": {...}}
  dep/<station>.json  {"l": [[lijn, modus, bestemming, 0, kleur], ...], "<patroon>": [[minuten, lijn-index], ...]}

Gebruik:
  python3 scripts/build_italia.py
  python3 scripts/build_italia.py --local ti=ti.zip --local trenord=trenord.zip
"""
import argparse
import csv
import datetime
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

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MATCH_METERS = 300  # stations van verschillende bedrijven binnen deze afstand = hetzelfde station
WINDOW_DAYS = 90    # losse datums verder vooruit laten we weg (de Action bouwt elke nacht opnieuw)


def trenord_name(short):
    # "RE11"/"R31" past niet in 3 tekens zonder te misleiden: toon het treintype
    if short.startswith("RE"): return "RE"
    if re.fullmatch(r"R\d+", short): return "R"
    return short


# Per bron: download-URL, welke route_types meedoen (None = alles), lijnnaam en kleur.
# Trenord- en ARST-kleuren komen uit de GTFS; de rest zijn eigen keuzes (geen officiële codes gevonden).
TI_COLORS = {
    "FR": "E2001A", "FA": "A7A9AC", "FB": "E6E6E6", "IC": "1F5FA8", "ICN": "1F5FA8", "EC": "1F5FA8",
    "EN": "1F5FA8", "EXP": "1F5FA8", "REG": "2E9E4F", "RV": "2E9E4F", "SFM": "2E9E4F", "FL": "2E9E4F",
    "MET": "2E9E4F", "BUS": "888888",
}
FEEDS = {
    "ti": {"url": "https://raw.githubusercontent.com/deryclem/trenitalia-gtfs/main/gtfs-trenitalia.zip",
           "types": None, "name": lambda s, r: s, "color": lambda n, r: TI_COLORS.get(n, "")},
    "it": {"url": "https://raw.githubusercontent.com/deryclem/italo-gtfs/main/gtfs-italo.zip",
           "types": None, "name": lambda s, r: {"Italo": "ITA", "ItaloBus": "BUS"}.get(s, s),
           "color": lambda n, r: "888888" if n == "BUS" else "8E1B3C"},  # bordeaux, duidelijk anders dan FR-rood
    "trenord": {"url": "https://www.dati.lombardia.it/download/3z4k-mxz9/application%2Fzip",
                "types": None, "name": lambda s, r: "BUS" if r["route_type"] == "3" else trenord_name(s),
                "color": lambda n, r: "888888" if r["route_type"] == "3" else r.get("route_color", "")},
    "eav": {"url": "https://www.wimob.it/cfile/download.php?file=google-transit.zip",
            "types": {"2"}, "name": lambda s, r: "EAV", "color": lambda n, r: "005CA9"},
    "arst": {"url": "https://www.arstspa.info/arst-cagliari-it.zip",
             "types": {"2"}, "name": lambda s, r: "ARS", "color": lambda n, r: r.get("route_color", "")},
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
    """Namen in hoofdletters ("ROMA TERMINI", "S.CANDIDO") worden "Roma Termini", "S.Candido"."""
    if name != name.upper():
        return name
    return re.sub(r"[A-Za-zÀ-ÿ]+", lambda m: m.group(0).capitalize(), name.lower())


def plain(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def distance(a, b):
    lat = math.radians((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * 111320, (a[1] - b[1]) * 111320 * math.cos(lat))


def assign_stations(feeds):
    """(bron, stop_id) -> station-id. Trenitalia-ID's (stop_code) blijven gelijk, zodat gekozen stations geldig blijven."""
    station_of, stations = {}, {}  # stations: id -> {"name", "code", "pos", "feed"}
    for kind, zf in feeds.items():
        stops = list(read_csv(zf, "stops.txt"))
        for s in stops:
            if s.get("parent_station"):
                continue  # perrons/haltepunten volgen hun station hieronder
            pos = (float(s["stop_lat"]), float(s["stop_lon"]))
            name = nice(s["stop_name"])
            if kind == "ti":
                if s.get("location_type") != "1":
                    continue
                sid = s["stop_code"] or s["stop_id"].rsplit(":", 1)[-1]
                stations[sid] = {"name": name, "code": sid, "pos": pos, "feed": kind}
            else:
                near = [(distance(pos, st["pos"]), k) for k, st in stations.items()
                        if abs(st["pos"][0] - pos[0]) < 0.01 and abs(st["pos"][1] - pos[1]) < 0.014]
                near = [(d, k) for d, k in near if d <= MATCH_METERS and
                        (stations[k]["feed"] != kind or plain(stations[k]["name"]) == plain(name))]
                if near:
                    sid = min(near)[1]
                    if kind == "it":
                        stations[sid]["name"] = name  # Italo schrijft de namen netter dan Trenitalia
                else:
                    sid = "9" + str(zlib.crc32((kind + ":" + s["stop_id"]).encode()))  # vast en alleen cijfers
                    stations[sid] = {"name": name, "code": s.get("stop_code") or sid, "pos": pos, "feed": kind}
            station_of[(kind, s["stop_id"])] = sid
        for s in stops:
            if s.get("parent_station"):
                station_of[(kind, s["stop_id"])] = station_of.get((kind, s["parent_station"]))
    return station_of, stations


def build(feeds, out):
    station_of, stations = assign_stations(feeds)

    # ---- kalender: identieke patronen samenvoegen (Trenitalia heeft één service per rit)
    exceptions = defaultdict(lambda: ([], []))
    today = datetime.date.today()
    first = (today - datetime.timedelta(days=2)).strftime("%Y%m%d")
    last = (today + datetime.timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    for kind, zf in feeds.items():
        for cd in read_csv(zf, "calendar_dates.txt"):
            if not first <= cd["date"] <= last:
                continue
            exceptions[(kind, cd["service_id"])][0 if cd["exception_type"] == "1" else 1].append(cd["date"])
    patterns, service_pat = {}, {}
    weekly, added, removed = {}, defaultdict(list), defaultdict(list)

    def pattern(key):
        if key not in patterns:
            pid = "p" + str(len(patterns))
            patterns[key] = pid
            days, start, end, add, rem = key
            weekly[pid] = {"days": list(days), "start": start, "end": end}
            for d in add: added[d].append(pid)
            for d in rem: removed[d].append(pid)
        return patterns[key]

    for kind, zf in feeds.items():
        seen = set()
        for c in read_csv(zf, "calendar.txt"):
            if c["end_date"] < first or c["start_date"] > last:
                continue
            add, rem = exceptions.get((kind, c["service_id"]), ([], []))
            days = tuple(i for i, d in enumerate(DAYS) if c[d] == "1")
            service_pat[(kind, c["service_id"])] = pattern((days, c["start_date"], c["end_date"], tuple(sorted(add)), tuple(sorted(rem))))
            seen.add(c["service_id"])
        # services die alleen in calendar_dates staan (EAV)
        for (k, sid), (add, rem) in exceptions.items():
            if k == kind and sid not in seen:
                service_pat[(kind, sid)] = pattern(((), "", "", tuple(sorted(add)), tuple(sorted(rem))))

    # ---- ritten
    per_stop = defaultdict(lambda: defaultdict(set))
    for kind, zf in feeds.items():
        cfg = FEEDS[kind]
        routes = {}
        for r in read_csv(zf, "routes.txt"):
            if cfg["types"] and r["route_type"] not in cfg["types"]:
                continue
            short = cfg["name"](r.get("route_short_name") or r.get("route_long_name", ""), r)
            routes[r["route_id"]] = (short, "BUS" if r["route_type"] == "3" else "TRAIN", cfg["color"](short, r))
        trips = {}
        for t in read_csv(zf, "trips.txt"):
            pid = service_pat.get((kind, t["service_id"]))
            if pid and t["route_id"] in routes:
                trips[t["trip_id"]] = (routes[t["route_id"]], nice(t.get("trip_headsign", "")), pid)
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
    parser.add_argument("--local", action="append", default=[], help="bron=zipbestand, in plaats van downloaden")
    parser.add_argument("out", nargs="?", default="public/italia")
    args = parser.parse_args()
    local = dict(x.split("=", 1) for x in args.local)
    feeds = {}
    for kind, cfg in FEEDS.items():
        try:
            if kind in local:
                raw = open(local[kind], "rb").read()
            else:
                print("download", cfg["url"])
                with urllib.request.urlopen(cfg["url"], timeout=600) as r:
                    raw = r.read()
            feeds[kind] = zipfile.ZipFile(io.BytesIO(raw))
        except Exception as e:
            # Eén kapotte bron mag de rest niet tegenhouden (Trenitalia is wel verplicht: daar hangen de station-ID's aan)
            if kind == "ti":
                raise
            print("OVERGESLAGEN", kind, e)
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)
    build(feeds, args.out)


if __name__ == "__main__":
    main()
