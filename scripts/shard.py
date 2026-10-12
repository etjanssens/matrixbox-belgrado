"""Bundelt dep/<station>.json tot SHARDS bestanden (shards/<station % SHARDS>.json), voor privé-opslag in R2.

Voor bronnen die niet openbaar mogen (Slovenië, NAP met account): duizenden losse bestanden uploaden duurt
te lang, een paar honderd bundels niet. De Worker rekent dezelfde bundel uit (station-ID modulo SHARDS).

Gebruik: python3 scripts/shard.py private/sz
"""
import json
import os
import shutil
import sys

SHARDS = 128  # moet gelijk zijn aan `shards` bij de feed in worker/index.js


def main(folder):
    dep = os.path.join(folder, "dep")
    shards = [{} for _ in range(SHARDS)]
    for name in os.listdir(dep):
        sid = name[:-5]
        with open(os.path.join(dep, name), encoding="utf-8") as f:
            shards[int(sid) % SHARDS][sid] = json.load(f)
    out = os.path.join(folder, "shards")
    os.makedirs(out, exist_ok=True)
    for i, data in enumerate(shards):
        with open(os.path.join(out, f"{i}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    shutil.rmtree(dep)
    print(f"{sum(len(s) for s in shards)} stations in {SHARDS} bundels")


if __name__ == "__main__":
    main(sys.argv[1])
