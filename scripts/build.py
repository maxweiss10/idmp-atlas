#!/usr/bin/env python3
"""
build.py - render data/ (from sync.py) into docs/, a static site for GitHub Pages.

Design in one paragraph: the source site is organised by *section* (empiric therapy,
dosing, guidelines, antibiograms) but a clinician's question crosses all four -
syndrome -> drug -> dose -> local susceptibility -> site-specific policy. So every
node is rendered as its own clean page, every internal link is rewritten to stay
inside the mirror, drug links open in a side drawer so you never lose your place,
drug pages list the syndromes that recommend them, and one instant search covers
everything. Every page carries the source URL and the source's own `changed`
timestamp so nothing here is ever more than one click from verification.
"""
from __future__ import annotations
import base64, hashlib, html, json, os, re, shutil, sys
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, Comment, NavigableString

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SITE = os.path.join(ROOT, "site")
OUT = os.path.join(ROOT, "docs")
BASE = "https://idmp.ucsf.edu"
REPO = "maxweiss10/idmp-atlas"
SITE_NAME = "IDMP Atlas"
TAGLINE = "UCSF Infectious Diseases Management Program guidance, repackaged for the wards."

# ----------------------------------------------------------------------------- io
def load(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)

def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "item"

def human_date(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%b %-d, %Y")
    except Exception:
        return iso[:10]

def text_only(fragment):
    return re.sub(r"\s+", " ", BeautifulSoup(fragment or "", "lxml").get_text(" ")).strip()

# ----------------------------------------------------------------------------- sanitizer + link rewriting
ALLOWED = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "s", "sup", "sub", "mark", "a", "img",
           "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "h2", "h3", "h4", "h5", "h6",
           "blockquote", "hr", "code", "pre", "small", "dl", "dt", "dd", "figure", "figcaption"}
DROP = {"script", "style", "iframe", "form", "input", "button", "noscript", "select", "textarea", "object", "embed"}

class Ctx:
    def __init__(self):
        self.route_by_nid = {}
        self.nid_by_path = {}
        self.drug_slug_by_nid = {}
        self.warnings = []
        self.images = {}

    def route_for_path(self, path):
        nid = self.nid_by_path.get(path)
        if nid is None:
            m = re.match(r"^/node/(\d+)$", path)
            if m:
                nid = int(m.group(1))
        return (nid, self.route_by_nid.get(nid)) if nid is not None else (None, None)

def rewrite_href(href, ctx, root):
    href = html.unescape((href or "").strip())
    if not href or href.startswith("#"):
        return href, "anchor", None
    if href.startswith(("mailto:", "tel:")):
        return href, "external", None
    u = urljoin(BASE + "/", href)
    p = urlparse(u)
    if p.netloc in ("idmp.ucsf.edu", "idmpsbx.ucsfsitebuilder.acsitefactory.com"):
        path = re.sub(r"/+$", "", p.path) or "/"
        if path in ("/", "/content/home"):
            return root + "index.html", "internal", None
        nid, route = ctx.route_for_path(path)
        if route:
            return root + route + (f"#{p.fragment}" if p.fragment else ""), "internal", nid
        if path.startswith(("/document/", "/sites/g/files/", "/media/")):
            return u, "pdf", None
        if path.startswith("/site/"):
            return root + "guidelines/index.html?site=" + path.split("/")[-1], "internal", None
        if path.startswith("/notations/"):
            return root + "drugs/index.html?tag=" + path.split("/")[-1], "internal", None
        if path.startswith("/guidelines-for-empiric-therapy-adults"):
            return root + "empiric/index.html", "internal", None
        if path.startswith("/guidelines-for-empiric-therapy-pediatrics"):
            return root + "empiric/peds.html", "internal", None
        if path.startswith("/adult-antimicrobial-dosing-non-dialysis") or path.startswith("/antimicrobial-dosing-intermittent"):
            return root + "drugs/index.html", "internal", None
        if path.startswith("/guidelines-"):
            return root + "guidelines/index.html", "internal", None
        return u, "source", None
    if "box.com" in p.netloc or "sharepoint.com" in p.netloc or "idm.oclc.org" in p.netloc:
        return u, "login", None
    return u, "external", None

def sanitize(fragment, ctx, root, page_slug="x"):
    """Clean Drupal/Word HTML down to semantic markup and rewrite links into the mirror."""
    if not fragment or not fragment.strip():
        return ""
    soup = BeautifulSoup(fragment, "lxml")
    for el in soup.find_all(list(DROP)):
        el.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for el in list(soup.find_all(True)):
        if el.name in ("html", "body"):
            continue
        if el.name not in ALLOWED:
            el.unwrap()
            continue
        attrs = {}
        if el.name == "a":
            href, kind, nid = rewrite_href(el.get("href"), ctx, root)
            if not href:
                el.unwrap(); continue
            attrs["href"] = href
            if kind == "internal":
                if nid in ctx.drug_slug_by_nid:
                    attrs["data-drug"] = ctx.drug_slug_by_nid[nid]
            else:
                attrs["class"] = f"x-{kind}"
                attrs["target"] = "_blank"
                attrs["rel"] = "noopener"
                if kind == "login":
                    attrs["title"] = "Opens on Box/SharePoint - UCSF login required"
                elif kind == "source":
                    attrs["title"] = "Opens on idmp.ucsf.edu"
        elif el.name == "img":
            src = el.get("src") or ""
            if src.startswith("data:image/"):
                m = re.match(r"data:image/(png|jpe?g|gif|webp);base64,(.+)", src, flags=re.S)
                if m:
                    ext = "jpg" if m.group(1).startswith("jp") else m.group(1)
                    raw = base64.b64decode(m.group(2))
                    name = hashlib.sha1(raw).hexdigest()[:12] + "." + ext
                    ctx.images[name] = raw
                    src = root + "img/" + name
                else:
                    el.decompose(); continue
            elif src.startswith("blob:") or not src.strip():
                el.decompose(); continue
            else:
                src = urljoin(BASE + "/", src)
            attrs["src"] = src
            attrs["alt"] = el.get("alt") or ""
            attrs["loading"] = "lazy"
        elif el.name in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if el.get(k) and str(el.get(k)).isdigit() and int(el.get(k)) > 1:
                    attrs[k] = el.get(k)
        el.attrs = attrs
    for h in soup.find_all("h1"):
        h.name = "h2"
    # drop empty paragraphs / list items (Word paste leaves many "<p>&nbsp;</p>")
    for el in soup.find_all(["p", "li", "h2", "h3", "h4", "h5", "h6"]):
        if not el.get_text(strip=True) and not el.find(["img", "table"]):
            el.decompose()
    body = soup.body
    return body.decode_contents() if body else str(soup)

# ----------------------------------------------------------------------------- tables
def table_rows(table):
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]

def inner_html(el):
    return el.decode_contents().strip()

def expand_grid(table):
    rows = table_rows(table)
    grid, pending = [], {}
    for r_i, tr in enumerate(rows):
        row, c = [], 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r_i, c) in pending:
                row.append(pending.pop((r_i, c))); c += 1
            cs = int(cell.get("colspan") or 1) if str(cell.get("colspan") or "1").isdigit() else 1
            rs = int(cell.get("rowspan") or 1) if str(cell.get("rowspan") or "1").isdigit() else 1
            entry = {"html": inner_html(cell), "text": cell.get_text(" ", strip=True), "th": cell.name == "th"}
            for k in range(cs):
                row.append(entry if k == 0 else dict(entry, dup=True))
                for rr in range(1, rs):
                    pending[(r_i + rr, c)] = dict(entry, from_above=True)
                c += 1
        while (r_i, c) in pending:
            row.append(pending.pop((r_i, c))); c += 1
        grid.append(row)
    return grid

COLMAP = [
    ("duration", r"duration|length of|course"),
    ("alt", r"alternat|second[- ]line|allerg"),
    ("comments", r"comment|^notes?$|consideration|remark|additional|caveat"),
    ("pathogens", r"pathogen|organism|etiolog|microb|bug"),
    ("condition", r"diagnos|^condition|clinical|setting|scenario|presentation|syndrome|indication|situation|population|patient|category|^type"),
    ("first", r"first|preferred|primary|initial|empiric|recommended|regimen|treatment|therapy|drug|antibiotic|antimicrobial"),
]
LABELS = {"condition": "Condition", "pathogens": "Common pathogens", "first": "First choice", "alt": "Alternative",
          "comments": "Comments", "duration": "Duration"}

def map_columns(headers):
    out, used = [], set()
    for h in headers:
        key = None
        for name, rx in COLMAP:
            if name not in used and re.search(rx, h, flags=re.I):
                key = name; break
        if key is None:
            key = "extra:" + h
        used.add(key)
        out.append(key)
    return out

def header_row_index(grid):
    if not grid:
        return None
    if any(c["th"] for c in grid[0]) and sum(1 for c in grid[0] if c["th"]) >= max(1, len(grid[0]) - 1):
        return 0
    return None

def render_therapy_cards(grid, cols, page_slug):
    parts = ['<div class="rx-list">']
    for r_i, row in enumerate(grid[1:], start=1):
        cells = {}
        for c_i, key in enumerate(cols):
            if c_i < len(row) and not row[c_i].get("dup"):
                cells[key] = row[c_i]
        cond = cells.get("condition")
        title = cond["html"] if cond else ""
        if not title.strip() and not any(v["text"] for v in cells.values()):
            continue
        anchor = f"rx-{r_i}"
        parts.append(f'<article class="rx" id="{anchor}">')
        parts.append('<header class="rx-head">')
        parts.append(f'<div class="rx-title">{title or "<em>Any</em>"}</div>')
        if cells.get("duration") and cells["duration"]["text"]:
            parts.append(f'<div class="rx-dur"><span class="rx-dur-k">Duration</span>{cells["duration"]["html"]}</div>')
        parts.append("</header>")
        parts.append('<div class="rx-grid">')
        for key in ("first", "alt", "pathogens"):
            if key in cells and cells[key]["text"]:
                parts.append(f'<section class="rx-col rx-{key}"><h4>{LABELS[key]}</h4><div class="rx-body">{cells[key]["html"]}</div></section>')
        parts.append("</div>")
        if cells.get("comments") and cells["comments"]["text"]:
            parts.append(f'<section class="rx-notes"><h4>Comments</h4><div class="rx-body">{cells["comments"]["html"]}</div></section>')
        for key, cell in cells.items():
            if key.startswith("extra:") and cell["text"]:
                parts.append(f'<section class="rx-notes"><h4>{esc(key[6:])}</h4><div class="rx-body">{cell["html"]}</div></section>')
        parts.append("</article>")
    parts.append("</div>")
    return "\n".join(parts)

def render_plain_table(grid, heat=False):
    hdr = header_row_index(grid)
    parts = ['<div class="tbl-wrap"><table class="tbl' + (' heat' if heat else '') + '">']
    skip_cols = set()
    for r_i, row in enumerate(grid):
        tag = "th" if (hdr == r_i) or all(c["th"] for c in row) else "td"
        if r_i == hdr and heat:
            for c_i, c in enumerate(row):
                if re.search(r"isolate|organism|^n$|number|total", c["text"], flags=re.I):
                    skip_cols.add(c_i)
        parts.append("<tr>")
        for c_i, c in enumerate(row):
            if c.get("dup"):
                continue
            cls = ""
            if heat and tag == "td" and c_i not in skip_cols and c_i > 0:
                m = re.match(r"^\s*[<>≤≥]?\s*(\d{1,3})\s*%?\s*$", c["text"])
                if m:
                    v = int(m.group(1))
                    cls = "s-hi" if v >= 90 else "s-ok" if v >= 80 else "s-mid" if v >= 60 else "s-lo"
                elif re.match(r"^\s*R\s*$", c["text"]):
                    cls = "s-r"
            if c.get("from_above"):
                cls = (cls + " from-above").strip()
            attrs = (f' class="{cls}"' if cls else "")
            parts.append(f"<{tag}{attrs}>{c['html']}</{tag}>")
        parts.append("</tr>")
    parts.append("</table></div>")
    return "".join(parts)

def render_rich(fragment, ctx, root, page_slug, mode="auto", heat=False):
    """Sanitize a field and render each table either as therapy cards or a clean table.
    Returns (html, notes) where notes lists tables that fell back to raw layout."""
    clean = sanitize(fragment, ctx, root, page_slug)
    if not clean:
        return "", []
    soup = BeautifulSoup(clean, "lxml")
    body = soup.body
    if body is None:
        return clean, []
    notes = []
    out = []
    for child in list(body.contents):
        if isinstance(child, NavigableString):
            if str(child).strip():
                out.append(f"<p>{esc(str(child).strip())}</p>")
            continue
        if child.name == "table":
            grid = expand_grid(child)
            hdr = header_row_index(grid)
            if mode in ("auto", "cards") and hdr == 0 and len(grid) > 1:
                cols = map_columns([c["text"] for c in grid[0]])
                if "first" in cols or ("condition" in cols and len(cols) >= 3 and mode == "cards"):
                    out.append(render_therapy_cards(grid, cols, page_slug))
                    continue
            if hdr is None and mode == "cards" and len(grid) > 1 and len(grid[0]) >= 4:
                notes.append("info: a table without a header row is shown in its original layout")
            out.append(render_plain_table(grid, heat=heat))
        else:
            # tables nested deeper (inside div/p) are rare after unwrap; render as-is
            out.append(str(child))
    return "\n".join(out), notes

# ----------------------------------------------------------------------------- site sites
SITE_GROUPS = {"ucsf": "UCSF Health", "zsfg": "ZSFG", "va": "VA", "bch": "BCH"}
def site_group(slug):
    s = slug.lower()
    if "zuckerberg" in s or "zsfg" in s:
        return "zsfg"
    if "veteran" in s or s.startswith("va") or "-va-" in s:
        return "va"
    if "benioff" in s or "children" in s:
        return "bch"
    return "ucsf"

def site_short(slug, name):
    s = slug.lower()
    if "parnassus" in s: return "Parnassus"
    if "mission-bay" in s: return "Mission Bay"
    if "mount-zion" in s or "mt-zion" in s: return "Mt Zion"
    if "stanyan" in s or "hyde" in s: return "Stanyan/Hyde"
    if "oakland" in s: return "BCH Oakland"
    if "benioff" in s or "children" in s: return "BCH SF"
    if "zuckerberg" in s or "zsfg" in s: return "ZSFG"
    if "veteran" in s or "va-" in s: return "VA"
    return name or slug

# ----------------------------------------------------------------------------- layout
NAV = [("empiric/index.html", "Empiric therapy"), ("drugs/index.html", "Dosing"), ("guidelines/index.html", "Guidelines"),
       ("antibiograms/index.html", "Antibiograms"), ("peds.html", "Pediatrics"), ("people.html", "People"), ("changes.html", "What changed")]

def layout(ctx, root, title, content, *, desc="", node=None, section="", extra_head="", crumbs=None, fallback_notes=None):
    ver = ctx.asset_ver
    crumb_html = ""
    if crumbs:
        crumb_html = '<nav class="crumbs">' + " <span>/</span> ".join(
            f'<a href="{root}{h}">{esc(t)}</a>' if h else f"<span>{esc(t)}</span>" for h, t in crumbs) + "</nav>"
    src_html = ""
    if node:
        src_html = (f'<div class="src"><a class="x-source" target="_blank" rel="noopener" href="{esc(node["source_url"])}">'
                    f'View on idmp.ucsf.edu ↗</a><span>IDMP last changed <time datetime="{esc(node["changed"])}">{esc(human_date(node["changed"]))}</time></span></div>')
    warn_html = ""
    if fallback_notes:
        warn_html = ('<div class="notice warn"><strong>Layout note:</strong> part of this page did not match the layout the '
                     'mirror expects, so it is shown in its original form. ' + esc("; ".join(fallback_notes)) + '.</div>')
    status = ctx.status or {}
    return f"""<!doctype html>
<html lang="en" data-root="{root}" data-section="{esc(section)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · {SITE_NAME}</title>
<meta name="description" content="{esc(desc or TAGLINE)}">
<meta name="robots" content="noindex">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🧫</text></svg>">
<link rel="stylesheet" href="{root}assets/site.css?v={ver}">
<script>try{{var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t);var s=localStorage.getItem('site');if(s)document.documentElement.setAttribute('data-site',s);}}catch(e){{}}</script>
{extra_head}
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="top">
  <div class="top-row">
    <a class="brand" href="{root}index.html"><span class="brand-mark">🧫</span><span class="brand-name">{SITE_NAME}</span><span class="brand-sub">unofficial mirror</span></a>
    <div class="search" id="search"><input type="search" id="q" placeholder="Search syndromes, drugs, guidelines, organisms…  ( / )" autocomplete="off" aria-label="Search"><div class="results" id="results" hidden></div></div>
    <div class="top-tools">
      <div class="site-switch" role="group" aria-label="Hospital site">
        <button data-site="">All</button><button data-site="ucsf">UCSF</button><button data-site="zsfg">ZSFG</button><button data-site="va">VA</button><button data-site="bch">BCH</button>
      </div>
      <button class="theme" id="theme" aria-label="Toggle dark mode" title="Toggle dark mode">☾</button>
    </div>
  </div>
  <nav class="nav" aria-label="Sections">{"".join(f'<a href="{root}{h}"{" class=on" if section == h.split("/")[0].replace(".html", "") else ""}>{esc(t)}</a>' for h, t in NAV)}<a class="status" id="status" href="{root}changes.html" title="Sync status">sync</a></nav>
</header>
<main id="main" class="wrap">
{crumb_html}
{warn_html}
{content}
{src_html}
</main>
<footer class="foot">
  <p><strong>{SITE_NAME}</strong> is an unofficial, read-only mirror of <a href="{BASE}" target="_blank" rel="noopener">idmp.ucsf.edu</a>, rebuilt nightly. It is not affiliated with UCSF or the IDMP. Content belongs to its authors; always confirm against the source before acting.
  Mirror last verified against the source <time datetime="{esc(status.get('run',''))}">{esc(human_date(status.get('run')))}</time>. <a href="{root}about.html">About this site</a> · <a href="https://github.com/{REPO}" target="_blank" rel="noopener">Source code</a></p>
</footer>
<div class="drawer" id="drawer" hidden><div class="drawer-bar"><button class="drawer-back" id="drawer-back" hidden>← Back</button><a class="drawer-open" id="drawer-open" href="#">Open full page</a><button class="drawer-close" id="drawer-close" aria-label="Close">✕</button></div><div class="drawer-body" id="drawer-body"></div></div>
<div class="scrim" id="scrim" hidden></div>
<script src="{root}assets/site.js?v={ver}" defer></script>
</body>
</html>
"""

# ----------------------------------------------------------------------------- build
def main():
    manifest = load("manifest.json")
    if not manifest:
        print("no data/manifest.json - run sync.py first", file=sys.stderr)
        return 1
    structure = load("structure.json", {})
    taxonomy = load("taxonomy.json", {})
    files = load("files.json", {})
    status = load("status.json", {})
    changelog = load("changelog.json", [])
    with open(os.path.join(ROOT, "scripts", "synonyms.json"), encoding="utf-8") as f:
        synonyms = json.load(f)
    ctx = Ctx()
    ctx.status = status
    # ---- load nodes
    nodes = {}
    for nid_s, rec in manifest["nodes"].items():
        if rec.get("status", "active") != "active":
            continue
        p = os.path.join(DATA, "nodes", f"{nid_s}.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                nodes[int(nid_s)] = json.load(f)
    def fv(node, field, key="value"):
        v = node.get(field) or []
        return v[0].get(key) if v and isinstance(v[0], dict) else None
    def fvs(node, field, key="value"):
        return [x.get(key) for x in (node.get(field) or []) if isinstance(x, dict) and x.get(key) is not None]

    # ---- alias map and routes
    for nid, n in nodes.items():
        alias = fv(n, "path", "alias") or f"/node/{nid}"
        ctx.nid_by_path[alias] = nid
        ctx.nid_by_path[f"/node/{nid}"] = nid
    for nid_s, path in (structure.get("node_paths") or {}).items():
        if int(nid_s) in nodes:
            ctx.nid_by_path[path] = int(nid_s)
    # every path the crawler saw redirect/resolve to a node (old aliases, redirects) maps to that node too
    for canonical, aliases in (structure.get("page_aliases") or {}).items():
        nid = ctx.nid_by_path.get(canonical)
        if nid is not None:
            for a in aliases:
                ctx.nid_by_path.setdefault(a, nid)
    # antibiogram page set
    abx_nids = set()
    for g in (structure.get("antibiograms") or {}).get("groups") or []:
        for path, _ in g["links"]:
            nid = ctx.nid_by_path.get(path)
            if nid in nodes and nodes[nid]["type"][0]["target_id"] == "page":
                abx_nids.add(nid)
    for nid in list(abx_nids):
        body = fv(nodes[nid], "field_body") or ""
        for href in re.findall(r'href="([^"]+)"', body):
            path = urlparse(urljoin(BASE + "/", html.unescape(href))).path.rstrip("/")
            n2 = ctx.nid_by_path.get(path)
            if n2 in nodes and nodes[n2]["type"][0]["target_id"] == "page":
                abx_nids.add(n2)
    for nid, n in nodes.items():
        if "susceptib" in (fv(n, "title") or "").lower() or "antibiogram" in (fv(n, "title") or "").lower():
            if n["type"][0]["target_id"] == "page":
                abx_nids.add(nid)
    used_slugs = set()
    models = {}
    for nid, n in sorted(nodes.items()):
        ntype = n["type"][0]["target_id"]
        alias = fv(n, "path", "alias") or f"/node/{nid}"
        slug = slugify(re.sub(r"^/(content|people)/", "", alias).strip("/").replace("/", "-"))
        if slug in used_slugs:
            slug = f"{slug}-{nid}"
        used_slugs.add(slug)
        if ntype == "diagnosis":
            route = f"empiric/{slug}.html"
        elif ntype == "drug":
            route = f"drugs/{slug}.html"; ctx.drug_slug_by_nid[nid] = slug
        elif ntype == "guidelines":
            route = f"guidelines/{slug}.html"
        elif ntype in ("ucsf_person", "other_person"):
            route = f"people/{slug}.html"
        elif ntype == "ucsf_publication":
            route = f"publications.html#p{nid}"
        elif nid in abx_nids:
            route = f"antibiograms/{slug}.html"
        else:
            route = f"pages/{slug}.html"
        ctx.route_by_nid[nid] = route
        models[nid] = {"nid": nid, "type": ntype, "title": (fv(n, "title") or "").strip(), "alias": alias, "slug": slug,
                       "route": route, "changed": fv(n, "changed"), "source_url": BASE + alias, "raw": n}
    # ---- assets
    os.makedirs(OUT, exist_ok=True)
    for sub in ("assets", "img", "empiric", "drugs", "guidelines", "antibiograms", "pages", "people"):
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)
    css = open(os.path.join(SITE, "assets", "site.css"), encoding="utf-8").read()
    js = open(os.path.join(SITE, "assets", "site.js"), encoding="utf-8").read().replace("__REPO__", REPO)
    ctx.asset_ver = hashlib.sha1((css + js).encode()).hexdigest()[:8]
    open(os.path.join(OUT, "assets", "site.css"), "w", encoding="utf-8").write(css)
    open(os.path.join(OUT, "assets", "site.js"), "w", encoding="utf-8").write(js)
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    written = set()
    def write(route, html_text):
        p = os.path.join(OUT, route)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(html_text)
        written.add(route)
    def root_for(route):
        return "../" * route.count("/")
    def tax_name(url):
        return taxonomy.get(url) or url.rsplit("/", 1)[-1].replace("-", " ")

    search = []
    by_type = defaultdict(list)
    for m in models.values():
        by_type[m["type"]].append(m)

    # ---- cross references: which diagnoses / guidelines link to which drug
    drug_used_in = defaultdict(list)
    dx_linked_guidelines = defaultdict(list)
    for m in models.values():
        n = m["raw"]
        body = ""
        if m["type"] == "diagnosis":
            body = (fv(n, "field_dosing") or "") + (fv(n, "field_notes") or "")
        elif m["type"] == "guidelines":
            body = fv(n, "body") or ""
        elif m["type"] == "page":
            body = fv(n, "field_body") or ""
        if not body:
            continue
        seen = set()
        for href in re.findall(r'href="([^"]+)"', body):
            path = urlparse(urljoin(BASE + "/", html.unescape(href))).path.rstrip("/")
            nid = ctx.nid_by_path.get(path)
            if nid is None or nid in seen or nid not in models:
                continue
            seen.add(nid)
            if models[nid]["type"] == "drug":
                drug_used_in[nid].append(m)
            if models[nid]["type"] == "guidelines" and m["type"] == "diagnosis":
                dx_linked_guidelines[m["nid"]].append(models[nid])

    # ---- guideline metadata
    def guideline_meta(m):
        n = m["raw"]
        sites = []
        for item in n.get("field_guideline_site") or []:
            url = item.get("url") or ""
            slug = url.rsplit("/", 1)[-1]
            sites.append({"slug": slug, "name": tax_name(url), "short": site_short(slug, tax_name(url)), "group": site_group(slug)})
        cat_url = fv(n, "field_guideline_category", "url") or ""
        category = tax_name(cat_url) if cat_url else "Uncategorized"
        dates = sorted(fvs(n, "field_guideline_modified_date"))
        body = fv(n, "body") or ""
        pdfs = [(item.get("url"), item) for item in (n.get("field_pdf") or []) if item.get("url")]
        kinds = []
        if pdfs:
            kinds.append("pdf")
        if re.search(r"box\.com|sharepoint\.com", body):
            kinds.append("login")
        if len(text_only(body)) > 350:
            kinds.append("inline")
        if re.search(r'href="[^"]*(?:/document/|/sites/g/files/)[^"]*"', body) and "pdf" not in kinds:
            kinds.append("pdf")
        desc = ""
        soup = BeautifulSoup(body or "", "lxml")
        for p in soup.find_all(["p", "li", "h2", "h3"]):
            t = re.sub(r"\s+", " ", p.get_text(" ")).strip()
            if len(t) > 25 and not re.match(r"^(link to|please refer|see |click)", t, flags=re.I):
                desc = t; break
        if len(desc) > 190:
            desc = desc[:187].rsplit(" ", 1)[0] + "…"
        return {"sites": sites, "category": category, "cat_slug": slugify(category), "dates": dates,
                "latest": dates[-1] if dates else None, "kinds": kinds, "desc": desc, "pdfs": pdfs}
    gmeta = {m["nid"]: guideline_meta(m) for m in by_type["guidelines"]}

    # ---- related guidelines for diagnoses (title-token overlap, transparent and conservative)
    STOP = {"the", "and", "or", "of", "in", "for", "with", "a", "an", "to", "at", "on", "guideline", "guidelines", "guidance",
            "treatment", "management", "adult", "adults", "pediatric", "pediatrics", "patients", "patient", "infection", "infections",
            "ucsf", "zsfg", "vasf", "sfva", "sf", "bch", "hospital", "hospitals", "no", "not", "other", "any", "unknown", "source",
            "empiric", "therapy", "algorithm", "protocol", "clinical", "disease", "acute", "suspected", "being", "evaluated", "person", "who"}
    ACR = {"hap": ["hospital-acquired", "pneumonia"], "vap": ["ventilator", "pneumonia"], "cap": ["community-acquired", "pneumonia"],
           "cdi": ["difficile"], "c": [], "ssti": ["skin", "soft", "tissue"], "uti": ["urinary", "tract"], "nsti": ["necrotizing", "soft", "tissue"],
           "iai": ["intra-abdominal", "abdominal"], "sbp": ["peritonitis"], "bsi": ["bloodstream"], "pid": ["pelvic", "inflammatory"],
           "sti": ["sexually", "transmitted"], "copd": ["bronchitis"], "ltu": ["liver", "transplant"], "esld": ["liver"],
           "hsv": ["herpes"], "cmv": ["cytomegalovirus"], "rsv": ["respiratory", "syncytial"], "tb": ["tuberculosis"], "opat": ["outpatient", "parenteral"]}
    def toks(s):
        words = re.findall(r"[a-z0-9][a-z0-9'-]*", s.lower())
        out = set()
        for w in words:
            w = w.strip("'-")
            if not w or w in STOP or len(w) < 3 and w not in ACR:
                continue
            out.add(w)
            for x in ACR.get(w, []):
                out.add(x)
            if w.endswith("s") and len(w) > 4:
                out.add(w[:-1])
        return out
    def related_guidelines(m):
        cat = ""
        for key in ("empiric_adult", "empiric_peds"):
            for g in (structure.get(key) or {}).get("groups") or []:
                if any(ctx.nid_by_path.get(p) == m["nid"] for p, _ in g["links"]):
                    cat = g["heading"] or ""
        mine = toks(m["title"]) | toks(cat)
        peds = "pediatric" in (fv(m["raw"], "field_patient_population", "url") or "")
        scored = []
        for g in by_type["guidelines"]:
            gt = toks(g["title"])
            common = mine & gt
            if not common:
                continue
            score = len(common)
            gs = gmeta[g["nid"]]["sites"]
            g_is_peds = any(s["group"] == "bch" for s in gs) and not any(s["group"] != "bch" for s in gs)
            if peds != g_is_peds:
                score -= 0.5
            scored.append((score, g))
        scored.sort(key=lambda x: (-x[0], x[1]["title"]))
        linked = dx_linked_guidelines.get(m["nid"], [])
        out = [g for g in linked]
        for s, g in scored:
            if s >= 1 and g not in out:
                out.append(g)
        return out[:6]

    people_view = {}
    for i, p in enumerate((structure.get("people") or {}).get("people") or []):
        if p.get("path"):
            people_view[p["path"]] = dict(p, order=i)

    # ---- per-node body rendering
    def render_node_body(m, root):
        n = m["raw"]
        notes = []
        parts = []
        t = m["type"]
        if t == "diagnosis":
            pop = fv(n, "field_patient_population", "url") or ""
            popname = "Pediatric" if "pediatric" in pop else "Adult"
            h, nt = render_rich(fv(n, "field_dosing"), ctx, root, m["slug"], mode="cards"); notes += nt
            parts.append(h)
            extra = fv(n, "field_notes")
            if extra and text_only(extra):
                h, nt = render_rich(extra, ctx, root, m["slug"]); notes += nt
                parts.append(f'<section class="block"><h2>Notes</h2>{h}</section>')
            refs = fv(n, "field_references")
            if refs and text_only(refs):
                h, nt = render_rich(refs, ctx, root, m["slug"]); notes += nt
                parts.append(f'<details class="block refs"><summary>References</summary>{h}</details>')
            rel = related_guidelines(m)
            if rel:
                items = []
                for g in rel:
                    gm = gmeta[g["nid"]]
                    badges = "".join(f'<span class="badge site s-{s["group"]}">{esc(s["short"])}</span>' for s in gm["sites"])
                    items.append(f'<li data-sites="{esc(" ".join(sorted({s["group"] for s in gm["sites"]})))}"><a href="{root}{g["route"]}">{esc(g["title"])}</a> {badges}</li>')
                parts.append(f'<aside class="block related"><h2>Related guidelines</h2><p class="muted">Matched by title, then ordered by your selected site.</p><ul class="rel-list">{"".join(items)}</ul></aside>')
            return "\n".join(parts), notes, popname
        if t == "drug":
            badges = []
            for item in n.get("field_notations") or []:
                url = item.get("url") or ""
                slug = url.rsplit("/", 1)[-1]
                label = tax_name(url)
                cls = "zsfg" if "zsfg" in slug else "ucsf" if "ucsf" in slug else "ivpo" if "iv-po" in slug else "misc"
                badges.append(f'<span class="badge tag t-{cls}" title="{esc(label)}">{esc(short_notation(slug, label))}</span>')
            dw = [tax_name(i.get("url") or "") for i in (n.get("field_dosing_weights") or [])]
            head = ""
            if badges or dw:
                head = '<div class="drug-meta">' + "".join(badges) + (f'<span class="dw"><strong>Dosing weight:</strong> {esc("; ".join(dw))}</span>' if dw else "") + "</div>"
            parts.append(head)
            restr = fv(n, "field_restriction_details")
            if restr and text_only(restr):
                h, nt = render_rich(restr, ctx, root, m["slug"]); notes += nt
                parts.append(f'<section class="block restrict"><h2>Restriction</h2>{h}</section>')
            nd = fv(n, "field_dosing")
            if nd and text_only(nd) and (fv(n, "field_bool_nondialysis") is not False):
                h, nt = render_rich(nd, ctx, root, m["slug"], mode="table"); notes += nt
                parts.append(f'<section class="block"><h2>Adult dosing, non-dialysis</h2>{h}</section>')
            hd = fv(n, "field_dosing_antimicrobial_dosin")
            if hd and text_only(hd) and (fv(n, "field_bool_hemodialysis") is not False):
                h, nt = render_rich(hd, ctx, root, m["slug"], mode="table"); notes += nt
                dn = fv(n, "field_dialysis_notes")
                dnh = ""
                if dn and text_only(dn):
                    dnh, nt = render_rich(dn, ctx, root, m["slug"]); notes += nt
                    dnh = f'<div class="subblock"><h3>Dialysis notes</h3>{dnh}</div>'
                parts.append(f'<section class="block"><h2>Dosing in intermittent HD and CRRT</h2><p class="muted">Intermittent HD assumes high-flux hemodialysis. CRRT assumes CVVHD with ultrafiltration rate 2 L/h and residual native GFR &lt; 10 mL/min (source assumptions).</p>{h}{dnh}</section>')
            for field, label in (("field_notes", "Notes"), ("field_monitoring", "Monitoring")):
                v = fv(n, field)
                if v and text_only(v):
                    h, nt = render_rich(v, ctx, root, m["slug"]); notes += nt
                    parts.append(f'<section class="block"><h2>{label}</h2>{h}</section>')
            used = drug_used_in.get(m["nid"], [])
            if used:
                adult = [u for u in used if u["type"] == "diagnosis" and "pediatric" not in (fv(u["raw"], "field_patient_population", "url") or "")]
                peds = [u for u in used if u["type"] == "diagnosis" and "pediatric" in (fv(u["raw"], "field_patient_population", "url") or "")]
                gls = [u for u in used if u["type"] != "diagnosis"]
                def lst(items):
                    return "<ul class=\"rel-list\">" + "".join(f'<li><a href="{root}{u["route"]}">{esc(u["title"])}</a></li>' for u in sorted(items, key=lambda x: x["title"])) + "</ul>"
                blocks = ""
                if adult: blocks += f"<h3>Adult empiric therapy</h3>{lst(adult)}"
                if peds: blocks += f"<h3>Pediatric empiric therapy</h3>{lst(peds)}"
                if gls: blocks += f"<h3>Guidelines and pages</h3>{lst(gls)}"
                parts.append(f'<aside class="block related"><h2>Where this drug is recommended</h2>{blocks}</aside>')
            refs = fv(n, "field_drug_references")
            if refs and text_only(refs):
                h, nt = render_rich(refs, ctx, root, m["slug"]); notes += nt
                parts.append(f'<details class="block refs"><summary>References</summary>{h}</details>')
            rev = fv(n, "field_revision_notes")
            if rev and rev.strip():
                lines = [esc(l.strip()) for l in re.split(r"[\r\n]+", rev) if l.strip()]
                parts.append('<details class="block refs"><summary>Revision notes from IDMP</summary><ul>' + "".join(f"<li>{l}</li>" for l in lines) + "</ul></details>")
            return "\n".join(parts), notes, None
        if t == "guidelines":
            gm = gmeta[m["nid"]]
            badges = "".join(f'<span class="badge site s-{s["group"]}" title="{esc(s["name"])}">{esc(s["short"])}</span>' for s in gm["sites"])
            kinds = "".join(f'<span class="badge kind k-{k}">{ {"pdf": "PDF", "login": "UCSF login", "inline": "Full text"}[k] }</span>' for k in gm["kinds"])
            dates = ""
            if gm["dates"]:
                hist = ", ".join(human_date(d) for d in gm["dates"])
                dates = f'<span class="gl-date" title="Modification history: {esc(hist)}">Guideline dated {esc(human_date(gm["latest"]))}</span>'
            parts.append(f'<div class="drug-meta">{badges}{kinds}<span class="badge cat">{esc(gm["category"])}</span>{dates}</div>')
            for url, item in gm["pdfs"]:
                full = urljoin(BASE + "/", url)
                fmeta = files.get(urlparse(full).path) or {}
                size = fmeta.get("size")
                size_h = f" · {int(size) // 1024} KB" if size and str(size).isdigit() else ""
                parts.append(f'<p class="cta"><a class="btn x-pdf" target="_blank" rel="noopener" href="{esc(full)}">Open guideline PDF ↗</a><span class="muted">hosted on idmp.ucsf.edu, no login{size_h}</span></p>')
            body = fv(n, "body")
            if body and text_only(body):
                h, nt = render_rich(body, ctx, root, m["slug"], mode="auto"); notes += nt
                parts.append(f'<section class="block prose">{h}</section>')
            elif not gm["pdfs"]:
                parts.append('<p class="muted">This guideline has no text on IDMP; open the source page for details.</p>')
            return "\n".join(parts), notes, None
        if t in ("ucsf_person", "other_person"):
            view = people_view.get(m["alias"]) or {}
            role = fv(n, "field_person_title_override") or fv(n, "field_person_working_title") or view.get("role") or ""
            sub = fv(n, "field_person_subtype", "url") or ""
            where = view.get("group") or (tax_name(sub) if sub else "")
            img = view.get("image")
            bio = fv(n, "field_person_research_biography", "processed") or view.get("bio_html") or ""
            link = fv(n, "field_profiles_link", "uri")
            honors = fvs(n, "field_profiles_honors_awards")
            pubs = [ctx.nid_by_path.get(urlparse(urljoin(BASE + "/", i.get("url") or "")).path.rstrip("/")) for i in (n.get("field_person_publications_list") or [])]
            pubs = [models[x] for x in pubs if x in models]
            parts.append('<div class="person-head">' + (f'<img class="headshot" loading="lazy" src="{esc(img)}" alt="">' if img else "") +
                         f'<div><p class="role">{esc(role)}</p>' + (f'<p class="muted">{esc(where)}</p>' if where else "") +
                         (f'<p><a class="x-external" target="_blank" rel="noopener" href="{esc(link)}">UCSF Profiles</a></p>' if link else "") + "</div></div>")
            if text_only(bio):
                h, nt = render_rich(bio, ctx, root, m["slug"]); notes += nt
                parts.append(f'<section class="block prose">{h}</section>')
            if honors:
                parts.append('<section class="block"><h2>Honors and awards</h2><ul>' + "".join(f"<li>{esc(x)}</li>" for x in honors) + "</ul></section>")
            if pubs:
                parts.append('<section class="block"><h2>Selected publications</h2><ul class="rel-list">' + "".join(
                    f'<li><a href="{root}{p["route"]}">{esc(p["title"])}</a> <span class="muted">{esc(fv(p["raw"], "field_publication_year") or "")}</span></li>' for p in pubs) + "</ul></section>")
            return "\n".join(parts), notes, None
        # page
        body = fv(n, "field_body")
        heat = m["nid"] in abx_nids
        if body and text_only(body):
            h, nt = render_rich(body, ctx, root, m["slug"], mode="auto", heat=heat); notes += nt
            if heat and "heat" in h:
                parts.append('<p class="legend"><span class="s-hi">≥ 90 %</span><span class="s-ok">80–89 %</span><span class="s-mid">60–79 %</span><span class="s-lo">&lt; 60 %</span><span class="s-r">R</span> shading added by the mirror; values are % susceptible as published.</p>')
            parts.append(f'<section class="block prose">{h}</section>')
        else:
            parts.append('<p class="muted">This page has no body text on IDMP (it may be a form or an image). Open the source page.</p>')
        return "\n".join(parts), notes, None

    def short_notation(slug, label):
        if "zsfg" in slug: return "ID-R · ZSFG"
        if "ucsf" in slug: return "ID-R · UCSF"
        if "iv-po" in slug: return "IV → PO"
        return label

    # ---- write node pages
    section_of = {"diagnosis": "empiric", "drug": "drugs", "guidelines": "guidelines", "ucsf_person": "people", "other_person": "people"}
    fallback_count = 0
    for m in models.values():
        if m["type"] == "ucsf_publication":
            continue
        route = m["route"]; root = root_for(route)
        body, notes, popname = render_node_body(m, root)
        if notes:
            fallback_count += 1
            ctx.warnings.append({"nid": m["nid"], "title": m["title"], "route": route, "notes": notes})
            notes = [x for x in notes if not x.startswith("info:")]  # only real fallbacks get an on-page banner
        crumbs = [("index.html", "Home")]
        sec = section_of.get(m["type"], "antibiograms" if m["nid"] in abx_nids else "pages")
        if m["type"] == "diagnosis":
            crumbs.append(("empiric/peds.html" if popname == "Pediatric" else "empiric/index.html", f"{popname} empiric therapy"))
        elif m["type"] == "drug":
            crumbs.append(("drugs/index.html", "Dosing"))
        elif m["type"] == "guidelines":
            crumbs.append(("guidelines/index.html", "Guidelines"))
        elif m["nid"] in abx_nids:
            crumbs.append(("antibiograms/index.html", "Antibiograms"))
        elif m["type"] in ("ucsf_person", "other_person"):
            crumbs.append(("people.html", "People"))
        crumbs.append((None, m["title"]))
        kicker = {"diagnosis": f"{popname} empiric therapy", "drug": "Antimicrobial dosing", "guidelines": "Guideline",
                  "ucsf_person": "IDMP team", "other_person": "IDMP team"}.get(m["type"], "Antibiogram" if m["nid"] in abx_nids else "Page")
        content = f'<article class="node node-{m["type"]}"><p class="kicker">{esc(kicker)}</p><h1>{esc(m["title"])}</h1>{body}</article>'
        write(route, layout(ctx, root, m["title"], content, node=m, section=sec, crumbs=crumbs, fallback_notes=notes,
                            desc=f"{kicker}: {m['title']} (mirrored from idmp.ucsf.edu)"))
        # search entry
        n = m["raw"]
        kw = []
        snippet = ""
        if m["type"] == "diagnosis":
            txt = text_only(fv(n, "field_dosing") or "")
            kw = re.findall(r"[A-Z][a-z]+\.? [a-z]+", txt)[:20]
            drug_names = []
            for href in re.findall(r'href="([^"]+)"', fv(n, "field_dosing") or ""):
                nid = ctx.nid_by_path.get(urlparse(urljoin(BASE + "/", html.unescape(href))).path.rstrip("/"))
                if nid in models and models[nid]["type"] == "drug":
                    drug_names.append(models[nid]["title"])
            kw += drug_names
            snippet = ", ".join(dict.fromkeys(drug_names))[:140]
            stype = "dxp" if popname == "Pediatric" else "dx"
        elif m["type"] == "drug":
            low = m["title"].lower()
            for key, alts in synonyms.items():
                if key.startswith("_"):
                    continue
                if key in low:
                    kw += alts
            inds = []
            for row in expand_grid(BeautifulSoup(fv(n, "field_dosing") or "", "lxml"))[1:] if fv(n, "field_dosing") else []:
                if row and row[0]["text"]:
                    inds.append(row[0]["text"])
            snippet = "; ".join(inds)[:140]
            stype = "drug"
        elif m["type"] == "guidelines":
            gm = gmeta[m["nid"]]
            kw = [gm["category"]] + [s["short"] for s in gm["sites"]]
            snippet = gm["desc"][:140]
            stype = "gl"
        elif m["type"] in ("ucsf_person", "other_person"):
            stype = "person"
            snippet = (fv(n, "field_person_title_override") or fv(n, "field_person_working_title") or (people_view.get(m["alias"]) or {}).get("role") or "")[:140]
        else:
            stype = "abx" if m["nid"] in abx_nids else "page"
            snippet = text_only(fv(n, "field_body") or "")[:140]
            if stype == "abx":
                grid_titles = re.findall(r"<td[^>]*>\s*(?:<[^>]+>\s*)*([A-Z][a-z]+ [a-z]+[^<]{0,40})", fv(n, "field_body") or "")
                kw = [text_only(x) for x in grid_titles][:80]
        search.append({"t": stype, "n": m["title"], "u": route, "k": " ".join(dict.fromkeys(kw))[:600], "s": snippet})

    # ---- index pages
    def resolve_groups(key):
        out = []
        for g in (structure.get(key) or {}).get("groups") or []:
            items = []
            for path, text in g["links"]:
                nid = ctx.nid_by_path.get(path)
                if nid in models:
                    items.append(models[nid])
                else:
                    ctx.warnings.append({"structure": key, "path": path, "text": text, "notes": ["index link does not resolve to a mirrored node"]})
            if items:
                out.append((g["heading"], items))
        return out

    def empiric_index(key, popname, route, other_route, other_label):
        root = root_for(route)
        groups = resolve_groups(key)
        listed = {m["nid"] for _, items in groups for m in items}
        extra = [m for m in by_type["diagnosis"] if m["nid"] not in listed and (("pediatric" in (fv(m["raw"], "field_patient_population", "url") or "")) == (popname == "Pediatric"))]
        if extra:
            groups.append(("Other", sorted(extra, key=lambda x: x["title"])))
        jump = "".join(f'<a href="#g-{slugify(h or "other")}">{esc(h or "Other")}</a>' for h, _ in groups)
        cards = []
        for h, items in groups:
            lis = "".join(f'<li><a href="{root}{m["route"]}">{esc(m["title"])}</a></li>' for m in items)
            cards.append(f'<section class="group" id="g-{slugify(h or "other")}"><h2>{esc(h or "Other")}</h2><ul class="links">{lis}</ul></section>')
        content = f"""<div class="page-head"><p class="kicker">Empiric therapy</p><h1>{popname} empiric antimicrobial therapy</h1>
<p class="lede">Initial regimens by syndrome, from the {"UCSF Benioff Children's Hospitals" if popname == "Pediatric" else "UCSF Health"} antimicrobial stewardship programs. Each syndrome page shows pathogens, first-choice and alternative regimens, comments and duration as cards; tap any drug for its dosing. <a href="{root}{other_route}">Switch to {other_label} →</a></p>
<p class="muted">These recommendations assist clinical decision-making for common situations and cannot replace individualized evaluation, including history of multidrug-resistant organisms.</p></div>
<div class="filter"><input type="search" id="filter" placeholder="Filter syndromes…" aria-label="Filter list"></div>
<nav class="jump">{jump}</nav>
<div class="groups" id="groups">{"".join(cards)}</div>"""
        write(route, layout(ctx, root, f"{popname} empiric therapy", content, section="empiric",
                            crumbs=[("index.html", "Home"), (None, f"{popname} empiric therapy")]))
    empiric_index("empiric_adult", "Adult", "empiric/index.html", "empiric/peds.html", "pediatrics")
    empiric_index("empiric_peds", "Pediatric", "empiric/peds.html", "empiric/index.html", "adults")

    # drugs index
    route = "drugs/index.html"; root = root_for(route)
    rows = []
    for m in sorted(by_type["drug"], key=lambda x: x["title"].lower()):
        n = m["raw"]
        tags = []
        for item in n.get("field_notations") or []:
            slug = (item.get("url") or "").rsplit("/", 1)[-1]
            tags.append(slug)
        badges = "".join(f'<span class="badge tag t-{"zsfg" if "zsfg" in s else "ucsf" if "ucsf" in s else "ivpo" if "iv-po" in s else "misc"}">{esc(short_notation(s, tax_name("/notations/" + s)))}</span>' for s in tags)
        has_hd = bool(fv(n, "field_dosing_antimicrobial_dosin")) and (fv(n, "field_bool_hemodialysis") is not False)
        has_nd = bool(fv(n, "field_dosing")) and (fv(n, "field_bool_nondialysis") is not False)
        low = m["title"].lower()
        syn = [a for key, alts in synonyms.items() if not key.startswith("_") and key in low for a in alts]
        rows.append(f'<li data-tags="{esc(" ".join(tags))}{" hd" if has_hd else ""}{" nd" if has_nd else ""}" data-syn="{esc(" ".join(syn))}"><a href="{root}{m["route"]}">{esc(m["title"])}</a><span class="row-badges">{badges}{"<span class=badge>HD/CRRT</span>" if has_hd else ""}</span></li>')
    content = f"""<div class="page-head"><p class="kicker">Dosing</p><h1>Adult antimicrobial dosing</h1>
<p class="lede">Renal-function and dialysis dosing for {len(rows)} agents, one page per drug. Brand names and ward shorthand work in the filter (Zosyn, pip-tazo, vanc, Bactrim…).</p>
<p class="muted">Source guidance: dosing recommendations are based on available literature and do not replace clinical judgement; account for weight, renal function, pharmacokinetics, toxicity and disease state. Pediatric and neonatal dosing live under <a href="{root}peds.html">Pediatrics</a>.</p></div>
<div class="filter"><input type="search" id="filter" placeholder="Filter drugs… (brand names work)" aria-label="Filter list">
<div class="chips" id="chips"><button data-chip="id-r-ucsf">ID-restricted at UCSF</button><button data-chip="id-r-zsfg">ID-restricted at ZSFG</button><button data-chip="iv-po">IV → PO candidates</button><button data-chip="hd">Has HD/CRRT table</button></div></div>
<ul class="links az" id="list">{"".join(rows)}</ul>"""
    write(route, layout(ctx, root, "Adult antimicrobial dosing", content, section="drugs", crumbs=[("index.html", "Home"), (None, "Dosing")]))

    # guidelines index
    route = "guidelines/index.html"; root = root_for(route)
    groups = resolve_groups("guidelines")
    listed = {m["nid"] for _, items in groups for m in items}
    extra = [m for m in by_type["guidelines"] if m["nid"] not in listed]
    if extra:
        groups.append(("Other guidelines", sorted(extra, key=lambda x: x["title"])))
    cats = sorted({gmeta[m["nid"]]["category"] for m in by_type["guidelines"]})
    cards = []
    for h, items in groups:
        lis = []
        for m in sorted(items, key=lambda x: x["title"].lower()):
            gm = gmeta[m["nid"]]
            badges = "".join(f'<span class="badge site s-{s["group"]}" title="{esc(s["name"])}">{esc(s["short"])}</span>' for s in gm["sites"])
            kinds = "".join(f'<span class="badge kind k-{k}">{ {"pdf": "PDF", "login": "UCSF login", "inline": "Full text"}[k] }</span>' for k in gm["kinds"])
            date = f'<span class="gl-date">{esc(human_date(gm["latest"]))}</span>' if gm["latest"] else ""
            lis.append(f'<li data-sites="{esc(" ".join(sorted({s["group"] for s in gm["sites"]})))}" data-cat="{esc(gm["cat_slug"])}"><a href="{root}{m["route"]}">{esc(m["title"])}</a><span class="row-badges">{badges}{kinds}{date}</span>{("<p class=desc>" + esc(gm["desc"]) + "</p>") if gm["desc"] else ""}</li>')
        cards.append(f'<section class="group" data-cat="{slugify(h or "other")}"><h2>{esc(h or "Other")}</h2><ul class="links gl" >{"".join(lis)}</ul></section>')
    content = f"""<div class="page-head"><p class="kicker">Guidelines</p><h1>Institutional guidelines</h1>
<p class="lede">{len(by_type["guidelines"])} guidelines across UCSF Health, ZSFG, the VA and the Benioff Children's Hospitals. Filter by site with the switch in the header or the chips below. <span class="badge kind k-pdf">PDF</span> opens on idmp.ucsf.edu without login; <span class="badge kind k-login">UCSF login</span> means the document lives on Box or SharePoint.</p></div>
<div class="filter"><input type="search" id="filter" placeholder="Filter guidelines…" aria-label="Filter list">
<div class="chips" id="chips"><button data-chip="site:ucsf">UCSF Health</button><button data-chip="site:zsfg">ZSFG</button><button data-chip="site:va">VA</button><button data-chip="site:bch">BCH</button></div></div>
<div class="groups" id="groups">{"".join(cards)}</div>"""
    write(route, layout(ctx, root, "Guidelines", content, section="guidelines", crumbs=[("index.html", "Home"), (None, "Guidelines")]))

    # antibiograms index
    route = "antibiograms/index.html"; root = root_for(route)
    groups = resolve_groups("antibiograms")
    lis = []
    for h, items in groups:
        for m in items:
            body = fv(m["raw"], "field_body") or ""
            sub = []
            for href, txt in re.findall(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, flags=re.S):
                full = urljoin(BASE + "/", html.unescape(href)); path = urlparse(full).path.rstrip("/")
                t = text_only(txt)
                nid = ctx.nid_by_path.get(path)
                if nid in models:
                    sub.append(f'<a href="{root}{models[nid]["route"]}">{esc(t)}</a>')
                elif path.startswith(("/document/", "/sites/g/files/")):
                    sub.append(f'<a class="x-pdf" target="_blank" rel="noopener" href="{esc(full)}">{esc(t)} (PDF)</a>')
            lis.append(f'<li><a class="big" href="{root}{m["route"]}">{esc(m["title"])}</a><div class="sub">{" · ".join(dict.fromkeys(sub))}</div></li>')
    content = f"""<div class="page-head"><p class="kicker">Antibiograms</p><h1>Antibiograms</h1>
<p class="lede">Aggregate susceptibility reports by hospital. Tables published as HTML are re-rendered with shading by % susceptible; reports published only as PDF open on idmp.ucsf.edu.</p></div>
<ul class="links abx">{"".join(lis)}</ul>"""
    write(route, layout(ctx, root, "Antibiograms", content, section="antibiograms", crumbs=[("index.html", "Home"), (None, "Antibiograms")]))

    # peds hub
    route = "peds.html"; root = ""
    peds_pages = [m for m in models.values() if m["type"] == "page" and re.search(r"pediatric|neonatal|benioff|children|kocher|icn", m["title"] + m["alias"], flags=re.I) and m["nid"] not in abx_nids]
    peds_gl = [m for m in by_type["guidelines"] if all(s["group"] == "bch" for s in gmeta[m["nid"]]["sites"]) and gmeta[m["nid"]]["sites"]]
    def ul(items):
        return '<ul class="links">' + "".join(f'<li><a href="{root}{m["route"]}">{esc(m["title"])}</a></li>' for m in sorted(items, key=lambda x: x["title"].lower())) + "</ul>"
    content = f"""<div class="page-head"><p class="kicker">Pediatrics</p><h1>Pediatrics and neonatal</h1>
<p class="lede">Benioff Children's Hospitals content: empiric therapy by syndrome, the pediatric and neonatal dosing cards, and BCH-only guidelines.</p></div>
<section class="group"><h2>Start here</h2><ul class="links"><li><a class="big" href="empiric/peds.html">Pediatric empiric therapy by syndrome</a></li></ul></section>
<section class="group"><h2>Dosing cards and pediatric pages</h2>{ul(peds_pages)}</section>
<section class="group"><h2>Guidelines for BCH sites only</h2>{ul(peds_gl)}</section>"""
    write(route, layout(ctx, root, "Pediatrics", content, section="peds", crumbs=[("index.html", "Home"), (None, "Pediatrics")]))

    # people (person nodes, ordered and grouped as the source's directory view lists them)
    route = "people.html"; root = ""
    persons = [m for m in models.values() if m["type"] in ("ucsf_person", "other_person")]
    def porder(m):
        v = people_view.get(m["alias"]); return (0, v["order"]) if v else (1, m["title"])
    groups_p = defaultdict(list)
    for m in sorted(persons, key=porder):
        v = people_view.get(m["alias"]) or {}
        sub = fv(m["raw"], "field_person_subtype", "url") or ""
        groups_p[v.get("group") or (tax_name(sub) if sub else "IDMP")].append((m, v))
    secs = []
    for g, ms in groups_p.items():
        cards = ""
        for m, v in ms:
            n = m["raw"]
            role = fv(n, "field_person_title_override") or fv(n, "field_person_working_title") or v.get("role") or ""
            cards += (f'<li class="person">' + (f'<img loading="lazy" src="{esc(v["image"])}" alt="">' if v.get("image") else "") +
                      f'<div><a href="{m["route"]}"><strong>{esc(m["title"])}</strong></a><div class="muted">{esc(role)}</div></div></li>')
        secs.append(f'<section class="group"><h2>{esc(g)}</h2><ul class="people">{cards}</ul></section>')
    npubs = len(by_type["ucsf_publication"])
    content = (f'<div class="page-head"><p class="kicker">People</p><h1>IDMP team</h1><p class="lede">As listed on <a class="x-source" target="_blank" rel="noopener" href="{BASE}/people">idmp.ucsf.edu/people</a>. '
               + (f'See also <a href="publications.html">{npubs} publications</a> by the team.' if npubs else "") + '</p></div>' + "".join(secs))
    write(route, layout(ctx, root, "People", content, section="people", crumbs=[("index.html", "Home"), (None, "People")]))

    # publications
    route = "publications.html"; root = ""
    pubs = sorted(by_type["ucsf_publication"], key=lambda m: (fv(m["raw"], "field_publication_year") or "", m["title"]), reverse=True)
    items = []
    for m in pubs:
        n = m["raw"]
        cite = fv(n, "field_publication_title", "processed") or fv(n, "field_publication_title") or ""
        h, _ = render_rich(cite, ctx, root, m["slug"])
        pmid = fv(n, "field_publication_pubmedid")
        links = []
        if pmid:
            links.append(f'<a class="x-external" target="_blank" rel="noopener" href="https://pubmed.ncbi.nlm.nih.gov/{esc(pmid)}/">PubMed</a>')
        prof = fv(n, "field_publication_id")
        if prof and prof.startswith("http"):
            links.append(f'<a class="x-external" target="_blank" rel="noopener" href="{esc(prof)}">Profile</a>')
        items.append(f'<li id="p{m["nid"]}"><div class="cite">{h}</div><div class="muted">{esc(fv(n, "field_publication_year") or "")} · {" · ".join(links)}</div></li>')
        search.append({"t": "pub", "n": m["title"], "u": m["route"], "k": text_only(fv(n, "field_publication_authorlist", "processed") or "")[:300], "s": (fv(n, "field_publication_year") or "")})
    content = f'<div class="page-head"><p class="kicker">Publications</p><h1>Publications</h1><p class="lede">Papers listed on IDMP team members\' pages, newest first.</p></div><ul class="pubs">{"".join(items) or "<li class=muted>None listed.</li>"}</ul>'
    write(route, layout(ctx, root, "Publications", content, section="people", crumbs=[("index.html", "Home"), ("people.html", "People"), (None, "Publications")]))

    # changes
    route = "changes.html"; root = ""
    KIND = {"added": "New", "updated": "Updated", "removed": "Removed", "unlisted": "Unlisted", "relisted": "Relisted",
            "structure-updated": "Index changed", "nav-updated": "Navigation changed", "file-updated": "PDF replaced", "file-unreferenced": "PDF unlinked"}
    runs = []
    for run in reversed(changelog[-60:]):
        evs = []
        for e in run.get("events", []):
            kind = e.get("kind"); label = KIND.get(kind, kind)
            if "nid" in e and e["nid"] in models:
                link = f'<a href="{models[e["nid"]]["route"]}">{esc(e.get("title"))}</a>'
            elif "title" in e:
                link = esc(e.get("title"))
            elif "path" in e:
                link = f'<a class="x-pdf" target="_blank" rel="noopener" href="{esc(urljoin(BASE + "/", e["path"]))}">{esc(e["path"])}</a>'
            else:
                link = esc(e.get("structure") or json.dumps({k: v for k, v in e.items() if k != "kind"}))
            detail = ""
            if kind == "updated" and e.get("fields"):
                items = []
                for fchg in e["fields"]:
                    diff = "\n".join(fchg.get("diff") or [])
                    items.append(f'<div class="fchg"><code>{esc(fchg["field"])}</code>' + (f'<pre>{esc(diff)}</pre>' if diff else "") + "</div>")
                detail = f'<details><summary>{len(e["fields"])} field(s) changed</summary>{"".join(items)}</details>'
            elif kind == "updated":
                detail = f'<span class="muted">changed {esc(human_date(e.get("changed_from")))} → {esc(human_date(e.get("changed")))}</span>'
            evs.append(f'<li><span class="badge ev ev-{esc(kind)}">{esc(label)}</span> {link} <span class="muted">{esc(e.get("type") or "")}</span> {detail}</li>')
        probs = "".join(f'<li class="prob {esc(p["level"])}"><strong>{esc(p["level"])}</strong> {esc(p["code"])}: {esc(p["message"])}</li>' for p in run.get("problems", []))
        if evs or probs:
            runs.append(f'<section class="group run"><h2>{esc(human_date(run["run"]))} <span class="muted">{esc(run["run"][11:16])} UTC</span></h2>{("<ul class=probs>" + probs + "</ul>") if probs else ""}<ul class="events">{"".join(evs)}</ul></section>')
    st_badge = "ok" if status.get("ok") else "bad"
    st_msg = "clean" if status.get("ok") else f'{len(status.get("problems", []))} problem(s) reported'
    warn_list = ""
    if ctx.warnings:
        items = "".join(f'<li>{esc(w.get("title") or w.get("path") or "")} <span class="muted">{esc("; ".join(w["notes"]))}</span>' + (f' <a href="{w["route"]}">open</a>' if w.get("route") else "") + "</li>" for w in ctx.warnings)
        warn_list = f'<details class="block"><summary>{len(ctx.warnings)} layout fallback(s) in this build</summary><ul>{items}</ul></details>'
    content = f"""<div class="page-head"><p class="kicker">Sync log</p><h1>What changed on IDMP</h1>
<p class="lede">Every night the mirror re-reads idmp.ucsf.edu, compares each page's content and <code>changed</code> timestamp with the last copy, and records the difference here. Last run {esc(human_date(status.get("run")))}: <span class="badge st-{st_badge}">{esc(st_msg)}</span>.</p></div>
{("<ul class=probs>" + "".join(f'<li class="prob {esc(p["level"])}"><strong>{esc(p["level"])}</strong> {esc(p["code"])}: {esc(p["message"])}</li>' for p in status.get("problems", [])) + "</ul>") if status.get("problems") else ""}
{warn_list}
{"".join(runs) or "<p class=muted>No changes recorded yet: this is the first snapshot.</p>"}"""
    write(route, layout(ctx, root, "What changed", content, section="changes", crumbs=[("index.html", "Home"), (None, "What changed")]))

    # about
    route = "about.html"; root = ""
    about_nid = ctx.nid_by_path.get("/content/about")
    about_html = ""
    if about_nid in models:
        h, _ = render_rich(fv(models[about_nid]["raw"], "field_body") or "", ctx, root, "about")
        about_html = f'<section class="block prose"><h2>About the IDMP (from idmp.ucsf.edu)</h2>{h}</section>'
    counts = manifest.get("counts") or {}
    content = f"""<div class="page-head"><p class="kicker">About</p><h1>About this mirror</h1>
<p class="lede">{TAGLINE}</p></div>
<section class="block prose">
<h2>What it is</h2>
<p>{SITE_NAME} re-publishes the public content of the UCSF Infectious Diseases Management Program site (<a href="{BASE}" target="_blank" rel="noopener">idmp.ucsf.edu</a>) in a layout built for use on the wards: one search across everything, syndrome pages rendered as cards instead of six-column tables, one page per drug with the syndromes that recommend it, guidelines filterable by hospital, and antibiograms with shading. It is a personal project and is not affiliated with, endorsed by, or maintained by UCSF or the IDMP.</p>
<h2>How it stays current</h2>
<p>A scheduled job re-reads every source page nightly through the site's own content API, stores each page's <code>changed</code> timestamp and a content hash, and rebuilds only what moved. Every mirrored page shows the source's last-changed date and links back to the original. Structural surprises (a page type the mirror does not know, a missing field, a table whose columns cannot be recognised, a vanished index page) are recorded on the <a href="changes.html">What changed</a> page, flagged in the header sync indicator, and raised as an issue on the repository so the drift is never silent. Documents hosted on Box or SharePoint require a UCSF login and are linked, not copied.</p>
<h2>Current snapshot</h2>
<ul><li>{counts.get("diagnosis", 0)} empiric-therapy syndromes</li><li>{counts.get("drug", 0)} drug dosing pages</li><li>{counts.get("guidelines", 0)} guidelines</li><li>{counts.get("page", 0)} other pages, including antibiograms</li><li>{len(files)} PDFs tracked</li></ul>
<p class="muted">Sync run {esc(status.get("run") or "")} · repository <a href="https://github.com/{REPO}" target="_blank" rel="noopener">{REPO}</a></p>
</section>
{about_html}"""
    write(route, layout(ctx, root, "About", content, section="about", crumbs=[("index.html", "Home"), (None, "About")]))

    # home
    route = "index.html"; root = ""
    STARTS = [("/content/community-acquired-pneumonia-0", "CAP"), ("/content/pneumonia-hospital-acquired-or-ventilator-associated", "HAP / VAP"),
              ("/content/urinary-tract-infection-uti", "UTI"), ("/content/cellulitis-abscess-cutaneous-ulcer-disease-necrotizing-fasciitis", "Cellulitis / abscess"),
              ("/content/community-acquired-sepsis-with-unknown-source", "Sepsis, unknown source"), ("/content/high-risk-neutropenic-fever", "Neutropenic fever"),
              ("/content/meningitis", "Meningitis"), ("/content/abdominal-infections", "Intra-abdominal"), ("/content/clostridioides-difficile-infection-0", "C. difficile"),
              ("/content/allergy-beta-lactam", "Beta-lactam allergy"), ("/content/surgical-prophylaxis", "Surgical prophylaxis"), ("/content/vancomycin-initial-dosing-nomogram", "Vancomycin nomogram"),
              ("/piptazoextendedinfusion", "Pip-tazo extended infusion"), ("/content/code-sepsis", "Code Sepsis"), ("/content/zsfg-restricted-antimicrobials-workflow", "ZSFG restricted drugs"),
              ("/content/gram-negative-bloodstream-infection", "GN bacteremia IV→PO")]
    starts = []
    for path, label in STARTS:
        nid = ctx.nid_by_path.get(path)
        if nid in models:
            starts.append(f'<a class="chip-link" href="{models[nid]["route"]}">{esc(label)}</a>')
    recent = sorted((m for m in models.values() if m.get("changed")), key=lambda m: m["changed"], reverse=True)[:8]
    recent_html = "".join(f'<li><a href="{m["route"]}">{esc(m["title"])}</a> <span class="muted">{esc({"diagnosis": "empiric", "drug": "dosing", "guidelines": "guideline"}.get(m["type"], "page"))} · {esc(human_date(m["changed"]))}</span></li>' for m in recent)
    tiles = [("empiric/index.html", "Adult empiric therapy", f'{len([m for m in by_type["diagnosis"] if "pediatric" not in (fv(m["raw"], "field_patient_population", "url") or "")])} syndromes · pathogens, first choice, alternatives, duration'),
             ("drugs/index.html", "Dosing", f'{counts.get("drug", 0)} drugs · renal bands, HD and CRRT, restrictions'),
             ("guidelines/index.html", "Guidelines by site", f'{counts.get("guidelines", 0)} guidelines · UCSF Health, ZSFG, VA, BCH'),
             ("antibiograms/index.html", "Antibiograms", "local susceptibility by hospital, shaded"),
             ("peds.html", "Pediatrics", "empiric therapy, dosing cards, neonatal"),
             ("changes.html", "What changed", "nightly diff against idmp.ucsf.edu")]
    tiles_html = "".join(f'<a class="tile" href="{h}"><strong>{esc(t)}</strong><span>{esc(d)}</span></a>' for h, t, d in tiles)
    banner = ""
    if status and not status.get("ok"):
        banner = f'<div class="notice warn"><strong>Heads up:</strong> the last sync ({esc(human_date(status.get("run")))}) reported problems, so some content may be stale or shown in fallback layout. <a href="changes.html">Details</a>.</div>'
    content = f"""{banner}
<section class="hero"><p class="kicker">Unofficial mirror of idmp.ucsf.edu</p><h1>Antimicrobial guidance, one search away.</h1>
<p class="lede">{TAGLINE} Search a syndrome, a drug (brand names work), a guideline or an organism, or start from a section below. Pick your hospital in the header to bring its guidelines and restrictions forward.</p></section>
<section class="tiles">{tiles_html}</section>
<section class="block"><h2>Common starting points</h2><div class="chip-links">{"".join(starts)}</div></section>
<section class="block two"><div><h2>Most recently edited on IDMP</h2><ul class="rel-list">{recent_html or "<li class=muted>Nothing recorded yet.</li>"}</ul></div>
<div><h2>How to read this site</h2><ul class="rel-list"><li>Every page shows the source's own last-changed date and links to the original.</li><li>Drug names inside syndrome pages open a dosing drawer; the page you came from stays put.</li><li><span class="badge tag t-ucsf">ID-R · UCSF</span> and <span class="badge tag t-zsfg">ID-R · ZSFG</span> mark ID-restricted agents; <span class="badge kind k-login">UCSF login</span> marks Box/SharePoint documents.</li><li>Press <kbd>/</kbd> to search from anywhere.</li></ul></div></section>"""
    write(route, layout(ctx, root, "Home", content, section="home"))

    # ---- search index, status, images
    with open(os.path.join(OUT, "search.json"), "w", encoding="utf-8") as f:
        json.dump(search, f, ensure_ascii=False, separators=(",", ":"))
    st = dict(status or {})
    st["built"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    st["build_warnings"] = len(ctx.warnings)
    st["counts"] = manifest.get("counts")
    with open(os.path.join(OUT, "status.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    for name, raw in ctx.images.items():
        with open(os.path.join(OUT, "img", name), "wb") as f:
            f.write(raw)
    # ---- prune stale generated pages
    for sub in ("empiric", "drugs", "guidelines", "antibiograms", "pages", "people"):
        d = os.path.join(OUT, sub)
        for fn in os.listdir(d):
            r = f"{sub}/{fn}"
            if r not in written and fn.endswith(".html"):
                os.remove(os.path.join(d, fn))
    with open(os.path.join(DATA, "build-warnings.json"), "w", encoding="utf-8") as f:
        json.dump(ctx.warnings, f, indent=1, ensure_ascii=False)
    print(f"built {len(written)} pages, {len(search)} search entries, {len(ctx.images)} images, {len(ctx.warnings)} layout warnings")
    return 0

if __name__ == "__main__":
    sys.exit(main())
