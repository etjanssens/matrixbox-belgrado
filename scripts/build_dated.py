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
import subprocess
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


def cz_line(trip, route):
    short = route.get("route_short_name", "")
    if re.fullmatch(r"[SRLTU]\d{1,2}", short):  # echte lijn (S1, R44, L2…)
        return short
    if route["route_type"] == "714" or "(NAD)" in short:
        return "NAD"  # vervangende bus
    return (trip.get("trip_short_name") or short).split(" ")[0][:3]


CZ_AGENCY_COLORS = [("České dráhy", "1F5FA8"), ("RegioJet", "F5C400"), ("Leo Express", "F28C28"),
                    ("ARRIVA", "00B2A9"), ("GW Train", "2E9E4F"), ("Länderbahn", "D7262E")]


def cz_color(line, route):
    if line == "NAD":
        return "888888"
    name = route.get("_agency_name", "").lower()
    return next((c for key, c in CZ_AGENCY_COLORS if key.lower() in name), "")


def download(url, timeout=900):
    # via curl: robuuster met certificaatketens dan urllib (data.gov.lv stuurt geen complete keten mee)
    if url.startswith("ftp://"):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    return subprocess.run(["curl", "-fsSL", "--retry", "3", "--max-time", str(timeout), url],
                          check=True, capture_output=True).stdout


def latest_ckan_resource(api):
    resources = json.loads(download(api, 60))["result"]["resources"]
    zips = [x for x in resources if (x.get("url") or "").lower().endswith(".zip")]
    return max(zips, key=lambda x: x.get("created") or x.get("last_modified") or "")["url"]


AGENCY_PALETTE = ["2E9E4F", "1F5FA8", "F28C28", "00B2A9", "A35CA0", "D7262E", "F5C400", "6CB33F"]


def agency_color(name):
    """Vaste kleur per bedrijf voor bronnen zonder eigen kleuren."""
    return AGENCY_PALETTE[zlib.crc32(name.encode()) % len(AGENCY_PALETTE)] if name else ""


def short_agency(name):
    """Drie letters voor een busbedrijf: initialen bij meerdere woorden ("Liepājas AP" -> "LAP"), anders het begin."""
    words = [w for w in re.split(r"[\s\-\"']+", name) if w and w.upper() not in ("AS", "SIA", "AP")]
    if not words: return "BUS"
    return (words[0][:3] if len(words) == 1 else "".join(w[0] for w in words[:3])).upper()


GREEK = {"Α": "A", "Β": "V", "Γ": "G", "Δ": "D", "Ε": "E", "Ζ": "Z", "Η": "I", "Θ": "Th", "Ι": "I", "Κ": "K",
         "Λ": "L", "Μ": "M", "Ν": "N", "Ξ": "X", "Ο": "O", "Π": "P", "Ρ": "R", "Σ": "S", "Τ": "T", "Υ": "Y",
         "Φ": "F", "Χ": "Ch", "Ψ": "Ps", "Ω": "O", "Ά": "A", "Έ": "E", "Ή": "I", "Ί": "I", "Ό": "O", "Ύ": "Y",
         "Ώ": "O", "Ϊ": "I", "Ϋ": "Y"}


def greek_to_latin(text):
    """Eenvoudige transliteratie (ELOT-achtig): "Λαγυνά" -> "Lagyna", "Θεσσαλονίκη" -> "Thessaloniki"."""
    text = text.replace("ου", "ou").replace("ού", "ou").replace("Ου", "Ou").replace("ΟΥ", "OU")
    out = []
    for ch in text:
        up = ch.upper()
        if up in GREEK or ch in "ςάέήίόύώϊΐϋΰ":
            lat = GREEK.get(up) or {"ς": "s", "ΐ": "i", "ΰ": "y"}.get(ch) or GREEK.get(ch.upper(), ch)
            out.append(lat if ch.isupper() else lat.lower())
        else:
            out.append(ch)
    return "".join(out)


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
    # Estland: landelijke open GTFS (Regionaal- ja Põllumajandusministeerium; Elron, bussen, Tallinn, veerboten).
    # Lijnkleuren uit de data (Elron oranje, Tallinn-trams groen, …).
    # mark_agency: ritten van het stadsvervoer van Tallinn krijgen een vlag; de Worker vervangt die door live tijden
    "ee": {"url": "https://eu-gtfs.remix.com/estonia_unified_gtfs.zip", "tz": "Europe/Tallinn", "mark_agency": "10312960",
           # Elron-lijnen als "RE14" passen niet in 3 tekens: bij treinen het type (E/RE/R), anders het lijnnummer
           "line": lambda t, r: (re.match(r"[A-Z]*", r.get("route_short_name", "")).group(0) or "EL") if r["route_type"] == "2"
                                else (r.get("route_short_name") or "BUS"), "color": None},
    # Tsjechië: alle treinen (ČD, RegioJet, Leo Express, Arriva, …) uit de officiële CIS JŘ-dienstregeling,
    # omgezet door het Oběhy-project met JrUtil (CC0). Treintype uit trip_short_name ("Os 10020"); lijnen als S1 blijven.
    "cz": {"url": "https://motis.obehy.cz/get-feeds/cz-czptt-gtfs.zip", "tz": "Europe/Prague",
           "line": lambda t, r: cz_line(t, r), "color": lambda line: "", "color_by_agency": cz_color},
    # Letland (CC0, data.gov.lv): drie aparte bronnen met elk eigen halte-ID's.
    # Vivi-treinen: trip_headsign herhaalt de route ("Rīga - Ludza"), dus bestemming = eindhalte.
    "vivi": {"url": "https://vivi.lv/uploads/GTFS.zip", "tz": "Europe/Riga",
             "line": lambda t, r: "BUS" if r["route_type"] == "3" else "VV", "color": lambda line: "00A0DF",
             "dest": lambda t: ""},
    # Rīgas Satiksme: maandelijks een nieuw bestand met een andere naam; het nieuwste via de CKAN-API
    "rs": {"url": lambda: latest_ckan_resource("https://data.gov.lv/dati/api/3/action/package_show?id=marsrutu-saraksti-rigas-satiksme-sabiedriskajam-transportam"),
           "tz": "Europe/Riga", "line": lambda t, r: r.get("route_short_name") or "RS", "color": None},
    # ATD: streek- en intercitybussen; routenummers zijn 4 cijfers, dus we tonen het bedrijf
    "atd": {"url": "https://www.atd.lv/sites/default/files/GTFS/gtfs-latvia-lv.zip", "tz": "Europe/Riga",
            "line": lambda t, r: "VV" if r["route_type"] == "2" else short_agency(r.get("_agency_name", "")),
            "color": lambda line: "", "color_by_agency": lambda line, r: agency_color(r.get("_agency_name", ""))},
    # Griekenland: Hellenic Train, community-GTFS van Transitous (gebouwd uit railway.gov.gr; geen licentie vermeld).
    # Alleen Hellenic Train (geen veerboten). Geen treintypes/bestemmingen: type uit route_type, bestemming = eindstation.
    # Stationsnamen: Engelse vertaling uit translations.txt, anders Griekse namen getranslitereerd (het bord kent geen Grieks).
    "gr": {"url": "https://jbb.ghsq.de/gtfs/gr-hellenic-train.gtfs.zip", "tz": "Europe/Athens", "latin_names": True,
           "routes": lambda r: r.get("agency_id") == "hellenic-train",
           "line": lambda t, r: {"109": "P", "106": "R", "102": "IC", "2": "R", "3": "BUS"}.get(r["route_type"], "HT"),
           "color": lambda line: {"P": "0072BC", "R": "2E9E4F", "IC": "C8102E", "BUS": "888888"}.get(line, "")},
    # Baskenland: Euskotren (treinen E1-E4, metro L3, trams Bilbao/Vitoria), Moveuskadi, CC BY 4.0. Officiële lijnkleuren.
    "euskotren": {"url": "ftp://ftp.geo.euskadi.net/cartografia/Transporte/Moveuskadi/Euskotren/google_transit.zip",
                  "tz": "Europe/Madrid", "line": lambda t, r: r.get("route_short_name") or "ET", "color": None},
    # Portugal: CP - Comboios de Portugal (publico.cp.pt, elke nacht opnieuw). Licentie niet vermeld.
    # Categorie = route_short_name (AP/IC/IR/R); voorstadstreinen (route_type 109) heten "U".
    "cp": {"url": "https://publico.cp.pt/gtfs/gtfs.zip", "tz": "Europe/Lisbon",
           "line": lambda t, r: "U" if r["route_type"] == "109" else (r.get("route_short_name") or "CP")[:3],
           "color": lambda line: {"AP": "7A1F5C", "IC": "1F5FA8", "IR": "2E9E4F", "R": "F2B700", "U": "0089CF"}.get(line, "")},
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
            # skipinitialspace: sommige bronnen (ATD Letland) scheiden velden met ", "
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"), skipinitialspace=True):
                yield {k.strip(): (v or "").strip() for k, v in row.items() if k}


def mode_of(route_type):
    # transport_mode zoals de MatrixBox het kent (filters per soort in de instellingen)
    if route_type in ("0", "900"): return "TRAM"
    if route_type in ("1", "401"): return "METRO"
    if route_type in ("4", "1000", "1200"): return "SHIP"
    if route_type in ("3", "11", "700", "714", "800"): return "BUS"
    return "TRAIN"


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
    english = {}
    if cfg.get("latin_names"):
        english = {t["record_id"]: t["translation"] for t in gtfs.rows("translations.txt")
                   if t.get("table_name") == "stops" and t.get("field_name") == "stop_name" and t.get("language") == "en"}
    for s in gtfs.rows("stops.txt"):
        names[s["stop_id"]] = s["stop_name"]
        if cfg.get("latin_names"):
            names[s["stop_id"]] = english.get(s["stop_id"]) or greek_to_latin(s["stop_name"])
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
            if "color_by_agency" in cfg:
                color = cfg["color_by_agency"](line, r)
            else:
                color = cfg["color"](line) if cfg["color"] else r.get("route_color", "")
            headsign = cfg["dest"](t) if "dest" in cfg else t.get("trip_headsign", "")
            mark = 1 if cfg.get("mark_agency") and r.get("agency_id") == cfg["mark_agency"] else 0
            trips[t["trip_id"]] = (line, mode_of(r["route_type"]), color, headsign, active[t["service_id"]], mark)
            line_colors[line] = color

    stop_times = defaultdict(list)
    for st in gtfs.rows("stop_times.txt"):
        if st["trip_id"] in trips:
            stop_times[st["trip_id"]].append((int(st["stop_sequence"]), station(st["stop_id"]),
                                              st["departure_time"], st.get("pickup_type", "")))

    per_stop = defaultdict(lambda: defaultdict(set))
    for trip_id, stops in stop_times.items():
        line, mode, color, headsign, dates, mark = trips[trip_id]
        stops.sort()
        dest = headsign or names.get(stops[-1][1], "")
        info = (line, mode, dest, 0, color, 1) if mark else (line, mode, dest, 0, color)
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
    elif callable(FEEDS[args.kind]["url"]):
        url = FEEDS[args.kind]["url"]()
        print("download", url)
        raw = download(url)
    else:
        url = FEEDS[args.kind]["url"].format(year=datetime.date.today().year + (1 if datetime.date.today() >= datetime.date(datetime.date.today().year, 12, 13) else 0))
        print("download", url)
        raw = download(url)
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)
    build(args.kind, Gtfs(raw), args.out)


if __name__ == "__main__":
    main()
