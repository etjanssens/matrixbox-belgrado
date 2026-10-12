"""Downloadt een dataset van data.mobilitaetsverbuende.at (account nodig: MVO_USER / MVO_PASSWORD).

Inloggen gaat via Keycloak (wachtwoord-grant, client dbp-public-ui), daarna de datasetlijst en het bestand
van de actieve versie. De licentie ("Data License Mobilitätsverbünde Österreich") moet één keer in de
webinterface geaccepteerd zijn. De data gaat niet naar de openbare Pages maar privé naar R2.

Gebruik: python3 scripts/mvo_download.py "Railway Timetable Data (GTFS) - Current" uit.zip
"""
import json
import os
import sys
import urllib.parse
import urllib.request

KEYCLOAK = "https://user.mobilitaetsverbuende.at/auth/realms/dbp-public/protocol/openid-connect/token"
API = "https://data.mobilitaetsverbuende.at/api/public/v1/data-sets"


def main(name_prefix, out):
    form = urllib.parse.urlencode({"client_id": "dbp-public-ui", "grant_type": "password", "scope": "openid",
                                   "username": os.environ["MVO_USER"], "password": os.environ["MVO_PASSWORD"]}).encode()
    with urllib.request.urlopen(urllib.request.Request(KEYCLOAK, data=form), timeout=60) as r:
        token = json.load(r)["access_token"]
    auth = {"Authorization": "Bearer " + token}
    with urllib.request.urlopen(urllib.request.Request(API + "?tagFilterModeInclusive=true", headers=auth), timeout=60) as r:
        datasets = json.load(r)
    ds = next(d for d in datasets if d.get("nameEn", "").startswith(name_prefix))
    year = ds["activeVersions"][0]["year"]
    print("dataset", ds["id"], year, ds["nameEn"])
    with urllib.request.urlopen(urllib.request.Request(f"{API}/{ds['id']}/{year}/file", headers=auth), timeout=1800) as r, \
            open(out, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    print("opgeslagen:", out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
