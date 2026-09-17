# IDMP Atlas

An unofficial, read-only mirror of the public content on [idmp.ucsf.edu](https://idmp.ucsf.edu) (the UCSF Infectious Diseases Management Program), rebuilt nightly and re-organised for use on the wards.

Live site: https://maxweiss10.github.io/idmp-atlas/

Not affiliated with UCSF or the IDMP. Content belongs to its authors. Always confirm against the source before acting.

## Why

The source site is organised by section (empiric therapy, dosing, guidelines, antibiograms), but a clinical question crosses all of them: syndrome, then drug, then dose for this kidney, then local susceptibility, then what this hospital's policy says. The mirror keeps every fact from the source and changes only the packaging:

- one instant search across syndromes, drugs (brand names and ward shorthand work), guidelines, organisms and pages;
- syndrome pages rendered as cards (condition, pathogens, first choice, alternative, comments, duration) instead of six-column tables;
- one page per drug, with the syndromes that recommend it listed at the bottom, and a side drawer so a drug opened from a syndrome page never loses your place;
- guidelines filterable by hospital (UCSF Health, ZSFG, VA, BCH), with badges for PDF vs Box/SharePoint (UCSF login);
- antibiogram tables shaded by % susceptible;
- every page shows the source's own last-changed date and links to the original.

## How it stays honest

`scripts/sync.py` runs nightly in GitHub Actions:

1. crawls the source's index pages (which enumerate every published node) and follows internal links;
2. fetches every node as JSON from the site's own REST endpoint (`/node/<nid>?_format=json`), which carries the `changed` timestamp, revision id and every field;
3. compares each node with the previous copy (content hash + `changed`), and writes a field-level diff into `data/changelog.json`;
4. checks a small contract for each content type (known type, expected fields present, index pages still exist, node counts not collapsing) and records problems in `data/status.json`.

`scripts/build.py` renders `data/` into `docs/` (plain HTML, CSS and JS, no framework). Tables whose columns cannot be recognised are shown in their original form with a visible note. If the sync reports problems the workflow still publishes what it could, then opens or updates a GitHub issue labelled `drift` and fails, so the drift is never silent. The header pill on every page shows when the mirror was last verified against the source and turns red if the job is failing.

Documents hosted on Box or SharePoint need a UCSF login and are linked, not copied. PDFs hosted on idmp.ucsf.edu are linked directly.

## Run locally

```bash
pip install -r requirements.txt
python3 scripts/sync.py     # ~3 minutes, polite 0.2 s delay between requests
python3 scripts/build.py    # writes docs/
python3 -m http.server -d docs 8080
```

## Layout

```
scripts/sync.py        fetch + change detection
scripts/build.py       static site generator
scripts/synonyms.json  brand names / shorthand used by search (edit freely)
site/assets/           CSS and JS sources copied into docs/assets
data/nodes/<nid>.json  raw source JSON, one file per node (git history = audit trail)
data/manifest.json     per-node summary used by the next sync
data/changelog.json    what changed, per run
data/status.json       last run result
docs/                  generated site (GitHub Pages)
```
