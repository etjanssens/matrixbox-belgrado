"""Oezbekistan: vertrekstaten per station (PDF's van Uzbekistan Railways) omzetten naar een weekdienstregeling.

Er is geen open GTFS. Op de site van de spoorwegen kun je per station een PDF "Departure from the station"
downloaden ("Jadvalni yuklash"); die zetten we met de hand over in een CSV per station:

  station,trein,naar,tijd,dagen          dagen: 0=ma .. 6=zo, "daily", of "odd"/"even" (oneven/even datums)
  TC,768Ф,Samarkand,07:55,daily

De bron is geen open data, dus de CSV's staan niet in de repo (map private/, zie .gitignore) en de uitvoer
gaat naar de privé R2-opslag, niet naar GitHub Pages. Er is geen nachtelijke build: de dienstregeling is
per week vast; bij een nieuwe PDF de CSV bijwerken en opnieuw bouwen en uploaden. Treinen op even/oneven
datums krijgen losse datums t/m LAST_DATE: daarvoor opnieuw bouwen.

Gebruik:
  python3 scripts/build_uz.py private/uz-bron private/uz
  cd private && for f in uz/stops.json uz/calendar.json uz/shards/0.json; do
    npx wrangler r2 object put "matrixbox-private/$f" --file "$f" --content-type application/json --remote; done
"""
import csv
import datetime
import json
import os
import shutil
import sys
from collections import Counter, defaultdict

# station-code in de CSV -> (vast ID voor de Worker, naam op het bord)
STATIONS = {
    "TC": ("1", "Tashkent Central"),   # Toshkent Shimoliy (ТОШКЕНТ-ШИ)
    "TS": ("2", "Tashkent South"),     # Toshkent Janubiy (ТОШКЕНТ ЖА)
    "BK": ("3", "Bukhara"),
    "KR": ("4", "Karshi"),
    "AN": ("5", "Andijan"),
    "KG": ("6", "Kungrad"),
    "TZ": ("7", "Termez"),
    "SR": ("8", "Sariosiyo"),
    "KH": ("9", "Khiva"),
    "AL": ("10", "Alat"),
    "SH": ("11", "Shovot"),
    "UR": ("12", "Urgench"),
    "SM": ("13", "Samarkand"),
    "SZ": ("14", "Shakhrisabz"),
}
LAST_DATE = datetime.date(2028, 12, 31)

LATIN = str.maketrans({"Ф": "F", "Т": "T", "Ж": "J", "З": "Z", "Ь": "", "Щ": "", "Г": "G", "Х": "X", "М": "M",
                       "Ч": "C", "Й": "Y"})


def line_and_color(train):
    """Treinnummer zonder letter (3 cijfers past in de lijnkolom); kleur naar soort trein (eigen keuze)."""
    num = train.translate(LATIN).rstrip("FTJZGXMCY")
    n = int(num)
    if 763 <= n <= 778:
        color = "C8102E"  # Afrosiyob (hogesnelheid)
    elif 700 <= n < 800:
        color = "0072BC"  # dagtreinen (Sharq, Nasaf, naar Andijan/Khiva)
    else:
        color = "2E9E4F"  # nacht- en internationale treinen
    return num, color


def main(src, out):
    rows = []
    for name in sorted(os.listdir(src)):
        if name.endswith(".csv"):
            with open(os.path.join(src, name), encoding="utf-8") as f:
                rows += list(csv.DictReader(line for line in f if not line.startswith("#")))

    services = {}  # "0123456" -> lijst van weekdagen; "odd"/"even" -> [] (rijdt alleen op losse datums)
    per_stop = defaultdict(lambda: {"l": []})
    for r in rows:
        dagen = r["dagen"].strip()
        if dagen in ("odd", "even"):
            sid, days = dagen, []
        else:
            days = list(range(7)) if dagen == "daily" else sorted(int(d) for d in dagen.split())
            sid = "".join(map(str, days))
        services[sid] = days
        line, color = line_and_color(r["trein"].strip())
        info = [line, "TRAIN", r["naar"].strip(), 0, color]
        stop = per_stop[STATIONS[r["station"]][0]]
        if info not in stop["l"]:
            stop["l"].append(info)
        h, m = r["tijd"].split(":")
        stop.setdefault(sid, []).append([int(h) * 60 + int(m), stop["l"].index(info)])
    for stop in per_stop.values():
        for key, deps in stop.items():
            if key != "l":
                deps.sort()

    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "shards"))
    calendar = {"weekly": {sid: {"days": days, "start": "20260101", "end": "20991231"} for sid, days in services.items()},
                "added": defaultdict(list), "removed": {}}
    d = datetime.date.today() - datetime.timedelta(days=1)
    while d <= LAST_DATE:
        parity = "odd" if d.day % 2 else "even"
        if parity in services:
            calendar["added"][d.strftime("%Y%m%d")].append(parity)
        d += datetime.timedelta(days=1)
    stops = []
    for code, (sid, name) in STATIONS.items():
        if sid in per_stop:
            dests = Counter(per_stop[sid]["l"][i][2] for k, deps in per_stop[sid].items() if k != "l" for _, i in deps)
            stops.append([sid, sid, name, ", ".join(d for d, _ in dests.most_common(2))])
    for fname, data in (("calendar.json", calendar), ("stops.json", stops), ("shards/0.json", dict(per_stop))):
        with open(os.path.join(out, fname), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(rows)} vertrekken, {len(stops)} stations, {len(services)} dienstregelingen naar {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
