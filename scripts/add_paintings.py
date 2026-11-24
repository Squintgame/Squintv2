"""
Squint: add a batch of painting puzzles automatically.

Runs inside a GitHub Action (see .github/workflows/add-paintings.yml). For each new puzzle it:
  1. finds famous paintings on Wikidata whose artist died before 1956,
  2. checks on Wikimedia Commons that the image is marked public domain and is large enough,
  3. downloads it, picks a starting zoom spot (detail-rich, away from the center and from faces),
  4. writes a puzzle folder in exactly the crop picker's format into art/,
  5. adds new paintings to paintings.json and appends the puzzles to art/schedule.json.

It also writes private files (record files and a review page) to build/private/, which the
workflow offers as a download instead of committing, because they contain the answers.
"""

import argparse, base64, hashlib, html, io, json, math, os, re, secrets, sys, time, unicodedata
from pathlib import Path

import numpy as np
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ART_DIR = ROOT / "art"
CATALOG = ROOT / "paintings.json"
PRIVATE = ROOT / "build" / "private"

USER_AGENT = "SquintPuzzleBuilder/1.0 (https://playsquint.com; daily art puzzle)"
WIKIDATA = "https://query.wikidata.org/sparql"
COMMONS = "https://commons.wikimedia.org/w/api.php"

LEVEL_SCALES = [9, 6, 4, 2.6, 1.7, 1.2]   # same as the crop picker's defaults
OUTPUT_SIZE = 800                          # pixels per square image, like the crop picker
DOWNLOAD_WIDTH = 4000                      # request this width from Commons (or the original if smaller)
MIN_SHORT_SIDE = 1800                      # skip images smaller than this, so the first zoom stays sharp
DIED_BEFORE = 1956                         # artist died 70+ years ago
REVEAL_BACKGROUND = (232, 228, 220)        # neutral "gallery wall" around the full painting

GENERIC_TITLE = re.compile(r"^(self-portrait|self portrait|portrait of a (man|woman|young|lady|girl|boy|gentleman)|portrait of an|still life|still-life|landscape|untitled|study|sketch|head of|bust of|figure|composition|painting)\b", re.I)

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


# ----------------------------------------------------------------- helpers
def fold(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"^the ", "", s)


def obscure(text):
    """Same scheme as the crop picker: reversed base64 of the UTF-8 text."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")[::-1]


def unobscure(s):
    return base64.b64decode(s[::-1]).decode("utf-8")


def get_json(url, params, tries=4):
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == tries - 1:
                raise
            wait = 5 * (attempt + 1)
            print(f"  retrying in {wait}s ({e})")
            time.sleep(wait)


def download(url, tries=4):
    for attempt in range(tries):
        try:
            r = session.get(url, timeout=180)
            r.raise_for_status()
            return r.content
        except Exception as e:
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ----------------------------------------------------------------- 1. candidates from Wikidata
SPARQL = """
SELECT ?item (SAMPLE(?label) AS ?title) (SAMPLE(?creatorLabel) AS ?artist) (SAMPLE(?img) AS ?image)
       (MAX(?sl) AS ?sitelinks) (MIN(YEAR(?inception)) AS ?year) (SAMPLE(?collLabel) AS ?collection)
WHERE {
  ?item wdt:P31 wd:Q3305213; wdt:P18 ?img; wdt:P170 ?creator; wikibase:sitelinks ?sl.
  FILTER(?sl >= %(min_languages)d)
  ?creator wdt:P570 ?death. FILTER(YEAR(?death) < %(died_before)d)
  ?item rdfs:label ?label. FILTER(LANG(?label) = "en")
  ?creator rdfs:label ?creatorLabel. FILTER(LANG(?creatorLabel) = "en")
  OPTIONAL { ?item wdt:P571 ?inception. }
  OPTIONAL { ?item wdt:P195 ?coll. ?coll rdfs:label ?collLabel. FILTER(LANG(?collLabel) = "en") }
}
GROUP BY ?item
ORDER BY DESC(?sitelinks)
LIMIT %(limit)d
"""


def fetch_candidates(min_languages, limit=2500):
    print(f"Asking Wikidata for famous paintings (artist died before {DIED_BEFORE}, "
          f"Wikipedia articles in at least {min_languages} languages)...")
    data = get_json(WIKIDATA, {"query": SPARQL % {"min_languages": min_languages, "died_before": DIED_BEFORE, "limit": limit},
                               "format": "json"})
    out = []
    for b in data["results"]["bindings"]:
        title = b.get("title", {}).get("value", "").strip()
        artist = b.get("artist", {}).get("value", "").strip()
        image = b.get("image", {}).get("value", "")
        if not title or not artist or not image:
            continue
        year = b.get("year", {}).get("value")
        out.append({
            "qid": b["item"]["value"].rsplit("/", 1)[-1],
            "title": title, "artist": artist,
            "year": str(int(float(year))) if year else "",
            "file": requests.utils.unquote(image.rsplit("/", 1)[-1]).replace("_", " "),
            "languages": int(b["sitelinks"]["value"]),
            "collection": b.get("collection", {}).get("value", "").strip(),
        })
    print(f"  {len(out)} candidates")
    return out


def plausible(c):
    t, a = c["title"], c["artist"]
    if re.fullmatch(r"Q\d+", t) or re.fullmatch(r"Q\d+", a):
        return False                          # no English name on Wikidata
    if GENERIC_TITLE.match(t):
        return False                          # "Self-Portrait", "Still Life"... too vague to guess
    if re.search(r"anonymous|unknown|workshop of|circle of|follower of|school of", a, re.I):
        return False
    if len(t) > 90:
        return False
    return True


# ----------------------------------------------------------------- 2. license and size on Commons
PD_LICENSE = re.compile(r"public domain|^pd\b|pd-|cc0|cc-zero|no restrictions", re.I)


def commons_info(files):
    """Returns {file name: info} for up to 50 files at a time."""
    info = {}
    for i in range(0, len(files), 50):
        chunk = files[i:i + 50]
        data = get_json(COMMONS, {
            "action": "query", "format": "json", "prop": "imageinfo",
            "titles": "|".join("File:" + f for f in chunk),
            "iiprop": "url|size|extmetadata|mime", "iiurlwidth": DOWNLOAD_WIDTH,
        })
        norm = {n["to"]: n["from"] for n in data.get("query", {}).get("normalized", [])}
        for page in data.get("query", {}).get("pages", {}).values():
            if "imageinfo" not in page:
                continue
            ii = page["imageinfo"][0]
            name = norm.get(page["title"], page["title"])[len("File:"):]
            meta = ii.get("extmetadata", {})
            info[name] = {
                "width": ii.get("width", 0), "height": ii.get("height", 0), "mime": ii.get("mime", ""),
                "download": ii.get("thumburl") or ii.get("url"),
                "page": ii.get("descriptionurl", ""),
                "license": strip_tags(meta.get("LicenseShortName", {}).get("value", "")),
                "copyrighted": meta.get("Copyrighted", {}).get("value", ""),
                "credit": strip_tags(meta.get("Credit", {}).get("value", "")),
            }
        time.sleep(0.5)
    return info


def license_ok(inf):
    if inf.get("copyrighted", "").lower() == "false":
        return True
    return bool(PD_LICENSE.search(inf.get("license", "")))


# ----------------------------------------------------------------- 3. choosing the zoom
_face_detector = None


def detect_faces(gray_uint8):
    global _face_detector
    try:
        import cv2
        if _face_detector is None:
            _face_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = _face_detector.detectMultiScale(gray_uint8, scaleFactor=1.1, minNeighbors=5, minSize=(18, 18))
        return [tuple(map(int, f)) for f in faces]
    except Exception as e:
        print(f"  (face detection unavailable: {e})")
        return []


def box_mean(a, x0, y0, w):
    return float(a[y0:y0 + w, x0:x0 + w].mean())


def choose_focus(img, seed):
    """Pick the level-1 centre: lots of detail and colour, away from faces and from the very centre."""
    W, H = img.size
    k = 600 / max(W, H)
    small = img.resize((max(1, round(W * k)), max(1, round(H * k))), Image.LANCZOS)
    w, h = small.size
    rgb = np.asarray(small, dtype=np.float32)
    gray = rgb.mean(axis=2)
    gy, gx = np.gradient(gray)
    detail = np.hypot(gx, gy)
    face_mask = np.zeros((h, w), dtype=np.float32)
    for (fx, fy, fw, fh) in detect_faces(gray.astype(np.uint8)):
        px, py = int(fw * 0.4), int(fh * 0.4)
        face_mask[max(0, fy - py):fy + fh + py, max(0, fx - px):fx + fw + px] = 1

    win = max(8, int(round(min(w, h) / LEVEL_SCALES[0])))
    margin = int(round(min(w, h) / LEVEL_SCALES[1] / 2))          # leave room for level 2 around it
    step = max(2, win // 2)
    spots = []
    for y0 in range(margin, h - margin - win + 1, step):
        for x0 in range(margin, w - margin - win + 1, step):
            patch = rgb[y0:y0 + win, x0:x0 + win]
            d = box_mean(detail, x0, y0, win)
            colour = float(patch.reshape(-1, 3).std(axis=0).mean())
            face = box_mean(face_mask, x0, y0, win)
            cx, cy = (x0 + win / 2) / w, (y0 + win / 2) / h
            off_centre = math.hypot(cx - 0.5, cy - 0.5) / math.hypot(0.5, 0.5)
            spots.append((d, colour, face, off_centre, cx, cy))
    if not spots:
        return W / 2, H / 2
    ds = np.array([s[0] for s in spots]); cs = np.array([s[1] for s in spots])
    dn = (ds - ds.min()) / (np.ptp(ds) or 1); cn = (cs - cs.min()) / (np.ptp(cs) or 1)
    scored = []
    for (d, colour, face, off, cx, cy), a, b in zip(spots, dn, cn):
        score = 0.6 * a + 0.4 * b - 2.5 * face - 0.8 * max(0.0, 0.3 - off) / 0.3
        if a < 0.15:
            score -= 1.0                                              # nearly flat: tells the player nothing
        scored.append((score, cx, cy))
    scored.sort(reverse=True)
    best = scored[:5]
    pick = best[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(best)]
    return pick[1] * W, pick[2] * H


def nested_boxes(W, H, fx, fy):
    """Six nested square boxes (smallest first) around the focus, each inside the next and inside the image."""
    S = min(W, H)
    boxes = [None] * 6
    parent = (0.0, 0.0, float(W), float(H))
    for i in range(5, -1, -1):
        s = S / LEVEL_SCALES[i]
        px, py, pw, ph = parent
        x = min(max(fx - s / 2, px), px + pw - s)
        y = min(max(fy - s / 2, py), py + ph - s)
        boxes[i] = (round(x), round(y), round(s))
        parent = (x, y, s, s)
    return boxes


# ----------------------------------------------------------------- 4. building one puzzle
def build_puzzle(img, folder_name, entry, inf, seed):
    W, H = img.size
    fx, fy = choose_focus(img, seed)
    boxes = nested_boxes(W, H, fx, fy)
    folder = ART_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    levels = []
    for (x, y, s) in boxes:
        name = secrets.token_hex(6) + ".webp"
        img.crop((x, y, x + s, y + s)).resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS) \
           .save(folder / name, "WEBP", quality=80, method=6)
        levels.append({"file": name, "rect": {"x": x, "y": y, "size": s}})
    # The reveal shows the whole painting on a neutral square, so nothing is cropped off.
    L = max(W, H)
    canvas = Image.new("RGB", (L, L), REVEAL_BACKGROUND)
    canvas.paste(img, ((L - W) // 2, (L - H) // 2))
    name = secrets.token_hex(6) + ".webp"
    canvas.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS).save(folder / name, "WEBP", quality=82, method=6)
    levels.append({"file": name, "rect": {"x": -((L - W) // 2), "y": -((L - H) // 2), "size": L}})

    credit_name = entry["collection"] or "Wikimedia Commons"
    game = {
        "a": obscure(entry["title"]), "ar": obscure(entry["artist"]),
        "hint": "", "category": "",
        "source": inf["page"], "photographer": credit_name, "creditPrefix": "Image from",
        "levels": levels,
    }
    (folder / "game.json").write_text(json.dumps(game, indent=2, ensure_ascii=False), encoding="utf-8")
    record = {
        "id": folder_name.replace("squint-art-", ""), "type": "art",
        "answer": entry["title"], "artist": entry["artist"], "year": entry["year"],
        "hint": "", "category": "", "accept": [],
        "source": inf["page"], "photographer": credit_name, "license": inf["license"],
        "image_url": inf["download"], "wikidata": entry["qid"], "languages": entry["languages"],
        "photo": {"name": entry["file"], "width": W, "height": H},
        "outputSize": OUTPUT_SIZE,
        "levels": [{"level": i + 1 if i < 6 else "full", "file": l["file"], "rect": l["rect"]} for i, l in enumerate(levels)],
    }
    return record, levels


# ----------------------------------------------------------------- 5. schedule order
def order_for_schedule(items, recent_artists):
    """Mix easier and harder paintings and keep the same artist at least a week apart."""
    items = sorted(items, key=lambda r: -r["languages"])
    n = len(items)
    tiers = {"easy": items[:round(n * 0.35)], "medium": items[round(n * 0.35):round(n * 0.75)], "hard": items[round(n * 0.75):]}
    pattern = ["easy", "medium", "easy", "medium", "hard", "medium", "easy"]
    recent = list(recent_artists)[-7:]
    out = []
    while any(tiers.values()):
        want = pattern[len(out) % len(pattern)]
        order = [want] + [t for t in ("easy", "medium", "hard") if t != want]
        chosen = None
        for strict in (True, False):
            for t in order:
                for r in tiers[t]:
                    if not strict or r["artist"] not in recent:
                        chosen = (t, r); break
                if chosen: break
            if chosen: break
        tiers[chosen[0]].remove(chosen[1])
        out.append(chosen[1])
        recent = (recent + [chosen[1]["artist"]])[-7:]
    return out


# ----------------------------------------------------------------- 6. review page
def review_page(records, thumbs):
    rows = []
    for r in records:
        imgs = "".join(f'<img src="data:image/jpeg;base64,{t}" alt="">' for t in thumbs[r["folder"]])
        rows.append(f"""<section><h2>#{r['scheduleNumber']} · {html.escape(r['answer'])} <small>by {html.escape(r['artist'])}{(' (' + r['year'] + ')') if r['year'] else ''}</small></h2>
<p>Folder <code>art/{r['folder']}</code> · Wikipedia in {r['languages']} languages · License: {html.escape(r['license'])} ·
<a href="{html.escape(r['source'])}" target="_blank" rel="noopener">image page</a>{' · <b>new to the catalog</b>' if r.get('addedToCatalog') else ''}</p>
<div class="levels">{imgs}</div></section>""")
    return f"""<!DOCTYPE html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Squint paintings review</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:24px;background:#f7f6f2;color:#1c1d1f}}
section{{background:#fff;border:1px solid #deddd5;border-radius:14px;padding:14px 16px;margin:14px 0}}
h2{{margin:0 0 4px;font-size:1.1rem}} small{{color:#6b6c70;font-weight:500}} p{{margin:0 0 10px;color:#6b6c70;font-size:.9rem}}
.levels{{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}} .levels img{{width:100%;border-radius:6px;border:1px solid #ddd}}
@media(max-width:700px){{.levels{{grid-template-columns:repeat(4,1fr)}}}}</style>
<h1>{len(records)} new painting puzzles</h1>
<p>Each row shows the seven looks, from the first zoom to the full reveal. To fix one, download the image from its
"image page" at the size listed in its record file, open it in the crop picker, and use "Load a record file".</p>
{''.join(rows)}</html>"""


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--max-per-artist", type=int, default=8)
    ap.add_argument("--min-languages", type=int, default=12)
    args = ap.parse_args()

    ART_DIR.mkdir(exist_ok=True)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    (PRIVATE / "records").mkdir(exist_ok=True)
    schedule_path = ART_DIR / "schedule.json"
    schedule = json.loads(schedule_path.read_text(encoding="utf-8")) if schedule_path.exists() else []
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    paintings = catalog["paintings"]

    # What's already used, so nothing repeats.
    used, recent_artists, per_artist = set(), [], {}
    numbers = [0]
    for folder in sorted(ART_DIR.glob("*/game.json")):
        try:
            g = json.loads(folder.read_text(encoding="utf-8"))
            t, a = unobscure(g["a"]), unobscure(g["ar"]) if g.get("ar") else ""
            used.add((fold(t), fold(a)))
            per_artist[a] = per_artist.get(a, 0) + 1
        except Exception:
            pass
        m = re.search(r"squint-art-(\d+)$", folder.parent.name)
        if m: numbers.append(int(m.group(1)))
    for name in schedule[-7:]:
        p = ART_DIR / name / "game.json"
        if p.exists():
            g = json.loads(p.read_text(encoding="utf-8"))
            if g.get("ar"): recent_artists.append(unobscure(g["ar"]))
    next_number = max(numbers) + 1

    def in_catalog(title, artist):
        k, ak = fold(title), fold(artist)
        for p in paintings:
            if fold(p["artist"]) == ak and (fold(p["title"]) == k or any(fold(x) == k for x in p.get("aliases", []))):
                return p
        return None

    # Candidates: famous first, one per painting, nothing already used, no vague titles.
    seen, candidates = set(), []
    for c in fetch_candidates(args.min_languages):
        key = (fold(c["title"]), fold(c["artist"]))
        if key in seen or key in used or not plausible(c):
            continue
        seen.add(key)
        known = in_catalog(c["title"], c["artist"])
        if known:                                   # use the catalog's exact spelling
            c["title"], c["artist"] = known["title"], known["artist"]
            c["year"] = known.get("year") or c["year"]
        c["inCatalog"] = bool(known)
        candidates.append(c)
    print(f"{len(candidates)} usable candidates after removing duplicates, used and vague titles")

    built, thumbs, skipped = [], {}, []
    batch = 0
    while len(built) < args.count and batch < len(candidates):
        chunk = candidates[batch:batch + 40]; batch += 40
        info = commons_info([c["file"] for c in chunk])
        for c in chunk:
            if len(built) >= args.count:
                break
            if per_artist.get(c["artist"], 0) >= args.max_per_artist:
                continue
            inf = info.get(c["file"])
            why = None
            if not inf:
                why = "image not found on Commons"
            elif not license_ok(inf):
                why = f"license not public domain ({inf.get('license') or 'unknown'})"
            elif min(inf["width"], inf["height"]) < MIN_SHORT_SIDE:
                why = f"image too small ({inf['width']}x{inf['height']})"
            elif not inf["mime"].startswith("image/"):
                why = "not an image"
            if why:
                skipped.append(f"{c['title']} ({c['artist']}): {why}"); continue
            try:
                img = Image.open(io.BytesIO(download(inf["download"]))).convert("RGB")
            except Exception as e:
                skipped.append(f"{c['title']} ({c['artist']}): download failed ({e})"); continue
            if min(img.size) < MIN_SHORT_SIDE * 0.9:
                skipped.append(f"{c['title']} ({c['artist']}): downloaded image too small {img.size}"); continue
            folder_name = f"squint-art-{next_number}"; next_number += 1
            record, levels = build_puzzle(img, folder_name, c, inf, seed=c["qid"])
            record.update({"folder": folder_name, "languages": c["languages"], "addedToCatalog": not c["inCatalog"]})
            thumbs[folder_name] = []
            for l in levels:
                t = Image.open(ART_DIR / folder_name / l["file"]).convert("RGB").resize((160, 160))
                buf = io.BytesIO(); t.save(buf, "JPEG", quality=70); thumbs[folder_name].append(base64.b64encode(buf.getvalue()).decode())
            built.append(record)
            per_artist[c["artist"]] = per_artist.get(c["artist"], 0) + 1
            if not c["inCatalog"]:
                paintings.append({"title": c["title"], "artist": c["artist"], "year": c["year"], "aliases": []})
            print(f"  [{len(built)}/{args.count}] {c['title']} by {c['artist']}")
            time.sleep(0.3)

    if not built:
        print("No puzzles were built."); sys.exit(1)

    ordered = order_for_schedule(built, recent_artists)
    for i, r in enumerate(ordered):
        schedule.append(r["folder"])
        r["scheduleNumber"] = len(schedule)
    schedule_path.write_text(json.dumps(schedule, indent=2), encoding="utf-8")
    CATALOG.write_text(json.dumps(catalog, indent=1, ensure_ascii=False), encoding="utf-8")

    for r in ordered:
        rec = {k: v for k, v in r.items() if k not in ("folder", "scheduleNumber", "addedToCatalog")}
        (PRIVATE / "records" / f"{r['folder']}-record.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    (PRIVATE / "review.html").write_text(review_page(ordered, thumbs), encoding="utf-8")
    (PRIVATE / "skipped.txt").write_text("\n".join(skipped) + "\n", encoding="utf-8")
    added = sum(1 for r in ordered if r["addedToCatalog"])
    summary = f"Built {len(ordered)} puzzles ({added} paintings new to the catalog). Skipped {len(skipped)} candidates."
    (PRIVATE / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
