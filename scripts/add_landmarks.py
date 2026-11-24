"""
Squint: add a batch of landmark puzzles automatically.

Runs inside a GitHub Action (see .github/workflows/add-landmarks.yml). Unlike paintings, landmarks come
only from the catalog (landmarks.json), so every one is a well-known place. For each unused landmark it:
  1. finds the matching Wikidata entry (checking the country matches the catalog),
  2. checks on Wikimedia Commons that its photo has an allowed license and is large enough,
  3. downloads it, picks a starting zoom spot and writes a puzzle folder into landmarks/,
  4. appends the puzzles to landmarks/schedule.json, keeping the same country a week apart.

Landmark photos are usually CC BY-SA rather than public domain. Those may be used with credit, so each
puzzle's credit line names the photographer and license. Use --public-domain-only to avoid them.
Record files and a review page go to build/private/ (a private download), since they contain the answers.
"""

import argparse, base64, io, json, re, secrets, sys, time
from pathlib import Path

from PIL import Image

import add_paintings as common          # shared helpers: Wikidata/Commons access, zoom choice, scheduling

ROOT = common.ROOT
LM_DIR = ROOT / "landmarks"
CATALOG = ROOT / "landmarks.json"
PRIVATE = ROOT / "build" / "private"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Wikidata's names for some countries differ from the catalog's.
COUNTRY_ALIASES = {
    "united states of america": "united states", "people's republic of china": "china",
    "kingdom of the netherlands": "netherlands", "czech republic": "czechia", "holy see": "vatican city",
    "state of palestine": "palestine", "republic of ireland": "ireland", "türkiye": "turkey",
    "russian federation": "russia", "republic of china": "taiwan", "korea, south": "south korea",
}
OPEN_LICENSE = re.compile(r"^cc[ -]by(-sa)?[ -]?\d", re.I)       # CC BY or CC BY-SA, any version


def norm_country(c):
    c = c.strip().lower()
    return COUNTRY_ALIASES.get(c, c)


def search_wikidata(name):
    data = common.get_json(WIKIDATA_API, {"action": "wbsearchentities", "search": name, "language": "en",
                                           "type": "item", "limit": 7, "format": "json"})
    return [r["id"] for r in data.get("search", [])]


def describe(qids):
    """Image, countries and popularity for a list of Wikidata items."""
    out = {}
    for i in range(0, len(qids), 120):
        chunk = qids[i:i + 120]
        query = """
SELECT ?item (SAMPLE(?img) AS ?image) (MAX(?sl) AS ?sitelinks) (GROUP_CONCAT(DISTINCT ?cl; separator="|") AS ?countries)
WHERE {
  VALUES ?item { %s }
  ?item wikibase:sitelinks ?sl.
  OPTIONAL { ?item wdt:P18 ?img. }
  OPTIONAL { ?item wdt:P17 ?c. ?c rdfs:label ?cl. FILTER(LANG(?cl) = "en") }
} GROUP BY ?item""" % " ".join("wd:" + q for q in chunk)
        data = common.get_json(common.WIKIDATA, {"query": query, "format": "json"})
        for b in data["results"]["bindings"]:
            q = b["item"]["value"].rsplit("/", 1)[-1]
            img = b.get("image", {}).get("value", "")
            out[q] = {
                "file": common.requests.utils.unquote(img.rsplit("/", 1)[-1]).replace("_", " ") if img else "",
                "languages": int(b["sitelinks"]["value"]),
                "countries": [c for c in b.get("countries", {}).get("value", "").split("|") if c],
            }
        time.sleep(0.5)
    return out


def license_allowed(inf, public_domain_only):
    if common.license_ok(inf):
        return True, "public domain"
    lic = inf.get("license", "")
    if public_domain_only:
        return False, f"not public domain ({lic or 'unknown'})"
    if OPEN_LICENSE.match(lic) and not re.search(r"\bnc\b|\bnd\b|non-?commercial|no-?deriv", lic, re.I):
        return True, lic
    return False, f"license not allowed ({lic or 'unknown'})"


def credit_for(inf, kind):
    author = re.sub(r"\s+", " ", inf.get("author", "")).strip()
    if len(author) > 60 or not author or re.search(r"unknown", author, re.I):
        author = "Wikimedia Commons contributor" if kind != "public domain" else "Wikimedia Commons"
    return author if kind == "public domain" else f"{author} ({kind})"


def build_puzzle(img, folder_name, item, inf, kind, seed):
    W, H = img.size
    fx, fy = common.choose_focus(img, seed)
    boxes = common.nested_boxes(W, H, fx, fy)
    folder = LM_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    levels = []
    for (x, y, s) in boxes:
        name = secrets.token_hex(6) + ".webp"
        img.crop((x, y, x + s, y + s)).resize((common.OUTPUT_SIZE, common.OUTPUT_SIZE), Image.LANCZOS) \
           .save(folder / name, "WEBP", quality=80, method=6)
        levels.append({"file": name, "rect": {"x": x, "y": y, "size": s}})
    L = max(W, H)                         # the reveal shows the whole photo, nothing cropped off
    canvas = Image.new("RGB", (L, L), common.REVEAL_BACKGROUND)
    canvas.paste(img, ((L - W) // 2, (L - H) // 2))
    name = secrets.token_hex(6) + ".webp"
    canvas.resize((common.OUTPUT_SIZE, common.OUTPUT_SIZE), Image.LANCZOS).save(folder / name, "WEBP", quality=82, method=6)
    levels.append({"file": name, "rect": {"x": -((L - W) // 2), "y": -((L - H) // 2), "size": L}})

    credit = credit_for(inf, kind)
    game = {"a": common.obscure(item["name"]), "ar": common.obscure(item["countries"][0]),
            "hint": "", "category": "", "source": inf["page"], "photographer": credit, "creditPrefix": "Photo by",
            "levels": levels}
    (folder / "game.json").write_text(json.dumps(game, indent=2, ensure_ascii=False), encoding="utf-8")
    record = {"id": folder_name.replace("squint-landmark-", ""), "type": "landmarks",
              "answer": item["name"], "artist": item["countries"][0], "countries": item["countries"], "city": item.get("city", ""),
              "hint": "", "category": "", "accept": [], "source": inf["page"], "photographer": credit,
              "license": inf.get("license", ""), "licenseUrl": inf.get("licenseUrl", ""), "image_url": inf["download"],
              "photo": {"name": inf["file"], "width": W, "height": H}, "outputSize": common.OUTPUT_SIZE,
              "levels": [{"level": i + 1 if i < 6 else "full", "file": l["file"], "rect": l["rect"]} for i, l in enumerate(levels)]}
    return record, levels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--max-per-country", type=int, default=12)
    ap.add_argument("--public-domain-only", action="store_true")
    args = ap.parse_args()

    LM_DIR.mkdir(exist_ok=True)
    (PRIVATE / "records").mkdir(parents=True, exist_ok=True)
    schedule_path = LM_DIR / "schedule.json"
    schedule = json.loads(schedule_path.read_text(encoding="utf-8")) if schedule_path.exists() else []
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))["landmarks"]

    used, per_country, numbers, recent = set(), {}, [0], []
    for gpath in sorted(LM_DIR.glob("*/game.json")):
        try:
            g = json.loads(gpath.read_text(encoding="utf-8"))
            used.add(common.fold(common.unobscure(g["a"])))
            c = common.unobscure(g["ar"]) if g.get("ar") else ""
            per_country[c] = per_country.get(c, 0) + 1
        except Exception:
            pass
        m = re.search(r"squint-landmark-(\d+)$", gpath.parent.name)
        if m: numbers.append(int(m.group(1)))
    for name in schedule[-7:]:
        gp = LM_DIR / name / "game.json"
        if gp.exists():
            g = json.loads(gp.read_text(encoding="utf-8"))
            if g.get("ar"): recent.append(common.unobscure(g["ar"]))
    next_number = max(numbers) + 1

    todo = [i for i in catalog if common.fold(i["name"]) not in used]
    print(f"{len(todo)} catalog landmarks not used yet. Looking them up on Wikidata...")

    # 1. find each landmark's Wikidata entry, checking the country matches
    searches = {}
    for i, item in enumerate(todo):
        try:
            searches[item["name"]] = search_wikidata(item["name"])
        except Exception as e:
            print(f"  search failed for {item['name']}: {e}")
            searches[item["name"]] = []
        time.sleep(0.2)
    details = describe(sorted({q for qs in searches.values() for q in qs}))
    matched, skipped = [], []
    for item in todo:
        want = {norm_country(c) for c in item["countries"]}
        pick = None
        for q in searches.get(item["name"], []):
            d = details.get(q)
            if not d or not d["file"]:
                continue
            have = {norm_country(c) for c in d["countries"]}
            if have & want or (not have and q == searches[item["name"]][0]):
                pick = (q, d); break
        if not pick:
            skipped.append(f"{item['name']}: no Wikidata entry with a photo in {', '.join(item['countries'])}"); continue
        matched.append({**item, "qid": pick[0], "file": pick[1]["file"], "languages": pick[1]["languages"]})
    matched.sort(key=lambda m: -m["languages"])          # most famous first
    print(f"  {len(matched)} matched")

    # 2. check licenses and sizes, then build
    built, thumbs = [], {}
    for start in range(0, len(matched), 40):
        if len(built) >= args.count:
            break
        chunk = matched[start:start + 40]
        info = common.commons_info([m["file"] for m in chunk])
        for m in chunk:
            if len(built) >= args.count:
                break
            country = m["countries"][0]
            if per_country.get(country, 0) >= args.max_per_country:
                continue
            inf = info.get(m["file"])
            if not inf:
                skipped.append(f"{m['name']}: photo not found on Commons"); continue
            ok, kind = license_allowed(inf, args.public_domain_only)
            if not ok:
                skipped.append(f"{m['name']}: {kind}"); continue
            if min(inf["width"], inf["height"]) < common.MIN_SHORT_SIDE:
                skipped.append(f"{m['name']}: photo too small ({inf['width']}x{inf['height']})"); continue
            try:
                img = Image.open(io.BytesIO(common.download(inf["download"]))).convert("RGB")
            except Exception as e:
                skipped.append(f"{m['name']}: download failed ({e})"); continue
            inf["file"] = m["file"]
            folder_name = f"squint-landmark-{next_number}"; next_number += 1
            record, levels = build_puzzle(img, folder_name, m, inf, kind, seed=m["qid"])
            record.update({"folder": folder_name, "languages": m["languages"], "year": ""})
            thumbs[folder_name] = []
            for l in levels:
                t = Image.open(LM_DIR / folder_name / l["file"]).convert("RGB").resize((160, 160))
                buf = io.BytesIO(); t.save(buf, "JPEG", quality=70); thumbs[folder_name].append(base64.b64encode(buf.getvalue()).decode())
            built.append(record)
            per_country[country] = per_country.get(country, 0) + 1
            print(f"  [{len(built)}/{args.count}] {m['name']} ({', '.join(m['countries'])}) - {kind}")
            time.sleep(0.3)

    if not built:
        print("No puzzles were built."); (PRIVATE / "skipped.txt").write_text("\n".join(skipped) + "\n"); sys.exit(1)

    ordered = common.order_for_schedule(built, recent)      # "artist" holds the main country, for spacing
    for r in ordered:
        schedule.append(r["folder"]); r["scheduleNumber"] = len(schedule)
    schedule_path.write_text(json.dumps(schedule, indent=2), encoding="utf-8")
    for r in ordered:
        rec = {k: v for k, v in r.items() if k not in ("folder", "scheduleNumber")}
        (PRIVATE / "records" / f"{r['folder']}-record.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    (PRIVATE / "review.html").write_text(common.review_page(ordered, thumbs, noun="landmark", folder_dir="landmarks",
        who=lambda r: f"in {r['city'] + ', ' if r.get('city') else ''}{' / '.join(r['countries'])}"), encoding="utf-8")
    (PRIVATE / "skipped.txt").write_text("\n".join(skipped) + "\n", encoding="utf-8")
    cc = sum(1 for r in ordered if not common.license_ok({"license": r["license"]}))
    summary = f"Built {len(ordered)} landmark puzzles ({cc} use CC-licensed photos, credited). Skipped {len(skipped)}."
    (PRIVATE / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
