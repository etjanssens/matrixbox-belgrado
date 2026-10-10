"""Zet de GTFS-dienstregeling van Beograd Plus om naar kleine JSON-bestanden.

Uitvoer (in de map die als argument wordt meegegeven, standaard ./public):
  stops.json        alle haltes: [[stop_id, stop_code, naam, richting-hint], ...]
  calendar.json     welke dienstregeling (service_id) op welke dag rijdt
  dep/<stop_id>.json  alle vertrektijden van één halte, per service_id

Gebruik:
  python3 scripts/build.py                 # downloadt de actuele GTFS van data.gov.rs
  python3 scripts/build.py --zip bg.zip    # gebruikt een lokaal zipbestand
"""
import argparse
import csv
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict

DATASET_API = "https://data.gov.rs/api/1/datasets/gradski-javni-prevoz-u-beogradu-gtfs/"

# GTFS route_type -> transport_mode zoals de MatrixBox het kent.
# Trolleybussen (11) tellen als bus; de MatrixBox heeft daar geen aparte categorie voor.
MODES = {"0": "TRAM", "3": "BUS", "11": "BUS"}


def download_zip():
    with urllib.request.urlopen(DATASET_API, timeout=60) as r:
        dataset = json.load(r)
    zips = [res for res in dataset["resources"] if (res.get("format") or "").lower() == "zip"]
    if not zips:
        sys.exit("data.gov.rs heeft geen zip voor deze dataset")
    latest = max(zips, key=lambda res: res.get("last_modified", ""))
    print("GTFS:", latest.get("last_modified"), latest["url"])
    with urllib.request.urlopen(latest["url"], timeout=900) as r:
        return r.read()


def read_csv(zf, name):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def to_minutes(hms):
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)


def build(zf, out):
    routes = {}
    for r in read_csv(zf, "routes.txt"):
        routes[r["route_id"]] = (r["route_short_name"], MODES.get(r["route_type"], "BUS"))

    trips = {}
    for t in read_csv(zf, "trips.txt"):
        line, mode = routes[t["route_id"]]
        direction = 1 if t.get("direction_id", "0") == "0" else 2
        trips[t["trip_id"]] = (t["service_id"], (line, mode, t["trip_headsign"].strip(), direction))

    # Eerste ronde: laatste halte per rit. Daar vertrekt niets meer, dus die slaan we over.
    last_seq = {}
    for st in read_csv(zf, "stop_times.txt"):
        seq = int(st["stop_sequence"])
        if seq > last_seq.get(st["trip_id"], -1):
            last_seq[st["trip_id"]] = seq

    # Tweede ronde: vertrektijden per halte.
    per_stop = defaultdict(lambda: defaultdict(list))
    for st in read_csv(zf, "stop_times.txt"):
        trip = trips.get(st["trip_id"])
        if trip is None or st.get("pickup_type") == "1":
            continue
        if int(st["stop_sequence"]) == last_seq[st["trip_id"]]:
            continue
        service, info = trip
        per_stop[st["stop_id"]][service].append((to_minutes(st["departure_time"]), info))

    dep_dir = os.path.join(out, "dep")
    os.makedirs(dep_dir, exist_ok=True)
    hints = {}
    for stop_id, services in per_stop.items():
        lines = []  # unieke (lijn, modus, bestemming, richting) voor deze halte
        index = {}
        data = {}
        for service, deps in services.items():
            rows = set()  # set: de feed bevat soms dubbele ritten
            for minutes, info in deps:
                if info not in index:
                    index[info] = len(lines)
                    lines.append(list(info))
                rows.add((minutes, index[info]))
            data[service] = sorted(rows)
        data["l"] = lines
        with open(os.path.join(dep_dir, stop_id + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        counts = Counter(info[2] for deps in services.values() for _, info in deps)
        hints[stop_id] = ", ".join(h for h, _ in counts.most_common(2))

    stops = []
    for s in read_csv(zf, "stops.txt"):
        if s.get("location_type", "0") not in ("", "0") or s["stop_id"] not in per_stop:
            continue
        stops.append([s["stop_id"], s["stop_code"], s["stop_name"].strip(), hints.get(s["stop_id"], "")])
    with open(os.path.join(out, "stops.json"), "w", encoding="utf-8") as f:
        json.dump(stops, f, ensure_ascii=False, separators=(",", ":"))

    calendar = {"weekly": {}, "added": defaultdict(list), "removed": defaultdict(list)}
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for c in read_csv(zf, "calendar.txt"):
        calendar["weekly"][c["service_id"]] = {
            "days": [i for i, d in enumerate(days) if c[d] == "1"],  # 0 = maandag
            "start": c["start_date"],
            "end": c["end_date"],
        }
    if "calendar_dates.txt" in zf.namelist():
        for cd in read_csv(zf, "calendar_dates.txt"):
            key = "added" if cd["exception_type"] == "1" else "removed"
            calendar[key][cd["date"]].append(cd["service_id"])
    with open(os.path.join(out, "calendar.json"), "w", encoding="utf-8") as f:
        json.dump(calendar, f, separators=(",", ":"))

    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset=utf-8><title>MatrixBox Belgrado</title>"
                "<p>Dienstregelingsdata voor de MatrixBox (Beograd Plus GTFS, data.gov.rs).</p>")

    print(f"{len(stops)} haltes, {len(per_stop)} vertrekbestanden geschreven naar {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", help="lokaal GTFS-zipbestand in plaats van downloaden")
    parser.add_argument("out", nargs="?", default="public")
    args = parser.parse_args()

    raw = open(args.zip, "rb").read() if args.zip else download_zip()
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        build(zf, args.out)


if __name__ == "__main__":
    main()
