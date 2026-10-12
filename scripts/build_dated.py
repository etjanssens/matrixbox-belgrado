"""Dienstregelingen met per dag andere ritten (ÖBB, HŽPP) omzetten naar JSON per station, per datum.

Net als bij Renfe rekenen we de ritten uit voor gisteren t/m DAYS_AHEAD dagen vooruit; de GitHub Action
draait dit elke nacht. Geen live data (die is voor deze landen niet open beschikbaar).

Bronnen:
  oebb  ÖBB Open Data, Soll-Fahrplan GTFS (data.oebb.at), CC BY 4.0
  hzpp  HŽ Putnički prijevoz, GTFS (hzpp.hr)
  sz    Slovenië (SŽ-treinen + bussen van Nomago, Arriva, AP Murska Sobota, LPP), uit de landelijke
        GTFS van het Sloveense NAP (nap.si, met account)

Uitvoer per bron (public/<bron>):
  stops.json          [[station_id, code, naam, richting-hint], ...]
  dep/<station>.json  {"l": [[lijn, modus, bestemming, 0, kleur], ...], "d": {"YYYYMMDD": [[minuten, lijn-index], ...]}}

Gebruik:
  python3 scripts/build_dated.py oebb public/oebb
  python3 scripts/build_dated.py hzpp public/hzpp --zip hzpp.zip
"""
import argparse
import csv
import datetime
import io
import json
import os
import re
import shutil
import urllib.request
import zipfile
import zlib
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

DAYS_AHEAD = 4
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# ÖBB: het treintype staat in trip_short_name ("RJ 658"). Kleuren zijn eigen keuzes in de stijl van de merken.
OEBB_COLORS = {
    "RJ": "E2002A", "RJX": "E2002A", "NJ": "1E2A5A", "EN": "1E2A5A", "EC": "1F5FA8", "IC": "1F5FA8",
    "ICE": "1F5FA8", "D": "1F5FA8", "REX": "2E9E4F", "CJX": "2E9E4F", "R": "2E9E4F",
    "S": "0089CF", "CAT": "7AB800", "BUS": "888888",
}


def oebb_line(trip, route):
    if route["route_type"] == "3":
        return "BUS"
    cat = (trip.get("trip_short_name") or "").split(" ")[0]
    if cat == "S":
        return route.get("route_short_name") or "S"  # S-Bahn: toon de lijn (S1, S45, …)
    return cat or route.get("route_short_name", "")[:3]


def oebb_color(line):
    return OEBB_COLORS.get("S" if re.fullmatch(r"S\d+", line) else line, "")


# Slovenië: treinen van SŽ plus de streek- en intercitybussen. De lijnnamen in deze data zijn interne
# codes ("PR A15 0522 1"), dus we tonen het bedrijf. Kleuren zijn eigen keuzes in de stijl van de merken.
SZ_AGENCIES = [("SŽ", "SŽ"), ("Nomago", "NOM"), ("Arriva", "ARR"), ("Murska Sobota", "APM"), ("Ljubljanski potniški", "LPP")]
SZ_COLORS = {"SŽ": "0062A6", "NOM": "6CB33F", "ARR": "00B2A9", "APM": "F28C28", "LPP": "F4D000"}


def sz_line(route):
    name = route.get("_agency_name", "")
    for key, short in SZ_AGENCIES:
        if key.lower() in name.lower():
            return short
    return "BUS" if route["route_type"] == "3" else name[:3].upper()


def nice(name):
    """Namen in hoofdletters ("NOVE STOŽICE P+R") worden "Nove Stožice P+R"."""
    if name != name.upper():
        return name
    return re.sub(r"[^\W\d_]{2,}", lambda m: m.group(0).capitalize(), name)


FEEDS = {
    "oebb": {"url": "https://static.web.oebb.at/open-data/soll-fahrplan-gtfs/GTFS_Fahrplan_{year}.zip",
             "tz": "Europe/Vienna", "line": oebb_line, "color": oebb_color},
    # Slovenië: de landelijke GTFS van het NAP (nap.si, alleen met account). De Action downloadt hem
    # wekelijks met een token (NAP_USER/NAP_PASSWORD) en geeft hem via --zip mee.
    "sz": {"url": None, "tz": "Europe/Ljubljana",
           "line": lambda t, r: sz_line(r), "color": lambda line: SZ_COLORS.get(line, "")},
    # Stadsbussen Ljubljana (LPP, open data). Station-ID = stop_code, dezelfde code als de live-API van LPP
    # (data.lpp.si/api/station/arrival), die de Worker eerst probeert; dit is de reserve als die niet antwoordt.
    "lpp": {"url": "https://data.lpp.si/api/gtfs/feed.zip", "tz": "Europe/Ljubljana", "use_stop_code": True,
            "line": lambda t, r: r.get("route_short_name", "").lstrip("0") or "BUS",
            "color": None,  # route_color uit de GTFS
            "dest": lambda t: nice(t.get("trip_short_name", ""))},
    "hzpp": {"url": "https://www.hzpp.hr/GTFS_files.zip", "tz": "Europe/Zagreb",
             # Geen treintypes in de data: alles heet "HŽ" (bussen "BUS"), in HŽ-blauw
             "line": lambda t, r: "BUS" if r["route_type"] == "3" else "HŽ",
             "color": lambda line: "888888" if line == "BUS" else "0055A4"},
}


class Gtfs:
    """Leest GTFS-bestanden, ook als ze in een submap van de zip staan (zoals bij ÖBB)."""
    def __init__(self, raw):
        self.zf = zipfile.ZipFile(io.BytesIO(raw))
        self.names = {n.rsplit("/", 1)[-1]: n for n in self.zf.namelist()}

    def rows(self, name):
        if name not in self.names:
            return
        with self.zf.open(self.names[name]) as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                yield {k.strip(): (v or "").strip() for k, v in row.items() if k}


def to_minutes(hms):
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)


def build(kind, gtfs, out):
    cfg = FEEDS[kind]
    today = datetime.datetime.now(ZoneInfo(cfg["tz"])).date()
    window = {(today + datetime.timedelta(days=i)).strftime("%Y%m%d"): (today + datetime.timedelta(days=i)).weekday()
              for i in range(-1, DAYS_AHEAD + 1)}

    active = defaultdict(set)  # service_id -> datums in het venster
    for c in gtfs.rows("calendar.txt"):
        for key, wd in window.items():
            if c["start_date"] <= key <= c["end_date"] and c[DAYS[wd]] == "1":
                active[c["service_id"]].add(key)
    for cd in gtfs.rows("calendar_dates.txt"):
        if cd["date"] in window:
            (active[cd["service_id"]].add if cd["exception_type"] == "1" else active[cd["service_id"]].discard)(cd["date"])

    # stations: perrons volgen hun station (parent_station)
    names, parent, codes = {}, {}, {}
    for s in gtfs.rows("stops.txt"):
        names[s["stop_id"]] = s["stop_name"]
        codes[s["stop_id"]] = s.get("stop_code", "")
        if s.get("parent_station"):
            parent[s["stop_id"]] = s["parent_station"]
    station = lambda sid: parent.get(sid, sid)

    agencies = {a.get("agency_id", ""): a.get("agency_name", "") for a in gtfs.rows("agency.txt")}
    routes = {}
    for r in gtfs.rows("routes.txt"):
        r["_agency_name"] = agencies.get(r.get("agency_id", ""), "")
        routes[r["route_id"]] = r
    trips, line_colors = {}, {}
    for t in gtfs.rows("trips.txt"):
        if active.get(t["service_id"]) and t["route_id"] in routes:
            r = routes[t["route_id"]]
            if "routes" in cfg and not cfg["routes"](r):
                continue
            line = cfg["line"](t, r)
            color = cfg["color"](line) if cfg["color"] else r.get("route_color", "")
            headsign = cfg["dest"](t) if "dest" in cfg else t.get("trip_headsign", "")
            trips[t["trip_id"]] = (line, "BUS" if r["route_type"] == "3" else "TRAIN", color, headsign, active[t["service_id"]])
            line_colors[line] = color

    stop_times = defaultdict(list)
    for st in gtfs.rows("stop_times.txt"):
        if st["trip_id"] in trips:
            stop_times[st["trip_id"]].append((int(st["stop_sequence"]), station(st["stop_id"]),
                                              st["departure_time"], st.get("pickup_type", "")))

    per_stop = defaultdict(lambda: defaultdict(set))
    for trip_id, stops in stop_times.items():
        line, mode, color, headsign, dates = trips[trip_id]
        stops.sort()
        dest = headsign or names.get(stops[-1][1], "")
        info = (line, mode, dest, 0, color)
        for _, sid, dep, pickup in stops[:-1]:
            if pickup == "1" or sid == stops[-1][1]:
                continue
            for d in dates:
                per_stop[sid][d].add((to_minutes(dep), info))
    print(kind, len(trips), "ritten in venster")

    dep_dir = os.path.join(out, "dep")
    os.makedirs(dep_dir, exist_ok=True)
    stops_out = []
    for sid, by_date in per_stop.items():
        lines, index, data, dests = [], {}, {}, Counter()
        for d, deps in by_date.items():
            rows = []
            for minutes, info in sorted(deps):
                if info not in index:
                    index[info] = len(lines)
                    lines.append(list(info))
                rows.append([minutes, index[info]])
                dests[info[2]] += 1
            data[d] = rows
        num = sid if sid.isdigit() else str(zlib.crc32(sid.encode()))  # vast per station, alleen cijfers (Worker)
        if cfg.get("use_stop_code") and codes.get(sid, "").isdigit():
            num = codes[sid]
        with open(os.path.join(dep_dir, num + ".json"), "w", encoding="utf-8") as f:
            json.dump({"l": lines, "d": data}, f, ensure_ascii=False, separators=(",", ":"))
        stops_out.append([num, num, names.get(sid, sid), ", ".join(h for h, _ in dests.most_common(2))])

    stops_out.sort(key=lambda s: s[2])
    with open(os.path.join(out, "stops.json"), "w", encoding="utf-8") as f:
        json.dump(stops_out, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out, "lines.json"), "w", encoding="utf-8") as f:  # lijn -> kleur (voor live data)
        json.dump(line_colors, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(stops_out)} stations geschreven naar {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=FEEDS)
    parser.add_argument("out")
    parser.add_argument("--zip", help="lokaal zipbestand in plaats van downloaden")
    args = parser.parse_args()
    if args.zip:
        raw = open(args.zip, "rb").read()
    elif FEEDS[args.kind]["url"] is None:
        parser.error(args.kind + " heeft geen open download-URL: geef het zipbestand mee met --zip")
    else:
        url = FEEDS[args.kind]["url"].format(year=datetime.date.today().year + (1 if datetime.date.today() >= datetime.date(datetime.date.today().year, 12, 13) else 0))
        print("download", url)
        with urllib.request.urlopen(url, timeout=900) as r:
            raw = r.read()
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)
    build(args.kind, Gtfs(raw), args.out)


if __name__ == "__main__":
    main()
