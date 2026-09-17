#!/usr/bin/env python3
"""
sync.py - mirror idmp.ucsf.edu content into data/ and record exactly what changed.

The source is a Drupal 10 site. Two things make a faithful mirror possible:
  1. every content node is exposed as JSON at /node/<nid>?_format=json, including
     its `changed` timestamp and revision id, so edits are detectable field by field;
  2. the site's own index views (empiric therapy lists, dosing lists, guideline list,
     antibiogram hub, people) enumerate every published node, so a crawl of those
     views is a complete inventory.

Outputs (all under data/):
  nodes/<nid>.json   raw REST JSON per node (sorted keys -> readable git diffs)
  structure.json     grouped link structure harvested from the source's index pages
  taxonomy.json      taxonomy term url -> display name (harvested from link text)
  files.json         metadata for on-site PDFs (size / last-modified / etag)
  manifest.json      per-node summary used by build.py and by the next sync
  changelog.json     append-only list of change events, one entry per run with changes
  status.json        result of the last run: counts, problems, exit code

Exit codes: 0 clean, 2 drift or problems (data written, site should still build),
            3 fatal (data untouched apart from status.json).
"""
from __future__ import annotations
import difflib, hashlib, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter, deque
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
NODES = os.path.join(DATA, "nodes")
BASE = "https://idmp.ucsf.edu"
UA = "idmp-atlas sync (unofficial read-only mirror; github.com/maxweiss10/idmp-atlas)"
DELAY = float(os.environ.get("SYNC_DELAY", "0.2"))
MAX_PAGES = int(os.environ.get("SYNC_MAX_PAGES", "900"))

DISALLOW = ("/admin/", "/comment/reply/", "/filter/tips", "/node/add/", "/search/", "/user/",
            "/users/", "/media/oembed", "/core/", "/profiles/", "/cdn-cgi/")
FILE_PREFIXES = ("/document/", "/sites/g/files/", "/media/")
SKIP_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".webp")
TAXONOMY_VOCABS = ("site", "notations", "dosing-weights", "guideline-categories", "patient-population",
                   "guidelines-empiric-therapy-categories", "person-type", "publications", "departments-units-and-organizations")
KNOWN_TYPES = {"drug", "diagnosis", "guidelines", "page", "ucsf_person", "other_person", "ucsf_publication"}
# Fields we deliberately do not mirror: directory plumbing and contact details that add
# nothing clinically, plus import chatter that changes on every source import.
PRIVATE_FIELDS = {"field_person_address_postal", "field_person_ucid", "field_person_md5", "field_import_messages",
                  "field_person_payroll_title", "uid", "revision_uid", "revision_log", "field_person_eds_degrees"}
# Fields that must exist (as keys, possibly empty) for us to trust that the source's
# content model still matches the renderer. A missing key means the schema moved.
CONTRACT = {
    "drug": ["title", "field_dosing", "field_dosing_antimicrobial_dosin", "field_dialysis_notes",
             "field_dosing_weights", "field_notations", "field_restriction_details", "field_notes",
             "field_monitoring", "field_drug_references", "field_revision_notes",
             "field_bool_hemodialysis", "field_bool_nondialysis"],
    "diagnosis": ["title", "field_dosing", "field_patient_population", "field_guidelines_for_empiric_the",
                  "field_notes", "field_references"],
    "guidelines": ["title", "body", "field_guideline_category", "field_guideline_modified_date",
                   "field_guideline_site", "field_pdf"],
    "page": ["title", "field_body"],
    "ucsf_person": ["title", "field_person_research_biography", "field_person_working_title", "field_person_subtype",
                    "field_profiles_link", "field_person_publications_list", "field_person_headshot_default"],
    "other_person": ["title", "field_person_title_override", "field_person_subtype", "field_person_headshot_default"],
    "ucsf_publication": ["title", "field_publication_title", "field_publication_authorlist", "field_publication_year",
                         "field_publication_pubmedid", "field_publication_id"],
}
# Index pages whose grouped-link structure drives navigation in the built site.
STRUCTURE_PAGES = {
    "empiric_adult": "/guidelines-for-empiric-therapy-adults",
    "empiric_peds": "/guidelines-for-empiric-therapy-pediatrics",
    "guidelines": "/guidelines-view",
    "dosing_nondialysis": "/adult-antimicrobial-dosing-non-dialysis",
    "dosing_dialysis": "/antimicrobial-dosing-intermittent-continuous-hemodialysis",
    "antibiograms": "/content/antibiograms",
    "people": "/people",
    "home": "/",
}

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def log(msg):
    print(msg, flush=True)

# ----------------------------------------------------------------------------- http
def fetch(url, method="GET", retries=3):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = b"" if method == "HEAD" else r.read()
                return r.status, r.geturl(), dict(r.headers), body
        except urllib.error.HTTPError as e:
            return e.code, url, dict(e.headers) if e.headers else {}, b""
        except Exception as e:  # network hiccup: back off and retry
            last = e
            time.sleep(1.5 * (attempt + 1))
    return -1, url, {"error": str(last)}, b""

def norm_link(href, base_url):
    href = html.unescape(href.strip())
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    u = urllib.parse.urljoin(base_url, href)
    u, _ = urllib.parse.urldefrag(u)
    p = urllib.parse.urlparse(u)
    if p.netloc != "idmp.ucsf.edu":
        return None
    path = p.path or "/"
    path = re.sub(r"/+$", "", path) or "/"
    return path

# ----------------------------------------------------------------------------- parsing helpers
def text_of(fragment):
    t = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()

def page_meta(body_text, url):
    m = re.search(r'<link rel="canonical" href="([^"]+)"', body_text)
    canonical = norm_link(m.group(1), url) if m else None
    m = re.search(r'<link rel="shortlink" href="(?:https?://idmp\.ucsf\.edu)?/node/(\d+)"', body_text)
    nid = int(m.group(1)) if m else None
    m = re.search(r'<article[^>]*class="[^"]*node--type-([a-z0-9_-]+)[^"]*node--view-mode-full[^"]*"', body_text)
    ntype = m.group(1) if m else None
    m = re.search(r'<h1[^>]*class="[^"]*page-title[^"]*"[^>]*>(.*?)</h1>', body_text, flags=re.S)
    title = text_of(m.group(1)) if m else None
    if not title:
        m = re.search(r"<title>(.*?)</title>", body_text, flags=re.S)
        title = text_of(m.group(1)).split(" | ")[0] if m else None
    main = re.search(r"<main[^>]*>(.*?)</main>", body_text, flags=re.S)
    main_html = main.group(1) if main else body_text
    return canonical, nid, ntype, title, main_html

def grouped_links(main_html):
    """Walk the main region in document order and attach every internal link to the
    nearest preceding h2/h3 heading. Generic enough for every index view on the site."""
    frag = re.sub(r"<script.*?</script>", "", main_html, flags=re.S)
    frag = re.sub(r'<form.*?</form>', "", frag, flags=re.S)  # exposed filter forms
    groups, current = [], {"heading": None, "links": []}
    for m in re.finditer(r'<h([23])[^>]*>(.*?)</h\1>|<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', frag, flags=re.S):
        if m.group(2) is not None:
            if current["links"] or current["heading"]:
                groups.append(current)
            current = {"heading": text_of(m.group(2)), "links": []}
        else:
            path = norm_link(m.group(3), BASE + "/")
            txt = text_of(m.group(4))
            if path and txt and not path.startswith(DISALLOW):
                current["links"].append([path, txt])
    if current["links"] or current["heading"]:
        groups.append(current)
    return groups

def nav_links(body_text):
    """Header + footer navigation as [path, text] pairs (order preserved, deduped)."""
    out, seen = [], set()
    for region in re.findall(r"<header.*?</header>|<nav.*?</nav>|<footer.*?</footer>", body_text, flags=re.S):
        for href, txt in re.findall(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', region, flags=re.S):
            path = norm_link(href, BASE + "/")
            t = text_of(txt)
            if path and t and path not in seen and not path.startswith(DISALLOW):
                seen.add(path)
                out.append([path, t])
    return out

def people_cards(main_html):
    people, group = [], None
    frag = re.sub(r"\s+", " ", main_html)
    for m in re.finditer(r'<h3>(.*?)</h3>|<div class="people-row views-row">(.*?)(?=<div class="people-row views-row">|<h3>|</div></div></div></div></div>$)', frag, flags=re.S):
        if m.group(1) is not None:
            group = text_of(m.group(1)); continue
        card = m.group(2)
        name = re.search(r"<h4>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", card, flags=re.S)
        if not name:
            continue
        role = re.search(r'field-name-field-person-primary-dept[^>]*>(.*?)</div>', card, flags=re.S)
        bio = re.search(r'field-name-field-person-what-i-do-text[^>]*>(.*?)</div>', card, flags=re.S)
        img = re.search(r'<img[^>]*src="([^"]+)"', card)
        people.append({
            "group": group, "path": norm_link(name.group(1), BASE + "/"), "name": text_of(name.group(2)),
            "role": text_of(role.group(1)) if role else "", "bio_html": (bio.group(1).strip() if bio else ""),
            "image": (BASE + html.unescape(img.group(1))) if img else None,
        })
    return people

def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def field_text(v):
    """Flatten a Drupal field value list into comparable text."""
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, dict):
                if "value" in item and isinstance(item["value"], str):
                    parts.append(text_of(item["value"]) if "<" in item["value"] else item["value"])
                elif "url" in item:
                    parts.append(str(item.get("url")))
                else:
                    parts.append(canonical_json(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return canonical_json(v)

VOLATILE = {"changed", "revision_timestamp", "vid", "revision_uid", "revision_log", "revision_translation_affected"}

def node_diff(old, new):
    """Field-level summary of what changed between two REST payloads."""
    changed_fields = []
    for k in sorted(set(old) | set(new)):
        if k in VOLATILE or k.startswith("comment_"):
            continue
        if canonical_json(old.get(k)) != canonical_json(new.get(k)):
            a, b = field_text(old.get(k, [])).splitlines(), field_text(new.get(k, [])).splitlines()
            diff = list(difflib.unified_diff(a, b, lineterm="", n=0))[2:]
            changed_fields.append({"field": k, "diff": diff[:80], "truncated": len(diff) > 80})
    return changed_fields

# ----------------------------------------------------------------------------- main
def main():
    t0 = time.time()
    run_ts = now_iso()
    os.makedirs(NODES, exist_ok=True)
    def load(name, default):
        p = os.path.join(DATA, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return default
    prev_manifest = load("manifest.json", {"nodes": {}, "pages": {}, "files": {}, "nav": []})
    changelog = load("changelog.json", [])
    problems = []       # {"level": "hard"|"soft", "code", "message"}
    events = []         # change events for this run
    def problem(level, code, message):
        problems.append({"level": level, "code": code, "message": message})
        log(f"  !! {level.upper()} {code}: {message}")

    def write_status(exit_code, extra=None):
        st = {"run": run_ts, "ok": exit_code == 0, "exit_code": exit_code, "duration_s": round(time.time() - t0, 1),
              "problems": problems, "warnings": sum(1 for p in problems if p["level"] == "soft"),
              "events": len(events), "source": BASE}
        if extra:
            st.update(extra)
        with open(os.path.join(DATA, "status.json"), "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1, ensure_ascii=False)
        return exit_code

    # ---- 1. crawl HTML -------------------------------------------------------
    log("== crawl")
    status, _, _, body = fetch(BASE + "/")
    if status != 200 or b"<html" not in body[:2000].lower():
        problem("hard", "home-unreachable", f"GET {BASE}/ returned {status}")
        return write_status(3)
    home = body.decode("utf-8", "replace")
    nav = nav_links(home)
    queue = deque(["/"] + [p for p, _ in nav])
    seen = {"/"} | {p for p, _ in nav}
    pages = {}          # canonical path -> meta
    files = {}          # path -> meta (HEAD)
    taxonomy = {}       # path -> Counter(text)
    nid_to_path = {}
    unavailable = []
    n = 0
    while queue and n < MAX_PAGES:
        path = queue.popleft()
        if any(path.startswith(d) for d in DISALLOW) or path.lower().endswith(SKIP_EXT) or path.endswith("/feed"):
            continue
        if path.startswith(FILE_PREFIXES):
            files.setdefault(path, {})
            continue
        n += 1
        url = BASE + path
        if path == "/":
            status, final, hdrs, body = 200, url, {}, home.encode()
        else:
            status, final, hdrs, body = fetch(url)
            time.sleep(DELAY)
        ctype = hdrs.get("Content-Type", "") if isinstance(hdrs, dict) else ""
        if status != 200:
            unavailable.append([path, status]); continue
        if body[:5] == b"%PDF-" or "pdf" in ctype.lower():
            files.setdefault(path, {}); continue
        text = body.decode("utf-8", "replace")
        canonical, nid, ntype, title, main_html = page_meta(text, url)
        key = canonical or path
        meta = pages.setdefault(key, {"path": key, "aliases": [], "nid": nid, "type": ntype, "title": title})
        if path not in meta["aliases"]:
            meta["aliases"].append(path)
        if nid:
            nid_to_path[nid] = key
        # harvest taxonomy names and follow links
        for href, txt in re.findall(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, flags=re.S):
            p = norm_link(href, url)
            if not p:
                continue
            seg = p.split("/")[1] if "/" in p[1:] or p.count("/") >= 1 else ""
            if p.count("/") >= 2 and p.split("/")[1] in TAXONOMY_VOCABS:
                t = text_of(txt)
                if t:
                    taxonomy.setdefault(p, Counter())[t] += 1
            if p not in seen:
                seen.add(p); queue.append(p)
        # structure pages: keep grouped links
        for skey, spath in STRUCTURE_PAGES.items():
            if path == spath or key == spath:
                meta["structure_key"] = skey
                meta["groups"] = grouped_links(main_html)
                if skey == "people":
                    meta["people"] = people_cards(main_html)
        if n % 25 == 0:
            log(f"  crawled {n} pages, queue {len(queue)}")
    log(f"  crawl done: {n} pages, {len(nid_to_path)} nodes, {len(files)} files, {len(unavailable)} unavailable")
    if n >= MAX_PAGES:
        problem("soft", "crawl-cap", f"crawl hit the {MAX_PAGES}-page cap; the site may have grown or a link loop appeared")
    for skey, spath in STRUCTURE_PAGES.items():
        if not any(m.get("structure_key") == skey for m in pages.values()):
            problem("hard", "structure-missing", f"index page {spath} ({skey}) was not found or not parsed")
    if len(nid_to_path) < 50:
        problem("hard", "too-few-nodes", f"only {len(nid_to_path)} nodes discovered; refusing to overwrite data")
        return write_status(3)

    # ---- 2. fetch node JSON ----------------------------------------------------
    log("== nodes")
    nodes = {}
    fetch_fail = 0
    for i, (nid, path) in enumerate(sorted(nid_to_path.items())):
        status, _, hdrs, body = fetch(f"{BASE}/node/{nid}?_format=json")
        time.sleep(DELAY)
        try:
            data = json.loads(body.decode("utf-8")) if status == 200 else None
        except Exception:
            data = None
        if not isinstance(data, dict) or "type" not in data:
            fetch_fail += 1
            problem("soft", "node-fetch", f"/node/{nid} ({path}) JSON fetch failed with HTTP {status}")
            if i < 5 and fetch_fail == i + 1:
                problem("hard", "json-endpoint", "the first nodes all failed to return JSON; the REST endpoint may be gone")
                return write_status(3)
            continue
        for k in PRIVATE_FIELDS:
            data.pop(k, None)
        nodes[nid] = data
        if (i + 1) % 50 == 0:
            log(f"  fetched {i + 1}/{len(nid_to_path)}")
    if fetch_fail > 3:
        problem("hard", "node-fetch-many", f"{fetch_fail} nodes failed to fetch")

    # ---- 3. files (HEAD) -------------------------------------------------------
    log("== files")
    for nid, data in nodes.items():
        for item in data.get("field_pdf") or []:
            if isinstance(item, dict) and item.get("url"):
                files.setdefault(norm_link(item["url"], BASE + "/") or item["url"], {})
    for path in sorted(files):
        status, final, hdrs, _ = fetch(BASE + path, method="HEAD")
        time.sleep(DELAY / 2)
        h = {k.lower(): v for k, v in hdrs.items()} if isinstance(hdrs, dict) else {}
        files[path] = {"status": status, "final": final, "size": h.get("content-length"),
                       "last_modified": h.get("last-modified"), "etag": h.get("etag"), "type": h.get("content-type")}

    # ---- 4. compare with previous manifest ------------------------------------
    log("== compare")
    old_nodes = prev_manifest.get("nodes", {})
    manifest_nodes = {}
    type_counts = Counter()
    for nid, data in nodes.items():
        ntype = data["type"][0]["target_id"]
        type_counts[ntype] += 1
        if ntype not in KNOWN_TYPES:
            problem("hard", "unknown-type", f"node {nid} has content type '{ntype}' which the renderer does not know")
        missing = [k for k in CONTRACT.get(ntype, []) if k not in data]
        if missing:
            problem("hard", "schema", f"{ntype} node {nid} ({data['title'][0]['value']}) is missing fields {missing}")
        title = data["title"][0]["value"].strip()
        changed = data["changed"][0]["value"]
        vid = data["vid"][0]["value"]
        content_hash = sha(canonical_json({k: v for k, v in data.items() if k not in VOLATILE and not k.startswith("comment_")}))
        alias = (data.get("path") or [{}])[0].get("alias") or nid_to_path[nid]
        old = old_nodes.get(str(nid))
        rec = {"nid": nid, "type": ntype, "title": title, "alias": alias, "changed": changed, "vid": vid,
               "hash": content_hash, "first_seen": old["first_seen"] if old else run_ts, "status": "active"}
        manifest_nodes[str(nid)] = rec
        # write node file (only if changed, to keep git quiet)
        fn = os.path.join(NODES, f"{nid}.json")
        new_text = json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
        old_data = None
        if os.path.exists(fn):
            with open(fn, encoding="utf-8") as f:
                old_text = f.read()
            if old_text != new_text:
                try:
                    old_data = json.loads(old_text)
                except Exception:
                    old_data = None
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(new_text)
        else:
            with open(fn, "w", encoding="utf-8") as f:
                f.write(new_text)
        if old is None:
            events.append({"kind": "added", "nid": nid, "type": ntype, "title": title, "alias": alias, "changed": changed})
        elif old.get("hash") != content_hash or old.get("changed") != changed:
            fields = node_diff(old_data or {}, data) if old_data is not None else []
            events.append({"kind": "updated", "nid": nid, "type": ntype, "title": title, "alias": alias,
                           "changed_from": old.get("changed"), "changed": changed, "fields": fields})
        elif old.get("status") != "active":
            events.append({"kind": "relisted", "nid": nid, "type": ntype, "title": title, "alias": alias})
    # removed / unlisted nodes
    for nid_s, old in old_nodes.items():
        if nid_s in manifest_nodes:
            continue
        status, _, _, body = fetch(f"{BASE}/node/{nid_s}?_format=json")
        time.sleep(DELAY)
        gone = status in (403, 404)
        rec = dict(old)
        rec["status"] = "removed" if gone else "unlisted"
        rec.setdefault("gone_since", run_ts)
        manifest_nodes[nid_s] = rec
        if old.get("status") == "active":
            events.append({"kind": "removed" if gone else "unlisted", "nid": int(nid_s), "type": old["type"],
                           "title": old["title"], "alias": old.get("alias"), "http": status})
    for ntype in KNOWN_TYPES:
        before = sum(1 for r in old_nodes.values() if r.get("type") == ntype and r.get("status", "active") == "active")
        after = type_counts.get(ntype, 0)
        if before >= 8 and after < 0.75 * before:
            problem("hard", "count-drop", f"{ntype} nodes fell from {before} to {after}")
    # structure pages + nav
    old_pages = prev_manifest.get("pages", {})
    manifest_pages = {}
    for key, meta in pages.items():
        if "structure_key" not in meta:
            continue
        payload = {"groups": meta.get("groups"), "people": meta.get("people")}
        h = sha(canonical_json(payload))
        manifest_pages[meta["structure_key"]] = {"path": key, "title": meta.get("title"), "hash": h}
        if meta["structure_key"] in old_pages and old_pages[meta["structure_key"]].get("hash") != h:
            events.append({"kind": "structure-updated", "structure": meta["structure_key"], "path": key})
    if prev_manifest.get("nav") and [p for p, _ in nav] != [p for p, _ in prev_manifest["nav"]]:
        old_set = {p for p, _ in prev_manifest["nav"]}
        new_set = {p for p, _ in nav}
        problem("soft", "nav-changed", f"source navigation changed; added {sorted(new_set - old_set)} removed {sorted(old_set - new_set)}")
        events.append({"kind": "nav-updated", "added": sorted(new_set - old_set), "removed": sorted(old_set - new_set)})
    # files
    old_files = prev_manifest.get("files", {})
    for path, meta in files.items():
        o = old_files.get(path)
        meta["first_seen"] = o["first_seen"] if o else run_ts
        if o and (o.get("size") != meta.get("size") or o.get("etag") != meta.get("etag")):
            events.append({"kind": "file-updated", "path": path, "size_from": o.get("size"), "size": meta.get("size")})
        if meta.get("status") not in (200, 302, 301):
            problem("soft", "file-unavailable", f"{path} returned HTTP {meta.get('status')}")
    for path, o in old_files.items():
        if path not in files:
            o = dict(o); o["status_note"] = "no longer referenced"
            files[path] = o
            events.append({"kind": "file-unreferenced", "path": path})

    # ---- 5. write --------------------------------------------------------------
    log("== write")
    structure = {m["structure_key"]: {"path": k, "title": m.get("title"), "groups": m.get("groups"),
                                      **({"people": m["people"]} if m.get("people") else {})}
                 for k, m in pages.items() if m.get("structure_key")}
    structure["nav"] = nav
    structure["unavailable_links"] = sorted(unavailable)
    structure["node_paths"] = {str(nid): p for nid, p in nid_to_path.items()}
    structure["page_aliases"] = {k: m["aliases"] for k, m in pages.items() if m.get("nid")}
    tax = {p: c.most_common(1)[0][0] for p, c in taxonomy.items()}
    manifest = {"generated": run_ts, "source": BASE, "nodes": dict(sorted(manifest_nodes.items(), key=lambda kv: int(kv[0]))),
                "pages": manifest_pages, "files": dict(sorted(files.items())), "nav": nav,
                "counts": dict(type_counts)}
    def dump(name, obj):
        with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False, sort_keys=isinstance(obj, dict) and name != "manifest.json")
            f.write("\n")
    dump("structure.json", structure)
    dump("taxonomy.json", tax)
    dump("files.json", files)
    dump("manifest.json", manifest)
    hard = [p for p in problems if p["level"] == "hard"]
    if not old_nodes and events:
        # first snapshot: one event instead of a page-long list of "added"
        events = [{"kind": "snapshot", "count": len(nodes), "types": dict(type_counts)}]
    if events or hard:
        # soft problems (e.g. a PDF that 404s on the source) live in status.json as current state;
        # the changelog only records real changes and hard drift
        changelog.append({"run": run_ts, "events": events, "problems": hard})
        changelog = changelog[-400:]
        dump("changelog.json", changelog)
    log(f"  nodes={len(nodes)} {dict(type_counts)} events={len(events)} problems={len(problems)} ({len(hard)} hard) in {time.time() - t0:.0f}s")
    # exit 2 (job fails, drift issue opened) only for hard problems; soft ones are shown on the site
    return write_status(2 if hard else 0, {"counts": dict(type_counts), "pages_crawled": n, "files": len(files)})

if __name__ == "__main__":
    sys.exit(main())
