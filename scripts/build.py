#!/usr/bin/env python3
"""
Φτιάχνει τους καταλόγους της εφαρμογής IptvSimple.

  python scripts/build.py live     -> live.m3u          (Live κανάλια)
  python scripts/build.py series   -> catalog/*.json    (σειρές ανά κανάλι)

Μόνο standard library, χωρίς εξαρτήσεις.
Αν μια πηγή αποτύχει ή φέρει ύποπτα λίγα αποτελέσματα, κρατιέται το παλιό αρχείο.
"""
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (X11; Linux x86_64) IptvList/1.0 (+github.com/Tzanis72/Stendor-IPTV)"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "catalog")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, accept="*/*", tries=3):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8", "replace"), r.headers
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(3 * (i + 1))
    raise err


def write_if_ok(path, data, new_count, min_ratio=0.5):
    """Γράφει το αρχείο μόνο αν δεν «έπεσε» απότομα ο αριθμός (προστασία από χαλασμένη πηγή)."""
    old_count = 0
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            old_count = len(old.get("shows", []))
        except Exception:  # noqa: BLE001
            pass
    if new_count == 0 or (old_count and new_count < old_count * min_ratio):
        print(f"  ! {os.path.basename(path)}: {new_count} (πριν {old_count}) - κρατάω το παλιό")
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  ✓ {os.path.basename(path)}: {new_count} εκπομπές")
    return True


# ============================== LIVE ==============================
# (χώρα, πηγή, ελάχιστος αριθμός καναλιών για να θεωρηθεί σωστή)
LIVE_SOURCES = [
    ("Ελλάδα", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/gr.m3u", 20),
    ("Ρωσία", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/ru.m3u", 20),
]
LIVE_FILE = "live.m3u"

_group = re.compile(r'\s*group-title="[^"]*"')


def m3u_entries(text):
    """Χωρίζει μια λίστα M3U σε κανάλια (κάθε κανάλι = #EXTINF + επιλογές + URL)."""
    entries, cur = [], []
    for line in text.splitlines():
        l = line.strip()
        if not l or l.upper().startswith("#EXTM3U"):
            continue
        if l.upper().startswith("#EXTINF"):
            cur = [l]
        elif l.startswith("#"):
            if cur:
                cur.append(l)
        elif cur:
            cur.append(l)
            entries.append(cur)
            cur = []
    return entries


def with_group(entry, group):
    first = _group.sub("", entry[0])
    first = re.sub(r"^(#EXTINF:-?\d+)", rf'\1 group-title="{group}"', first, count=1, flags=re.I)
    return [first] + entry[1:]


def entry_group(entry):
    m = re.search(r'group-title="([^"]*)"', entry[0])
    return m.group(1) if m else ""


def build_live():
    out = os.path.join(ROOT, LIVE_FILE)
    old = []
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            old = m3u_entries(f.read())

    result = []
    for group, src, minimum in LIVE_SOURCES:
        try:
            t, _ = get(src)
            entries = m3u_entries(t)
            if len(entries) < minimum:
                raise Exception(f"μόνο {len(entries)} κανάλια")
            result += [with_group(e, group) for e in entries]
            print(f"  ✓ {group}: {len(entries)} κανάλια")
        except Exception as e:  # noqa: BLE001
            kept = [e2 for e2 in old if entry_group(e2) == group]
            result += kept
            print(f"  ! {group}: {e} - κρατάω τα {len(kept)} παλιά")

    extra = os.path.join(ROOT, "extra.m3u")
    if os.path.exists(extra):
        with open(extra, encoding="utf-8") as f:
            mine = m3u_entries(f.read())
        result += [e if entry_group(e) else with_group(e, "Δικά μου") for e in mine]
        print(f"  ✓ Δικά μου: {len(mine)} κανάλια")

    if not result:
        print("Καμία πηγή δεν δούλεψε - κρατάω την παλιά λίστα")
        return
    with open(out, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n" + "\n".join("\n".join(e) for e in result) + "\n")
    print(f"  ✓ {LIVE_FILE}: {len(result)} κανάλια συνολικά")


# ============================== ΕΡΤ ==============================
# archive.ert.gr: δημόσιο WordPress API. Κάθε post = ένα επεισόδιο.
# Το βίντεο: iframe ...dt-uni-vod-arxeio-mp4.php?f=<path>.mp4 -> HLS στο mediastream.ert.gr

ERT_API = "https://archive.ert.gr/wp-json/wp/v2"
ERT_TV_CAT = 256       # «ΑΡΧΕΙΟ ΤΗΛΕΟΠΤΙΚΟΥ ΠΡΟΓΡΑΜΜΑΤΟΣ»
ERT_SERIES_CAT = 389   # «ΣΕΙΡΑ» - οι υποκατηγορίες της είναι τα είδη
ERT_STANDALONE = 399   # «ΑΥΤΟΤΕΛΕΣ»

_spaces = re.compile(r"\s+")
_part = re.compile(r"\s*[–\-]\s*[Α-ΩA-Z]\s*['’΄]?\s*ΜΕΡΟΣ\s*$", re.I)
_epsuf = re.compile(r"\s*[–\-:]?\s*ΕΠΕΙΣΟΔΙΟ\s*\d+\s*$", re.I)
_fparam = re.compile(r"""[?&](?:amp;)?f=([^"'&\s<]+)""")
_epnum = re.compile(r"Επεισόδιο\s*:?\s*0*(\d+)", re.I)
_year = re.compile(r"Χρονολογία Παραγωγής(?:\s|<[^>]*>)*(\d{4})")
_strong = re.compile(r"<strong>([^<]+)</strong>")
_code = re.compile(r"^[0-9A-Z]{5,12}$")


def clean(s):
    return _spaces.sub(" ", html.unescape(s)).strip()


def show_title(raw):
    t = clean(raw)
    t2 = _epsuf.sub("", _part.sub("", t)).strip()
    return t2 or t


def ert_hls(content):
    m = _fparam.search(content)
    if not m or not m.group(1).lower().endswith(".mp4"):
        return None
    return f"https://mediastream.ert.gr/vodedge/_definst_/mp4:archive/{m.group(1)}/playlist.m3u8"


def ert_episode(post, show):
    content = post["content"]["rendered"]
    title = clean(post["title"]["rendered"])
    num = _epnum.search(content)
    num = int(num.group(1)) if num else None
    year = _year.search(content)
    year = int(year.group(1)) if year else None
    sub = _strong.search(content)
    sub = clean(sub.group(1)) if sub else None
    if sub and (_code.match(sub) or "επεισόδιο" in sub.lower() or sub.lower() == show.lower()):
        sub = None
    parts = []
    if num is not None:
        parts.append(f"Επεισόδιο {num}")
    if sub:
        parts.append(sub)
    if title.lower() != show.lower() and title != sub:
        parts.append(title)
    if not parts:
        parts.append(title)
    label = " · ".join(parts) + (f"  ({year})" if year else "")
    return {"l": label, "u": ert_hls(content), "n": num, "y": year, "id": post["id"], "d": post.get("date", "")}


def pretty_genre(name):
    n = clean(name).replace("ΜΥΘΟΠΛΑΣΙΑ - ΔΡΑΜΑΤΟΠΟΙΗΜΕΝΕΣ ΣΕΙΡΕΣ", "ΜΥΘΟΠΛΑΣΙΑ").strip()
    return n[:1].upper() + n[1:].lower()


def ert_group(posts, genres):
    groups = {}
    for p in posts:
        title = show_title(p["title"]["rendered"])
        if not title:
            continue
        g = groups.setdefault(title.upper(), {"t": title, "posts": []})
        g["posts"].append(p)
    shows = []
    for g in groups.values():
        eps = [ert_episode(p, g["t"]) for p in g["posts"]]
        eps = [e for e in eps if e["u"]]
        if not eps:
            continue
        eps.sort(key=lambda e: (e["n"] is None, e["n"] or 0, e["y"] or 0, e["id"]))
        genre = next((genres[c] for p in g["posts"] for c in p.get("categories", []) if c in genres), "")
        shows.append({
            "t": g["t"], "g": genre, "d": max(e["d"] for e in eps)[:10],
            "e": [{"l": e["l"], "u": e["u"]} for e in eps],
        })
    shows.sort(key=lambda s: s["t"])
    return shows


def build_ert():
    cats = json.loads(get(f"{ERT_API}/categories?parent={ERT_SERIES_CAT}&per_page=100&_fields=id,name", "application/json")[0])
    genres = {c["id"]: pretty_genre(c["name"]) for c in cats}
    genres[ERT_STANDALONE] = "Αυτοτελή"

    def url(p):
        return (f"{ERT_API}/posts?categories={ERT_TV_CAT}&per_page=100&page={p}"
                f"&_fields=id,title,date,categories,content")

    body, headers = get(url(1), "application/json")
    total = int(headers.get("X-WP-TotalPages") or 1)
    print(f"  ΕΡΤ: {total} σελίδες")
    pages = [json.loads(body)]
    with cf.ThreadPoolExecutor(4) as ex:
        pages += list(ex.map(lambda p: json.loads(get(url(p), "application/json")[0]), range(2, total + 1)))
    posts, seen = [], set()
    for page in pages:
        for p in page:
            if p["id"] not in seen:
                seen.add(p["id"])
                posts.append(p)
    shows = ert_group(posts, genres)
    return {"id": "ert", "name": "Αρχείο ΕΡΤ", "updated": now(),
            "genres": sorted({s["g"] for s in shows if s["g"]}), "shows": shows}


# ============================== ΣΕΙΡΕΣ ==============================

# Κάθε κανάλι: (id, όνομα, συνάρτηση). Νέα κανάλια προστίθενται εδώ.
CHANNELS = [
    ("ert", "Αρχείο ΕΡΤ", build_ert),
]


def build_series():
    os.makedirs(CATALOG, exist_ok=True)
    for cid, name, fn in CHANNELS:
        print(f"→ {name}")
        try:
            data = fn()
            write_if_ok(os.path.join(CATALOG, f"{cid}.json"), data, len(data["shows"]))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {name}: {e} - κρατάω το παλιό")

    # index.json: ό,τι αρχεία καναλιών υπάρχουν (και παλιά που δεν ανανεώθηκαν)
    index = []
    for cid, name, _ in CHANNELS:
        path = os.path.join(CATALOG, f"{cid}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            index.append({"id": cid, "name": name, "file": f"{cid}.json",
                          "shows": len(d.get("shows", [])), "updated": d.get("updated", "")})
    with open(os.path.join(CATALOG, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": now(), "channels": index}, f, ensure_ascii=False, indent=1)
    print(f"  ✓ index.json: {len(index)} κανάλια")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("live", "all"):
        build_live()
    if what in ("series", "all"):
        build_series()
