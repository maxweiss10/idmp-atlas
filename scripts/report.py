#!/usr/bin/env python3
"""report.py - small helpers for the workflow.

  report.py fatal-status   copy the failed sync's status into docs/status.json so the live
                           header pill turns red even though the site was not rebuilt
  report.py issue-body     print a Markdown summary of the last run's problems (for gh issue)
  report.py commit-msg     print a one-line commit message for this run
"""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
st = json.load(open(os.path.join(ROOT, "data", "status.json")))
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "fatal-status":
    docs = os.path.join(ROOT, "docs", "status.json")
    old = json.load(open(docs)) if os.path.exists(docs) else {}
    old.update({"run": st["run"], "ok": False, "problems": st["problems"], "exit_code": st["exit_code"]})
    json.dump(old, open(docs, "w"), indent=1)
elif cmd == "issue-body":
    lines = [f"Sync run `{st['run']}` exited with code {st['exit_code']}.", "", "| level | code | message |", "|---|---|---|"]
    for p in st.get("problems", []):
        lines.append(f"| {p['level']} | `{p['code']}` | {p['message'].replace('|', '/')} |")
    lines += ["", "The mirror keeps serving the last good copy of anything it could not parse, and marks it on the page.",
              "Fix the parser in `scripts/sync.py` / `scripts/build.py`, or close this issue if the change is benign.",
              "", "_Opened automatically by the sync workflow._"]
    print("\n".join(lines))
elif cmd == "commit-msg":
    print(f"sync {st['run'][:10]}: {st.get('events', 0)} change(s), {len(st.get('problems', []))} problem(s)")
else:
    print(__doc__); sys.exit(1)
