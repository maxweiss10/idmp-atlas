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

def regimen_line(cell_html, dose_data, drug_titles, context=""):
    """Read a first-choice cell and return (segments, html). Segments describe linked drugs and the
    connector words between them; the html is the prescription-style line with dose strips."""
    soup = BeautifulSoup(cell_html or "", "lxml")
    body = soup.body
    if body is None:
        return [], ""
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
    drugs = [i for i, t in enumerate(tokens) if t[0] == "d"]
    if not drugs:
        return [], ""
    segs = []
    for k, i in enumerate(drugs):
        nxt = drugs[k + 1] if k + 1 < len(drugs) else len(tokens)
        after = "".join(t[1] for t in tokens[i + 1:nxt] if t[0] == "t")
        before = "".join(t[1] for t in tokens[(drugs[k - 1] + 1 if k else 0):i] if t[0] == "t") if k == 0 else ""
        slug, name, href = tokens[i][1], tokens[i][2], tokens[i][3]
        conj = ""
        a = re.sub(r"\s+", " ", after).strip()
        if re.search(r"with or without|\+/-|±|\bmay add\b|\boptional\b", a, flags=re.I):
            conj = "±"
        elif re.search(r"\bplus\b|\band\b|\+|\bwith\b|\bfollowed by\b", a, flags=re.I):
            conj = "+"
        elif re.search(r"\bor\b|\beither\b", a, flags=re.I):
            conj = "or"
        segs.append({"slug": slug, "name": name, "href": href, "inline_dose": bool(DOSE_RX.search(a[:80])) or bool(DOSE_RX.search(name)),
                     "conj": conj if k + 1 < len(drugs) else "", "note": ""})
    # if every drug already carries a dose in the cell, a synthesized line adds nothing
    out = ['<div class="rxline">']
    for s in segs:
        dd = dose_data.get(s["slug"])
        out.append(f'<span class="rxdrug"><a data-drug="{esc(s["slug"])}" href="{esc(s["href"])}">{esc(drug_titles.get(s["slug"], s["name"]))}</a>')
        if dd and not s["inline_dose"]:
            ri, matched = pick_dose_row(dd, context)
            title = ("Dose for this indication from the IDMP dosing table for this drug." if matched
                     else "Standard dose from the IDMP dosing table for this drug.") + " Set a renal function to switch columns."
            out.append(f'<span class="dose" data-dose="{esc(s["slug"])}" data-row="{ri}" data-matched="{int(matched)}" title="{esc(title)}">{dose_strip_html(dd, 0, ri, matched)}</span>')
        elif not dd and not s["inline_dose"]:
            out.append('<span class="dose muted">see page</span>')
        out.append("</span>")
        if s["conj"]:
            out.append(f'<span class="conj">{esc(s["conj"])}</span>')
    out.append("</div>")
    return segs, "".join(out)

DOSE_STOP = {"the", "and", "or", "of", "in", "for", "with", "a", "an", "to", "at", "on", "infection", "infections",
             "including", "dosing", "dose", "standard", "usual", "all", "other", "adult", "adults", "therapy", "treatment",
             "acute", "severe", "non", "suspected", "documented"}
def dose_tokens(s):
    out = set()
    for w in re.findall(r"[a-z]{4,}", (s or "").lower()):
        if w in DOSE_STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 5 else w)
    return out

def pick_dose_row(dd, context):
    """Choose the dosing row that matches the syndrome, else the standard/first row."""
    ctx = dose_tokens(context)
    best, best_score = 0, 0
    for i, r in enumerate(dd["rows"]):
        score = len(ctx & dose_tokens(r["i"]))
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
    if "zsfg" in slug: return "ID-R · ZSFG"
    if "ucsf" in slug: return "ID-R · UCSF"
    if "iv-po" in slug: return "IV → PO"
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
NAV = [("empiric/index.html", "Empiric therapy"), ("drugs/index.html", "Dosing"), ("antibiograms/index.html", "Antibiograms"),
       ("guidelines/index.html", "Guidelines"), ("peds.html", "Pediatrics"), ("changes.html", "What changed"), ("people.html", "People")]

def layout(ctx, root, title, content, *, desc="", node=None, section="", extra_head="", crumbs=None, fallback_notes=None,
           rail="", fresh=None, page_class=""):
    ver = ctx.asset_ver
    crumb_html = ""
    if crumbs:
        crumb_html = '<nav class="crumbs">' + " <span>/</span> ".join(
            f'<a href="{root}{h}">{esc(t)}</a>' if h else f"<span>{esc(t)}</span>" for h, t in crumbs) + "</nav>"
    src_html = ""
    if node:
        src_html = (f'<div class="src"><a class="x-source" target="_blank" rel="noopener" href="{esc(node["source_url"])}">View on idmp.ucsf.edu</a>'
                    f'<span>IDMP last changed <time datetime="{esc(node["changed"])}">{esc(human_date(node["changed"]))}</time></span>'
                    f'<button class="fav" data-fav="{esc(node["route"])}" data-title="{esc(node["title"])}" data-kind="{esc(node["type"])}" aria-label="Pin this page">☆ Pin</button></div>')
    warn_html = ""
    if fallback_notes:
        warn_html = ('<div class="notice warn"><strong>Layout note:</strong> part of this page did not match the layout the '
                     'mirror expects, so it is shown in its original form. ' + esc("; ".join(fallback_notes)) + '.</div>')
    status = ctx.status or {}
    nav_html = "".join(f'<a href="{root}{h}"{" class=on" if section == h.split("/")[0].replace(".html", "") else ""}>{esc(t)}</a>' for h, t in NAV)
    return f"""<!doctype html>
<html lang="en" data-root="{root}" data-section="{esc(section)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(title)} · {SITE_NAME}</title>
<meta name="description" content="{esc(desc or TAGLINE)}">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#08090a" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="{SITE_NAME}">
<link rel="manifest" href="{root}manifest.webmanifest">
<link rel="icon" href="{root}assets/icon-192.png">
<link rel="apple-touch-icon" href="{root}assets/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,300..700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<link rel="stylesheet" href="{root}assets/site.css?v={ver}">
<script>try{{var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t);var L=JSON.parse(localStorage.getItem('lens')||'{{}}');for(var k in L)if(L[k])document.documentElement.setAttribute('data-'+k,L[k]);}}catch(e){{}}</script>
{extra_head}
</head>
<body class="{esc(page_class)}">
<a class="skip" href="#main">Skip to content</a>
<header class="top">
  <div class="top-row">
    <a class="brand" href="{root}index.html"><img src="{root}assets/icon-192.png" alt="" width="22" height="22"><span class="brand-name">{SITE_NAME}</span><span class="brand-sub">unofficial mirror</span></a>
    <button class="ask" id="ask-open" type="button"><span class="ask-icon">⌕</span><span class="ask-text">Search or ask: cap icu, cefepime crcl 30, e coli cipro…</span><kbd>⌘K</kbd></button>
    <div class="top-tools">
      <button class="lens-btn" id="lens-open" type="button" title="Where / Setting / Patient"><span id="lens-summary">All sites · Any setting · Adult</span></button>
      <button class="theme" id="theme" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">☾</button>
    </div>
  </div>
  <nav class="nav" aria-label="Sections">{nav_html}<a class="status" id="status" href="{root}changes.html" title="Sync status">sync</a></nav>
</header>
<div class="page{" with-rail" if rail else ""}">
<main id="main" class="wrap">
{crumb_html}
{warn_html}
{content}
{src_html}
</main>
{f'<aside class="rail">{rail}</aside>' if rail else ""}
</div>
<footer class="foot">
  <p><strong>{SITE_NAME}</strong> is an unofficial, read-only mirror of <a href="{BASE}" target="_blank" rel="noopener">idmp.ucsf.edu</a>, rebuilt nightly. Not affiliated with UCSF or the IDMP. Content belongs to its authors; confirm against the source before acting.
  Mirror last verified against the source <time datetime="{esc(status.get('run',''))}">{esc(human_date(status.get('run')))}</time>. <a href="{root}about.html">About</a> · <a href="{root}publications.html">Publications</a> · <a href="https://github.com/{REPO}" target="_blank" rel="noopener">Source code</a> · <button class="linklike" id="offline-btn" type="button">Save for offline</button></p>
</footer>
<nav class="tabbar" aria-label="Quick navigation">
  <button type="button" data-open="palette"><span class="ti">⌕</span>Ask</button>
  <a href="{root}empiric/index.html"><span class="ti">Rx</span>Empiric</a>
  <a href="{root}drugs/index.html"><span class="ti">mg</span>Dosing</a>
  <a href="{root}antibiograms/explore.html"><span class="ti">%</span>Bugs</a>
  <button type="button" data-open="more"><span class="ti">⋯</span>More</button>
</nav>
<div class="modal" id="palette" hidden><div class="modal-box palette-box" role="dialog" aria-label="Search">
  <div class="palette-head"><span class="ask-icon">⌕</span><input type="search" id="q" placeholder="Syndrome, drug + CrCl, organism + drug, guideline…" autocomplete="off" aria-label="Search"><button class="modal-close" data-close type="button">Esc</button></div>
  <div class="palette-body" id="results"></div>
  <div class="palette-foot"><span><kbd>↑↓</kbd> move</span><span><kbd>↵</kbd> open</span><span><kbd>⌘K</kbd> anywhere</span><span class="grow"></span><span id="palette-hint">Try: <b>hap zsfg</b> · <b>vanc hd</b> · <b>pseudomonas cefepime</b></span></div>
</div></div>
<div class="modal" id="lens" hidden><div class="modal-box lens-box" role="dialog" aria-label="Context">
  <div class="lens-head"><strong>Your context</strong><span class="muted">Applied everywhere, remembered on this device.</span><button class="modal-close" data-close type="button">Done</button></div>
  <div class="lens-grid">
    <div class="lens-row"><label>Where</label><div class="seg" data-lens="where"><button data-v="">All</button><button data-v="ucsf">UCSF Health</button><button data-v="zsfg">ZSFG</button><button data-v="va">VA</button><button data-v="bch">BCH</button></div></div>
    <div class="lens-row"><label>Setting</label><div class="seg" data-lens="setting"><button data-v="">Any</button><button data-v="outpatient">Outpatient</button><button data-v="inpatient">Inpatient</button><button data-v="icu">ICU</button></div></div>
    <div class="lens-row"><label>Patient</label><div class="seg" data-lens="patient"><button data-v="">Adult</button><button data-v="peds">Pediatric</button></div></div>
    <div class="lens-row"><label>Renal</label><div class="renal"><input type="number" id="lens-crcl" inputmode="numeric" min="0" max="250" placeholder="CrCl"><span class="unit">mL/min</span><div class="seg" data-lens="renal"><button data-v="">Any</button><button data-v="hd">HD</button><button data-v="crrt">CRRT</button></div></div></div>
    <div class="lens-row"><label>Allergy</label><div class="seg" data-lens="allergy"><button data-v="">None</button><button data-v="bl">Severe beta-lactam allergy</button></div></div>
  </div>
  <div class="lens-foot"><button type="button" id="lens-reset" class="linklike">Reset all</button><span class="muted">Lenses change what is shown first, never what the source says.</span></div>
</div></div>
<div class="modal" id="more" hidden><div class="modal-box more-box" role="dialog" aria-label="More"><div class="lens-head"><strong>More</strong><button class="modal-close" data-close type="button">Close</button></div>
  <ul class="more-list">{"".join(f'<li><a href="{root}{h}">{esc(t)}</a></li>' for h, t in NAV)}<li><a href="{root}antibiograms/explore.html">Bug-drug explorer</a></li><li><a href="{root}publications.html">Publications</a></li><li><a href="{root}about.html">About this mirror</a></li><li><button type="button" class="linklike" data-open="lens">Your context (Where / Setting / Patient)</button></li><li><button type="button" class="linklike" id="offline-btn-2">Save whole site for offline</button></li></ul>
</div></div>
<div class="drawer" id="drawer" hidden><div class="drawer-bar"><span class="handle"></span><button class="drawer-back" id="drawer-back" type="button" hidden>← Back</button><a class="drawer-open" id="drawer-open" href="#">Open full page</a><button class="drawer-close" id="drawer-close" type="button" aria-label="Close">✕</button></div><div class="drawer-body" id="drawer-body"></div></div>
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
    css = open(os.path.join(SITE, "assets", "site.css"), encoding="utf-8").read()
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
                ctx.warnings.append({"title": slug, "route": f"empiric/{slug}.html", "notes": [f"info: curated setting tags skipped, row count changed ({cur['n']} → {total})"]})
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
            all_rows = [r for kind, r in doc if kind == "rows"]
            total = sum(len(r) for r in all_rows)
            page_dose = {}
            idx = 0
            row_index = []
            for kind, payload in doc:
                if kind == "html":
                    parts.append(payload)
                elif kind == "table":
                    parts.append(payload)
                elif kind == "notes":
                    notes += payload
                elif kind == "rows":
                    chooser, cards = [], []
                    for cells in payload:
                        idx += 1
                        cond = cells.get("condition")
                        label = text_only(cond["html"]) if cond else f"Row {idx}"
                        short = short_label(m["title"], label)
                        tags = row_tags(m["slug"], idx, label, total)
                        anchor = f"rx-{idx}"
                        row_index.append({"a": anchor, "l": short, "tags": tags})
                        chooser.append(f'<button type="button" data-row="{anchor}" data-tags="{esc(" ".join(tags))}" title="{esc(re.sub(r"\s+", " ", label)[:160])}">{esc(short)}</button>')
                        first = cells.get("first"); alt = cells.get("alt")
                        segs, rxl = regimen_line(first["html"] if first else "", dose_data_all, drug_titles, context=m["title"] + " " + label)
                        for s in segs:
                            if s["slug"] in dose_data_all:
                                page_dose[s["slug"]] = dose_data_all[s["slug"]]
                        ivpo = [drug_titles[s["slug"]] for s in segs if "iv-po" in drug_tags.get(s["slug"], [])]
                        card = [f'<article class="rx" id="{anchor}" data-tags="{esc(" ".join(tags))}">']
                        card.append('<header class="rx-head">')
                        card.append(f'<div class="rx-title">{cond["html"] if cond else "<em>Any</em>"}</div>')
                        card.append('<div class="rx-tools">')
                        if cells.get("duration") and cells["duration"]["text"]:
                            card.append(f'<div class="rx-dur"><span class="rx-dur-k">Duration</span>{cells["duration"]["html"]}</div>')
                        card.append(f'<button type="button" class="copy" data-copy="{anchor}" title="Copy regimen as plain text for a note">Copy</button>')
                        card.append('</div></header>')
                        if rxl:
                            card.append(rxl)
                        panes = [k for k in ("first", "alt", "pathogens") if k in cells and cells[k]["text"]]
                        if panes:
                            card.append(f'<div class="rx-grid" data-panes="{len(panes)}">')
                            for key in panes:
                                card.append(f'<section class="rx-col rx-{key}"><h4>{LABELS[key]}</h4><div class="rx-body">{cells[key]["html"]}</div></section>')
                            card.append("</div>")
                        if cells.get("comments") and cells["comments"]["text"]:
                            ch = cells["comments"]["html"]
                            csoup = BeautifulSoup(ch, "lxml")
                            for ul in csoup.find_all(["ul", "ol"]):
                                prev = ul.find_previous_sibling(["p", "h3", "h4", "strong"])
                                if prev is not None and re.search(r"if any|consider|criteria|risk factor|indication|following|when", prev.get_text(" "), flags=re.I):
                                    ul["class"] = (ul.get("class") or []) + ["check"]
                            ch = csoup.body.decode_contents() if csoup.body else ch
                            card.append(f'<section class="rx-notes"><h4>Comments</h4><div class="rx-body">{ch}</div></section>')
                        for key, cell in cells.items():
                            if key.startswith("extra:") and cell["text"]:
                                card.append(f'<section class="rx-notes"><h4>{esc(key[6:])}</h4><div class="rx-body">{cell["html"]}</div></section>')
                        # then-what
                        tw = []
                        consult = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text_only(cells["comments"]["html"] if cells.get("comments") else "")) if re.search(r"\bID\b.*consult|infectious diseases? consult|consult(ation)? (is )?recommended", s, flags=re.I)][:2]
                        if ivpo:
                            r = route_for_alias(site_cur["ucsf"]["ivpo"]) or ""
                            tw.append(f'<li><b>IV → PO candidates:</b> {esc(", ".join(dict.fromkeys(ivpo)))}' + (f' · <a href="{root}{r}">step-down guidance</a>' if r else "") + "</li>")
                        for c in consult:
                            tw.append(f'<li><b>Consult:</b> {esc(c)}</li>')
                        if tw:
                            card.append('<section class="then-what"><h4>Then what</h4><ul>' + "".join(tw) + "</ul></section>")
                        card.append("</article>")
                        cards.append("".join(card))
                    if len(chooser) > 1:
                        parts.append(f'<div class="chooser" role="group" aria-label="Choose the clinical situation">{"".join(chooser)}<button type="button" class="chooser-all" data-row="all">All rows</button></div>')
                    parts.append(f'<div class="rx-list">{"".join(cards)}</div>')
            if page_dose:
                parts.append(f'<script type="application/json" id="dose-data">{jdump(page_dose)}</script>')
            extra = fv(n, "field_notes")
            if extra and text_only(extra):
                parts.append(f'<section class="block"><h2>Notes</h2>{sanitize(extra, ctx, root, m["slug"])}</section>')
            refs = fv(n, "field_references")
            if refs and text_only(refs):
                parts.append(f'<details class="block refs"><summary>References</summary>{sanitize(refs, ctx, root, m["slug"])}</details>')
            # site-aware related links
            rel = related_guidelines(m)
            site_items = site_link_list()
            box = ['<aside class="block related" id="related"><h2>For your hospital</h2><p class="muted">Guidelines matched by title, plus each site\'s restriction and allergy policies. Your Where lens brings the matching hospital to the top.</p>']
            for key, label, links in site_items:
                gl = [g for g in rel if any(s["group"] == key for s in gmeta[g["nid"]]["sites"])]
                if not gl and not links:
                    continue
                lis = "".join(f'<li><a href="{root}{g["route"]}">{esc(g["title"])}</a> {kind_badges(gmeta[g["nid"]]["kinds"])}</li>' for g in gl)
                lis += "".join(f'<li class="policy"><a href="{root}{r}">{esc(lbl)}</a></li>' for r, lbl in links)
                box.append(f'<section class="site-sec" data-site="{key}"><h3><span class="badge site s-{key}">{esc(label)}</span></h3><ul class="rel-list">{lis}</ul></section>')
            other = [g for g in rel if not gmeta[g["nid"]]["sites"]]
            if other:
                box.append('<section class="site-sec" data-site=""><h3>General</h3><ul class="rel-list">' + "".join(f'<li><a href="{root}{g["route"]}">{esc(g["title"])}</a></li>' for g in other) + "</ul></section>")
            box.append("</aside>")
            parts.append("".join(box))
            return "\n".join(parts), notes, {"popname": popname, "rows": row_index}
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
                parts.append(f'<div class="pdf-card">{thumb}<div><p class="cta"><a class="btn x-pdf" target="_blank" rel="noopener" href="{esc(full)}">Open guideline PDF</a></p><p class="muted">Hosted on idmp.ucsf.edu, no login. {esc(" · ".join(x for x in (pages_h, size_h) if x))}</p></div></div>')
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
    section_of = {"diagnosis": "empiric", "drug": "drugs", "guidelines": "guidelines", "ucsf_person": "people", "other_person": "people"}
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
        crumbs.append((None, m["title"]))
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
        content = f'<article class="node node-{m["type"]}" data-slug="{esc(m["slug"])}"><p class="kicker">{esc(kicker)} {fresh}</p><h1>{esc(m["title"])}</h1>{body}</article>'
        write(route, layout(ctx, root, m["title"], content, node=m, section=sec, crumbs=crumbs, fallback_notes=notes, rail=rail,
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
                    sub = '<div class="sub">' + " · ".join(f'<a href="{root}{m["route"]}#{r["a"]}" data-tags="{esc(" ".join(r["tags"]))}">{esc(r["l"][:48])}</a>' for r in rows[:6]) + ("…" if len(rows) > 6 else "") + "</div>"
                lis.append(f'<li data-tags="{esc(" ".join(tags))}"><a href="{root}{m["route"]}">{esc(m["title"])}</a>{sub}</li>')
            cards.append(f'<section class="group" id="g-{slugify(h or "other")}"><h2>{esc(h or "Other")}</h2><ul class="links">{"".join(lis)}</ul></section>')
        content = f"""<div class="page-head"><p class="kicker">Empiric therapy</p><h1>{popname} empiric antimicrobial therapy</h1>
<p class="lede">Initial regimens by syndrome from the {"UCSF Benioff Children's Hospitals" if popname == "Pediatric" else "UCSF Health"} antimicrobial stewardship programs. Pick the clinical situation on each page; drug names open their dosing without leaving the page. <a href="{root}{other_route}">Switch to {other_label} →</a></p>
<p class="muted">These recommendations assist clinical decision-making for common situations and cannot replace individualized evaluation, including history of multidrug-resistant organisms.</p></div>
<div class="filter"><input type="search" id="filter" placeholder="Filter syndromes…" aria-label="Filter list"><div class="chips" id="chips"><button data-chip="outpatient">Outpatient</button><button data-chip="inpatient">Inpatient</button><button data-chip="icu">ICU</button></div></div>
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
        tags = drug_tags.get(m["slug"], [])
        badges = "".join(f'<span class="badge tag t-{"zsfg" if "zsfg" in s else "ucsf" if "ucsf" in s else "ivpo" if "iv-po" in s else "misc"}">{esc(short_notation(s, tax_name("/notations/" + s)))}</span>' for s in tags)
        has_hd = bool(fv(n, "field_dosing_antimicrobial_dosin")) and (fv(n, "field_bool_hemodialysis") is not False)
        low = m["title"].lower()
        syn = [a for key, alts in synonyms.items() if key in low for a in alts]
        dd = dose_data_all.get(m["slug"])
        first_dose = ""
        if dd and dd["rows"]:
            first_dose = f'<span class="muted mono">{esc(dd["rows"][0]["d"][0][:40])}</span>'
        rows.append(f'<li data-tags="{esc(" ".join(tags))}{" hd" if has_hd else ""}" data-syn="{esc(" ".join(syn))}"><a href="{root}{m["route"]}">{esc(m["title"])}</a>{first_dose}<span class="row-badges">{badges}{"<span class=badge>HD/CRRT</span>" if has_hd else ""}</span></li>')
    content = f"""<div class="page-head"><p class="kicker">Dosing</p><h1>Adult antimicrobial dosing</h1>
<p class="lede">Renal-function and dialysis dosing for {len(rows)} agents, one page per drug with a renal dial. Brand names and ward shorthand work here and in the Ask palette (Zosyn, pip-tazo, vanc, Bactrim…).</p>
<p class="muted">Source guidance: dosing recommendations are based on available literature and do not replace clinical judgement. Pediatric and neonatal dosing live under <a href="{root}peds.html">Pediatrics</a>.</p></div>
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
    cards = []
    for h, items in groups:
        lis = []
        for m in sorted(items, key=lambda x: x["title"].lower()):
            gm = gmeta[m["nid"]]
            date = f'<span class="gl-date">{esc(human_date(gm["latest"]))}</span>' if gm["latest"] else ""
            thumb = f'<img class="mini-thumb" loading="lazy" src="{root}thumbs/{gm["thumb"]}.jpg" alt="">' if gm["thumb"] else '<span class="mini-thumb blank"></span>'
            lis.append(f'<li data-sites="{esc(" ".join(sorted({s["group"] for s in gm["sites"]})))}" data-cat="{esc(gm["cat_slug"])}">{thumb}<div class="gl-main"><a href="{root}{m["route"]}">{esc(m["title"])}</a>{("<p class=desc>" + esc(gm["desc"]) + "</p>") if gm["desc"] else ""}<span class="row-badges">{site_badges(gm["sites"])}{kind_badges(gm["kinds"])}{date}</span></div></li>')
        cards.append(f'<section class="group" data-cat="{slugify(h or "other")}"><h2>{esc(h or "Other")}</h2><ul class="links gl">{"".join(lis)}</ul></section>')
    content = f"""<div class="page-head"><p class="kicker">Guidelines</p><h1>Institutional guidelines</h1>
<p class="lede">{len(by_type["guidelines"])} guidelines across UCSF Health, ZSFG, the VA and the Benioff Children's Hospitals. Your Where lens brings your hospital's to the top; <span class="badge kind k-pdf">PDF</span> opens on idmp.ucsf.edu without login and <span class="badge kind k-login">UCSF login</span> means Box or SharePoint.</p></div>
<div class="filter"><input type="search" id="filter" placeholder="Filter guidelines…" aria-label="Filter list">
<div class="chips" id="chips"><button data-chip="site:ucsf">UCSF Health</button><button data-chip="site:zsfg">ZSFG</button><button data-chip="site:va">VA</button><button data-chip="site:bch">BCH</button></div></div>
<div class="groups" id="groups">{"".join(cards)}</div>"""
    write(route, layout(ctx, root, "Guidelines", content, section="guidelines", crumbs=[("index.html", "Home"), (None, "Guidelines")]))

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
            lis.append(f'<li><a class="big" href="{root}{m["route"]}">{esc(m["title"])}</a><div class="sub">{" · ".join(dict.fromkeys(sub))}</div></li>')
    content = f"""<div class="page-head"><p class="kicker">Antibiograms</p><h1>Antibiograms</h1>
<p class="lede">Aggregate susceptibility by hospital. Tables published as HTML feed the <a href="{root}antibiograms/explore.html">bug-drug explorer</a> and the Ask palette (try <b>e coli cipro</b>); reports published only as PDF open on idmp.ucsf.edu.</p></div>
<p class="cta"><a class="btn" href="{root}antibiograms/explore.html">Open the bug-drug explorer</a></p>
<ul class="links abx">{"".join(lis)}</ul>"""
    write(route, layout(ctx, root, "Antibiograms", content, section="antibiograms", crumbs=[("index.html", "Home"), (None, "Antibiograms")]))
    route = "antibiograms/explore.html"; root = root_for(route)
    content = f"""<div class="page-head"><p class="kicker">Antibiograms</p><h1>Bug-drug explorer</h1>
<p class="lede">Pick an organism to see every drug, or a drug to see every organism, across the UCSF adult tables the mirror could parse. Values are % susceptible as published; shading is added by the mirror.</p></div>
<div class="explore" id="explore"><div class="explore-controls"><label>Organism <input list="bug-list" id="bug" placeholder="Escherichia coli"><datalist id="bug-list"></datalist></label><span class="muted">or</span><label>Drug <input list="drug-list" id="abx-drug" placeholder="ciprofloxacin"><datalist id="drug-list"></datalist></label></div><div id="explore-out"><p class="muted">Loading tables…</p></div></div>
<p class="legend"><span class="s-hi">≥ 90 %</span><span class="s-ok">80–89 %</span><span class="s-mid">60–79 %</span><span class="s-lo">&lt; 60 %</span><span class="s-r">R</span></p>"""
    write(route, layout(ctx, root, "Bug-drug explorer", content, section="antibiograms", crumbs=[("index.html", "Home"), ("antibiograms/index.html", "Antibiograms"), (None, "Explorer")], page_class="explore-page"))
    with open(os.path.join(OUT, "antibiogram.json"), "w", encoding="utf-8") as f:
        json.dump({"tables": abx_tables, "built": build_time.isoformat()}, f, ensure_ascii=False, separators=(",", ":"))

    # peds hub
    route = "peds.html"; root = ""
    peds_pages = [m for m in models.values() if m["type"] == "page" and re.search(r"pediatric|neonatal|benioff|children|kocher|icn", m["title"] + m["alias"], flags=re.I) and m["nid"] not in abx_nids]
    peds_gl = [m for m in by_type["guidelines"] if gmeta[m["nid"]]["sites"] and all(s["group"] == "bch" for s in gmeta[m["nid"]]["sites"])]
    def ul(items):
        return '<ul class="links">' + "".join(f'<li><a href="{root}{m["route"]}">{esc(m["title"])}</a></li>' for m in sorted(items, key=lambda x: x["title"].lower())) + "</ul>"
    content = f"""<div class="page-head"><p class="kicker">Pediatrics</p><h1>Pediatrics and neonatal</h1>
<p class="lede">Benioff Children's Hospitals content: empiric therapy by syndrome, the pediatric and neonatal dosing cards, and BCH-only guidelines. Set Patient to Pediatric in your context to make the home screen and search favour these.</p></div>
<section class="group"><h2>Start here</h2><ul class="links"><li><a class="big" href="empiric/peds.html">Pediatric empiric therapy by syndrome</a></li></ul></section>
<section class="group"><h2>Dosing cards and pediatric pages</h2>{ul(peds_pages)}</section>
<section class="group"><h2>Guidelines for BCH sites only</h2>{ul(peds_gl)}</section>"""
    write(route, layout(ctx, root, "Pediatrics", content, section="peds", crumbs=[("index.html", "Home"), (None, "Pediatrics")]))

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
        pmid = fv(n, "field_publication_pubmedid")
        links = []
        if pmid:
            links.append(f'<a class="x-external" target="_blank" rel="noopener" href="https://pubmed.ncbi.nlm.nih.gov/{esc(pmid)}/">PubMed</a>')
        prof = fv(n, "field_publication_id")
        if prof and prof.startswith("http"):
            links.append(f'<a class="x-external" target="_blank" rel="noopener" href="{esc(prof)}">Profile</a>')
        items.append(f'<li id="p{m["nid"]}"><div class="cite">{sanitize(cite, ctx, root, m["slug"])}</div><div class="muted">{esc(fv(n, "field_publication_year") or "")} · {" · ".join(links)}</div></li>')
        search.append({"t": "pub", "n": m["title"], "u": m["route"], "k": text_only(fv(n, "field_publication_authorlist", "processed") or "")[:300], "s": (fv(n, "field_publication_year") or "")})
    content = f'<div class="page-head"><p class="kicker">Publications</p><h1>Publications</h1><p class="lede">Papers listed on IDMP team members\' pages, newest first.</p></div><ul class="pubs">{"".join(items) or "<li class=muted>None listed.</li>"}</ul>'
    write(route, layout(ctx, root, "Publications", content, section="people", crumbs=[("index.html", "Home"), ("people.html", "People"), (None, "Publications")]))

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
                detail = f'<span class="muted">IDMP moved the date {esc(human_date(e.get("changed_from")))} → {esc(human_date(e.get("changed")))}; nothing the mirror shows changed</span>'
            elif kind == "updated":
                detail = f'<span class="muted">changed {esc(human_date(e.get("changed_from")))} → {esc(human_date(e.get("changed")))}</span>'
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
        about_html = f'<section class="block prose"><h2>About the IDMP (from idmp.ucsf.edu)</h2>{sanitize(fv(models[about_nid]["raw"], "field_body") or "", ctx, root, "about")}</section>'
    counts = manifest.get("counts") or {}
    content = f"""<div class="page-head"><p class="kicker">About</p><h1>About this mirror</h1><p class="lede">{TAGLINE}</p></div>
<section class="block prose">
<h2>What it is</h2>
<p>{SITE_NAME} re-publishes the public content of the UCSF Infectious Diseases Management Program site (<a href="{BASE}" target="_blank" rel="noopener">idmp.ucsf.edu</a>) in a layout built for use on the wards: one Ask palette that returns answers, three lenses (hospital, setting, patient) that reorder every page, syndrome pages as a chooser plus one regimen card, one page per drug with a renal dial and the syndromes that recommend it, guidelines by hospital, and antibiograms in a bug-drug explorer. It is a personal project, not affiliated with, endorsed by, or maintained by UCSF or the IDMP.</p>
<h2>Where the mirror adds a layer</h2>
<p>Three things on this site are not on the source page they appear on, and each is labelled: the prescription line on a regimen card pulls doses from the same drug's IDMP dosing table (hover it to see the note; the original cell is always shown underneath); outpatient, inpatient and ICU tags on regimen rows are hand-curated by the site author from the row's own wording; and the "for your hospital" box lists the site's restriction, allergy and step-down policy pages next to matched guidelines. Lenses change what is shown first, never what the source says.</p>
<h2>How it stays current</h2>
<p>A scheduled job re-reads every source page nightly through the site's own content API, stores each page's <code>changed</code> timestamp and a content hash, and rebuilds only what moved. Structural surprises (a page type the mirror does not know, a missing field, a table whose columns cannot be recognised, a vanished index page) are recorded on the <a href="changes.html">What changed</a> page, flagged in the header sync indicator, and raised as an issue on the repository so drift is never silent. Documents on Box or SharePoint need a UCSF login and are linked, not copied.</p>
<h2>Current snapshot</h2>
<ul><li>{counts.get("diagnosis", 0)} empiric-therapy syndromes</li><li>{counts.get("drug", 0)} drug dosing pages</li><li>{counts.get("guidelines", 0)} guidelines</li><li>{counts.get("page", 0)} other pages, including antibiograms</li><li>{len(files)} PDFs tracked</li></ul>
<p class="muted">Sync run {esc(status.get("run") or "")} · repository <a href="https://github.com/{REPO}" target="_blank" rel="noopener">{REPO}</a></p>
</section>
{about_html}"""
    write(route, layout(ctx, root, "About", content, section="about", crumbs=[("index.html", "Home"), (None, "About")]))

    # offline fallback
    write("offline.html", layout(ctx, "", "Offline", '<div class="page-head"><p class="kicker">Offline</p><h1>This page is not saved yet</h1><p class="lede">You are offline and this page was never opened on this device. Pages you have visited are available; use "Save for offline" in the footer to keep the whole site.</p></div>', section="about"))

    # home
    route = "index.html"; root = ""
    def tiles(key):
        out = []
        for alias, label in curation["home"].get(key, []):
            r = route_for_alias(alias, f"curation.json home.{key}")
            if r:
                out.append(f'<a class="chip-link" href="{r}">{esc(label)}</a>')
        return "".join(out)
    recent = sorted((m for m in models.values() if m.get("changed") and m["type"] in ("diagnosis", "drug", "guidelines", "page")), key=lambda m: m["changed"], reverse=True)[:8]
    recent_html = "".join(f'<li><a href="{m["route"]}">{esc(m["title"])}</a> <span class="muted">{esc({"diagnosis": "empiric", "drug": "dosing", "guidelines": "guideline"}.get(m["type"], "page"))} · {esc(human_date(m["changed"]))}</span></li>' for m in recent)
    n_adult = len([m for m in by_type["diagnosis"] if not is_peds(m)])
    big_tiles = [("empiric/index.html", "Empiric therapy", f"{n_adult} adult syndromes as chooser + regimen cards"),
                 ("drugs/index.html", "Dosing", f'{counts.get("drug", 0)} drugs · renal dial, HD and CRRT, restrictions'),
                 ("antibiograms/explore.html", "Bug-drug explorer", "local susceptibility, organism by drug"),
                 ("guidelines/index.html", "Guidelines by hospital", f'{counts.get("guidelines", 0)} guidelines · UCSF Health, ZSFG, VA, BCH'),
                 ("peds.html", "Pediatrics", "empiric therapy, dosing cards, neonatal"),
                 ("changes.html", "What changed", "nightly diff against idmp.ucsf.edu")]
    tiles_html = "".join(f'<a class="tile" href="{h}"><strong>{esc(t)}</strong><span>{esc(d)}</span></a>' for h, t, d in big_tiles)
    banner = ""
    if status and not status.get("ok"):
        banner = f'<div class="notice warn"><strong>Heads up:</strong> the last sync ({esc(human_date(status.get("run")))}) reported problems, so some content may be stale or shown in fallback layout. <a href="changes.html">Details</a>.</div>'
    content = f"""{banner}
<section class="hero"><p class="kicker">Unofficial mirror of idmp.ucsf.edu</p><h1>What do I give, and how much?</h1>
<p class="lede">Ask in plain words: a syndrome and a setting, a drug and a creatinine clearance, an organism and a drug. Set your hospital and patient once and every page reorders itself.</p>
<button type="button" class="ask hero-ask" data-open="palette"><span class="ask-icon">⌕</span><span class="ask-text">cap icu pcn allergy · cefepime crcl 30 · e coli cipro · hap zsfg</span><kbd>⌘K</kbd></button>
<div class="lens-chips" id="home-lens"><button type="button" data-open="lens"><span id="home-lens-summary">All sites · Any setting · Adult</span> · change</button></div></section>
<section class="block" id="starts">
  <div class="starts" data-for="inpatient_adult"><h2>Inpatient, adult</h2><div class="chip-links">{tiles("inpatient_adult")}</div></div>
  <div class="starts" data-for="outpatient_adult"><h2>Outpatient, adult</h2><div class="chip-links">{tiles("outpatient_adult")}</div></div>
  <div class="starts" data-for="inpatient_peds"><h2>Inpatient, pediatric</h2><div class="chip-links">{tiles("inpatient_peds")}</div></div>
  <div class="starts" data-for="outpatient_peds"><h2>Outpatient, pediatric</h2><div class="chip-links">{tiles("outpatient_peds")}</div></div>
</section>
<section class="block" id="mine" hidden><h2>Pinned and recent</h2><div class="chip-links" id="mine-list"></div></section>
<section class="tiles">{tiles_html}</section>
<section class="block two"><div><h2>Most recently edited on IDMP</h2><ul class="rel-list">{recent_html}</ul></div>
<div><h2>How to read this site</h2><ul class="rel-list"><li>Drug names open a dosing drawer; the page you came from stays put.</li><li>The prescription line above each regimen pulls doses from the drug's own IDMP dosing table and says so; the original cell is always underneath.</li><li><span class="badge tag t-ucsf">ID-R · UCSF</span> and <span class="badge tag t-zsfg">ID-R · ZSFG</span> mark ID-restricted agents; <span class="badge kind k-login">UCSF login</span> marks Box/SharePoint documents.</li><li>Press <kbd>⌘K</kbd> or <kbd>/</kbd> to ask from anywhere; ☆ pins a page to this list.</li></ul></div></section>"""
    write(route, layout(ctx, root, "Home", content, section="home", page_class="home"))

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
    pages_list = sorted(written) + ["index.json", "antibiogram.json", "status.json", f"assets/site.css?v={ver}", f"assets/site.js?v={ver}", "assets/icon-192.png", "manifest.webmanifest"]
    pages_list += ["thumbs/" + fn for fn in os.listdir(os.path.join(OUT, "thumbs")) if fn.endswith(".jpg")]
    pages_list += ["img/" + fn for fn in os.listdir(os.path.join(OUT, "img"))]
    with open(os.path.join(OUT, "pages.json"), "w", encoding="utf-8") as f:
        json.dump({"ver": ver, "pages": pages_list}, f)
    with open(os.path.join(DATA, "build-warnings.json"), "w", encoding="utf-8") as f:
        json.dump(ctx.warnings, f, indent=1, ensure_ascii=False)
    print(f"built {len(written)} pages, {len(search)} search entries, {len(abx_tables)} antibiogram tables, {len(ctx.images)} images, {len(ctx.warnings)} notes")
    return 0

if __name__ == "__main__":
    sys.exit(main())
