"""Thaise letters voor font_small van de MatrixBox (bestemmingen van State Railway of Thailand).

font_small is 8 pixels hoog: rij 0-1 tekens erboven, 2-6 romp, 7 tekens eronder. De letters zijn getekend naar
Tahoma op 10 px (dat font is voor Thais op klein formaat gemaakt) en met de hand bijgewerkt; de laatste kolom
is de ruimte tot de volgende letter.

Het bord tekent letters gewoon naast elkaar en kent geen tekens die bóven of onder een letter staan (klinkers
als ิ ุ, toontekens als ่ ้). Daarom krijgt elke combinatie letter + teken(s) een eigen teken in de
Private Use Area (U+E000 en verder): `encode()` zet een Thaise naam daarin om, en geeft None als er een letter
of combinatie in staat die hier (nog) niet getekend is; het bouwscript toont dan de Latijnse naam.
Nieuwe combinaties ALTIJD achteraan CLUSTERS toevoegen: de codes moeten gelijk blijven aan wat er op het bord staat.

Een firmware-update kan /lib/font_small.py overschrijven. Opnieuw toevoegen:
  python3 matrixbox/thai_font.py font_small.py        (een kopie van /lib/font_small.py van het bord)
en het bestand terugzetten. Het script slaat tekens over die er al in staan.
"""
import re
import sys

BASE = {
    "ก": ["......", "......", ".###..", "#...#.", "##..#.", "#...#.", "#...#.", "......"],
    "ข": ["......", "......", "###.#.", "#.#.#.", ".#..#.", ".#..#.", ".####.", "......"],
    "ค": ["......", "......", ".###..", "#...#.", "###.#.", "#...#.", "#...#.", "......"],
    "ง": [".....", ".....", "..##.", "...#.", "#..#.", ".#.#.", "..##.", "....."],
    "ช": ["......", "......", "#####.", "#.#.#.", ".#..#.", ".#..#.", ".####.", "......"],
    "ญ": ["........", "........", ".###..#.", "#...#.#.", "##..#.#.", "#...#.#.", "##..###.", "....#.#."],
    "ฐ": [".....", ".....", ".###.", "#....", "####.", "...#.", "###..", "#.#.."],
    "ฒ": [".........", ".........", ".#.#...#.", "#.#.#..#.", "#...#..#.", "#.#.##.#.", "##..#.##.", "........."],
    "ด": ["......", "......", ".###..", "#...#.", "#.#.#.", "#.#.#.", "##..#.", "......"],
    "ต": ["......", "......", ".#.#..", "#.#.#.", "#...#.", "#.#.#.", "##..#.", "......"],
    "ท": [".......", ".......", "##..##.", ".#.#.#.", ".#.#.#.", ".##..#.", ".#...#.", "......."],
    "ธ": [".....", ".....", ".###.", "#....", "####.", ".#.#.", "####.", "....."],
    "น": ["......", "......", "##..#.", ".#..#.", ".#..#.", ".#.##.", ".##.#.", "......"],
    "บ": ["......", "......", "##..#.", ".#..#.", ".#..#.", ".#..#.", ".####.", "......"],
    "ป": ["....#.", "....#.", "##..#.", ".#..#.", ".#..#.", ".#..#.", ".####.", "......"],
    "พ": [".......", ".......", "##.#.#.", ".#.#.#.", ".##.##.", ".##.##.", ".#...#.", "......."],
    "ภ": ["......", "......", ".###..", "#...#.", "###.#.", ".#..#.", "##..#.", "......"],
    "ม": ["......", "......", "##..#.", ".#..#.", ".#..#.", "###.#.", "##.##.", "......"],
    "ย": ["......", "......", "##..#.", "#...#.", ".#..#.", "#...#.", "#####.", "......"],
    "ร": ["....", "....", ".##.", "#...", "###.", "..#.", ".##.", "...."],
    "ล": ["......", "......", "####..", "....#.", ".##.#.", "#..##.", "##..#.", "......"],
    "ว": [".....", ".....", ".###.", "#..#.", "...#.", "...#.", "..##.", "....."],
    "ศ": ["......", "......", ".####.", "#..##.", "###.#.", "#...#.", "#...#.", "......"],
    "ส": ["......", "......", "#####.", "...##.", ".##.#.", "#..##.", "##..#.", "......"],
    "ห": [".......", ".......", "##..##.", ".#..##.", ".#.#.#.", ".##..#.", ".#...#.", "......."],
    "อ": ["......", "......", "####..", "....#.", "##..#.", "#...#.", "#####.", "......"],
    "ะ": [".....", ".....", "#..#.", "###..", ".....", "#..#.", "###..", "....."],
    "า": [".....", ".....", "###..", "...#.", "...#.", "...#.", "...#.", "....."],
    "เ": ["...", "...", "#..", "#..", "#..", "#..", "##.", "..."],
    "แ": ["......", "......", "#..#..", "#..#..", "#..#..", "#..#..", "##.##.", "......"],
    "โ": [".####.", "#.....", "...#..", "...#..", "...#..", "...#..", "...##.", "......"],
    "ใ": ["####.", "#..#.", "..#..", "..#..", "..#..", "..#..", "..##.", "....."],
}

# Tekens boven (rij 0-1) of onder (rij 7) de letter, rechts uitgelijnd op de romp
ABOVE = {
    "่": ["#", "#"],                # ่ mai ek
    "้": ["##.#", ".##."],          # ้ mai tho
    "์": ["###", "#.."],            # ์ thanthakhat
    "ั": ["#.#", "##."],            # ั mai han-akat
    "ิ": [".##.", "####"],          # ิ sara i
    "ี": [".#.#", "####"],          # ี sara ii
}
BELOW = {"ุ": "##"}                 # ุ sara u

# Met de hand: twee tekens boven elkaar passen niet in twee rijen, en ำ (sara am) heeft een rondje boven de letter ervoor
SPECIAL = {
    "ดื่": ["..#.#.#.", ".####.#.", ".###....", "#...#...", "#.#.#...", "#.#.#...", "##..#...", "........"],
    "ลำ": ["....##.....", "....##.....", "####..###..", "....#....#.", ".##.#....#.", "#..##....#.", "##..#....#.", "..........."],
}

# Volgorde = code op het bord (U+E000 + plaats). Alleen achteraan aanvullen!
CLUSTERS = ["ก่", "ข้", "ชั", "ชี", "ชุ", "ญ่", "ดื่", "นี", "น์", "บุ", "บ้", "พ่", "ภิ", "ม่", "รั", "รี", "รุ", "ร์", "ลำ",
            "วั", "วี", "สิ", "หั", "อุ"]
CODE = {c: chr(0xE000 + i) for i, c in enumerate(CLUSTERS)}

MARKS = "ัิ-ฺ็-๎"
CLUSTER_RE = re.compile("(.)([%s]*)(ำ?)" % MARKS)


def compose(cluster):
    if cluster in SPECIAL:
        return SPECIAL[cluster]
    rows = list(BASE[cluster[0]])
    width = len(rows[0])
    ink = [c for c in range(width) if any(rows[r][c] == "#" for r in range(2, 7))]
    right = max(ink)
    for mark in cluster[1:]:
        if mark in ABOVE:
            pattern, targets = ABOVE[mark], (0, 1)
        else:
            pattern, targets = [BELOW[mark]], (7,)
        w = len(pattern[0])
        start = max(0, right - w + 1)
        for r, line in zip(targets, pattern):
            row = list(rows[r])
            for i, px in enumerate(line):
                if px == "#" and start + i < width - 1:
                    row[start + i] = "#"
            rows[r] = "".join(row)
    return rows


def glyphs():
    """Alle tekens voor het bord: {teken: 8 rijen}."""
    out = dict(BASE)
    for c in CLUSTERS:
        out[CODE[c]] = compose(c)
    return out


def encode(text):
    """Thaise tekst -> tekens zoals het bord ze kent, of None als er iets niet getekend is."""
    out = []
    for m in CLUSTER_RE.finditer(text):
        whole = m.group(0)
        if m.group(2) or m.group(3):
            if whole not in CODE:
                return None
            out.append(CODE[whole])
        elif "฀" <= whole <= "๿":
            if whole not in BASE:
                return None
            out.append(whole)
        else:
            out.append(whole)
    return "".join(out)


def add_to_font(text):
    """Voegt de tekens toe vóór de afsluitende } van de font-dict; behoudt de regeleinden van het bestand."""
    nl = "\r\n" if "\r\n" in text else "\n"
    entries = []
    for ch, rows in glyphs().items():
        key = '"\\u%04x"' % ord(ch) if ord(ch) >= 0xE000 else '"%s"' % ch  # PUA als escape: leesbaar en veilig
        if key + ":" in text:
            continue
        bits = [("    0b" + r.replace("#", "1").replace(".", "0") + ",") for r in rows]
        entries.append(nl.join(["  %s: [" % key, "    %d," % len(rows[0])] + bits + ["  ],"]))
    if not entries:
        return text
    end = text.rstrip().rfind("}")
    return text[:end] + nl + "  ### THAIS" + nl + nl + (nl + nl).join(entries) + nl + nl + text[end:]


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(add_to_font(text))
