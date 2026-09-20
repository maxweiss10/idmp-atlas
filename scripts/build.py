#!/usr/bin/env python3
"""
build.py - render data/ (from sync.py) into docs/, a static site for GitHub Pages.

Design: organise around the bedside question, not the source document.
  * one Ask palette that returns answers (a regimen row, a dose cell, a susceptibility number)
  * three persistent lenses: Where (hospital), Setting (outpatient / inpatient / ICU), Patient (age, renal, allergy)
  * syndrome pages as a chooser strip + one regimen card, with a synthesized "prescription line"
    whose doses come from the same drug's IDMP dosing table (labelled as such)
  * one page per drug with a renal dial, restriction workflow per site, and back-links
  * antibiograms parsed into a bug-drug explorer
  * every page carries the source URL and the source's own `changed` timestamp
"""
from __future__ import annotations
import base64, hashlib, html, json, os, re, sys
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
TAGLINE = "UCSF Infectious Diseases Management Program guidance, rebuilt around the question you have at the bedside."

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

INI_STOP = {"and", "or", "of", "in", "the", "with", "for", "a", "an", "to", "at", "on", "no", "without", "other"}
def initialism(title):
    """CAP from 'Community-Acquired Pneumonia'. Only for 2-4 significant words, so it stays a real acronym."""
    words = [w for w in re.split(r"[^A-Za-z]+", re.sub(r"\(.*?\)", " ", title)) if w and w.lower() not in INI_STOP]
    if 2 <= len(words) <= 4:
        ini = "".join(w[0] for w in words).lower()
        if 2 <= len(ini) <= 5:
            return ini
    return None

def human_date(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d, %Y")
    except Exception:
        return iso[:10]

def text_only(fragment):
    return re.sub(r"\s+", " ", BeautifulSoup(fragment or "", "lxml").get_text(" ")).strip()

def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

# ----------------------------------------------------------------------------- sanitizer + links
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
        self.status = {}
        self.asset_ver = ""

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
                if not m:
                    el.decompose(); continue
                ext = "jpg" if m.group(1).startswith("jp") else m.group(1)
                raw = base64.b64decode(m.group(2))
                name = hashlib.sha1(raw).hexdigest()[:12] + "." + ext
                ctx.images[name] = raw
                src = root + "img/" + name
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
            entry = {"html": inner_html(cell), "text": cell.get_text(" ", strip=True).replace("\xa0", " "), "th": cell.name == "th"}
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

def parse_band(header):
    """Turn a renal-function column header into a machine-readable band, or None.
    Handles 'CrCl > 50 mL/min', '10 - 29 mL/min', '< 10mL/min', 'CrCl ≥ 30', 'eGFR >50mL/min/1.73m2',
    'Intermittent Hemodialysis', 'Continuous Hemodialysis (CVVHD)'."""
    t = header.replace("\xa0", " ").strip()
    low = t.lower()
    if re.search(r"intermittent", low) and re.search(r"hemodialysis|\bhd\b", low):
        return {"kind": "hd", "label": t}
    if re.search(r"continuous|crrt|cvvh", low):
        return {"kind": "crrt", "label": t}
    if re.search(r"hd or crrt", low):
        return {"kind": "hd", "label": t, "also": "crrt"}
    if re.search(r"criteria|increase|target|equation|dose|dosing|note|comment|peak|trough|weight|age", low):
        return None
    n = low
    n = n.replace("–", "-").replace("—", "-").replace("≥", ">=").replace("≤", "<=")
    n = re.sub(r">\s*or\s*=", ">=", n); n = re.sub(r"<\s*or\s*=", "<=", n)
    n = re.sub(r">\s*=", ">=", n); n = re.sub(r"<\s*=", "<=", n)
    n = re.sub(r"crcl|egfr|clcr|ml/min(/1\.73\s*m2)?|ml/mi\s*n|/1\.73m2", " ", n)
    n = re.sub(r"\s+", "", n)
    m = re.fullmatch(r"(>=|>)(\d+)", n)
    if m:
        return {"kind": "crcl", "lo": int(m.group(2)), "loi": m.group(1) == ">=", "hi": None, "hii": False, "label": t}
    m = re.fullmatch(r"(<=|<)(\d+)", n)
    if m:
        return {"kind": "crcl", "lo": None, "loi": False, "hi": int(m.group(2)), "hii": m.group(1) == "<=", "label": t}
    m = re.fullmatch(r"(\d+)-(\d+)", n)
    if m:
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        return {"kind": "crcl", "lo": a, "loi": True, "hi": b, "hii": True, "label": t}
    return None

def render_plain_table(grid, heat=False, bands=False):
    """bands=True offers renal-band tagging; the class is only applied when a real band is recognised,
    so prose tables keep prose styling."""
    hdr = header_row_index(grid)
    band_attrs, skip_cols = {}, set()
    if hdr is not None:
        for c_i, c in enumerate(grid[hdr]):
            if heat and re.search(r"isolate|organism|^n$|number|total", c["text"], flags=re.I):
                skip_cols.add(c_i)
            if bands and c_i > 0:
                b = parse_band(c["text"])
                if b:
                    band_attrs[c_i] = b
    use_bands = bool(band_attrs)
    parts = ['<div class="tbl-wrap"><table class="tbl' + (" heat" if heat else "") + (" bands" if use_bands else "") + '">']
    for r_i, row in enumerate(grid):
        tag = "th" if (hdr == r_i) or all(c["th"] for c in row) else "td"
        parts.append("<tr>")
        for c_i, c in enumerate(row):
            if c.get("dup"):
                continue
            cls, extra = "", ""
            if heat and tag == "td" and c_i not in skip_cols and c_i > 0:
                m = re.match(r"^\s*[<>≤≥]?\s*(\d{1,3})\s*%?", c["text"])
                if m:
                    v = int(m.group(1))
                    cls = "s-hi" if v >= 90 else "s-ok" if v >= 80 else "s-mid" if v >= 60 else "s-lo"
                elif re.match(r"^\s*R\s*$", c["text"]):
                    cls = "s-r"
            if c.get("from_above"):
                cls = (cls + " from-above").strip()
            if c_i in band_attrs:
                b = band_attrs[c_i]
                extra = f' data-col="{c_i}" data-kind="{b["kind"]}"'
                if b["kind"] == "crcl":
                    extra += f' data-lo="{"" if b["lo"] is None else b["lo"]}" data-loi="{int(b["loi"])}" data-hi="{"" if b["hi"] is None else b["hi"]}" data-hii="{int(b["hii"])}"'
                if b.get("also"):
                    extra += f' data-also="{b["also"]}"'
            elif use_bands:
                extra = f' data-col="{c_i}"'
            attrs = (f' class="{cls}"' if cls else "") + extra
            parts.append(f"<{tag}{attrs}>{c['html']}</{tag}>")
        parts.append("</tr>")
    parts.append("</table></div>")
    return "".join(parts)


# ----------------------------------------------------------------------------- icons
# Inline SVG, stroked with currentColor. No emoji: those render differently (or not at
# all) outside macOS, and several of the ones that fit here have no cross-platform glyph.
ICON_SPRITE = """<svg class="sprite" aria-hidden="true" focusable="false">
<defs>
<symbol id="i-iv" viewBox="0 0 24 24"><path d="M8.5 3h7v5.5l-3.5 4.5-3.5-4.5V3zM8.5 6h7"/><path d="M12 13v8"/></symbol>
<symbol id="i-po" viewBox="0 0 24 24"><rect x="3" y="8.5" width="18" height="7" rx="3.5"/><path d="M12 8.8v6.4"/></symbol>
<symbol id="i-renal" viewBox="0 0 24 24"><path d="M9.2 4C5.6 4 3.5 7.2 3.5 11.4c0 4.6 2.7 8.2 6 8.2 2.1 0 2.5-1.7 2.7-3.3.2-1.4.6-2.6 1.8-3.3"/><path d="M14.8 4c3.6 0 5.7 3.2 5.7 7.4 0 4.6-2.7 8.2-6 8.2"/></symbol>
<symbol id="i-weight" viewBox="0 0 24 24"><path d="M12 4.5v15M5 8h14"/><path d="M8 8 5 14.5h6L8 8zM16 8l-3 6.5h6L16 8z"/></symbol>
<symbol id="i-gap" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.6v5.2M12 16.2v.1"/></symbol>
<symbol id="i-lock" viewBox="0 0 24 24"><rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></symbol>
<symbol id="i-restrict" viewBox="0 0 24 24"><path d="M12 3.2 20 6v6.2c0 4.3-3.2 7.4-8 8.6-4.8-1.2-8-4.3-8-8.6V6l8-2.8z"/><path d="m9 15 6-6"/></symbol>
<symbol id="i-branch" viewBox="0 0 24 24"><path d="M12 3v5"/><path d="M5 21v-5a3 3 0 0 1 3-3h8a3 3 0 0 1 3 3v5"/><path d="M12 13v8"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5.5v13M5.5 12h13"/></symbol>
<symbol id="i-or" viewBox="0 0 24 24"><path d="M6 12h12"/><path d="m14 8 4 4-4 4"/></symbol>
<symbol id="i-ext" viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-8.5 8.5"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></symbol>
<symbol id="i-doc" viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6V3z"/><path d="M14 3v4h4M9 12h6M9 16h6"/></symbol>
</defs></svg>"""

def icon(name, cls=""):
    return f'<svg class="ic {cls}" aria-hidden="true"><use href="#i-{name}"/></svg>'

def dose_marks(dose_text, band_count=1, restricted=False):
    """Marks that change a decision: route, renal band count, weight-based, restriction.
    Nothing decorative. Read from the dose string itself, never assumed."""
    t = (dose_text or "").upper()
    out = []
    iv = re.search(r"\bIV\b|INFUS", t)
    po = re.search(r"\bPO\b|ORAL|\bNG\b", t)
    if iv and po:
        out.append(f'<span class="mk mk-both" title="Interchangeable IV and oral: a step-down candidate">{icon("iv")}{icon("po")}</span>')
    elif iv:
        out.append(f'<span class="mk" title="Intravenous">{icon("iv")}</span>')
    elif po:
        out.append(f'<span class="mk" title="Oral">{icon("po")}</span>')
    if re.search(r"MG/KG|/KG\b|G/KG", t):
        out.append(f'<span class="mk" title="Weight-based: the number shown is per kilogram">{icon("weight")}</span>')
    if band_count > 1:
        out.append(f'<span class="mk mk-renal" title="Renal-function dependent: {band_count} bands published">{icon("renal")}<b>{band_count}</b></span>')
    if restricted:
        out.append(f'<span class="mk mk-restrict" title="ID approval required at your selected hospital">{icon("restrict")}</span>')
    return "".join(out)

# ----------------------------------------------------------------------------- therapy rows
def therapy_rows(fragment, ctx, root, page_slug):
    """Sanitize a diagnosis field and split it into a document of parts:
    ('html', str) for prose, ('rows', [row...]) for each therapy table, ('table', html) for other tables."""
    clean = sanitize(fragment, ctx, root, page_slug)
    if not clean:
        return []
    soup = BeautifulSoup(clean, "lxml")
    body = soup.body
    if body is None:
        return [("html", clean)]
    parts, notes = [], []
    for child in list(body.contents):
        if isinstance(child, NavigableString):
            if str(child).strip():
                parts.append(("html", f"<p>{esc(str(child).strip())}</p>"))
            continue
        if child.name == "table":
            grid = expand_grid(child)
            hdr = header_row_index(grid)
            if hdr == 0 and len(grid) > 1:
                cols = map_columns([c["text"] for c in grid[0]])
                if "first" in cols:
                    rows = []
                    for r in grid[1:]:
                        real = [c for c in r if not c.get("dup")]
                        if len(real) == 1 and len(cols) > 1 and real[0]["text"]:
                            # one cell spanning the table: a heading for the rows beneath it
                            rows.append({"__group__": real[0]["text"]})
                            continue
                        cells = {}
                        for c_i, key in enumerate(cols):
                            if c_i < len(r) and not r[c_i].get("dup"):
                                cells[key] = r[c_i]
                        if any(v["text"] for v in cells.values()):
                            rows.append(cells)
                    parts.append(("rows", rows))
                    continue
            if hdr is None and len(grid) > 1 and len(grid[0]) >= 4:
                notes.append("info: a table without a header row is shown in its original layout")
            parts.append(("table", render_plain_table(grid)))
        else:
            parts.append(("html", str(child)))
    if notes:
        parts.append(("notes", notes))
    return parts

DOSE_RX = re.compile(r"\d+(\.\d+)?\s*(mg|g|mcg|µg|units?|million|iu|ml)\b", re.I)

def regimen_tree(cell_html):
    """Turn a 'Drug(s) of first choice' cell into a small tree:
       lead prose, then steps joined by PLUS, each step holding one or more OR alternatives.
       'Vancomycin PLUS one of: Ceftriaxone OR Pip-tazo OR Ertapenem' -> two steps."""
    soup = BeautifulSoup(cell_html or "", "lxml")
    body = soup.body
    if body is None:
        return {"lead": "", "steps": [], "tail": ""}
    tokens = []
    def walk(node):
        for ch in node.children:
            if isinstance(ch, NavigableString):
                if not isinstance(ch, Comment):
                    tokens.append(("t", str(ch)))
            elif ch.name == "a" and ch.get("data-drug"):
                tokens.append(("d", ch.get("data-drug"), ch.get_text(" ", strip=True), ch.get("href")))
            elif ch.name in ("p", "li", "br", "div"):
                tokens.append(("t", " \n "))
                walk(ch)
            else:
                walk(ch)
    walk(body)
    idxs = [i for i, t in enumerate(tokens) if t[0] == "d"]
    if not idxs:
        return {"lead": "", "steps": [], "tail": ""}
    def between(a, b):
        return re.sub(r"\s+", " ", "".join(t[1] for t in tokens[a:b] if t[0] == "t")).strip()
    lead = between(0, idxs[0]).strip(" :;,-")
    raw_tail = between(idxs[-1] + 1, len(tokens)).strip()
    steps, cur = [], []
    for k, i in enumerate(idxs):
        nxt = idxs[k + 1] if k + 1 < len(idxs) else len(tokens)
        after = between(i + 1, nxt)
        # a trailing parenthetical or short clause belongs to this drug, not the next one
        note = ""
        m = re.match(r"^\s*(\([^)]{2,90}\)|(?:if|for|when|unless)\b[^.;]{2,90})", after, flags=re.I)
        if m:
            note = m.group(1).strip("() ")
            after = after[m.end():]
        cur.append({"slug": tokens[i][1], "name": tokens[i][2], "href": tokens[i][3], "note": note,
                    "inline_dose": bool(DOSE_RX.search(tokens[i][2])) or bool(DOSE_RX.search(after[:70]))})
        low = after.lower()
        if k + 1 >= len(idxs):
            steps.append({"drugs": cur, "optional": False})
            cur = []
            break
        optional = bool(re.search(r"with or without|\+/-|±|may add|optional", low))
        is_or = bool(re.search(r"\bor\b|\beither\b", low)) and not re.search(r"\bplus\b|\band\b(?! ?/ ?or)|\bfollowed by\b", low)
        if is_or:
            continue  # same step: these are alternatives to each other
        steps.append({"drugs": cur, "optional": optional})
        cur = []
    if cur:
        steps.append({"drugs": cur, "optional": False})
    # a lead like "PLUS one of:" belongs to the join, not the prose
    lead = re.sub(r"^(plus|and)\b\s*", "", lead, flags=re.I).strip(" :;,-")
    if re.fullmatch(r"(one of|any one of|either)?", lead, flags=re.I):
        lead = ""
    last_note = steps[-1]["drugs"][-1]["note"] if steps and steps[-1]["drugs"] else ""
    tail = raw_tail
    if last_note and re.sub(r"\W+", "", last_note.lower()) in re.sub(r"\W+", "", raw_tail.lower()):
        tail = ""
    return {"lead": lead, "steps": steps, "tail": tail.strip(" ()")}

def drug_dose_pointer(node, ctx, title=""):
    """IDMP does not tabulate every drug. When it does not, say where the dose lives and
    what you have to decide to pick the right one. Never a bare link, never 'see page'."""
    v = (node.get("field_dosing") or [{}])[0].get("value") or ""
    if not v.strip():
        return None
    soup = BeautifulSoup(v, "lxml")
    if soup.find("table"):
        return None
    links = []
    for a_ in soup.find_all("a"):
        label = re.sub(r"\s+", " ", a_.get_text(" ", strip=True)).strip(" .:")
        if not label:
            continue
        href, kind, _nid = rewrite_href(a_.get("href"), ctx, "")
        where = "on this site" if kind == "internal" else ("Box or SharePoint, UCSF login" if kind == "login" else "external")
        site = ""
        m = re.match(r"^(ZSFG|UCSF|VASF|SFVA|BCH)\b[:\s-]*", label, flags=re.I)
        if m:
            site = m.group(1).upper()
            label = label[m.end():].strip() or label
        links.append({"t": label[:70], "u": href, "x": kind, "where": where, "site": site})
    prose = []
    for para in soup.find_all(["p", "li"]):
        t = re.sub(r"\s+", " ", para.get_text(" ", strip=True))
        if len(t) > 12:
            prose.append(t)
    sites = [l["site"] for l in links if l["site"]]
    if sites:
        decision = "Pick by hospital: " + ", ".join(dict.fromkeys(sites)) + "."
    elif len(links) > 1:
        decision = "More than one source is published. Pick the one your hospital uses."
    elif links:
        decision = "One source is published for this drug."
    else:
        decision = "No link is published. Ask ID or ASP pharmacy."
    return {"note": (prose[0][:190] if prose else ""), "links": links[:4], "decision": decision,
            "title": title}

DOSE_STOP = {"the", "and", "or", "of", "in", "for", "with", "a", "an", "to", "at", "on", "infection", "infections",
             "including", "dosing", "dose", "standard", "usual", "all", "other", "adult", "adults", "therapy", "treatment",
             "acute", "severe", "non", "suspected", "documented"}
def dose_tokens(x):
    out = set()
    for w in re.findall(r"[a-z]{4,}", (x or "").lower()):
        if w in DOSE_STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 5 else w)
    return out

def dose_line(dd, row_index, band_index, matched, restricted=False):
    """One line: dose, route, frequency, exactly as IDMP publishes it."""
    if row_index >= len(dd["rows"]):
        row_index = 0
    r = dd["rows"][row_index]
    txt = r["d"][band_index] if band_index < len(r["d"]) else ""
    if not txt:
        return "", ""
    marks = dose_marks(txt, len(dd["bands"]), restricted)
    label = ""
    if matched and r["i"]:
        label = f'<span class="dl-ind">for {esc(r["i"][:46].rsplit(" ", 1)[0] if len(r["i"]) > 46 else r["i"])}</span>'
    elif r["i"] and not re.match(r"^(standard|usual|general|all|normal)", r["i"], flags=re.I) and len(dd["rows"]) > 1:
        label = f'<span class="dl-ind">{esc(r["i"][:46])}</span>'
    band = dd["bands"][band_index] if band_index < len(dd["bands"]) else None
    bl = ""
    if band and len(dd["bands"]) > 1:
        bl = f'<span class="dl-band">{esc(band.get("label", ""))}</span>'
    return f'<span class="dl"><span class="dl-d">{esc(txt)}</span>{marks}</span>{label}{bl}', txt

def band_table(dd, row_index, band_index):
    """All published renal bands for the row on show. Inline, never behind a hover."""
    if len(dd["bands"]) < 2 or row_index >= len(dd["rows"]):
        return ""
    r = dd["rows"][row_index]
    cells = ""
    for i, b in enumerate(dd["bands"]):
        v = r["d"][i] if i < len(r["d"]) else ""
        on = " on" if i == band_index else ""
        cells += f'<div class="bt-c{on}"><span class="bt-k">{esc(b.get("label", ""))}</span><span class="bt-v">{esc(v or "not published")}</span></div>'
    return f'<div class="bt" aria-label="All published renal bands">{cells}</div>'

def other_indications(dd, row_index):
    names = [r["i"] for i, r in enumerate(dd["rows"]) if i != row_index and r["i"]]
    if not names:
        return ""
    def clip(x, n=40):
        x = x.strip()
        if len(x) <= n:
            return x
        cut = x[:n].rsplit(" ", 1)[0]
        if cut.count("(") > cut.count(")"):        # never end on an open bracket
            cut = cut[:cut.rfind("(")].rstrip(" ,;")
        return cut + "…"
    return f'<span class="dl-other">Also published for: {esc("; ".join(clip(n) for n in names[:3]))}</span>'

def pick_dose_row(dd, context):
    """Choose the dosing row that matches the syndrome, else the standard/first row."""
    ctxt = dose_tokens(context)
    best, best_score = 0, 0
    for i, r in enumerate(dd["rows"]):
        score = len(ctxt & dose_tokens(r["i"]))
        if score > best_score:
            best, best_score = i, score
    if best_score:
        return best, True
    for i, r in enumerate(dd["rows"]):
        if not r["i"] or re.match(r"^(standard|usual|general|all|normal)", r["i"], flags=re.I):
            return i, False
    return 0, False

def dose_strip_html(dd, band_index, row_index=0, matched=False):
    if row_index >= len(dd["rows"]):
        row_index = 0
    r = dd["rows"][row_index]
    d = r["d"][band_index] if band_index < len(r["d"]) else ""
    if not d:
        return "see page"
    label = ""
    if matched and r["i"]:
        label = f'<b>{esc(r["i"][:38])}</b> '
    elif r["i"] and not re.match(r"^(standard|usual|general|all|normal)", r["i"], flags=re.I) and len(dd["rows"]) > 1:
        label = f'<b>{esc(r["i"][:38])}</b> '
    more = f'<i class="more">+{len(dd["rows"]) - 1} more</i>' if len(dd["rows"]) > 1 else ""
    return label + esc(d) + more

def drug_dose_data_hd(node):
    """HD / CRRT dosing rows, so the palette can answer 'vanc hd' with numbers."""
    v = (node.get("field_dosing_antimicrobial_dosin") or [{}])[0].get("value") or ""
    if not v:
        return None
    soup = BeautifulSoup(v, "lxml")
    for t in soup.find_all("table"):
        grid = expand_grid(t)
        if header_row_index(grid) != 0 or len(grid) < 2:
            continue
        hdrs = [c["text"] for c in grid[0]]
        bands = []
        for i, h in enumerate(hdrs[1:], start=1):
            b = parse_band(h)
            if b and b["kind"] in ("hd", "crrt"):
                bands.append({"col": i, "kind": b["kind"], "label": b["label"]})
        if not bands:
            di = next((i for i, h in enumerate(hdrs) if re.search(r"^dos", h, flags=re.I)), None)
            if di is None:
                continue
            bands = [{"col": di, "kind": "hd", "label": hdrs[di]}]
        rows = []
        for r in grid[1:]:
            ind = re.sub(r"\*+$", "", r[0]["text"]).strip() if r else ""
            doses = [re.sub(r"\s+", " ", r[b["col"]]["text"]).strip() if b["col"] < len(r) else "" for b in bands]
            if any(doses):
                rows.append({"i": ind, "d": doses})
        if rows:
            return {"bands": [{"kind": b["kind"], "label": b["label"]} for b in bands], "rows": rows}
    return None

def drug_dose_data(node):
    """Compact dosing data for one drug: bands + first rows, used by regimen lines and the palette."""
    v = (node.get("field_dosing") or [{}])[0].get("value") or ""
    if not v:
        return None
    soup = BeautifulSoup(v, "lxml")
    for t in soup.find_all("table"):
        grid = expand_grid(t)
        if header_row_index(grid) != 0 or len(grid) < 2:
            continue
        hdrs = [c["text"] for c in grid[0]]
        bands = []
        for i, h in enumerate(hdrs[1:], start=1):
            b = parse_band(h)
            if b and b["kind"] == "crcl":
                bands.append({"col": i, **{k: b[k] for k in ("lo", "loi", "hi", "hii", "label")}})
        cols = [b["col"] for b in bands]
        if not cols:
            # single dose column table (Indication | Dose | Notes)
            di = next((i for i, h in enumerate(hdrs) if re.search(r"^dos", h, flags=re.I)), None)
            if di is None:
                continue
            cols = [di]
            bands = [{"col": di, "lo": None, "loi": False, "hi": None, "hii": False, "label": hdrs[di]}]
        rows = []
        for r in grid[1:]:
            ind = re.sub(r"\*+$", "", r[0]["text"]).strip() if r else ""
            doses = [re.sub(r"\s+", " ", r[c]["text"]).strip() if c < len(r) else "" for c in cols]
            if any(doses):
                rows.append({"i": ind, "d": doses})
        if rows:
            return {"bands": bands, "rows": rows}
    return None

# ----------------------------------------------------------------------------- misc helpers
SITE_GROUPS = {"ucsf": "UCSF Health", "zsfg": "ZSFG", "va": "VA", "bch": "BCH"}
def site_group(slug):
    s = slug.lower()
    if "zuckerberg" in s or "zsfg" in s: return "zsfg"
    if "veteran" in s or s.startswith("va") or "-va-" in s: return "va"
    if "benioff" in s or "children" in s: return "bch"
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

def short_notation(slug, label):
    if "zsfg" in slug: return "ID-R ZSFG"
    if "ucsf" in slug: return "ID-R UCSF"
    if "iv-po" in slug: return "IV to PO"
    return label

CUT_RX = re.compile(r"\s+(?:If\b|This\b|These\b|Refer\b|See\b|Defined\b|Diagnosed\b|Characterized\b|Includes?\b|Need for\b|Usually\b|With known\b|Note:|\(e\.g|\(i\.e|Please\b|For full\b|Recommend)", re.I)
def short_label(page_title, label, limit=46):
    """One-line label for the chooser: drop the explanatory sentence and any repeat of the page title."""
    t = re.sub(r"\s+", " ", label or "").strip(" .;:,")
    t = re.sub(r"\b([\w-]{4,})\s+\1\b", r"\1", t, flags=re.I)   # "non-severe Non-severe" -> "non-severe"
    m = CUT_RX.search(t)
    if m and m.start() > 12 and (len(t) > limit or not m.group(0).lstrip().startswith("(")):
        t = t[:m.start()].strip(" .;:,-")
    pt = re.sub(r"[^a-z0-9]+", " ", (page_title or "").lower()).strip()
    tl = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
    if pt and tl.startswith(pt) and len(tl) > len(pt) + 3:
        t = t[len(t) - (len(tl) - len(pt)):].strip(" ,;:-–—")
        t = t[0].upper() + t[1:] if t else t
    if len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0]
        t = (cut if len(cut) > limit * 0.6 else t[:limit]).rstrip(" ,;:-") + "…"
    return t or (label or "")[:limit]

def outline_of(html_fragment):
    soup = BeautifulSoup(html_fragment or "", "lxml")
    items = []
    for i, h in enumerate(soup.find_all(["h2", "h3"])):
        t = h.get_text(" ", strip=True)
        if not t or len(t) > 90:
            continue
        hid = f"s-{slugify(t)[:40]}-{i}"
        h["id"] = hid
        items.append((h.name, hid, t))
    body = soup.body
    return (body.decode_contents() if body else html_fragment), items

# ----------------------------------------------------------------------------- layout
SECTIONS = [
    ("empiric", "Empiric therapy", "empiric/index.html"),
    ("drugs", "Dosing", "drugs/index.html"),
    ("antibiograms", "Antibiograms", "antibiograms/index.html"),
    ("guidelines", "Guidelines & policies", "guidelines/index.html"),
    ("reference", "Reference", "about.html"),
]

def nav_html(tree, section, current_route, root):
    """The persistent index. The current section is expanded inline so it works without
    JS; other sections are headers that expand from nav.json on click."""
    out = ['<nav class="idx" aria-label="Contents">']
    out.append('<div class="idx-filter"><input type="search" id="idx-q" placeholder="Filter this index" aria-label="Filter the index" autocomplete="off"></div>')
    for key, label, href in SECTIONS:
        on = key == section
        groups = tree.get(key) or []
        count = sum(len(g["items"]) for g in groups)
        # the lens hides the population the clinician excluded, so the badge has to count per population
        n_adult = sum(len(g["items"]) for g in groups if g.get("pop") != "Pediatric")
        n_peds = sum(len(g["items"]) for g in groups if g.get("pop") != "Adult")
        out.append(f'<div class="idx-sec{" on" if on else ""}" data-sec="{key}">')
        out.append(f'<div class="idx-sec-head"><a href="{root}{href}">{esc(label)}</a>'
                   + (f'<button type="button" class="idx-toggle" aria-expanded="{"true" if on else "false"}" aria-label="Show {esc(label)} contents">'
                      f'<span class="idx-count" data-n-adult="{n_adult}" data-n-peds="{n_peds}">{count}</span></button>' if groups else "") + "</div>")
        if groups:
            out.append('<div class="idx-body"' + ("" if on else " hidden") + ">")
            if on:
                for g in groups:
                    gp = f' data-pop="{g["pop"].lower()}"' if g.get("pop") else ""
                    out.append(f'<div class="idx-grp"{gp}>')
                    if g.get("label"):
                        pop = f'<span class="idx-pop">{esc(g["pop"])}</span>' if g.get("pop") else ""
                        out.append(f'<div class="idx-grp-h">{pop}{esc(g["label"])}</div>')
                    out.append("<ul>")
                    for it in g["items"]:
                        cur = ' aria-current="page"' if it["u"] == current_route else ""
                        out.append(f'<li><a href="{root}{it["u"]}"{cur}>{esc(it["t"])}</a></li>')
                    out.append("</ul></div>")
            out.append("</div>")
        out.append("</div>")
    out.append("</nav>")
    return "".join(out)

def pager_html(prev_item, next_item, root):
    if not prev_item and not next_item:
        return ""
    bits = ['<nav class="pager" aria-label="Within this group">']
    if prev_item:
        bits.append(f'<a class="pg pg-prev" href="{root}{prev_item["u"]}"><span>Previous</span><b>{esc(prev_item["t"])}</b></a>')
    else:
        bits.append("<span></span>")
    if next_item:
        bits.append(f'<a class="pg pg-next" href="{root}{next_item["u"]}"><span>Next</span><b>{esc(next_item["t"])}</b></a>')
    bits.append("</nav>")
    return "".join(bits)

def layout(ctx, root, title, content, *, desc="", node=None, section="", extra_head="", crumbs=None,
           fallback_notes=None, rail="", nav="", pager="", page_class="", kicker="", h1="", meta_bar=""):
    ver = ctx.asset_ver
    status = ctx.status or {}
    crumb_html = ""
    if crumbs:
        crumb_html = '<nav class="crumbs" aria-label="Breadcrumb">' + "".join(
            (f'<a href="{root}{h}">{esc(t)}</a>' if h else f"<span>{esc(t)}</span>") for h, t in crumbs) + "</nav>"
    warn_html = ""
    if fallback_notes:
        warn_html = ('<div class="note note-warn"><b>Layout note</b> Part of this page did not match the layout the '
                     'mirror expects, so it is shown in its original form. ' + esc("; ".join(fallback_notes)) + '.</div>')
    src_html = ""
    if node:
        src_html = (f'<div class="prov">'
                    f'<span class="prov-i"><span class="prov-k">Source</span>'
                    f'<a target="_blank" rel="noopener" href="{esc(node["source_url"])}">idmp.ucsf.edu</a></span>'
                    f'<span class="prov-i"><span class="prov-k">Revised</span>'
                    f'<time datetime="{esc(node["changed"])}">{esc(human_date(node["changed"]))}</time></span>'
                    f'<button class="pin" data-fav="{esc(node["route"])}" data-title="{esc(node["title"])}" data-kind="{esc(node["type"])}">Pin</button></div>')
    head_block = ""
    if h1:
        head_block = (f'<header class="doc-head">{crumb_html}'
                      + (f'<p class="kicker-line">{kicker}</p>' if kicker else "")
                      + f'<h1>{esc(h1)}</h1>{meta_bar}{src_html}</header>')
    elif crumb_html:
        head_block = crumb_html
    return f"""<!doctype html>
<html lang="en" data-root="{root}" data-section="{esc(section)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(title)} · {SITE_NAME}</title>
<meta name="description" content="{esc(desc or TAGLINE)}">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#FFFCF0" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#100F0F" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{SITE_NAME}">
<link rel="manifest" href="{root}manifest.webmanifest">
<link rel="icon" href="{root}assets/icon-192.png">
<link rel="apple-touch-icon" href="{root}assets/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,300..700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<link rel="stylesheet" href="{root}assets/site.css?v={ver}">
<script>try{{var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t);var L=JSON.parse(localStorage.getItem('lens')||'{{}}');for(var k in L)if(L[k])document.documentElement.setAttribute('data-'+k,L[k]);if(localStorage.getItem('idx')==='0')document.documentElement.setAttribute('data-idx','0');}}catch(e){{}}</script>
{extra_head}
</head>
<body class="{esc(page_class)}">
{ICON_SPRITE}
<a class="skip" href="#doc">Skip to content</a>
<div class="app">
  <header class="bar">
    <button class="bar-menu" id="idx-open" type="button" aria-label="Contents" aria-expanded="false" aria-controls="idx-wrap"><span></span><span></span><span></span></button>
    <a class="mark" href="{root}index.html"><img src="{root}assets/icon-192.png" alt="" width="20" height="20"><span class="mark-n">IDMP Atlas</span><span class="mark-s">unofficial mirror</span></a>
    <button class="ask" id="ask-open" type="button"><span class="ask-i">/</span><span class="ask-t">Ask<i>cap icu</i><i>cefepime crcl 30</i><i>e coli cipro</i></span><kbd>⌘K</kbd></button>
    <div class="bar-end">
      <button class="lens-btn" id="lens-open" type="button" title="Where / Setting / Patient"><span id="lens-summary">All sites, any setting, adult</span></button>
      <a class="sync" id="status" href="{root}changes.html" title="Sync status">sync</a>
      <button class="icon-btn" id="theme" type="button" aria-label="Toggle dark mode">◐</button>
    </div>
  </header>
  <aside class="idx-wrap" id="idx-wrap">{nav}</aside>
  <main class="doc{" has-rail" if rail else ""}" id="doc">
    <div class="doc-in">
      {warn_html}
      {head_block}
      {content}
      {pager}
      <footer class="doc-foot"><p><b>{SITE_NAME}</b> is an unofficial, read-only mirror of <a href="{BASE}" target="_blank" rel="noopener">idmp.ucsf.edu</a>, rebuilt nightly. Not affiliated with UCSF or the IDMP. Content belongs to its authors; confirm against the source before acting. Last verified <time datetime="{esc(status.get('run',''))}">{esc(human_date(status.get('run')))}</time>. <span class="foot-l"><a href="{root}about.html">About</a><a href="https://github.com/{REPO}" target="_blank" rel="noopener">Source</a><button class="linklike" id="offline-btn" type="button">Save offline</button></span></p></footer>
    </div>
    {f'<aside class="meta">{rail}</aside>' if rail else ""}
  </main>
</div>
<nav class="tabs" aria-label="Quick navigation">
  <button type="button" data-open="palette"><i>/</i>Ask</button>
  <button type="button" id="idx-open-2"><i>≡</i>Index</button>
  <a href="{root}empiric/index.html"><i>Rx</i>Empiric</a>
  <a href="{root}drugs/index.html"><i>mg</i>Dosing</a>
  <a href="{root}antibiograms/explore.html"><i>%</i>Bugs</a>
</nav>
<div class="ovl" id="palette" hidden><div class="pal" role="dialog" aria-label="Search">
  <div class="pal-in"><span class="pal-i">/</span><input type="search" id="q" placeholder="Syndrome, drug + CrCl, organism + drug, guideline…" autocomplete="off" aria-label="Search"><button class="esc" data-close type="button">esc</button></div>
  <div class="pal-list" id="results"></div>
  <div class="pal-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span><span class="grow"></span><span class="pal-try">Try<i>hap zsfg</i><i>vanc hd</i><i>pseudomonas cefepime</i></span></div>
</div></div>
<div class="ovl" id="lens" hidden><div class="sheet" role="dialog" aria-label="Context">
  <div class="sheet-h"><b>Your context</b><span>Applied everywhere, remembered on this device.</span><button class="esc" data-close type="button">done</button></div>
  <div class="sheet-b">
    <div class="fld"><label>Where</label><div class="seg" data-lens="where"><button data-v="">All</button><button data-v="ucsf">UCSF Health</button><button data-v="zsfg">ZSFG</button><button data-v="va">VA</button><button data-v="bch">BCH</button></div></div>
    <div class="fld"><label>Setting</label><div class="seg" data-lens="setting"><button data-v="">Any</button><button data-v="outpatient">Outpatient</button><button data-v="inpatient">Inpatient</button><button data-v="icu">ICU</button></div></div>
    <div class="fld"><label>Patient</label><div class="seg" data-lens="patient"><button data-v="">Adult</button><button data-v="peds">Pediatric</button></div></div>
    <div class="fld"><label>Renal</label><div class="renal"><input type="number" id="lens-crcl" inputmode="numeric" min="0" max="250" placeholder="CrCl"><span class="u">mL/min</span><div class="seg" data-lens="renal"><button data-v="">Any</button><button data-v="hd">HD</button><button data-v="crrt">CRRT</button></div></div></div>
    <div class="fld"><label>Allergy</label><div class="seg" data-lens="allergy"><button data-v="">None</button><button data-v="bl">Severe beta-lactam</button></div></div>
  </div>
  <div class="sheet-f"><button type="button" id="lens-reset" class="linklike">Reset</button><span>Lenses change what is shown first, never what the source says.</span></div>
</div></div>
<div class="drawer" id="drawer" hidden><div class="drawer-bar"><span class="grip"></span><button class="drawer-back" id="drawer-back" type="button" hidden>Back</button><a class="drawer-open" id="drawer-open" href="#">Open full page</a><button class="esc" id="drawer-close" type="button" aria-label="Close">esc</button></div><div class="drawer-body" id="drawer-body"></div></div>
<div class="scrim" id="scrim" hidden></div>
<div class="toast" id="toast" hidden></div>
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
        synonyms = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    with open(os.path.join(ROOT, "scripts", "curation.json"), encoding="utf-8") as f:
        curation = json.load(f)
    with open(os.path.join(ROOT, "scripts", "aliases.json"), encoding="utf-8") as f:
        ward_aliases = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    ctx = Ctx()
    ctx.status = status
    build_time = datetime.now(timezone.utc).replace(microsecond=0)

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
    def tax_name(url):
        return taxonomy.get(url) or url.rsplit("/", 1)[-1].replace("-", " ")

    # ---- alias map, antibiogram set, routes
    for nid, n in nodes.items():
        alias = fv(n, "path", "alias") or f"/node/{nid}"
        ctx.nid_by_path[alias] = nid
        ctx.nid_by_path[f"/node/{nid}"] = nid
    for nid_s, path in (structure.get("node_paths") or {}).items():
        if int(nid_s) in nodes:
            ctx.nid_by_path[path] = int(nid_s)
    for canonical, aliases in (structure.get("page_aliases") or {}).items():
        nid = ctx.nid_by_path.get(canonical)
        if nid is not None:
            for a in aliases:
                ctx.nid_by_path.setdefault(a, nid)
    abx_nids = set()
    for g in (structure.get("antibiograms") or {}).get("groups") or []:
        for path, _ in g["links"]:
            nid = ctx.nid_by_path.get(path)
            if nid in nodes and nodes[nid]["type"][0]["target_id"] == "page":
                abx_nids.add(nid)
    for nid in list(abx_nids):
        for href in re.findall(r'href="([^"]+)"', fv(nodes[nid], "field_body") or ""):
            n2 = ctx.nid_by_path.get(urlparse(urljoin(BASE + "/", html.unescape(href))).path.rstrip("/"))
            if n2 in nodes and nodes[n2]["type"][0]["target_id"] == "page":
                abx_nids.add(n2)
    for nid, n in nodes.items():
        t = (fv(n, "title") or "").lower()
        if n["type"][0]["target_id"] == "page" and ("susceptib" in t or "antibiogram" in t):
            abx_nids.add(nid)
    used_slugs, models = set(), {}
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
    by_type = defaultdict(list)
    for m in models.values():
        by_type[m["type"]].append(m)
    drug_by_slug = {m["slug"]: m for m in by_type["drug"]}
    dose_route = {m["slug"]: m["route"] for m in by_type["drug"]}
    drug_titles = {m["slug"]: m["title"] for m in by_type["drug"]}
    def is_peds(m):
        return "pediatric" in (fv(m["raw"], "field_patient_population", "url") or "")
    def route_for_alias(alias_with_hash, where=None):
        path, _, frag = alias_with_hash.partition("#")
        nid = ctx.nid_by_path.get(path)
        if nid in models:
            return models[nid]["route"] + (f"#{frag}" if frag else "")
        if where:
            ctx.warnings.append({"title": path, "notes": [f"curated link in {where} no longer resolves on IDMP; it was dropped from the page"]})
        return None

    # ---- assets
    os.makedirs(OUT, exist_ok=True)
    for sub in ("assets", "img", "empiric", "drugs", "guidelines", "antibiograms", "pages", "people", "thumbs"):
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)
    flexoki = open(os.path.join(SITE, "assets", "vendor", "flexoki.css"), encoding="utf-8").read()
    css = ("/* Flexoki by kepano - MIT. Vendored verbatim from github.com/kepano/flexoki\n"
           "   See assets/vendor/NOTICE.md and assets/vendor/LICENSE-flexoki. */\n"
           + flexoki + "\n" + open(os.path.join(SITE, "assets", "site.css"), encoding="utf-8").read())
    js = open(os.path.join(SITE, "assets", "site.js"), encoding="utf-8").read().replace("__REPO__", REPO)
    ctx.asset_ver = hashlib.sha1((css + js).encode()).hexdigest()[:8]
    open(os.path.join(OUT, "assets", "site.css"), "w", encoding="utf-8").write(css)
    open(os.path.join(OUT, "assets", "site.js"), "w", encoding="utf-8").write(js)
    for fn in os.listdir(os.path.join(SITE, "assets")):
        if fn.endswith(".png"):
            with open(os.path.join(SITE, "assets", fn), "rb") as src, open(os.path.join(OUT, "assets", fn), "wb") as dst:
                dst.write(src.read())
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

    search = []
    dose_data_all = {m["slug"]: drug_dose_data(m["raw"]) for m in by_type["drug"]}
    dose_data_all = {k: v for k, v in dose_data_all.items() if v}
    dose_hd_all = {m["slug"]: drug_dose_data_hd(m["raw"]) for m in by_type["drug"]}
    dose_hd_all = {k: v for k, v in dose_hd_all.items() if v}
    # drugs IDMP does not tabulate (vancomycin, for one): keep the pointer to where the dose lives
    dose_ptr_all = {m["slug"]: drug_dose_pointer(m["raw"], ctx, m["title"]) for m in by_type["drug"] if m["slug"] not in dose_data_all}
    dose_ptr_all = {k: v for k, v in dose_ptr_all.items() if v}
    drug_tags = {}
    for m in by_type["drug"]:
        drug_tags[m["slug"]] = [(i.get("url") or "").rsplit("/", 1)[-1] for i in (m["raw"].get("field_notations") or [])]

    # ---- cross references
    drug_used_in = defaultdict(list)
    dx_linked_guidelines = defaultdict(list)
    for m in models.values():
        n = m["raw"]
        body = {"diagnosis": (fv(n, "field_dosing") or "") + (fv(n, "field_notes") or ""), "guidelines": fv(n, "body") or "",
                "page": fv(n, "field_body") or ""}.get(m["type"], "")
        if not body:
            continue
        seen = set()
        for href in re.findall(r'href="([^"]+)"', body):
            nid = ctx.nid_by_path.get(urlparse(urljoin(BASE + "/", html.unescape(href))).path.rstrip("/"))
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
        thumb = None
        pages = None
        for url, _ in pdfs:
            fm = files.get(urlparse(urljoin(BASE + "/", url)).path) or {}
            if fm.get("thumb"):
                thumb, pages = fm["thumb"], fm.get("pages")
                break
        if not thumb:
            for href in re.findall(r'href="([^"]*(?:/document/|/sites/g/files/)[^"]*)"', body):
                fm = files.get(urlparse(urljoin(BASE + "/", html.unescape(href))).path) or {}
                if fm.get("thumb"):
                    thumb, pages = fm["thumb"], fm.get("pages"); break
        return {"sites": sites, "category": category, "cat_slug": slugify(category), "dates": dates,
                "latest": dates[-1] if dates else None, "kinds": kinds, "desc": desc, "pdfs": pdfs, "thumb": thumb, "pages": pages}
    gmeta = {m["nid"]: guideline_meta(m) for m in by_type["guidelines"]}
    KIND_LABEL = {"pdf": "PDF", "login": "UCSF login", "inline": "Full text"}
    def site_badges(sites):
        return "".join(f'<span class="badge site s-{s["group"]}" title="{esc(s["name"])}">{esc(s["short"])}</span>' for s in sites)
    def kind_badges(kinds):
        return "".join(f'<span class="badge kind k-{k}">{KIND_LABEL[k]}</span>' for k in kinds)

    # ---- the index tree: one entry per section, grouped the way a manual's index would be
    def is_peds_gl(m):
        gs = gmeta[m["nid"]]["sites"]
        return bool(gs) and all(x["group"] == "bch" for x in gs)

    def dx_groups(peds):
        key = "empiric_peds" if peds else "empiric_adult"
        seen, groups = set(), []
        for g in (structure.get(key) or {}).get("groups") or []:
            items = []
            for path, _ in g["links"]:
                nid = ctx.nid_by_path.get(path)
                if nid in models and nid not in seen:
                    seen.add(nid)
                    items.append({"t": models[nid]["title"], "u": models[nid]["route"]})
            if items:
                groups.append({"label": g["heading"] or "Other", "items": items})
        rest = [m for m in by_type["diagnosis"] if m["nid"] not in seen and is_peds(m) == peds]
        if rest:
            groups.append({"label": "Other", "items": [{"t": m["title"], "u": m["route"]} for m in sorted(rest, key=lambda x: x["title"])]})
        return groups

    def letter_groups(items):
        out, cur, letter = [], [], None
        for m in items:
            l = m["title"][:1].upper()
            if l != letter:
                if cur:
                    out.append({"label": letter, "items": cur})
                letter, cur = l, []
            cur.append({"t": m["title"], "u": m["route"]})
        if cur:
            out.append({"label": letter, "items": cur})
        return out

    peds_pages_all = [m for m in models.values() if m["type"] == "page"
                      and re.search(r"pediatric|neonatal|benioff|children|kocher|icn", m["title"] + m["alias"], flags=re.I)
                      and m["nid"] not in abx_nids]
    policy_items = []
    for skey, sc in curation["sites"].items():
        for k in ("restricted", "allergy", "ivpo"):
            r = route_for_alias(sc[k]) if sc.get(k) else None
            nid = ctx.nid_by_path.get((sc.get(k) or "").split("#")[0])
            if r and nid in models and not any(x["u"] == r for x in policy_items):
                policy_items.append({"t": models[nid]["title"], "u": r})
    gl_by_cat = defaultdict(list)
    for m in by_type["guidelines"]:
        gl_by_cat[gmeta[m["nid"]]["category"]].append(m)
    abx_items = [{"t": models[n]["title"], "u": models[n]["route"]} for n in sorted(abx_nids) if n in models]

    tree = {
        "empiric": [{"label": g["label"], "pop": "Adult", "items": g["items"]} for g in dx_groups(False)]
                 + [{"label": g["label"], "pop": "Pediatric", "items": g["items"]} for g in dx_groups(True)],
        "drugs": letter_groups(sorted(by_type["drug"], key=lambda x: x["title"].lower()))
               + ([{"label": "Pediatric & neonatal", "pop": "Pediatric",
                    "items": [{"t": m["title"], "u": m["route"]} for m in sorted(peds_pages_all, key=lambda x: x["title"])]}] if peds_pages_all else []),
        "antibiograms": [{"label": "Explore", "items": [{"t": "Bug-drug explorer", "u": "antibiograms/explore.html"}]},
                         {"label": "Reports", "items": sorted(abx_items, key=lambda x: x["t"])}],
        "guidelines": [{"label": cat, "items": [{"t": m["title"], "u": m["route"]} for m in sorted(ms, key=lambda x: x["title"].lower())]}
                       for cat, ms in sorted(gl_by_cat.items())]
                    + ([{"label": "Policies by hospital", "items": policy_items}] if policy_items else []),
        "reference": [{"label": "", "items": [{"t": "What changed on IDMP", "u": "changes.html"},
                                              {"t": "IDMP team", "u": "people.html"},
                                              {"t": "Publications", "u": "publications.html"},
                                              {"t": "About this mirror", "u": "about.html"}]}],
    }
    # flat order for previous/next inside a group
    flat = {}
    for skey, groups in tree.items():
        for g in groups:
            for i, it in enumerate(g["items"]):
                flat.setdefault(it["u"], (g["items"], i))
    def pager_for(route, root):
        pair = flat.get(route)
        if not pair:
            return ""
        items, i = pair
        return pager_html(items[i - 1] if i > 0 else None, items[i + 1] if i + 1 < len(items) else None, root)
    def nav_for(section, route, root):
        return nav_html(tree, section, route, root)

    # ---- related guidelines (title-token overlap; transparent and conservative)
    STOP = {"the", "and", "or", "of", "in", "for", "with", "a", "an", "to", "at", "on", "guideline", "guidelines", "guidance",
            "treatment", "management", "adult", "adults", "pediatric", "pediatrics", "patients", "patient", "infection", "infections",
            "ucsf", "zsfg", "vasf", "sfva", "sf", "bch", "hospital", "hospitals", "no", "not", "other", "any", "unknown", "source",
            "empiric", "therapy", "algorithm", "protocol", "clinical", "disease", "acute", "suspected", "being", "evaluated", "person", "who"}
    ACR = {"hap": ["hospital-acquired", "pneumonia"], "vap": ["ventilator", "pneumonia"], "cap": ["community-acquired", "pneumonia"],
           "cdi": ["difficile"], "ssti": ["skin", "soft", "tissue"], "uti": ["urinary", "tract"], "nsti": ["necrotizing", "soft", "tissue"],
           "iai": ["intra-abdominal", "abdominal"], "sbp": ["peritonitis"], "bsi": ["bloodstream"], "pid": ["pelvic", "inflammatory"],
           "sti": ["sexually", "transmitted"], "copd": ["bronchitis"], "ltu": ["liver", "transplant"], "esld": ["liver"],
           "hsv": ["herpes"], "cmv": ["cytomegalovirus"], "rsv": ["respiratory", "syncytial"], "tb": ["tuberculosis"], "opat": ["outpatient", "parenteral"]}
    def toks(s):
        out = set()
        for w in re.findall(r"[a-z0-9][a-z0-9'-]*", s.lower()):
            w = w.strip("'-")
            if not w or w in STOP or (len(w) < 3 and w not in ACR):
                continue
            out.add(w)
            for x in ACR.get(w, []):
                out.add(x)
            if w.endswith("s") and len(w) > 4:
                out.add(w[:-1])
        return out
    group_of_dx = {}
    for key in ("empiric_adult", "empiric_peds"):
        for g in (structure.get(key) or {}).get("groups") or []:
            for p, _ in g["links"]:
                nid = ctx.nid_by_path.get(p)
                if nid:
                    group_of_dx[nid] = g["heading"] or ""
    def related_guidelines(m):
        mine = toks(m["title"]) | toks(group_of_dx.get(m["nid"], ""))
        peds = is_peds(m)
        scored = []
        for g in by_type["guidelines"]:
            common = mine & toks(g["title"])
            if not common:
                continue
            score = len(common)
            gs = gmeta[g["nid"]]["sites"]
            g_is_peds = bool(gs) and all(s["group"] == "bch" for s in gs)
            if peds != g_is_peds:
                score -= 0.5
            scored.append((score, g))
        scored.sort(key=lambda x: (-x[0], x[1]["title"]))
        out = list(dx_linked_guidelines.get(m["nid"], []))
        for s, g in scored:
            if s >= 1 and g not in out:
                out.append(g)
        return out[:6]

    # ---- settings curation
    def row_tags(slug, idx, label, total):
        cur = curation["settings"].get(slug)
        tags = set()
        if cur:
            if cur.get("n") is not None and cur["n"] != total:
                ctx.warnings.append({"title": slug, "route": f"empiric/{slug}.html", "notes": [f"info: curated setting tags skipped, row count changed ({cur["n"]} to {total})"]})
                cur = {"default": cur.get("default", [])}
            tags |= set(cur.get("rows", {}).get(str(idx), []) or cur.get("default", []))
        if not tags:
            l = label.lower()
            if re.search(r"outpatient|clinic|ambulatory|uncomplicated|\bmild\b|no comorbid|discharge", l): tags.add("outpatient")
            if re.search(r"\bicu\b|intensive|(?<!non-)(?<!non )severe|septic shock|critical|life-?threatening", l): tags |= {"inpatient", "icu"}
            if re.search(r"\bward\b|admitted|inpatient|hospitali|healthcare|nosocomial|ventilator|hospital-acquired", l): tags.add("inpatient")
        if "icu" in tags:
            tags.add("inpatient")
        return sorted(tags)

    # ---- antibiogram parsing
    abx_tables = []
    for nid in sorted(abx_nids):
        m = models[nid]
        body = fv(m["raw"], "field_body") or ""
        soup = BeautifulSoup(body, "lxml")
        legend = {}
        for p in soup.find_all("p"):
            for abbr, name in re.findall(r"([A-Z][A-Z/]{1,9})\s*-\s*([a-z][a-z/\-]+(?: [a-z/\-]+)*)", p.get_text(" ")):
                legend.setdefault(abbr, name.strip())
        heading = soup.find(["h1", "h2"])
        heading_t = heading.get_text(" ", strip=True) if heading else m["title"]
        heading_t = re.sub(r"\s*\d\s*$", "", heading_t)
        ym = re.search(r"(20\d\d)", m["alias"] + " " + heading_t)
        year = ym.group(1) if ym else None
        for ti, t in enumerate(soup.find_all("table")):
            grid = expand_grid(t)
            if not grid or not re.search(r"organism", grid[0][0]["text"], flags=re.I):
                continue
            hdr = [re.sub(r"\s*\d+\s*$", "", c["text"]).strip() for c in grid[0]]
            drugs = []
            for i, h in enumerate(hdr):
                if i == 0 or re.search(r"isolate|total", h, flags=re.I):
                    continue
                parts = [x.strip() for x in h.split("/")]
                names = [legend.get(x) for x in parts]
                name = legend.get(h) or "/".join(n for n in names if n) or h
                drugs.append({"col": i, "abbr": h, "name": name})
            rows = []
            for r in grid[1:]:
                org = re.sub(r"\s*\d+\s*$", "", r[0]["text"]).strip()
                if not org:
                    continue
                n_iso = None
                for i, h in enumerate(hdr):
                    if re.search(r"isolate|total", h, flags=re.I) and i < len(r):
                        mm = re.search(r"\d+", r[i]["text"]); n_iso = int(mm.group(0)) if mm else None
                vals = {}
                for d in drugs:
                    raw = r[d["col"]]["text"] if d["col"] < len(r) else ""
                    mm = re.match(r"^\s*[<>≤≥]?\s*(\d{1,3})", raw)
                    vals[d["abbr"]] = {"v": int(mm.group(1)) if mm else None, "raw": raw.strip()}
                rows.append({"organism": org, "n": n_iso, "v": vals})
            abx_tables.append({"key": f"{m['slug']}-{ti}", "title": heading_t, "page": m["title"], "route": m["route"], "year": year,
                               "drugs": [{"abbr": d["abbr"], "name": d["name"]} for d in drugs], "rows": rows, "changed": m["changed"]})
    os.makedirs(os.path.join(DATA, "antibiograms"), exist_ok=True)
    for t in abx_tables:
        fn = os.path.join(DATA, "antibiograms", f"{t['key']}-{t['year'] or 'undated'}.json")
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(t, f, indent=1, ensure_ascii=False)
    # drug name -> slug for linking antibiogram columns
    def _dnorm(x):
        return re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()
    def drug_slug_for_name(name):
        """Map an antibiogram column name to a drug page. Separator-insensitive, and a
        combination column (imipenem-cilastatin/meropenem) matches on any of its parts."""
        cands = [_dnorm(name)] + [_dnorm(p) for p in re.split(r"[/,]| or ", name or "") if _dnorm(p)]
        for cand in cands:
            if len(cand) < 4:
                continue
            best, best_rank = None, 0
            for slug, title in drug_titles.items():
                tl, head = _dnorm(title), _dnorm(title.split(" (")[0])
                rank = 3 if cand in (tl, head) else 2 if cand in tl else 1 if len(head) > 5 and head in cand else 0
                if rank and (rank > best_rank or (rank == best_rank and best and len(title) < len(drug_titles[best]))):
                    best, best_rank = slug, rank
            if best:
                return best
        return None
    for t in abx_tables:
        for d in t["drugs"]:
            d["slug"] = drug_slug_for_name(d["name"]) if d["name"] else None

    # ---- per-node body rendering
    # IDMP writes many regimens as prose with no link. Recognise drug names in that prose so
    # the reader can still reach a dose. Names only: never inject a dose into prose, because
    # the prose may already carry one or may modify the regimen.
    name_index = []
    for _m in by_type["drug"]:
        head = _m["title"].split(" (")[0].strip()
        cands = {head}
        _par = re.search(r"\(([^)]+)\)", _m["title"])
        if _par:
            cands.add(_par.group(1).strip())
        low = _m["title"].lower()
        for _k, _alts in synonyms.items():
            if _k in low:
                cands.update(a for a in _alts if len(a) >= 6)
        for c in cands:
            if len(c) >= 5 and not re.search(r"\d", c):
                name_index.append((c, _m["slug"], _m["route"]))
    name_index.sort(key=lambda x: -len(x[0]))
    NAME_RX = re.compile(r"(?<![\w-])(" + "|".join(re.escape(c) for c, _s, _r in name_index) + r")(?![\w-])", re.I) if name_index else None
    NAME_TO = {c.lower(): (sl, rt) for c, sl, rt in name_index}

    def linkify_drugs(html_str, root):
        if not html_str or not NAME_RX:
            return html_str, []
        soup = BeautifulSoup(html_str, "lxml")
        found = []
        for node in list(soup.find_all(string=True)):
            if node.find_parent("a") or not node.strip():
                continue
            out, last, parts = [], 0, NAME_RX.finditer(str(node))
            txt = str(node)
            for mt in parts:
                sl, rt = NAME_TO.get(mt.group(1).lower(), (None, None))
                if not sl:
                    continue
                out.append(txt[last:mt.start()])
                out.append(f'<a data-drug="{esc(sl)}" href="{root}{esc(rt)}">{esc(mt.group(1))}</a>')
                last = mt.end()
                found.append(sl)
            if not found or last == 0:
                continue
            out.append(txt[last:])
            node.replace_with(BeautifulSoup("".join(out), "lxml").body or BeautifulSoup("", "lxml"))
        body = soup.body
        return (body.decode_contents() if body else html_str), found

    def regimen_column(cell_html, context, root, tone, gaps, where, drop_lead=False):
        """One regimen as a vertical stack of steps. PLUS and OR are drawn, not written."""
        tree = regimen_tree(cell_html)
        if not tree["steps"]:
            # IDMP states this regimen as prose. Show it at regimen scale rather than a blank
            # box, with any drug it names linked through to its dosing page.
            if not text_only(cell_html):
                return "", {}
            linked, names = linkify_drugs(cell_html, root)
            hint = ""
            if names:
                uniq = list(dict.fromkeys(names))
                hint = (f'<p class="rg-hint">{icon("gap")}IDMP writes this one as prose. '
                        f'{esc("Dosing for " + ", ".join(drug_titles.get(x, x) for x in uniq[:4]))} '
                        f'is on {"its" if len(uniq) == 1 else "their"} own page, linked above.</p>')
            return f'<div class="rg-prose">{linked}</div>{hint}', {}
        used, out = {}, []
        for si, step in enumerate(tree["steps"]):
            if si:
                out.append('<div class="jn">' + icon("plus")
                           + ('<span>with or without</span>' if step["optional"] else "") + "</div>")
            multi = len(step["drugs"]) > 1
            out.append(f'<div class="stp{" stp-any" if multi else ""}">')
            if multi:
                out.append('<div class="stp-k">any one of</div>')
            for di, d in enumerate(step["drugs"]):
                if di:
                    out.append(f'<div class="orx">{icon("or")}<span>or</span></div>')
                slug = d["slug"]
                title = drug_titles.get(slug, d["name"])
                dd = dose_data_all.get(slug)
                rtags = [x for x in drug_tags.get(slug, []) if x.startswith("id-r-")]
                restricted = bool(rtags)
                body = ""
                if dd:
                    ri, matched = pick_dose_row(dd, context)
                    used[slug] = dd
                    line, _raw = dose_line(dd, ri, 0, matched, restricted)
                    body = (f'<div class="dose" data-dose="{esc(slug)}" data-row="{ri}" data-matched="{int(matched)}">{line}</div>'
                            + band_table(dd, ri, 0) + other_indications(dd, ri))
                elif d["inline_dose"]:
                    body = '<div class="dose dose-inline">Dose is stated by IDMP in the row below</div>'
                else:
                    ptr = dose_ptr_all.get(slug)
                    if ptr:
                        bits = []
                        for l in ptr["links"]:
                            tgt = ' target="_blank" rel="noopener"' if l["x"] != "internal" else ""
                            href = (root + l["u"]) if l["x"] == "internal" else l["u"]
                            mark = icon("lock") if l["x"] == "login" else (icon("ext") if l["x"] != "internal" else "")
                            pre = f'<b>{esc(l["site"])}</b> ' if l["site"] else ""
                            bits.append(f'<li>{pre}<a href="{esc(href)}"{tgt}>{esc(l["t"])}</a>{mark}<span>{esc(l["where"])}</span></li>')
                        body = (f'<div class="gap">{icon("gap")}<div><b>No dose table on IDMP.</b> '
                                + (esc(ptr["note"]) + " " if ptr["note"] else "")
                                + f'<i>{esc(ptr["decision"])}</i>'
                                + (f'<ul class="gap-l">{"".join(bits)}</ul>' if bits else "") + "</div></div>")
                    else:
                        gaps.append({"drug": title, "slug": slug})
                        body = (f'<div class="gap gap-bad">{icon("gap")}<div><b>No dose published on IDMP and no pointer.</b> '
                                f'Ask ID or ASP pharmacy. <a href="{root}{esc(dose_route.get(slug, ""))}">Drug page</a></div></div>')
                if rtags:
                    sites = ", ".join(t.replace("id-r-", "").upper() for t in sorted(rtags))
                    body += (f'<div class="restr" data-sites="{esc(" ".join(t.replace("id-r-", "") for t in rtags))}">'
                             f'{icon("restrict")}<span>ID approval needed at {esc(sites)}</span></div>')
                out.append(f'<div class="rgd"><a class="rgd-n" data-drug="{esc(slug)}" href="{esc(d["href"])}">{esc(title)}</a>'
                           + (f'<span class="rgd-note">{esc(d["note"])}</span>' if d["note"] else "") + body + "</div>")
            out.append("</div>")
        lead = "" if drop_lead else (f'<p class="rgc-lead">{esc(tree["lead"])}</p>' if tree["lead"] else "")
        tail = f'<p class="rgc-tail">{esc(tree["tail"])}</p>' if tree["tail"] and len(tree["tail"]) > 3 else ""
        return lead + "".join(out) + tail, used

    ORG_MAP = curation.get("organisms") or {}
    def coverage_grid(pathogen_html, slugs, root):
        """Local susceptibility for the agents in this regimen, from the parsed UCSF
        antibiograms. Syndromes often name a family, so curation.json carries a taxonomy
        map from family to the species the antibiogram actually reports. That map is
        naming, not pharmacology: nothing about coverage is inferred here."""
        raw = [re.sub(r"\s+", " ", x).strip(" .,;()")
               for x in re.split(r"<[^>]+>|\n", pathogen_html or "") if x.strip()]
        def key(x):
            return re.sub(r"[^a-z ]", " ", x.lower()).replace(" spp", " spp").strip()
        wanted, seen_lbl = [], set()
        for term in raw:
            k = re.sub(r"\s+", " ", key(term)).strip()
            k = re.sub(r"\bspp\b", "spp", k)
            hits = ORG_MAP.get(k) or ORG_MAP.get(k.replace(" spp", "")) or []
            if not hits and re.match(r"^[a-z]+ [a-z]+$", k):
                hits = [term]
            for h in hits:
                if h not in seen_lbl:
                    seen_lbl.add(h); wanted.append((term, h))
        if not wanted or not slugs:
            return ""
        cols, seen = [], set()
        for t in abx_tables:
            for d in t["drugs"]:
                if d.get("slug") in slugs and d["abbr"] not in seen:
                    seen.add(d["abbr"]); cols.append((t, d))
        if not cols:
            return ""
        rows, hits_n = [], 0
        for term, org in wanted[:10]:
            cells, any_hit = [], False
            for t, d in cols:
                val = None
                for r in t["rows"]:
                    if r["organism"].lower().strip() == org.lower():
                        val = r["v"].get(d["abbr"], {}).get("v"); break
                if val is not None:
                    hits_n += 1; any_hit = True
                cls = "" if val is None else ("s-hi" if val >= 90 else "s-ok" if val >= 80 else "s-mid" if val >= 60 else "s-lo")
                cells.append(f'<td class="{cls}">{val if val is not None else "&mdash;"}</td>')
            if not any_hit:
                continue
            via = f'<span class="cv-via">via {esc(term)}</span>' if key(term) != key(org) else ""
            rows.append(f'<tr><td class="cv-b"><i>{esc(org)}</i>{via}</td>{"".join(cells)}</tr>')
        if hits_n < 2 or not rows:
            return ""
        head = "".join(f'<th title="{esc(d["name"])}">{esc(d["abbr"])}</th>' for _t, d in cols)
        src = cols[0][0]
        return (f'<section class="cv"><h3>Local susceptibility for these agents</h3>'
                f'<p class="cv-s">Percent susceptible from {esc(src["title"])} {esc(src["year"] or "")}. '
                f'A dash means the antibiogram does not report that pair, which is not the same as no activity. '
                f'This is local resistance, not spectrum of activity. '
                f'<a href="{root}antibiograms/explore.html">Open the explorer</a></p>'
                f'<div class="tbl-wrap"><table class="cv-t"><thead><tr><th>Organism</th>{head}</tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></div></section>')

    people_view = {}
    for i, p in enumerate((structure.get("people") or {}).get("people") or []):
        if p.get("path"):
            people_view[p["path"]] = dict(p, order=i)
    site_cur = curation["sites"]
    def site_link_list(m_for_group=None):
        """Per-site policy links (restriction workflow, allergy pathway, IV to PO) as an HTML list with data-sites."""
        items = []
        for key, sc in site_cur.items():
            links = []
            for k, label in (("restricted", "Restricted antimicrobials"), ("allergy", "Beta-lactam allergy pathway"), ("ivpo", "IV to PO step-down")):
                r = route_for_alias(sc[k], f"curation.json sites.{key}.{k}") if sc.get(k) else None
                if r:
                    links.append((r, label))
            for extra in sc.get("extras", []):
                r = route_for_alias(extra, f"curation.json sites.{key}.extras")
                if r:
                    nid = ctx.nid_by_path.get(extra)
                    links.append((r, models[nid]["title"] if nid in models else extra))
            items.append((key, sc["label"], links))
        return items

    def render_node_body(m, root):
        n = m["raw"]
        notes, parts = [], []
        t = m["type"]
        if t == "diagnosis":
            popname = "Pediatric" if is_peds(m) else "Adult"
            doc = therapy_rows(fv(n, "field_dosing"), ctx, root, m["slug"])
            page_dose, gaps, row_index = {}, [], []
            where = (curation.get("_default_site") or "")
            idx = 0
            total = sum(len([x for x in r if "__group__" not in x]) for k, r in doc if k == "rows")
            pre, post, ctx_blocks = [], [], []
            for kind, payload in doc:
                if kind == "html":
                    (ctx_blocks and post or pre).append(payload)
                elif kind == "table":
                    (ctx_blocks and post or pre).append(payload)
                elif kind == "notes":
                    notes += payload
                elif kind == "rows":
                    cols = []
                    group_label = ""
                    for cells in payload:
                        if "__group__" in cells:
                            group_label = cells["__group__"]
                            continue
                        idx += 1
                        cond = cells.get("condition")
                        label = text_only(cond["html"]) if cond else f"Option {idx}"
                        short = short_label(m["title"], label)
                        tags = row_tags(m["slug"], idx, label, total)
                        anchor = f"rx-{idx}"
                        row_index.append({"a": anchor, "l": short, "tags": tags})
                        first = cells.get("first"); alt = cells.get("alt")
                        ctx_text = m["title"] + " " + label
                        c_first, u1 = regimen_column(first["html"] if first else "", ctx_text, root, "first", gaps, where)
                        alt_tree = regimen_tree(alt["html"]) if (alt and alt["text"]) else {"steps": [], "lead": ""}
                        c_alt, u2 = (regimen_column(alt["html"], ctx_text, root, "alt", gaps, where, drop_lead=True)
                                     if alt_tree["steps"] else ("", {}))
                        page_dose.update(u1); page_dose.update(u2)
                        slugs = [d["slug"] for st in regimen_tree(first["html"] if first else "")["steps"] for d in st["drugs"]]
                        cols.append({"a": anchor, "short": short, "label": label, "tags": tags, "cells": cells,
                                     "first": c_first, "alt": c_alt, "alt_cond": alt_tree.get("lead", ""),
                                     "slugs": slugs, "group": group_label})
                    ctx_blocks.append(cols)
            body_parts = list(pre)
            for cols in ctx_blocks:
                nc = len(cols)
                wide = nc <= 4
                if nc > 1:
                    bits, seen_g = [], None
                    for i, c in enumerate(cols):
                        if c.get("group") and c["group"] != seen_g:
                            seen_g = c["group"]
                            bits.append(f'<span class="ctx-g">{esc(seen_g)}</span>')
                        bits.append(f'<button type="button" class="ctx-n{" on" if i == 0 else ""}" data-ctx="{c["a"]}" '
                                    f'data-tags="{esc(" ".join(c["tags"]))}" title="{esc(c["label"][:150])}">{esc(c["short"])}</button>')
                    nodes = "".join(bits)
                    body_parts.append(f'<nav class="ctx" aria-label="Clinical context"><span class="ctx-q">{icon("branch")}Which patient</span>'
                                      f'<span class="ctx-ns">{nodes}</span></nav>')
                shown = "".join(
                    f'<div class="rgc{"" if (wide or i == 0) else " dim"}" data-ctx="{c["a"]}" id="{c["a"]}">'
                    + (f'<div class="rgc-h">'
                       + (f'<span class="rgc-g">{esc(c["group"])}</span>' if c.get("group") else "")
                       + f'{esc(c["short"])}'
                       f'<button type="button" class="copy" data-copy="{c["a"]}">Copy</button></div>' if nc > 1 else "")
                    + f'<div class="rgc-b">{c["first"]}</div>'
                    + (f'<div class="rgc-d"><span>Duration</span><b>{c["cells"]["duration"]["html"]}</b></div>'
                       if c["cells"].get("duration") and c["cells"]["duration"]["text"] else '<div class="rgc-d rgc-d-none"><span>Duration</span><b>not stated</b></div>')
                    + "</div>" for i, c in enumerate(cols))
                copy_one = ("" if nc > 1 else
                            f'<button type="button" class="copy" data-copy="{cols[0]["a"]}">Copy for note</button>')
                body_parts.append(f'<section class="money{"" if wide else " money-one"}">'
                                  f'<div class="money-hd"><h2 class="money-h">First choice</h2>{copy_one}</div>'
                                  f'<div class="rgx" data-cols="{min(nc, 4) if wide else 1}">{shown}</div></section>')
                alts = "".join(
                    f'<div class="rgc{"" if (wide or i == 0) else " dim"}" data-ctx="{c["a"]}">'
                    + (f'<div class="rgc-h">{esc(c["short"])}{(" — " + esc(c["alt_cond"])) if c["alt_cond"] else ""}</div>' if nc > 1 else
                       (f'<div class="rgc-h">{esc(c["alt_cond"])}</div>' if c["alt_cond"] else ""))
                    + f'<div class="rgc-b">{c["alt"]}</div></div>' for i, c in enumerate(cols) if c["alt"])
                if alts:
                    n_alt = sum(1 for c in cols if c["alt"])
                    body_parts.append(f'<section class="alt-blk"><h2 class="alt-h">Alternative</h2>'
                                      f'<div class="rgx" data-cols="{min(n_alt, 4) if wide else 1}">{alts}</div></section>')
                cov = coverage_grid(cols[0]["cells"].get("pathogens", {}).get("html", ""), set(cols[0]["slugs"]), root)
                if cov:
                    body_parts.append(cov)
                # everything read once, not at 3am, sits below in prose
                below = []
                for c in cols:
                    cc = c["cells"]
                    blocks = ""
                    if cc.get("pathogens") and cc["pathogens"]["text"]:
                        blocks += f'<div class="bl"><h4>Common pathogens</h4><div class="rx-body">{cc["pathogens"]["html"]}</div></div>'
                    if cc.get("comments") and cc["comments"]["text"]:
                        ch = cc["comments"]["html"]
                        cs = BeautifulSoup(ch, "lxml")
                        for ul in cs.find_all(["ul", "ol"]):
                            prev = ul.find_previous_sibling(["p", "h3", "h4", "strong"])
                            if prev is not None and re.search(r"if any|consider|criteria|risk factor|indication|following|when", prev.get_text(" "), flags=re.I):
                                ul["class"] = (ul.get("class") or []) + ["check"]
                        blocks += f'<div class="bl"><h4>Comments</h4><div class="rx-body">{cs.body.decode_contents() if cs.body else ch}</div></div>'
                    for key, cell in cc.items():
                        if key.startswith("extra:") and cell["text"]:
                            blocks += f'<div class="bl"><h4>{esc(key[6:])}</h4><div class="rx-body">{cell["html"]}</div></div>'
                    tw = []
                    ivpo = [drug_titles[d["slug"]] for st in regimen_tree(cc.get("first", {}).get("html", ""))["steps"]
                            for d in st["drugs"] if "iv-po" in drug_tags.get(d["slug"], [])]
                    if ivpo:
                        rr = route_for_alias(site_cur["ucsf"]["ivpo"]) or ""
                        tw.append(f'<li><b>IV to PO candidates:</b> {esc(", ".join(dict.fromkeys(ivpo)))}'
                                  + (f' <a href="{root}{rr}">step-down guidance</a>' if rr else "") + "</li>")
                    for sent in [x.strip() for x in re.split(r"(?<=[.!?])\s+", text_only(cc["comments"]["html"] if cc.get("comments") else ""))
                                 if re.search(r"\bID\b.*consult|infectious diseases? consult|consult(ation)? (is )?recommended", x, flags=re.I)][:2]:
                        tw.append(f'<li><b>Consult:</b> {esc(sent)}</li>')
                    if tw:
                        blocks += '<div class="bl then-what"><h4>Then what</h4><ul>' + "".join(tw) + "</ul></div>"
                    if blocks:
                        below.append(f'<div class="dtl{"" if (wide or c is cols[0]) else " dim"}" data-ctx="{c["a"]}">'
                                     + (f'<h3>{esc(c["short"])}</h3>' if nc > 1 else "") + blocks + "</div>")
                if below:
                    body_parts.append(f'<section class="detail">{"".join(below)}</section>')
                raw = "".join(
                    f'<div class="src-row"><h4>{esc(c["short"])}</h4>' + "".join(
                        f'<div class="src-col"><h5>{LABELS.get(k, k)}</h5><div class="rx-body">{c["cells"][k]["html"]}</div></div>'
                        for k in ("first", "alt", "pathogens", "comments", "duration") if c["cells"].get(k) and c["cells"][k]["text"]) + "</div>"
                    for c in cols)
                body_parts.append(f'<details class="src-raw"><summary>{icon("doc")}These rows exactly as IDMP publishes them</summary>{raw}</details>')
            body_parts += post
            if page_dose:
                body_parts.append(f'<script type="application/json" id="dose-data">{jdump(page_dose)}</script>')
            if gaps:
                ctx.warnings.append({"title": m["title"], "route": m["route"], "code": "dose-gap",
                                     "notes": [f"no dose and no pointer for {g['drug']}" for g in gaps]})
            extra = fv(n, "field_notes")
            if extra and text_only(extra):
                body_parts.append(f'<section class="block"><h2>Notes</h2>{sanitize(extra, ctx, root, m["slug"])}</section>')
            refs = fv(n, "field_references")
            if refs and text_only(refs):
                body_parts.append(f'<details class="block refs"><summary>References</summary>{sanitize(refs, ctx, root, m["slug"])}</details>')
            rel = related_guidelines(m)
            site_items = site_link_list()
            box = ['<aside class="block related"><h2>For your hospital</h2><p>Guidelines matched by title, plus each site\'s restriction and allergy policies.</p>']
            for key, lbl, links in site_items:
                gl = [g for g in rel if any(x["group"] == key for x in gmeta[g["nid"]]["sites"])]
                if not gl and not links:
                    continue
                lis = "".join(f'<li><a href="{root}{g["route"]}">{esc(g["title"])}</a> {kind_badges(gmeta[g["nid"]]["kinds"])}</li>' for g in gl)
                lis += "".join(f'<li class="policy"><a href="{root}{r}">{esc(t2)}</a></li>' for r, t2 in links)
                box.append(f'<section class="site-sec" data-site="{key}"><h3><span class="badge site s-{key}">{esc(lbl)}</span></h3><ul class="rel-list">{lis}</ul></section>')
            other = [g for g in rel if not gmeta[g["nid"]]["sites"]]
            if other:
                box.append('<section class="site-sec" data-site=""><h3>General</h3><ul class="rel-list">'
                           + "".join(f'<li><a href="{root}{g["route"]}">{esc(g["title"])}</a></li>' for g in other) + "</ul></section>")
            box.append("</aside>")
            body_parts.append("".join(box))
            return "\n".join(body_parts), notes, {"popname": popname, "rows": row_index}
        if t == "drug":
            tags = drug_tags.get(m["slug"], [])
            badges = "".join(f'<span class="badge tag t-{"zsfg" if "zsfg" in s else "ucsf" if "ucsf" in s else "ivpo" if "iv-po" in s else "misc"}">{esc(short_notation(s, tax_name("/notations/" + s)))}</span>' for s in tags)
            dw = [tax_name(i.get("url") or "") for i in (n.get("field_dosing_weights") or [])]
            head = '<div class="drug-meta">' + badges + (f'<span class="dw"><strong>Dosing weight:</strong> {esc("; ".join(dw))}</span>' if dw else "") + "</div>"
            parts.append(head)
            sections = []
            nd = fv(n, "field_dosing")
            has_nd = bool(nd and text_only(nd) and fv(n, "field_bool_nondialysis") is not False)
            hd = fv(n, "field_dosing_antimicrobial_dosin")
            has_hd = bool(hd and text_only(hd) and fv(n, "field_bool_hemodialysis") is not False)
            parts.append('<div class="renal-dial"><label>Renal function</label><input type="number" id="dial-crcl" inputmode="numeric" min="0" max="250" placeholder="CrCl"><span class="unit">mL/min</span><div class="seg" data-lens="renal"><button data-v="">Any</button><button data-v="hd">HD</button><button data-v="crrt">CRRT</button></div><span class="muted dial-note">Highlights the matching column below. Shared with your context lens.</span></div>')
            restr = fv(n, "field_restriction_details")
            boxes = []
            for key, sc in site_cur.items():
                applies = (key == "ucsf" and "id-r-ucsf" in tags) or (key == "zsfg" and "id-r-zsfg" in tags)
                if not applies:
                    continue
                r = route_for_alias(sc["restricted"]) if sc.get("restricted") else None
                body = sanitize(restr, ctx, root, m["slug"]) if (restr and text_only(restr)) else ""
                boxes.append(f'<section class="restrict" data-site="{key}"><h3><span class="badge site s-{key}">{esc(sc["label"])}</span> ID-restricted</h3>{body or "<p>Restricted to ID or Antimicrobial Stewardship approval at this site.</p>"}' + (f'<p><a class="btn small" href="{root}{r}">How to get approval at {esc(sc["label"])}</a></p>' if r else "") + "</section>")
            if boxes:
                sections.append(("restriction", "Restriction", "".join(boxes)))
            elif restr and text_only(restr):
                sections.append(("restriction", "Restriction", f'<section class="restrict">{sanitize(restr, ctx, root, m["slug"])}</section>'))
            if has_nd:
                soup = BeautifulSoup(sanitize(nd, ctx, root, m["slug"]), "lxml")
                out = []
                for child in list(soup.body.contents) if soup.body else []:
                    if isinstance(child, NavigableString):
                        if str(child).strip(): out.append(f"<p>{esc(str(child).strip())}</p>")
                    elif child.name == "table":
                        out.append(render_plain_table(expand_grid(child), bands=True))
                    else:
                        out.append(str(child))
                sections.append(("dosing", "Dosing, non-dialysis", "".join(out)))
            if has_hd:
                soup = BeautifulSoup(sanitize(hd, ctx, root, m["slug"]), "lxml")
                out = ['<p class="muted">Intermittent HD assumes high-flux hemodialysis. CRRT assumes CVVHD with ultrafiltration rate 2 L/h and residual native GFR &lt; 10 mL/min (source assumptions).</p>']
                for child in list(soup.body.contents) if soup.body else []:
                    if isinstance(child, NavigableString):
                        if str(child).strip(): out.append(f"<p>{esc(str(child).strip())}</p>")
                    elif child.name == "table":
                        out.append(render_plain_table(expand_grid(child), bands=True))
                    else:
                        out.append(str(child))
                dn = fv(n, "field_dialysis_notes")
                if dn and text_only(dn):
                    out.append(f'<div class="subblock"><h3>Dialysis notes</h3>{sanitize(dn, ctx, root, m["slug"])}</div>')
                sections.append(("dialysis", "HD and CRRT", "".join(out)))
            for field, key, label in (("field_notes", "notes", "Notes"), ("field_monitoring", "monitoring", "Monitoring")):
                v = fv(n, field)
                if v and text_only(v):
                    sections.append((key, label, sanitize(v, ctx, root, m["slug"])))
            used = drug_used_in.get(m["nid"], [])
            if used:
                adult = [u for u in used if u["type"] == "diagnosis" and not is_peds(u)]
                peds = [u for u in used if u["type"] == "diagnosis" and is_peds(u)]
                gls = [u for u in used if u["type"] != "diagnosis"]
                def lst(items):
                    return '<ul class="rel-list">' + "".join(f'<li><a href="{root}{u["route"]}">{esc(u["title"])}</a></li>' for u in sorted(items, key=lambda x: x["title"])) + "</ul>"
                blocks = ""
                if adult: blocks += f"<h3>Adult empiric therapy</h3>{lst(adult)}"
                if peds: blocks += f"<h3>Pediatric empiric therapy</h3>{lst(peds)}"
                if gls: blocks += f"<h3>Guidelines and pages</h3>{lst(gls)}"
                sections.append(("used", "Where recommended", blocks))
            refs = fv(n, "field_drug_references")
            if refs and text_only(refs):
                sections.append(("refs", "References", sanitize(refs, ctx, root, m["slug"])))
            rev = fv(n, "field_revision_notes")
            if rev and rev.strip():
                lines = [esc(l.strip()) for l in re.split(r"[\r\n]+", rev) if l.strip()]
                sections.append(("revisions", "Revision notes from IDMP", "<ul>" + "".join(f"<li>{l}</li>" for l in lines) + "</ul>"))
            jump = "".join(f'<a href="#{k}">{esc(lbl)}</a>' for k, lbl, _ in sections)
            parts.append(f'<nav class="jump" aria-label="Sections">{jump}</nav>')
            for k, lbl, body in sections:
                cls = "block refs" if k in ("refs", "revisions") else "block"
                if k in ("refs", "revisions"):
                    parts.append(f'<details class="{cls}" id="{k}"><summary>{esc(lbl)}</summary>{body}</details>')
                else:
                    parts.append(f'<section class="{cls}" id="{k}"><h2>{esc(lbl)}</h2>{body}</section>')
            return "\n".join(parts), notes, {"sections": [(k, lbl) for k, lbl, _ in sections]}
        if t == "guidelines":
            gm = gmeta[m["nid"]]
            dates = ""
            if gm["dates"]:
                hist = ", ".join(human_date(d) for d in gm["dates"])
                dates = f'<span class="gl-date" title="Modification history: {esc(hist)}">Guideline dated {esc(human_date(gm["latest"]))}</span>'
            parts.append(f'<div class="drug-meta">{site_badges(gm["sites"])}{kind_badges(gm["kinds"])}<span class="badge cat">{esc(gm["category"])}</span>{dates}</div>')
            for url, item in gm["pdfs"]:
                full = urljoin(BASE + "/", url)
                fmeta = files.get(urlparse(full).path) or {}
                size = fmeta.get("size")
                size_h = f"{int(size) // 1024} KB" if size and str(size).isdigit() else ""
                pages_h = f'{fmeta["pages"]} page{"s" if fmeta.get("pages") != 1 else ""}' if fmeta.get("pages") else ""
                thumb = f'<a class="thumb" target="_blank" rel="noopener" href="{esc(full)}"><img loading="lazy" src="{root}thumbs/{fmeta["thumb"]}.jpg" alt="First page of the PDF"></a>' if fmeta.get("thumb") else ""
                parts.append(f'<div class="pdf-card">{thumb}<div><p class="cta"><a class="btn x-pdf" target="_blank" rel="noopener" href="{esc(full)}">Open guideline PDF</a></p><p class="muted">Hosted on idmp.ucsf.edu, no login. {esc(", ".join(x for x in (pages_h, size_h) if x))}</p></div></div>')
            body = fv(n, "body")
            outline = []
            if body and text_only(body):
                clean = sanitize(body, ctx, root, m["slug"])
                clean, outline = outline_of(clean)
                soup = BeautifulSoup(clean, "lxml")
                out = []
                for child in list(soup.body.contents) if soup.body else []:
                    if isinstance(child, NavigableString):
                        if str(child).strip(): out.append(f"<p>{esc(str(child).strip())}</p>")
                    elif child.name == "table":
                        grid = expand_grid(child)
                        hdr = header_row_index(grid)
                        cols = map_columns([c["text"] for c in grid[0]]) if hdr == 0 else []
                        out.append(render_plain_table(grid, bands="first" not in cols))
                    else:
                        out.append(str(child))
                parts.append(f'<section class="block prose reader">{"".join(out)}</section>')
                if "login" in gm["kinds"]:
                    parts.append('<p class="notice"><strong>Some links above open on Box or SharePoint</strong> and need a UCSF login; they are marked with a lock.</p>')
            elif not gm["pdfs"]:
                parts.append('<p class="muted">This guideline has no text on IDMP; open the source page for details.</p>')
            return "\n".join(parts), notes, {"outline": outline}
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
                parts.append(f'<section class="block prose">{sanitize(bio, ctx, root, m["slug"])}</section>')
            if honors:
                parts.append('<section class="block"><h2>Honors and awards</h2><ul>' + "".join(f"<li>{esc(x)}</li>" for x in honors) + "</ul></section>")
            if pubs:
                parts.append('<section class="block"><h2>Selected publications</h2><ul class="rel-list">' + "".join(
                    f'<li><a href="{root}{p["route"]}">{esc(p["title"])}</a> <span class="muted">{esc(fv(p["raw"], "field_publication_year") or "")}</span></li>' for p in pubs) + "</ul></section>")
            return "\n".join(parts), notes, {}
        # page
        body = fv(n, "field_body")
        heat = m["nid"] in abx_nids
        outline = []
        if body and text_only(body):
            clean = sanitize(body, ctx, root, m["slug"])
            clean, outline = outline_of(clean)
            soup = BeautifulSoup(clean, "lxml")
            out = []
            for child in list(soup.body.contents) if soup.body else []:
                if isinstance(child, NavigableString):
                    if str(child).strip(): out.append(f"<p>{esc(str(child).strip())}</p>")
                elif child.name == "table":
                    grid = expand_grid(child)
                    is_abx = heat and bool(grid) and re.search(r"organism", grid[0][0]["text"], flags=re.I)
                    out.append(render_plain_table(grid, heat=bool(is_abx)))
                else:
                    out.append(str(child))
            h = "".join(out)
            if heat and "heat" in h:
                parts.append('<p class="legend"><span class="s-hi">≥ 90 %</span><span class="s-ok">80–89 %</span><span class="s-mid">60–79 %</span><span class="s-lo">&lt; 60 %</span><span class="s-r">R</span> shading added by the mirror; values are % susceptible as published. <a href="' + root + 'antibiograms/explore.html">Open the bug-drug explorer</a>.</p>')
            parts.append(f'<section class="block prose reader">{h}</section>')
        else:
            parts.append('<p class="muted">This page has no body text on IDMP (it may be a form or an image). Open the source page.</p>')
        return "\n".join(parts), notes, {"outline": outline}

    # ---- write node pages
    section_of = {"diagnosis": "empiric", "drug": "drugs", "guidelines": "guidelines", "ucsf_person": "reference", "other_person": "reference"}
    fresh_days = 30
    dx_rows_index = {}
    for m in models.values():
        if m["type"] == "ucsf_publication":
            continue
        route = m["route"]; root = root_for(route)
        body, notes, meta = render_node_body(m, root)
        if notes:
            ctx.warnings.append({"nid": m["nid"], "title": m["title"], "route": route, "notes": notes})
            notes = [x for x in notes if not x.startswith("info:")]
        crumbs = [("index.html", "Home")]
        sec = section_of.get(m["type"], "antibiograms" if m["nid"] in abx_nids else "pages")
        popname = meta.get("popname")
        if m["type"] == "diagnosis":
            crumbs.append(("empiric/peds.html" if popname == "Pediatric" else "empiric/index.html", f"{popname} empiric therapy"))
            dx_rows_index[m["nid"]] = meta.get("rows", [])
        elif m["type"] == "drug":
            crumbs.append(("drugs/index.html", "Dosing"))
        elif m["type"] == "guidelines":
            crumbs.append(("guidelines/index.html", "Guidelines"))
        elif m["nid"] in abx_nids:
            crumbs.append(("antibiograms/index.html", "Antibiograms"))
        elif m["type"] in ("ucsf_person", "other_person"):
            crumbs.append(("people.html", "People"))
        kicker = {"diagnosis": f"{popname} empiric therapy", "drug": "Antimicrobial dosing", "guidelines": "Guideline",
                  "ucsf_person": "IDMP team", "other_person": "IDMP team"}.get(m["type"], "Antibiogram" if m["nid"] in abx_nids else "Page")
        fresh = ""
        try:
            if m["changed"] and (build_time - datetime.fromisoformat(m["changed"].replace("Z", "+00:00"))).days <= fresh_days:
                fresh = f'<span class="badge fresh" title="Changed on IDMP {esc(human_date(m["changed"]))}">Updated {esc(human_date(m["changed"]))}</span>'
        except Exception:
            pass
        rail = ""
        if meta.get("outline") and len(meta["outline"]) >= 3:
            rail = '<nav class="outline"><p class="kicker">On this page</p>' + "".join(f'<a class="o-{tag}" href="#{hid}">{esc(t)}</a>' for tag, hid, t in meta["outline"]) + "</nav>"
        elif meta.get("sections"):
            rail = '<nav class="outline"><p class="kicker">On this page</p>' + "".join(f'<a href="#{k}">{esc(lbl)}</a>' for k, lbl in meta["sections"]) + "</nav>"
        elif meta.get("rows") and len(meta["rows"]) > 1:
            rail = '<nav class="outline"><p class="kicker">Situations</p>' + "".join(f'<a href="#{r["a"]}">{esc(r["l"])}</a>' for r in meta["rows"]) + "</nav>"
        content = f'<article class="node node-{m["type"]}" data-slug="{esc(m["slug"])}">{body}</article>'
        write(route, layout(ctx, root, m["title"], content, node=m, section=sec, crumbs=crumbs, fallback_notes=notes, rail=rail,
                            nav=nav_for(sec, route, root), pager=pager_for(route, root),
                            kicker=esc(kicker) + (" " + fresh if fresh else ""), h1=m["title"],
                            desc=f"{kicker}: {m['title']} (mirrored from idmp.ucsf.edu)"))
        # search entry
        n = m["raw"]
        if m["type"] == "diagnosis":
            txt = text_only(fv(n, "field_dosing") or "")
            drug_names = []
            for href in re.findall(r'href="([^"]+)"', fv(n, "field_dosing") or ""):
                nid = ctx.nid_by_path.get(urlparse(urljoin(BASE + "/", html.unescape(href))).path.rstrip("/"))
                if nid in models and models[nid]["type"] == "drug":
                    drug_names.append(models[nid]["title"])
            kw = ward_aliases.get(m["slug"], []) + [x for x in [initialism(m["title"])] if x] + re.findall(r"[A-Z][a-z]+\.? [a-z]+", txt)[:20] + drug_names + [group_of_dx.get(m["nid"], "")]
            search.append({"t": "dxp" if popname == "Pediatric" else "dx", "n": m["title"], "u": route, "k": " ".join(dict.fromkeys(kw))[:500],
                           "s": ", ".join(dict.fromkeys(drug_names))[:120], "rows": [{"a": r["a"], "l": r["l"], "g": r["tags"]} for r in meta.get("rows", [])]})
        elif m["type"] == "drug":
            low = m["title"].lower()
            kw = [a for key, alts in synonyms.items() if key in low for a in alts]
            dd = dose_data_all.get(m["slug"])
            entry = {"t": "drug", "n": m["title"], "u": route, "k": " ".join(dict.fromkeys(kw))[:300],
                     "s": "; ".join(r["i"] for r in (dd["rows"] if dd else []))[:120], "tags": drug_tags.get(m["slug"], [])}
            if dd:
                entry["dose"] = dd
            if dose_hd_all.get(m["slug"]):
                entry["hd"] = dose_hd_all[m["slug"]]
            search.append(entry)
        elif m["type"] == "guidelines":
            gm = gmeta[m["nid"]]
            search.append({"t": "gl", "n": m["title"], "u": route, "k": " ".join([gm["category"]] + [s["short"] for s in gm["sites"]]),
                           "s": gm["desc"][:140], "sites": sorted({s["group"] for s in gm["sites"]}), "kinds": gm["kinds"]})
        elif m["type"] in ("ucsf_person", "other_person"):
            search.append({"t": "person", "n": m["title"], "u": route, "k": "", "s": (fv(n, "field_person_title_override") or fv(n, "field_person_working_title") or (people_view.get(m["alias"]) or {}).get("role") or "")[:120]})
        else:
            search.append({"t": "abx" if m["nid"] in abx_nids else "page", "n": m["title"], "u": route, "k": "", "s": text_only(fv(n, "field_body") or "")[:140]})
    # organisms
    for t in abx_tables:
        for r in t["rows"]:
            search.append({"t": "bug", "n": r["organism"], "u": f"antibiograms/explore.html?bug={html.escape(r['organism'])}", "k": t["title"], "s": f'{t["title"]} {t["year"] or ""}'.strip(),
                           "tbl": t["key"], "yr": t["year"], "n_iso": r["n"], "v": {d["abbr"]: r["v"][d["abbr"]]["v"] for d in t["drugs"]},
                           "d": {d["abbr"]: [d["name"], d.get("slug")] for d in t["drugs"]}})

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
        extra = [m for m in by_type["diagnosis"] if m["nid"] not in listed and (is_peds(m) == (popname == "Pediatric"))]
        if extra:
            groups.append(("Other", sorted(extra, key=lambda x: x["title"])))
        jump = "".join(f'<a href="#g-{slugify(h or "other")}">{esc(h or "Other")}</a>' for h, _ in groups)
        cards = []
        for h, items in groups:
            lis = []
            for m in items:
                rows = dx_rows_index.get(m["nid"], [])
                tags = sorted({t for r in rows for t in r["tags"]})
                sub = ""
                if len(rows) > 1:
                    sub = '<div class="sub">' + ", ".join(f'<a href="{root}{m["route"]}#{r["a"]}" data-tags="{esc(" ".join(r["tags"]))}">{esc(r["l"][:48])}</a>' for r in rows[:6]) + ("…" if len(rows) > 6 else "") + "</div>"
                lis.append(f'<li data-tags="{esc(" ".join(tags))}"><a href="{root}{m["route"]}">{esc(m["title"])}</a>{sub}</li>')
            cards.append(f'<section class="group" id="g-{slugify(h or "other")}"><h2>{esc(h or "Other")}</h2><ul class="links">{"".join(lis)}</ul></section>')
        content = f"""<p class="lede">Initial regimens by syndrome from the {"UCSF Benioff Children's Hospitals" if popname == "Pediatric" else "UCSF Health"} antimicrobial stewardship programs. Pick the clinical situation on each page; drug names open their dosing without leaving the page. <a href="{root}{other_route}">Switch to {other_label}</a></p>
<p class="small">These recommendations assist clinical decision-making for common situations and cannot replace individualized evaluation, including history of multidrug-resistant organisms.</p>
<div class="filter"><input type="search" id="filter" placeholder="Filter syndromes" aria-label="Filter list"><div class="chips" id="chips"><button data-chip="outpatient">Outpatient</button><button data-chip="inpatient">Inpatient</button><button data-chip="icu">ICU</button></div></div>
<nav class="jump">{jump}</nav>
<div class="groups" id="groups">{"".join(cards)}</div>"""
        write(route, layout(ctx, root, f"{popname} empiric therapy", content, section="empiric",
                            nav=nav_for("empiric", route, root), kicker="Empiric therapy", h1=f"{popname} empiric antimicrobial therapy",
                            crumbs=[("index.html", "Home"), (None, f"{popname} empiric therapy")]))
    empiric_index("empiric_adult", "Adult", "empiric/index.html", "empiric/peds.html", "pediatrics")
    empiric_index("empiric_peds", "Pediatric", "empiric/peds.html", "empiric/index.html", "adults")

    # drugs index
    route = "drugs/index.html"; root = root_for(route)
    rows = []
    for m in sorted(by_type["drug"], key=lambda x: x["title"].lower()):
        n = m["raw"]
        tags = drug_tags.get(m["slug"], [])
        badges = "".join(f'<span class="badge tag t-{"zsfg" if "zsfg" in s else "ucsf" if "ucsf" in s else "ivpo" if "iv-po" in s else "misc"}">{esc(short_notation(s, tax_name("/notations/" + s)))}</span>' for s in tags)
        has_hd = bool(fv(n, "field_dosing_antimicrobial_dosin")) and (fv(n, "field_bool_hemodialysis") is not False)
        low = m["title"].lower()
        syn = [a for key, alts in synonyms.items() if key in low for a in alts]
        dd = dose_data_all.get(m["slug"])
        first_dose = esc(dd["rows"][0]["d"][0][:44]) if (dd and dd["rows"]) else ""
        rows.append(f'<tr data-tags="{esc(" ".join(tags))}{" hd" if has_hd else ""}" data-syn="{esc(" ".join(syn))}">'
                    f'<td><a href="{root}{m["route"]}">{esc(m["title"])}</a></td>'
                    f'<td class="num">{first_dose}</td>'
                    f'<td class="flags">{badges}{"<span class=tag>HD/CRRT</span>" if has_hd else ""}</td></tr>')
    content = f"""<p class="lede">Renal-function and dialysis dosing for {len(rows)} agents, one page per drug with a renal dial. Brand names and ward shorthand work here and in the Ask palette: Zosyn, pip-tazo, vanc, Bactrim.</p>
<p class="small">Source guidance: dosing recommendations are based on available literature and do not replace clinical judgement. Pediatric and neonatal dosing cards are in the index under this section.</p>
<div class="filter"><input type="search" id="filter" placeholder="Filter drugs (brand names work)" aria-label="Filter list">
<div class="chips" id="chips"><button data-chip="id-r-ucsf">ID-restricted at UCSF</button><button data-chip="id-r-zsfg">ID-restricted at ZSFG</button><button data-chip="iv-po">IV to PO</button><button data-chip="hd">Has HD/CRRT</button></div></div>
<table class="idxtbl" id="list"><thead><tr><th>Drug</th><th>Usual dose</th><th>Flags</th></tr></thead><tbody>{"".join(rows)}</tbody></table>"""
    write(route, layout(ctx, root, "Adult antimicrobial dosing", content, section="drugs",
                        nav=nav_for("drugs", route, root), kicker="Dosing", h1="Adult antimicrobial dosing",
                        crumbs=[("index.html", "Home"), (None, "Dosing")]))

    # guidelines index
    route = "guidelines/index.html"; root = root_for(route)
    groups = resolve_groups("guidelines")
    listed = {m["nid"] for _, items in groups for m in items}
    extra = [m for m in by_type["guidelines"] if m["nid"] not in listed]
    if extra:
        groups.append(("Other guidelines", sorted(extra, key=lambda x: x["title"])))
    cards = []
    for h, items in groups:
        lis = []
        for m in sorted(items, key=lambda x: x["title"].lower()):
            gm = gmeta[m["nid"]]
            date = f'<span class="gl-date">{esc(human_date(gm["latest"]))}</span>' if gm["latest"] else ""
            thumb = f'<img class="mini-thumb" loading="lazy" src="{root}thumbs/{gm["thumb"]}.jpg" alt="">' if gm["thumb"] else '<span class="mini-thumb blank"></span>'
            lis.append(f'<li data-sites="{esc(" ".join(sorted({s["group"] for s in gm["sites"]})))}" data-cat="{esc(gm["cat_slug"])}">{thumb}<div class="gl-main"><a href="{root}{m["route"]}">{esc(m["title"])}</a>{("<p class=desc>" + esc(gm["desc"]) + "</p>") if gm["desc"] else ""}<span class="row-badges">{site_badges(gm["sites"])}{kind_badges(gm["kinds"])}{date}</span></div></li>')
        cards.append(f'<section class="group" data-cat="{slugify(h or "other")}"><h2>{esc(h or "Other")}</h2><ul class="links gl">{"".join(lis)}</ul></section>')
    content = f"""<p class="lede">{len(by_type["guidelines"])} guidelines across UCSF Health, ZSFG, the VA and the Benioff Children's Hospitals. Your Where lens brings your hospital's to the top. <span class="tag k-pdf">PDF</span> opens on idmp.ucsf.edu without login; <span class="tag k-login">UCSF login</span> means Box or SharePoint.</p>
<div class="filter"><input type="search" id="filter" placeholder="Filter guidelines" aria-label="Filter list">
<div class="chips" id="chips"><button data-chip="site:ucsf">UCSF Health</button><button data-chip="site:zsfg">ZSFG</button><button data-chip="site:va">VA</button><button data-chip="site:bch">BCH</button></div></div>
<div class="groups" id="groups">{"".join(cards)}</div>"""
    write(route, layout(ctx, root, "Guidelines", content, section="guidelines",
                        nav=nav_for("guidelines", route, root), kicker="Guidelines & policies", h1="Institutional guidelines",
                        crumbs=[("index.html", "Home"), (None, "Guidelines")]))

    # antibiograms index + explorer
    route = "antibiograms/index.html"; root = root_for(route)
    groups = resolve_groups("antibiograms")
    lis = []
    for h, items in groups:
        for m in items:
            body = fv(m["raw"], "field_body") or ""
            sub = []
            for href, txt in re.findall(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, flags=re.S):
                full = urljoin(BASE + "/", html.unescape(href)); path = urlparse(full).path.rstrip("/")
                tt = text_only(txt)
                nid = ctx.nid_by_path.get(path)
                if nid in models:
                    sub.append(f'<a href="{root}{models[nid]["route"]}">{esc(tt)}</a>')
                elif path.startswith(("/document/", "/sites/g/files/")):
                    sub.append(f'<a class="x-pdf" target="_blank" rel="noopener" href="{esc(full)}">{esc(tt)} (PDF)</a>')
            lis.append(f'<li><a class="big" href="{root}{m["route"]}">{esc(m["title"])}</a><div class="sub">{", ".join(dict.fromkeys(sub))}</div></li>')
    content = f"""<p class="lede">Aggregate susceptibility by hospital. Tables published as HTML feed the <a href="{root}antibiograms/explore.html">bug-drug explorer</a> and the Ask palette; try <i>e coli cipro</i>. Reports published only as PDF open on idmp.ucsf.edu.</p>
<p class="cta"><a class="btn" href="{root}antibiograms/explore.html">Open the bug-drug explorer</a></p>
<ul class="links abx">{"".join(lis)}</ul>"""
    write(route, layout(ctx, root, "Antibiograms", content, section="antibiograms",
                        nav=nav_for("antibiograms", route, root), kicker="Antibiograms", h1="Antibiograms",
                        crumbs=[("index.html", "Home"), (None, "Antibiograms")]))
    route = "antibiograms/explore.html"; root = root_for(route)
    content = f"""<p class="lede">Pick an organism to see every drug, or a drug to see every organism, across the UCSF adult tables the mirror could parse. Values are % susceptible as published; shading is added by the mirror.</p>
<div class="explore" id="explore"><div class="explore-controls"><label>Organism <input list="bug-list" id="bug" placeholder="Escherichia coli"><datalist id="bug-list"></datalist></label><span class="muted">or</span><label>Drug <input list="drug-list" id="abx-drug" placeholder="ciprofloxacin"><datalist id="drug-list"></datalist></label></div><div id="explore-out"><p class="muted">Loading tables…</p></div></div>
<p class="legend"><span class="s-hi">≥ 90 %</span><span class="s-ok">80–89 %</span><span class="s-mid">60–79 %</span><span class="s-lo">&lt; 60 %</span><span class="s-r">R</span></p>"""
    write(route, layout(ctx, root, "Bug-drug explorer", content, section="antibiograms",
                        nav=nav_for("antibiograms", route, root), kicker="Antibiograms", h1="Bug-drug explorer",
                        crumbs=[("index.html", "Home"), ("antibiograms/index.html", "Antibiograms"), (None, "Explorer")], page_class="explore-page"))
    with open(os.path.join(OUT, "antibiogram.json"), "w", encoding="utf-8") as f:
        json.dump({"tables": abx_tables, "built": build_time.isoformat()}, f, ensure_ascii=False, separators=(",", ":"))

    # peds hub
    route = "peds.html"; root = ""
    peds_pages = [m for m in models.values() if m["type"] == "page" and re.search(r"pediatric|neonatal|benioff|children|kocher|icn", m["title"] + m["alias"], flags=re.I) and m["nid"] not in abx_nids]
    peds_gl = [m for m in by_type["guidelines"] if gmeta[m["nid"]]["sites"] and all(s["group"] == "bch" for s in gmeta[m["nid"]]["sites"])]
    def ul(items):
        return '<ul class="links">' + "".join(f'<li><a href="{root}{m["route"]}">{esc(m["title"])}</a></li>' for m in sorted(items, key=lambda x: x["title"].lower())) + "</ul>"
    content = f"""<p class="lede">Benioff Children's Hospitals content in one place: empiric therapy by syndrome, the pediatric and neonatal dosing cards, and BCH-only guidelines. Set Patient to Pediatric in your context and the rest of the site favours these too.</p>
<section class="group"><h2>Start here</h2><ul class="links"><li><a class="big" href="empiric/peds.html">Pediatric empiric therapy by syndrome</a></li></ul></section>
<section class="group"><h2>Dosing cards and pediatric pages</h2>{ul(peds_pages)}</section>
<section class="group"><h2>Guidelines for BCH sites only</h2>{ul(peds_gl)}</section>"""
    write(route, layout(ctx, root, "Pediatrics", content, section="drugs",
                        nav=nav_for("drugs", route, root), kicker="Pediatrics", h1="Pediatrics and neonatal",
                        crumbs=[("index.html", "Home"), (None, "Pediatrics")]))

    # people
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
    content = (f'<p class="lede">As listed on <a class="x-source" target="_blank" rel="noopener" href="{BASE}/people">idmp.ucsf.edu/people</a>. '
               + (f'See also <a href="publications.html">{npubs} publications</a> by the team.' if npubs else "") + '</p>' + "".join(secs))
    write(route, layout(ctx, root, "People", content, section="reference",
                        nav=nav_for("reference", route, root), kicker="Reference", h1="IDMP team",
                        crumbs=[("index.html", "Home"), (None, "People")]))

    # publications
    route = "publications.html"; root = ""
    pubs = sorted(by_type["ucsf_publication"], key=lambda m: (fv(m["raw"], "field_publication_year") or "", m["title"]), reverse=True)
    items = []
    for m in pubs:
        n = m["raw"]
        cite = fv(n, "field_publication_title", "processed") or fv(n, "field_publication_title") or ""
        pmid = fv(n, "field_publication_pubmedid")
        links = []
        if pmid:
            links.append(f'<a class="x-external" target="_blank" rel="noopener" href="https://pubmed.ncbi.nlm.nih.gov/{esc(pmid)}/">PubMed</a>')
        prof = fv(n, "field_publication_id")
        if prof and prof.startswith("http"):
            links.append(f'<a class="x-external" target="_blank" rel="noopener" href="{esc(prof)}">Profile</a>')
        items.append(f'<li id="p{m["nid"]}"><div class="cite">{sanitize(cite, ctx, root, m["slug"])}</div><div class="muted">{esc(fv(n, "field_publication_year") or "")} {", ".join(links)}</div></li>')
        search.append({"t": "pub", "n": m["title"], "u": m["route"], "k": text_only(fv(n, "field_publication_authorlist", "processed") or "")[:300], "s": (fv(n, "field_publication_year") or "")})
    content = f'<p class="lede">Papers listed on IDMP team members\' pages, newest first.</p><ul class="pubs">{"".join(items) or "<li>None listed.</li>"}</ul>'
    write(route, layout(ctx, root, "Publications", content, section="reference",
                        nav=nav_for("reference", route, root), kicker="Reference", h1="Publications",
                        crumbs=[("index.html", "Home"), ("people.html", "People"), (None, "Publications")]))

    # changes
    route = "changes.html"; root = ""
    KIND = {"added": "New", "updated": "Updated", "removed": "Removed", "unlisted": "Unlisted", "relisted": "Relisted", "snapshot": "Snapshot",
            "structure-updated": "Index changed", "nav-updated": "Navigation changed", "file-updated": "PDF replaced", "file-unreferenced": "PDF unlinked"}
    runs = []
    for run in reversed(changelog[-60:]):
        evs = []
        ORDER = {"removed": 0, "unlisted": 1, "added": 2, "updated": 3, "structure-updated": 4, "nav-updated": 5,
                 "file-updated": 6, "file-unreferenced": 7, "snapshot": 8, "touched": 9}
        for e in sorted(run.get("events", []), key=lambda x: ORDER.get(x.get("kind"), 5)):
            kind = e.get("kind"); label = KIND.get(kind, kind)
            if kind == "snapshot":
                types = e.get("types") or {}
                label = "First snapshot"
                link = (f'{e.get("count", 0)} pages mirrored: {types.get("diagnosis", 0)} syndromes, {types.get("drug", 0)} drugs, '
                        f'{types.get("guidelines", 0)} guidelines, {types.get("page", 0)} pages, '
                        f'{types.get("ucsf_person", 0) + types.get("other_person", 0)} people, {types.get("ucsf_publication", 0)} publications')
            elif "nid" in e and e["nid"] in models:
                link = f'<a href="{models[e["nid"]]["route"]}">{esc(e.get("title"))}</a>'
            elif "title" in e:
                link = esc(e.get("title"))
            elif "path" in e:
                link = f'<a class="x-pdf" target="_blank" rel="noopener" href="{esc(urljoin(BASE + "/", e["path"]))}">{esc(e["path"])}</a>'
            else:
                link = esc(e.get("structure") or json.dumps({k: v for k, v in e.items() if k != "kind"}))
            detail = ""
            if kind == "updated" and e.get("fields"):
                its = []
                for fchg in e["fields"]:
                    diff = "\n".join(fchg.get("diff") or [])
                    its.append(f'<div class="fchg"><code>{esc(fchg["field"])}</code>' + (f'<pre class="diff">{esc(diff)}</pre>' if diff else "") + "</div>")
                detail = f'<details><summary>{len(e["fields"])} field(s) changed</summary>{"".join(its)}</details>'
            elif kind == "touched":
                detail = f'<span class="muted">IDMP moved the date {esc(human_date(e.get("changed_from")))} to {esc(human_date(e.get("changed")))}; nothing the mirror shows changed</span>'
            elif kind == "updated":
                detail = f'<span class="muted">changed {esc(human_date(e.get("changed_from")))} to {esc(human_date(e.get("changed")))}</span>'
            evs.append(f'<li><span class="badge ev ev-{esc(kind)}">{esc(label)}</span> {link} <span class="muted">{esc(e.get("type") or "")}</span> {detail}</li>')
        probs = "".join(f'<li class="prob {esc(p["level"])}"><strong>{esc(p["level"])}</strong> {esc(p["code"])}: {esc(p["message"])}</li>' for p in run.get("problems", []))
        if evs or probs:
            runs.append(f'<section class="group run"><h2>{esc(human_date(run["run"]))} <span class="muted">{esc(run["run"][11:16])} UTC</span></h2>{("<ul class=probs>" + probs + "</ul>") if probs else ""}<ul class="events">{"".join(evs)}</ul></section>')
    st_badge = "ok" if status.get("ok") else "bad"
    st_msg = "clean" if status.get("ok") else f'{len([p for p in status.get("problems", []) if p["level"] == "hard"])} problem(s) reported'
    warn_list = ""
    if ctx.warnings:
        its = "".join(f'<li>{esc(w.get("title") or w.get("path") or "")} <span class="muted">{esc("; ".join(w["notes"]))}</span>' + (f' <a href="{w["route"]}">open</a>' if w.get("route") else "") + "</li>" for w in ctx.warnings)
        warn_list = f'<details class="block"><summary>{len(ctx.warnings)} note(s) from this build</summary><ul>{its}</ul></details>'
    content = f"""<p class="lede">Every night the mirror re-reads idmp.ucsf.edu, compares each page's content and <code>changed</code> timestamp with the last copy, and records the difference here. Last run {esc(human_date(status.get("run")))}: <span class="tag st-{st_badge}">{esc(st_msg)}</span>.</p>
{("<ul class=probs>" + "".join(f'<li class="prob {esc(p["level"])}"><strong>{esc(p["level"])}</strong> {esc(p["code"])}: {esc(p["message"])}</li>' for p in status.get("problems", [])) + "</ul>") if status.get("problems") else ""}
{warn_list}
{"".join(runs) or "<p class=muted>No changes recorded yet: this is the first snapshot.</p>"}"""
    write(route, layout(ctx, root, "What changed", content, section="reference",
                        nav=nav_for("reference", route, root), kicker="Reference", h1="What changed on IDMP",
                        crumbs=[("index.html", "Home"), (None, "What changed")]))

    # about
    route = "about.html"; root = ""
    about_nid = ctx.nid_by_path.get("/content/about")
    about_html = ""
    if about_nid in models:
        about_html = f'<section class="block prose"><h2>About the IDMP (from idmp.ucsf.edu)</h2>{sanitize(fv(models[about_nid]["raw"], "field_body") or "", ctx, root, "about")}</section>'
    counts = manifest.get("counts") or {}
    content = f"""<p class="lede">{TAGLINE}</p>
<section class="block prose">
<h2>What it is</h2>
<p>{SITE_NAME} re-publishes the public content of the UCSF Infectious Diseases Management Program site (<a href="{BASE}" target="_blank" rel="noopener">idmp.ucsf.edu</a>) in a layout built for use on the wards: one Ask palette that returns answers, three lenses (hospital, setting, patient) that reorder every page, syndrome pages as a chooser plus one regimen card, one page per drug with a renal dial and the syndromes that recommend it, guidelines by hospital, and antibiograms in a bug-drug explorer. It is a personal project, not affiliated with, endorsed by, or maintained by UCSF or the IDMP.</p>
<h2>Where the mirror adds a layer</h2>
<p>Three things on this site are not on the source page they appear on, and each is labelled: the prescription line on a regimen card pulls doses from the same drug's IDMP dosing table (hover it to see the note; the original cell is always shown underneath); outpatient, inpatient and ICU tags on regimen rows are hand-curated by the site author from the row's own wording; and the "for your hospital" box lists the site's restriction, allergy and step-down policy pages next to matched guidelines. Lenses change what is shown first, never what the source says.</p>
<h2>How it stays current</h2>
<p>A scheduled job re-reads every source page nightly through the site's own content API, stores each page's <code>changed</code> timestamp and a content hash, and rebuilds only what moved. Structural surprises (a page type the mirror does not know, a missing field, a table whose columns cannot be recognised, a vanished index page) are recorded on the <a href="changes.html">What changed</a> page, flagged in the header sync indicator, and raised as an issue on the repository so drift is never silent. Documents on Box or SharePoint need a UCSF login and are linked, not copied.</p>
<h2>Current snapshot</h2>
<ul><li>{counts.get("diagnosis", 0)} empiric-therapy syndromes</li><li>{counts.get("drug", 0)} drug dosing pages</li><li>{counts.get("guidelines", 0)} guidelines</li><li>{counts.get("page", 0)} other pages, including antibiograms</li><li>{len(files)} PDFs tracked</li></ul>
<p class="muted">Sync run {esc(status.get("run") or "")}, repository <a href="https://github.com/{REPO}" target="_blank" rel="noopener">{REPO}</a></p>
</section>
{about_html}"""
    write(route, layout(ctx, root, "About", content, section="reference",
                        nav=nav_for("reference", route, root), kicker="Reference", h1="About this mirror",
                        crumbs=[("index.html", "Home"), (None, "About")]))

    # offline fallback
    write("offline.html", layout(ctx, "", "Offline", '<p class="lede">You are offline and this page was never opened on this device. Pages you have already visited are available. Use "Save offline" in the footer to keep the whole site next time.</p>',
                                 section="reference", kicker="Offline", h1="This page is not saved yet", nav=nav_for("reference", "offline.html", "")))

    # home: front matter of a manual - what is here, how much of it, and the fastest ways in
    route = "index.html"; root = ""
    def links_col(key):
        out = []
        for alias, label in curation["home"].get(key, []):
            r = route_for_alias(alias)
            if r:
                out.append(f'<li><a href="{r}">{esc(label)}</a></li>')
        return "".join(out)
    recent = sorted((m for m in models.values() if m.get("changed") and m["type"] in ("diagnosis", "drug", "guidelines", "page")),
                    key=lambda m: m["changed"], reverse=True)[:6]
    recent_html = "".join(
        f'<li><a href="{m["route"]}">{esc(m["title"])}</a><span class="when">{esc(human_date(m["changed"]))}</span></li>' for m in recent)
    n_adult = len([m for m in by_type["diagnosis"] if not is_peds(m)])
    n_peds = len([m for m in by_type["diagnosis"] if is_peds(m)])
    counts = manifest.get("counts") or {}
    sections_tbl = [
        ("empiric/index.html", "Empiric therapy", f"{n_adult} adult and {n_peds} pediatric syndromes", "Pathogens, first choice, alternative, duration. One card per clinical situation."),
        ("drugs/index.html", "Dosing", f'{counts.get("drug", 0)} agents', "Renal bands, HD and CRRT, restriction status, and the syndromes that recommend each drug."),
        ("antibiograms/explore.html", "Antibiograms", f"{len(abx_tables)} parsed tables", "Local susceptibility by organism or by drug, shaded, from the UCSF adult reports."),
        ("guidelines/index.html", "Guidelines & policies", f'{counts.get("guidelines", 0)} documents', "UCSF Health, ZSFG, the VA and BCH, with restriction and allergy pathways."),
    ]
    sec_rows = "".join(
        f'<a class="srow" href="{h}"><span class="srow-t">{esc(t)}</span><span class="srow-n">{esc(n)}</span><span class="srow-d">{esc(d)}</span></a>'
        for h, t, n, d in sections_tbl)
    banner = ""
    if status and not status.get("ok"):
        banner = f'<div class="note note-warn"><b>Heads up</b> The last sync ({esc(human_date(status.get("run")))}) reported problems, so some content may be stale or shown in fallback layout. <a href="changes.html">Details</a>.</div>'
    content = f"""{banner}
<section class="hero">
  <h1>What do I give,<br>and how much?</h1>
  <p class="hero-sub">An unofficial mirror of <a href="{BASE}" target="_blank" rel="noopener">idmp.ucsf.edu</a>, rebuilt nightly and reorganised for the wards. Ask in plain words, or use the index on the left.</p>
  <button type="button" class="hero-ask" data-open="palette"><span class="ask-i">/</span><span class="ask-t"><i>cap icu</i><i>cefepime crcl 30</i><i>e coli cipro</i><i>hap zsfg</i></span><kbd>⌘K</kbd></button>
</section>
<section class="sections">{sec_rows}</section>
<section class="cols">
  <div class="col" data-for="inpatient_adult"><h2>Inpatient, adult</h2><ul class="tight">{links_col("inpatient_adult")}</ul></div>
  <div class="col" data-for="outpatient_adult"><h2>Outpatient, adult</h2><ul class="tight">{links_col("outpatient_adult")}</ul></div>
  <div class="col" data-for="inpatient_peds"><h2>Inpatient, pediatric</h2><ul class="tight">{links_col("inpatient_peds")}</ul></div>
  <div class="col" data-for="outpatient_peds"><h2>Outpatient, pediatric</h2><ul class="tight">{links_col("outpatient_peds")}</ul></div>
</section>
<section class="cols" id="mine" hidden><div class="col"><h2>Pinned and recent</h2><ul class="tight" id="mine-list"></ul></div></section>
<section class="cols">
  <div class="col"><h2>Last edited on IDMP</h2><ul class="tight dated">{recent_html}</ul></div>
  <div class="col"><h2>How to read this site</h2><ul class="tight notes">
    <li>Drug names open dosing in a side panel; the page you came from stays put.</li>
    <li>The prescription line above each regimen takes doses from that drug's own IDMP table and says so. The original cell is always underneath.</li>
    <li><span class="tag t-ucsf">ID-R UCSF</span> and <span class="tag t-zsfg">ID-R ZSFG</span> mark restricted agents. <span class="tag k-login">UCSF login</span> marks Box or SharePoint documents.</li>
    <li>Set your hospital and patient once in the context button; every page reorders itself.</li>
    <li>Press <kbd>⌘K</kbd> or <kbd>/</kbd> to ask from anywhere.</li>
  </ul></div>
</section>"""
    write(route, layout(ctx, root, "Home", content, section="", page_class="home", nav=nav_for("", "index.html", root)))

    # ---- search index, pages list, pwa, status, images
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(search, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "search.json"), "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in e.items() if k in ("t", "n", "u", "k", "s")} for e in search], f, ensure_ascii=False, separators=(",", ":"))
    ver = ctx.asset_ver
    manifest_pwa = {"name": SITE_NAME, "short_name": "IDMP Atlas", "description": TAGLINE, "start_url": "./index.html", "scope": "./",
                    "display": "standalone", "background_color": "#ffffff", "theme_color": "#0a0c12",
                    "icons": [{"src": "assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
                              {"src": "assets/icon-512.png", "sizes": "512x512", "type": "image/png"},
                              {"src": "assets/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    with open(os.path.join(OUT, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(manifest_pwa, f, indent=1)
    sw = open(os.path.join(SITE, "sw.js"), encoding="utf-8").read().replace("__VER__", ver)
    open(os.path.join(OUT, "sw.js"), "w", encoding="utf-8").write(sw)
    st = dict(status or {})
    st["built"] = build_time.isoformat()
    st["build_warnings"] = len(ctx.warnings)
    st["counts"] = manifest.get("counts")
    st["ver"] = ver
    with open(os.path.join(OUT, "status.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    for name, raw in ctx.images.items():
        with open(os.path.join(OUT, "img", name), "wb") as f:
            f.write(raw)
    for sub in ("empiric", "drugs", "guidelines", "antibiograms", "pages", "people"):
        d = os.path.join(OUT, sub)
        for fn in os.listdir(d):
            if fn.endswith(".html") and f"{sub}/{fn}" not in written:
                os.remove(os.path.join(d, fn))
    pages_list = sorted(written) + ["index.json", "antibiogram.json", "status.json", "nav.json", f"assets/site.css?v={ver}", f"assets/site.js?v={ver}", "assets/icon-192.png", "manifest.webmanifest"]
    pages_list += ["thumbs/" + fn for fn in os.listdir(os.path.join(OUT, "thumbs")) if fn.endswith(".jpg")]
    pages_list += ["img/" + fn for fn in os.listdir(os.path.join(OUT, "img"))]
    with open(os.path.join(OUT, "nav.json"), "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "pages.json"), "w", encoding="utf-8") as f:
        json.dump({"ver": ver, "pages": pages_list}, f)
    with open(os.path.join(DATA, "build-warnings.json"), "w", encoding="utf-8") as f:
        json.dump(ctx.warnings, f, indent=1, ensure_ascii=False)
    print(f"built {len(written)} pages, {len(search)} search entries, {len(abx_tables)} antibiogram tables, {len(ctx.images)} images, {len(ctx.warnings)} notes")
    return 0

if __name__ == "__main__":
    sys.exit(main())
