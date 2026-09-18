# IDMP Atlas

An unofficial, read-only mirror of the public content on [idmp.ucsf.edu](https://idmp.ucsf.edu) (the UCSF Infectious Diseases Management Program), rebuilt nightly and re-organised around the question you have at the bedside.

Live site: https://maxweiss10.github.io/idmp-atlas/

Not affiliated with UCSF or the IDMP. Content belongs to its authors. Always confirm against the source before acting.

## Why

The source is organised by document (empiric therapy, dosing, guidelines, antibiograms), but a clinical question crosses all of them: syndrome, then drug, then dose for this kidney, then local susceptibility, then what this hospital allows. The mirror keeps every fact and changes only the packaging.

- **Ask palette** (`⌘K` or `/`) that returns answers, not just titles: `cefepime crcl 30` gives the dose cell, `e coli cipro` gives the susceptibility number, `cap icu` opens the ICU row of the CAP page, `hap zsfg` puts the ZSFG guideline first. Ward shorthand and brand names work.
- **Three lenses** — Where (hospital), Setting (outpatient / inpatient / ICU), Patient (adult or peds, CrCl or HD/CRRT, beta-lactam allergy). Set once, remembered on the device, and every page reorders itself: the matching renal column highlights, the allergy alternative moves first, your hospital's guidelines and restriction policy come to the top.
- **Syndrome pages as a chooser plus one regimen card**, with a prescription line whose doses come from the same drug's IDMP dosing table, matched to the indication where possible and labelled as synthesized.
- **Drug pages** with a renal dial, the restriction workflow for your site, and the syndromes that recommend the drug.
- **Bug-drug explorer** built by parsing the HTML antibiograms; organism by drug or drug by organism, with shading.
- **Guidelines by hospital**, with first-page PDF previews so you can see what you are about to open.
- **Offline**: installable, and one button caches the whole site for a basement with no signal.

## How it stays honest

`scripts/sync.py` runs nightly in GitHub Actions:

1. crawls the source's index pages (which enumerate every published node) and follows internal links;
2. fetches every node as JSON from the site's own REST endpoint (`/node/<nid>?_format=json`), which carries the `changed` timestamp, revision id and every field;
3. compares each node with the previous copy (content hash + `changed`) and writes a field-level diff into `data/changelog.json`;
4. checks a contract per content type (known type, expected fields present, index pages still there, node counts not collapsing) and records problems in `data/status.json`;
5. renders the first page of each public PDF into `docs/thumbs/`.

`scripts/build.py` renders `data/` into `docs/` as plain HTML, CSS and JS, no framework. Tables whose columns cannot be recognised are shown in their original form with a visible note. If the sync reports a hard problem the workflow still publishes what it could, then opens or updates a GitHub issue labelled `drift` and fails, so drift is never silent. The header pill shows when the mirror was last verified and turns red if the job is failing.

Documents on Box or SharePoint need a UCSF login and are linked, not copied.

## Curated layers (everything else is verbatim)

Three things go beyond the source, each labelled on the page:

- `scripts/curation.json` — outpatient / inpatient / ICU tags per regimen row, the per-hospital policy pages pinned on drug and syndrome pages, and the home-screen starting points. Row tags carry the expected row count; if IDMP edits the table, the tags are dropped and a note appears on the What changed page rather than mislabelling anything.
- `scripts/aliases.json` — ward shorthand for syndromes (CAP, HAP, SSTI, CDI…), search only.
- `scripts/synonyms.json` — brand names and abbreviations for drugs, search only.

## Run locally

```bash
pip install -r requirements.txt
python3 scripts/sync.py     # ~8 minutes, polite 0.2 s delay between requests
python3 scripts/build.py    # writes docs/
python3 scripts/check.py    # link and residue check
python3 -m http.server -d docs 8771
```

## Layout

```
scripts/sync.py        fetch, change detection, PDF thumbnails
scripts/build.py       static site generator
scripts/check.py       post-build link checker
scripts/curation.json  hand-checked layers (settings, site policies, home tiles)
scripts/aliases.json   syndrome shorthand for search
scripts/synonyms.json  drug brand names for search
site/assets/           tokens.css (Radix scales), site.css, site.js, icons
site/sw.js             service worker (offline)
data/nodes/<nid>.json  raw source JSON, one file per node (git history = audit trail)
data/manifest.json     per-node summary used by the next sync
data/changelog.json    what changed, per run
data/status.json       last run result
docs/                  generated site (GitHub Pages)
```
