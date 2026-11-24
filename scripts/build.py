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
import urllib.parse
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


# ============================== ΙΔΙΩΤΙΚΑ ΚΑΝΑΛΙΑ (ΣΚΑΪ, Mega, ANT1) ==============================
# Παίρνουμε μόνο τη λίστα εκπομπών/επεισοδίων από τις επίσημες σελίδες τους.
# Η διεύθυνση του βίντεο βρίσκεται από την εφαρμογή τη στιγμή που πατάς το επεισόδιο
# (skai:/mega:/ant1: μπροστά από το link), γιατί τα βίντεο παίζουν μόνο από Ελλάδα/Κύπρο.

BUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def bget(url, tries=2):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BUA, "Accept-Language": "el-GR,el;q=0.9"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 * (i + 1))
    raise err


def pmap(fn, items, workers=8):
    """Τρέχει παράλληλα· ό,τι αποτύχει επιστρέφει None."""
    def safe(x):
        try:
            return fn(x)
        except Exception as e:  # noqa: BLE001
            print(f"    ! {x}: {e}")
            return None
    with cf.ThreadPoolExecutor(workers) as ex:
        return list(ex.map(safe, items))


_epn = re.compile(r"(?:επεισ[οό]διο|episode|epeisodio)\D{0,3}0*(\d+)", re.I)


def ep_number(text):
    m = _epn.search(text or "")
    return int(m.group(1)) if m else None


def finish(cid, name, shows):
    shows = [s for s in shows if s and s["e"]]
    seen, out = set(), []
    for s in shows:
        k = s["t"].lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    out.sort(key=lambda s: s["t"].lower())
    return {"id": cid, "name": name, "updated": now(),
            "genres": sorted({s["g"] for s in out if s["g"]}), "shows": out}


# ---------- ΣΚΑΪ ----------
SKAI = "https://www.skai.gr"
SKAI_CATS = [("seires", "Σειρές"), ("ntokimanter", "Ντοκιμαντέρ"), ("psuchagogia", "Ψυχαγωγία")]


def js_blocks(t):
    out, pos, dec = [], 0, json.JSONDecoder()
    while True:
        i = t.find("var data = ", pos)
        if i < 0:
            return out
        try:
            obj, end = dec.raw_decode(t, i + 11)
            out.append(obj)
            pos = end
        except Exception:  # noqa: BLE001
            pos = i + 11


def skai_season(arg):
    path, genre = arg
    t = bget(SKAI + path)
    parts = path.strip("/").split("/")          # tv/show/<cat>/<slug>/<season>
    others = {p.rstrip("/") for p in re.findall(r'href="(/tv/show/%s/%s/[^"#?/]+)/?"' % (parts[2], re.escape(parts[3])), t)}
    blocks = [b for b in js_blocks(t) if isinstance(b, dict) and b.get("show")]
    if not blocks:
        return None, others
    d = blocks[0]
    show = d["show"]
    eps = d.get("episodes") or []
    if not eps:
        return None, others
    full = eps
    first = eps[0].get("link")
    if first:
        try:
            for b in js_blocks(bget(SKAI + "/tv" + first)):
                if isinstance(b, dict) and isinstance(b.get("episodes"), list) and len(b["episodes"]) > len(full):
                    full = b["episodes"]
        except Exception:  # noqa: BLE001
            pass
    title = clean(show.get("title") or "")
    sub = clean(show.get("subtitle") or "")
    items, seen = [], set()
    for e in full:
        link = e.get("link")
        if not link or link in seen:
            continue
        seen.add(link)
        lab = clean(re.sub(r"<[^>]+>", "", e.get("title") or ""))
        bits = [x.strip() for x in lab.split("|") if x.strip()]
        if len(bits) > 1 and bits[0].lower() == title.lower():
            lab = " | ".join(bits[1:])
        num = e.get("episode_number")
        num = int(num) if str(num).isdigit() else ep_number(lab)
        items.append((num, e.get("start") or "", lab or title, "skai:" + SKAI + "/tv" + link))
    items.sort(key=lambda x: (x[0] is None, x[0] or 0, x[1]))
    return ({"t": f"{title} · {sub}" if sub else title, "g": genre,
             "d": max((x[1] for x in items), default="")[:10],
             "e": [{"l": x[2], "u": x[3]} for x in items]}, others)


def build_skai():
    todo = {}
    for cat, genre in SKAI_CATS:
        t = bget(f"{SKAI}/tv/shows/{cat}")
        for p in re.findall(r'href="(/tv/show/%s/[^"#?]+)"' % cat, t):
            todo.setdefault(p.rstrip("/"), genre)
    done, shows = set(), []
    for _ in range(3):                         # και τις παλαιότερες σεζόν των ίδιων εκπομπών
        wave = [(p, g) for p, g in todo.items() if p not in done][:300]
        if not wave:
            break
        done.update(p for p, _ in wave)
        for (path, genre), res in zip(wave, pmap(skai_season, wave)):
            if not res:
                continue
            show, others = res
            shows.append(show)
            for o in others:
                todo.setdefault(o, genre)
    print(f"  ΣΚΑΪ: {len(done)} σεζόν")
    return finish("skai", "ΣΚΑΪ", shows)


# ---------- MEGA ----------
MEGA = "https://www.megatv.com"
MEGA_LISTS = [("/showtype/ntokimanter/", "Ντοκιμαντέρ"), ("/showtype/paidika/", "Παιδικά"),
              ("/showtype/entertainment/", "Ψυχαγωγία"), ("/showtype/flashback/", "Flashback"),
              ("/showtype/greekseries/", "Σειρές"), ("/tag/current-season/", "Σειρές"),
              ("/tag/archive/?stID=10", "Σειρές"), ("/tag/megaclassics/", "Σειρές")]
MEGA_SKIP = ("mega-zhn",)


def mega_show(arg):
    url, genre = arg
    t = bget(url)
    m = re.search(r"<title>(.*?)</title>", t, re.S)
    title = clean(re.sub(r"\s*\|\s*MEGA TV\s*$", "", m.group(1))) if m else url
    items, seen = [], set()
    for sid, typ in dict.fromkeys(re.findall(r"episodes/?\?id=([A-Za-z0-9]+)&(?:amp;)?type=([a-z]+)", t)):
        e = bget(f"{MEGA}/episodes/?id={sid}&type={typ}")
        for href, lab in re.findall(r'<a href="(https://www\.megatv\.com/[a-z]+/\d+/[^"]+)" class="card__link">([^<]*)</a>', e):
            if href in seen:
                continue
            seen.add(href)
            lab = clean(lab).rstrip(" -–")
            items.append((ep_number(lab), lab or title, "mega:" + href))
    items.reverse()                              # η σελίδα έχει πρώτα τα νεότερα
    items.sort(key=lambda x: (x[0] is None, x[0] or 0))
    return {"t": title, "g": genre, "d": "", "e": [{"l": x[1], "u": x[2]} for x in items]}


def build_mega():
    shows = {}
    for path, genre in MEGA_LISTS:
        try:
            t = bget(MEGA + path)
        except Exception as e:  # noqa: BLE001
            print(f"    ! {path}: {e}")
            continue
        for u in re.findall(r'href="(https://www\.megatv\.com/ekpompes/\d+/([^"/]+)/?)"', t):
            if u[1] not in MEGA_SKIP:
                shows.setdefault(u[0].rstrip("/") + "/", genre)
    print(f"  Mega: {len(shows)} εκπομπές")
    return finish("mega", "Mega", pmap(mega_show, list(shows.items())))


# ---------- ANT1 ----------
ANT = "https://www.antenna.gr"
_ant_skip = re.compile(r"sneak|preview|trailer|promo|teaser", re.I)


def ant_label(slug):
    n = ep_number(slug.replace("-", " "))
    season = re.search(r"(?:sezon-0*(\d+)|0*(\d+)(?:os|i)-(?:kyklos|sezon))", slug)
    if n is not None:
        lab = f"Επεισόδιο {n}"
        if season:
            lab = f"Σεζόν {season.group(1) or season.group(2)} · " + lab
        return lab
    return slug.replace("-", " ").strip().capitalize()


def ant_show(slug):
    t = bget(f"{ANT}/{slug}/videos")
    m = re.search(r"<title>\s*ANT1 TV\s*/\s*(.*?)\s*/\s*Videos", t, re.S)
    title = clean(m.group(1)) if m else slug
    links = re.findall(r'href="/watch/([a-z0-9]+)/([^"]+)"', t)
    sid = re.search(r"currentShowId\s*=\s*'([^']+)'", t)
    if sid:
        try:
            links += re.findall(r'href="/watch/([a-z0-9]+)/([^"]+)"', bget(f"{ANT}/templates/data/morevideos.aspx?showid={sid.group(1)}"))
        except Exception:  # noqa: BLE001
            pass
    items, seen = [], set()
    for cid, s in links:
        if cid in seen or _ant_skip.search(s):
            continue
        seen.add(cid)
        items.append((ep_number(s.replace("-", " ")), ant_label(s), "ant1:" + cid))
    items.reverse()
    items.sort(key=lambda x: (x[0] is None, x[0] or 0))
    return {"t": title.title() if title.isupper() else title, "g": "Εκπομπές",
            "d": "", "e": [{"l": x[1], "u": x[2]} for x in items]}


def build_ant1():
    watch = set()
    for path in ("/", "/webtv"):
        try:
            watch.update(re.findall(r'href="(/watch/[a-z0-9]+/[^"]+)"', bget(ANT + path)))
        except Exception as e:  # noqa: BLE001
            print(f"    ! {path}: {e}")
    slugs = set()
    for t in pmap(lambda w: bget(ANT + w), sorted(watch)[:150]):
        for sl in re.findall(r'href="/([a-z0-9\-]+)/videos"', t or ""):
            if sl not in ("lifestyle-showbiz",):
                slugs.add(sl)
    print(f"  ANT1: {len(slugs)} εκπομπές")
    return finish("ant1", "ANT1", pmap(ant_show, sorted(slugs)))


# ============================== ΡΑΔΙΟΦΩΝΟ ==============================
# Ελληνικοί σταθμοί που εκπέμπουν στο internet (radio-browser.info, ανοιχτή βάση).
RADIO_FILE = os.path.join(CATALOG, "radio.m3u")
RADIO_API = ["https://de1.api.radio-browser.info", "https://de2.api.radio-browser.info",
             "https://fi1.api.radio-browser.info", "https://all.api.radio-browser.info"]


AUDIO_TYPES = ("audio/", "application/ogg", "video/mp2t", "application/octet-stream", "video/mp4")


def _open(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": BUA, "Icy-MetaData": "0"})
    return urllib.request.urlopen(req, timeout=timeout)


def check_stream(url, depth=0):
    """Δοκιμάζει έναν σταθμό. Επιστρέφει το URL που παίζει (ίσως το πραγματικό stream
    μέσα από .pls/.m3u) ή None αν δεν στέλνει ήχο."""
    if depth > 2:
        return None
    with _open(url) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        first = r.read(4096)
        if ctype.startswith(AUDIO_TYPES) or first[:3] == b"ID3" or first[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or first[:4] == b"OggS":
            more = first + r.read(12288)          # πρέπει να ρέει ήχος, όχι μια σελίδα σφάλματος
            if len(more) >= 8192 and not more.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
                return url
            return None
        text = first.decode("utf-8", "replace")
    final = r.geturl()
    if "#EXTM3U" in text and "#EXT-X-" in text:     # HLS: δοκιμάζω και το πρώτο κομμάτι
        sub = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        if not sub:
            return None
        nxt = urllib.parse.urljoin(final, sub[0])
        if "#EXT-X-STREAM-INF" in text:
            return url if check_stream(nxt, depth + 1) else None
        with _open(nxt) as r2:
            return url if len(r2.read(2048)) >= 1024 else None
    # .pls / απλό .m3u: παίρνω το πρώτο πραγματικό stream
    m = re.search(r"(?im)^(?:File\d+=)?\s*(https?://\S+)", text)
    if m and ("[playlist]" in text.lower() or "mpegurl" in ctype or "scpls" in ctype or text.lstrip().startswith("#EXTM3U")):
        return check_stream(m.group(1).strip(), depth + 1)
    return None


def build_radio():
    data = None
    for base in RADIO_API:
        try:
            data = json.loads(get(f"{base}/json/stations/bycountrycodeexact/GR?hidebroken=true"
                                  f"&order=clickcount&reverse=true&limit=500", "application/json")[0])
            break
        except Exception as e:  # noqa: BLE001
            print(f"  ! ραδιόφωνο {base}: {e}")
    if not data:
        print("  ! ραδιόφωνο: κρατάω την παλιά λίστα")
        return
    seen, cands = set(), []
    for st in data:
        name = clean(st.get("name") or "")
        # Το κόμμα χαλάει το όνομα στο M3U: «95,1» -> «95.1», αλλιώς «, » -> « - »
        name = re.sub(r"(\d),(\d)", r"\1.\2", name).replace(",", " -").strip(" -")
        url = (st.get("url_resolved") or st.get("url") or "").strip()
        key = re.sub(r"[^a-z0-9α-ω]", "", name.lower())
        if not name or not url or key in seen:
            continue
        seen.add(key)
        cands.append((name, url, (st.get("favicon") or "").replace('"', "")))

    # Έλεγχος ένας-ένας (παράλληλα): μένουν μόνο όσοι στέλνουν πραγματικά ήχο
    def test(c):
        for i in range(2):
            try:
                ok = check_stream(c[1])
                if ok:
                    return ok
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        return None
    with cf.ThreadPoolExecutor(32) as ex:
        res = list(ex.map(test, cands))
    lines, kept, dead = ["#EXTM3U"], 0, []
    for (name, _, logo), ok in zip(cands, res):
        if not ok:
            dead.append(name)
            continue
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Ραδιόφωνο",{name}')
        lines.append(ok)
        kept += 1
        if kept >= 200:
            break
    print(f"  ραδιόφωνο: {len(cands)} δοκιμάστηκαν, {kept} παίζουν, {len(dead)} δεν δουλεύουν")
    if dead:
        print("    εκτός: " + ", ".join(dead[:80]))
    if kept < 20:
        print(f"  ! ραδιόφωνο: μόνο {kept} σταθμοί - κρατάω την παλιά λίστα")
        return
    os.makedirs(CATALOG, exist_ok=True)
    with open(RADIO_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  ✓ radio.m3u: {kept} σταθμοί", flush=True)


# ============================== ΡΩΣΙΚΕΣ ΣΕΙΡΕΣ (YouTube) ==============================
# Επίσημα κανάλια YouTube ρωσικών στούντιο, που ανεβάζουν νόμιμα και δωρεάν ολόκληρες σειρές.
# Η λίστα των καναλιών είναι στο youtube_ru.txt (μία γραμμή: Όνομα | https://www.youtube.com/@handle).
# Κάθε playlist του καναλιού = μία σειρά, κάθε βίντεο = ένα επεισόδιο.

YT_LIST = "youtube_ru.txt"
YT_MAX_PLAYLISTS = 80      # ανά κανάλι
YT_MIN_EPISODES = 2        # playlist με 1 βίντεο = ταινία/τρέιλερ, δεν είναι σειρά


def _ytdlp():
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        import importlib
        import site
        import subprocess
        pip = [sys.executable, "-m", "pip", "install", "--quiet", "yt-dlp"]
        if subprocess.run(pip).returncode != 0:
            subprocess.run(pip + ["--user"], check=True)
            sys.path.append(site.getusersitepackages())
        importlib.invalidate_caches()
    import yt_dlp
    return yt_dlp


def yt_channels():
    path = os.path.join(ROOT, YT_LIST)
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                name, url = [x.strip() for x in line.split("|", 1)]
                out.append((name, url.rstrip("/")))
    return out


def build_youtube_ru():
    yt_dlp = _ytdlp()
    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True,
            "ignoreerrors": True, "skip_download": True}
    shows = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for name, url in yt_channels():
            try:
                info = ydl.extract_info(url + "/playlists", download=False) or {}
                lists = [e for e in (info.get("entries") or []) if e and e.get("id")][:YT_MAX_PLAYLISTS]
                print(f"  {name}: {len(lists)} playlists")
            except Exception as e:  # noqa: BLE001
                print(f"  ! {name}: {e}")
                continue
            n = 0
            for pl in lists:
                try:
                    pinfo = ydl.extract_info(f"https://www.youtube.com/playlist?list={pl['id']}", download=False) or {}
                except Exception as e:  # noqa: BLE001
                    print(f"    ! {pl.get('title')}: {e}")
                    continue
                eps = [v for v in (pinfo.get("entries") or [])
                       if v and v.get("id") and (v.get("title") or "") not in ("[Private video]", "[Deleted video]")]
                if len(eps) < YT_MIN_EPISODES:
                    continue
                shows.append({
                    "t": clean(pinfo.get("title") or pl.get("title") or "?"),
                    "g": name,
                    "d": "",
                    "e": [{"l": clean(v.get("title") or f"Серия {i + 1}"),
                           "u": f"https://m.youtube.com/watch?v={v['id']}"} for i, v in enumerate(eps)],
                })
                n += 1
            print(f"  ✓ {name}: {n} σειρές")
    shows.sort(key=lambda s: s["t"].lower())
    return {"id": "ru_youtube", "name": "Ρωσικές σειρές", "lang": "ru", "updated": now(),
            "genres": sorted({s["g"] for s in shows}), "shows": shows}


# ============================== ΣΕΙΡΕΣ ==============================

# Κάθε κανάλι: (id, όνομα, συνάρτηση, γλώσσα). Νέα κανάλια προστίθενται εδώ.
# Γλώσσα "gr" = εμφανίζεται και στις δύο εφαρμογές, "ru" = μόνο στην IPTV GR-RU.
CHANNELS = [
    ("ert", "Αρχείο ΕΡΤ", build_ert, "gr"),
    ("skai", "ΣΚΑΪ", build_skai, "gr"),
    ("mega", "Mega", build_mega, "gr"),
    ("ant1", "ANT1", build_ant1, "gr"),
    ("ru_youtube", "Ρωσικές σειρές", build_youtube_ru, "ru"),
]


def build_series():
    os.makedirs(CATALOG, exist_ok=True)
    try:
        build_radio()
    except Exception as e:  # noqa: BLE001
        print(f"  ! ραδιόφωνο: {e}")
    for cid, name, fn, _lang in CHANNELS:
        print(f"→ {name}")
        try:
            data = fn()
            write_if_ok(os.path.join(CATALOG, f"{cid}.json"), data, len(data["shows"]))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {name}: {e} - κρατάω το παλιό")

    # index.json: ό,τι αρχεία καναλιών υπάρχουν (και παλιά που δεν ανανεώθηκαν)
    index = []
    for cid, name, _, lang in CHANNELS:
        path = os.path.join(CATALOG, f"{cid}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            index.append({"id": cid, "name": name, "file": f"{cid}.json", "lang": lang,
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
