#!/usr/bin/env python3
"""check.py - sanity checks on the built site: every internal link resolves, no Drupal/Word residue, counts."""
import os, re, sys, json
from urllib.parse import urlparse, urljoin
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs")
pages = []
for d, _, fns in os.walk(OUT):
    for fn in fns:
        if fn.endswith(".html"):
            pages.append(os.path.relpath(os.path.join(d, fn), OUT))
broken, residue = [], []
for rel in pages:
    text = open(os.path.join(OUT, rel), encoding="utf-8").read()
    for href in re.findall(r'(?:href|src)="([^"]+)"', text):
        if href.startswith(("http", "mailto:", "tel:", "data:", "#", "javascript:")):
            continue
        target = urlparse(href).path
        if not target:
            continue
        p = os.path.normpath(os.path.join(OUT, os.path.dirname(rel), target))
        if not os.path.exists(p):
            broken.append((rel, href))
    for pat in (r'paraeid=', r'class="MsoNormal', r'style="', r'<o:p>', r'node--type', r'drupal', r'field--name'):
        if re.search(pat, text):
            residue.append((rel, pat))
print(f"pages: {len(pages)}")
print(f"broken internal links: {len(broken)}")
for b in broken[:25]:
    print("  ", b)
print(f"residue hits: {len(residue)}")
for r in residue[:15]:
    print("  ", r)
idx = json.load(open(os.path.join(OUT, "search.json")))
from collections import Counter
print("search entries:", len(idx), Counter(e["t"] for e in idx))
sys.exit(1 if broken else 0)
