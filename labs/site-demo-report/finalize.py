"""Publish a finished run: anonymize collector identity, validate, render, audit.

Usage: finalize.py [--params ...] [--root ...] [--tag ...]
Reads experiments/<tag>/<tag>.json, writes experiments/<tag>/public/<tag>.{json,html} plus
<tag>-audit.json, <tag>-items.tsv, <tag>-browser.json, <tag>-plan.png. Originals stay untouched.
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import audit_report
from lab_common import LAB_DIR, Lab, add_common_arguments, lab_path, say, tool


def anonymize(lab: Lab, raw_text: str) -> tuple[str, dict[str, Any]]:
    publish = lab.params["publish"]
    artifact = json.loads(raw_text)
    changes: dict[str, Any] = {}
    text = raw_text
    for dotted, value in publish["replacements"].items():
        section, key = dotted.split(".")
        old = artifact[section][key]
        changes[dotted] = [old, value]
        old_enc = json.dumps(key) + ":" + json.dumps(old)
        new_enc = json.dumps(key) + ":" + json.dumps(value)
        # Key-scoped textual replacement keeps the compact JSON byte-identical elsewhere.
        assert text.count(old_enc) == 1, (dotted, old, text.count(old_enc))
        text = text.replace(old_enc, new_enc)
    document = json.loads(text)
    for dotted, value in publish["replacements"].items():
        section, key = dotted.split(".")
        assert document[section][key] == value
    # Hardware identifiers of the developer host (SSD serial/NQN/WWID, partition UUIDs) are
    # not part of the demo.
    hardware_columns = set(publish["hardware_id_columns"])
    counter = itertools.count(1)
    redacted = 0
    for item_id, item in document["items"].items():
        if not item_id.startswith("os.lshw_"):
            continue
        result = item.get("result") or {}
        columns = [c["name"] if isinstance(c, dict) else c for c in result.get("columns") or []]
        for row in result.get("rows") or []:
            for index, column in enumerate(columns):
                if index >= len(row) or row[index] in (None, ""):
                    continue
                if column in hardware_columns:
                    row[index] = f"redacted-{next(counter):02d}"
                    redacted += 1
                elif column == "configuration" and isinstance(row[index], dict):
                    for key in list(row[index]):
                        if key in hardware_columns:
                            row[index][key] = f"redacted-{next(counter):02d}"
                            redacted += 1
    changes["hardware_identifiers_redacted"] = redacted
    compact = ": " not in raw_text[:200]
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":") if compact else None)
    changes["remaining_matches"] = {p: len(re.findall(p, text)) for p in publish["leak_patterns"]}
    return text, changes


def browser_python(lab: Lab) -> Path | None:
    """Interpreter for browser_audit.py: this venv if it has playwright, else the configured one."""
    if importlib.util.find_spec("playwright") is not None:
        return Path(sys.executable)
    configured = lab.params["publish"].get("playwright_python")
    if configured:
        candidate = lab_path(configured)
        if candidate.exists():
            return candidate
    return None


def finalize(lab: Lab) -> dict[str, Any]:
    run_dir = lab.run_dir
    raw = run_dir / f"{lab.tag}.json"
    public = run_dir / "public"
    public.mkdir(exist_ok=True)
    text, changes = anonymize(lab, raw.read_text())
    out_json = public / f"{lab.tag}.json"
    out_json.write_text(text)
    say(f"anonymized JSON: {json.dumps(changes, ensure_ascii=False)}")
    leaks = {k: v for k, v in changes["remaining_matches"].items() if v}
    if leaks:
        say(f"WARNING: leak patterns still present: {leaks}")
    pg_diag = tool("pg-diag")
    subprocess.run([pg_diag, "validate-artifact", str(out_json)], check=True)
    html = public / f"{lab.tag}.html"
    subprocess.run(
        [pg_diag, "render", "--from-json", str(out_json), "--out", str(html)], check=True
    )
    say(f"public html {html} ({html.stat().st_size} bytes)")
    audit_report.main(out_json)
    browser: dict[str, Any] | None = None
    playwright_python = browser_python(lab)
    if playwright_python is not None:
        completed = subprocess.run(
            [str(playwright_python), str(LAB_DIR / "browser_audit.py"), str(html)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            browser = json.loads((public / f"{lab.tag}-browser.json").read_text())
            say(f"browser audit: graph {browser['graph']}, errors {browser['errors']}")
        else:
            say(
                "WARNING: browser audit failed:\n"
                + completed.stdout[-2000:]
                + completed.stderr[-2000:]
            )
    else:
        say("browser audit skipped: no Python with playwright (publish.playwright_python)")
    say("FINALIZE COMPLETED")
    return {"json": str(out_json), "html": str(html), "changes": changes, "browser": browser}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    finalize(Lab.from_args(parser.parse_args()))
