"""Ontbrekende letters voor het font van de VBZ-weergave (departures/skins/vbz/vbz_font.json).

Het font komt uit het Zwitserse voorbeeldproject en mist Å Ä Ö å Ü en X x j Q, nodig voor Zweedse namen
(Växjö, Mjölby) en bijv. Östgötatrafiken; een ontbrekende letter wordt "_". 12 rijen: 0-1 accent of punt,
hoofdletters 1-9, kleine letters 3-9, staart 10-11. Zoals É staat een hoofdletter met accent een rij lager
en is hij een rij korter. De laatste kolom is de ruimte tot de volgende letter.

Een update van de skin kan vbz_font.json overschrijven. Opnieuw toevoegen:
  python3 matrixbox/vbz_font.py vbz_font.json        (een kopie van het bestand van het bord)
en het bestand terugzetten. Het script slaat letters over die er al in staan.
"""
import sys

GLYPHS = {
    "Ö": ["##..##.", ".......", ".####..", "##..##.", "##..##.", "##..##.", "##..##.", "##..##.", "##..##.", ".####..", ".......", "......."],
    "Ä": ["##..##.", ".......", ".####..", "##..##.", "##..##.", "##..##.", "######.", "##..##.", "##..##.", "##..##.", ".......", "......."],
    "Å": ["..##...", ".#..#..", ".####..", "##..##.", "##..##.", "##..##.", "######.", "##..##.", "##..##.", "##..##.", ".......", "......."],
    "Ü": ["##..##.", ".......", "##..##.", "##..##.", "##..##.", "##..##.", "##..##.", "##..##.", "##..##.", ".####..", ".......", "......."],
    "å": ["..##...", ".#..#..", "..##...", ".####..", "##..##.", "....##.", ".#####.", "##..##.", "##..##.", ".###.#.", ".......", "......."],
    "X": [".......", "##..##.", "##..##.", "##..##.", ".####..", "..##...", ".####..", "##..##.", "##..##.", "##..##.", ".......", "......."],
    "x": [".......", ".......", ".......", "##..##.", "##..##.", ".####..", "..##...", ".####..", "##..##.", "##..##.", ".......", "......."],
    "j": ["..##.", "..##.", ".....", "..##.", "..##.", "..##.", "..##.", "..##.", "..##.", "..##.", "..##.", "###.."],
    "Q": [".......", ".####..", "##..##.", "##..##.", "##..##.", "##..##.", "##..##.", "##.###.", "##..##.", ".###.#.", ".....#.", "......."],
}


def add_to_font(text):
    """Voegt de letters toe vóór de afsluitende } van het JSON-font; behoudt de regeleinden van het bestand."""
    nl = "\r\n" if "\r\n" in text else "\n"
    entries = []
    for ch, rows in GLYPHS.items():
        if '"%s":' % ch in text:
            continue
        bits = ['    "%s",' % r.replace("#", "1").replace(".", "0") for r in rows]
        bits[-1] = bits[-1].rstrip(",")
        entries.append(nl.join(['  "%s": [' % ch, "    %d," % len(rows[0])] + bits + ["  ],"]))
    if not entries:
        return text
    end = text.rstrip().rfind("}")
    return text[:end] + nl.join(entries) + nl + text[end:]


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(add_to_font(text))
