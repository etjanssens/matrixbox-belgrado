"""Zet de GTFS-dienstregelingen van Renfe (Cercanías + AVE/Larga/Media Distancia) om naar JSON per station.

Anders dan Belgrado (vaste weekdienstregeling) heeft Renfe per dag andere diensten. Daarom rekenen we hier
alvast uit welke ritten er op welke datum rijden, voor een venster van gisteren t/m DAYS_AHEAD dagen vooruit.
De GitHub Action draait dit elke nacht. De Worker legt er daarna live vertragingen (GTFS-RT) overheen.

Uitvoer (standaard ./public/renfe):
  stops.json          [[stop_id, stop_id, naam, richting-hint], ...]  (zelfde vorm als Belgrado)
  dep/<stop_id>.json  {"l": [[lijn, modus, bestemming], ...],
                       "d": {"YYYYMMDD": [[minuten, lijn-index, trip_id], ...], ...}}

Gebruik:
  python3 scripts/build_renfe.py                          # downloadt beide feeds
  python3 scripts/build_renfe.py --cer cer.zip --ld ld.zip
"""
import argparse
import csv
import datetime
import io
import json
import os
import shutil
import urllib.request
import zipfile
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

FEEDS = {
    "cer": "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip",
    "ld": "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip",
}
DAYS_AHEAD = 4

# Korte lijnnamen voor de lange afstand; de MatrixBox toont standaard maar 3 tekens.
LD_NAMES = {
    "AVE": "AVE", "AVE INT": "AVE", "ALVIA": "ALV", "Intercity": "IC", "MD": "MD", "REGIONAL": "REG",
    "REG.EXP.": "REX", "AVANT": "AVT", "AVANT EXP": "AVT", "AVLO": "AVL", "EUROMED": "EMD",
    "PROXIMDAD": "PRX", "TRENCELTA": "TC",
}


def read_csv(zf, name):
    if name not in zf.namelist():
        return
    with zf.open(name) as f:
        # Renfe vult velden op met spaties, ook in de kopregel
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            yield {k.strip(): (v or "").strip() for k, v in row.items() if k}


def to_minutes(hms):
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)


def active_dates(zf, window):
    """service_id -> set van datums (YYYYMMDD) binnen het venster waarop hij rijdt."""
    keys = {d.strftime("%Y%m%d"): d.weekday() for d in window}
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    active = defaultdict(set)
    for c in read_csv(zf, "calendar.txt"):
        for key, wd in keys.items():
            if c["start_date"] <= key <= c["end_date"] and c[days[wd]] == "1":
                active[c["service_id"]].add(key)
    for cd in read_csv(zf, "calendar_dates.txt"):
        if cd["date"] in keys:
            if cd["exception_type"] == "1":
                active[cd["service_id"]].add(cd["date"])
            else:
                active[cd["service_id"]].discard(cd["date"])
    return active


def build(feeds, out):
    today = datetime.datetime.now(ZoneInfo("Europe/Madrid")).date()
    window = [today + datetime.timedelta(days=i) for i in range(-1, DAYS_AHEAD + 1)]

    names = {}
    per_stop = defaultdict(lambda: defaultdict(set))  # stop -> datum -> {(minuten, lijninfo, trip)}
    for kind, zf in feeds.items():
        for s in read_csv(zf, "stops.txt"):
            names.setdefault(s["stop_id"], s["stop_name"])

        routes = {}
        for r in read_csv(zf, "routes.txt"):
            short = r["route_short_name"]
            if kind == "ld":
                short = LD_NAMES.get(short, short[:3])
            routes[r["route_id"]] = (short, "BUS" if r["route_type"] == "3" else "TRAIN")

        dates = active_dates(zf, window)
        trips = {}
        for t in read_csv(zf, "trips.txt"):
            if dates.get(t["service_id"]):
                trips[t["trip_id"]] = (routes[t["route_id"]], t.get("trip_headsign", ""), dates[t["service_id"]])

        # Ritten verzamelen; bestemming = eindhalte als trip_headsign leeg is (meestal bij Renfe)
        stop_times = defaultdict(list)
        for st in read_csv(zf, "stop_times.txt"):
            if st["trip_id"] in trips:
                stop_times[st["trip_id"]].append(
                    (int(st["stop_sequence"]), st["stop_id"], st["departure_time"], st.get("pickup_type", "0")))

        for trip_id, stops in stop_times.items():
            (line, mode), headsign, trip_dates = trips[trip_id]
            stops.sort()
            dest = headsign or names.get(stops[-1][1], "")
            info = (line, mode, dest)
            for _, stop_id, dep, pickup in stops[:-1]:  # op de eindhalte vertrekt niets
                if pickup == "1":
                    continue
                minutes = to_minutes(dep)
                for d in trip_dates:
                    per_stop[stop_id][d].add((minutes, info, trip_id))
        print(kind, len(trips), "ritten in venster")

    dep_dir = os.path.join(out, "dep")
    os.makedirs(dep_dir, exist_ok=True)
    stops_out = []
    for stop_id, by_date in per_stop.items():
        lines, index, data = [], {}, {}
        dest_count = Counter()
        for d, deps in by_date.items():
            rows = []
            for minutes, info, trip_id in sorted(deps):
                if info not in index:
                    index[info] = len(lines)
                    lines.append(list(info))
                rows.append([minutes, index[info], trip_id])
                dest_count[info[2]] += 1
            data[d] = rows
        with open(os.path.join(dep_dir, stop_id + ".json"), "w", encoding="utf-8") as f:
            json.dump({"l": lines, "d": data}, f, ensure_ascii=False, separators=(",", ":"))
        hint = ", ".join(h for h, _ in dest_count.most_common(2))
        stops_out.append([stop_id, stop_id, names.get(stop_id, stop_id), hint])

    stops_out.sort(key=lambda s: s[2])
    with open(os.path.join(out, "stops.json"), "w", encoding="utf-8") as f:
        json.dump(stops_out, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out, "info.json"), "w", encoding="utf-8") as f:
        json.dump({"built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                   "window": [window[0].isoformat(), window[-1].isoformat()]}, f)
    print(f"{len(stops_out)} stations geschreven naar {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cer", help="lokaal Cercanías-zipbestand")
    parser.add_argument("--ld", help="lokaal AVE/LD-zipbestand")
    parser.add_argument("out", nargs="?", default="public/renfe")
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
