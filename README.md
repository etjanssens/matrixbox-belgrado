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

De MatrixBox zet zijn klok gelijk met de `Date`-header, maar Cloudflare maakt daar altijd GMT van. Daarom stuurt de Worker lokale tijd in Belgrado mee in `X-Local-Time` (`Sat, 10 Oct 2026 14:18:42 +0200`, het formaat van data.t-skylt.se). De aangepaste `fetch_data` op de MatrixBox geeft die header voorrang.

## Bronnen

| Vervoerder | Bron | Live |
|---|---|---|
| Belgrado (GSP) | [data.gov.rs](https://data.gov.rs/sr/datasets/gradski-javni-prevoz-u-beogradu-gtfs/) | nee |
| Renfe (Cercanías + AVE/LD) | [data.renfe.com](https://data.renfe.com) (CC BY 4.0), GTFS-RT van gtfsrt.renfe.com | ja (vertraging, uitval) |
| Trenitalia, Italo | [trenitalia-gtfs](https://github.com/deryclem/trenitalia-gtfs), [italo-gtfs](https://github.com/deryclem/italo-gtfs) (CC BY 4.0, © Clément Desouche; officiële NeTEx van het Italiaanse NAP) | nee |
| Trenord | [dati.lombardia.it](https://www.dati.lombardia.it) | nee |
| EAV (Circumvesuviana e.a.) | [eavsrl.it/open-data](https://www.eavsrl.it/open-data/) | nee |
| ARST (treinlijnen Sardinië) | [arstspa.info](https://www.arstspa.info) | nee |
| LPP (stadsbussen Ljubljana) | [data.lpp.si](https://data.lpp.si) (open data; GTFS + live-API) | ja (live aankomsten) |
| CP (Portugal) | [publico.cp.pt](https://publico.cp.pt/gtfs/gtfs.zip) (licentie niet vermeld) | nee |
| Euskotren (Baskenland) | Moveuskadi, ftp.geo.euskadi.net (CC BY 4.0) | nee |
| Estland (Elron, bussen, Tallinn) | [landelijke GTFS](https://eu-gtfs.remix.com/estonia_unified_gtfs.zip) (Regionaal- ja Põllumajandusministeerium) | nee |
| Tsjechië (alle treinen) | [Oběhy/JrUtil](https://motis.obehy.cz/get-feeds/) omzetting van CIS JŘ (CC0) | nee |
| Letland: Vivi, Rīgas Satiksme, ATD | [data.gov.lv](https://data.gov.lv) (CC0) | nee |
| Hellenic Train (Griekenland) | [Transitous-GTFS](https://jbb.ghsq.de/gtfs/) (community, uit railway.gov.gr; geen licentie vermeld) | nee |
| Tallinn stadsvervoer | transport.tallinn.ee (open; live vertrektijden) | ja |
| ÖBB (incl. Montafonerbahn, CAT) | [data.oebb.at](https://data.oebb.at/de/datensaetze~soll-fahrplan-gtfs~) (CC BY 4.0) | nee |
| HŽPP (Kroatië) | [hzpp.hr](https://www.hzpp.hr/GTFS_files.zip) | nee |
| SŽ + bussen (Nomago, Arriva, AP Murska Sobota, LPP) | landelijke GTFS van het [Sloveense NAP](https://www.nap.si) (account nodig; geheimen NAP_USER/NAP_PASSWORD) | nee (SIRI bevat alleen busposities, geen vertragingen) |

## Privé-data (Slovenië)

De NAP-data van Slovenië is alleen met een account beschikbaar en mag niet openbaar. De job `slovenia` in de
Action zet hem daarom niet op Pages maar in de privé-R2-opslag `matrixbox-private`, gebundeld in 128 bestanden
(`scripts/shard.py`); alleen de Worker kan erbij (binding `PRIVATE`). Geheimen: `NAP_USER`, `NAP_PASSWORD`,
`CLOUDFLARE_API_TOKEN` (R2 Edit). De NAP-download (~90 MB, traag) gebeurt één keer per week.

## Lokaal draaien

```bash
python3 scripts/build.py public          # downloadt de GTFS en maakt ./public
npx wrangler dev                         # Worker lokaal; zet DATA_URL in wrangler.toml
```

## Aanpassingen op de MatrixBox

In `departures/functions.py` stuurt `custom_apis` het land `rs` naar deze Worker in plaats van naar data.t-skylt.se. Verder staan Servië en het vlaggetje in `web.py` en `dicts.py`. Let op: een firmware-update van T-Skylt overschrijft deze bestanden. Opnieuw toepassen:

```bash
cd /Volumes/CIRCUITPY && patch -p1 < ~/Desktop/matrixbox-belgrado/matrixbox/departures.patch
```

(De MatrixBox heeft weinig vrije ruimte; als `patch` faalt omdat de schijf vol is, overschrijf de bestanden dan ter plekke.)
