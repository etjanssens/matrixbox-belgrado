# MatrixBox Belgrado

Vertrektijden van het openbaar vervoer in Belgrado (bus, tram en trolleybus van Beograd Plus) voor de MatrixBox met T-Skylt-firmware.

De tijden komen uit de **officiële dienstregeling** (GTFS, [data.gov.rs](https://data.gov.rs/sr/datasets/gradski-javni-prevoz-u-beogradu-gtfs/)). Ze zijn dus niet live: vertraging zie je niet.

## Hoe het werkt

```
data.gov.rs (GTFS-zip)
   │  GitHub Action, elke maandag: scripts/build.py
   ▼
GitHub Pages: stops.json, calendar.json, dep/<halte>.json
   │  opgehaald (en gecachet) door
   ▼
Cloudflare Worker (worker/index.js), spreekt dezelfde taal als data.t-skylt.se
   │  gewoon HTTP, poort 80
   ▼
MatrixBox: land "rs", vervoerder "bg"
```

Endpoints van de Worker:

| Pad | Wat |
|---|---|
| `/get_departures?station=20067` | volgende 15 vertrektijden; meerdere haltes met `20067-20068` |
| `/search_stop?station=slavija` | haltes zoeken (accenten maken niet uit) |
| `/get_deviations` | altijd leeg |

De `Date`-header bevat lokale tijd in Belgrado met offset (`+0200`/`+0100`). De MatrixBox zet daar zijn klok mee gelijk, net als bij data.t-skylt.se.

## Lokaal draaien

```bash
python3 scripts/build.py public          # downloadt de GTFS en maakt ./public
npx wrangler dev                         # Worker lokaal; zet DATA_URL in wrangler.toml
```

## Aanpassingen op de MatrixBox

In `departures/functions.py` stuurt `custom_apis` het land `rs` naar deze Worker in plaats van naar data.t-skylt.se. Verder staan Servië en het vlaggetje in `web.py` en `dicts.py`. Let op: een firmware-update van T-Skylt overschrijft deze bestanden.
