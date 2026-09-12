"""Summarize a pg_diag artifact: per-item status table (TSV) and an audit JSON next to it.

Usage: audit_report.py REPORT.json
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def main(path: Path) -> None:
    artifact = json.loads(path.read_text())
    items = artifact["items"]
    summary = []
    for key, item in items.items():
        result = item.get("result") or {}
        series = result.get("series", [])
        row = {
            "id": key,
            "status": item.get("collection_status"),
            "kind": result.get("kind"),
            "rows": result.get("row_count", len(result.get("rows", []))),
            "series": len(series),
            "points": sum(len(s.get("points", [])) for s in series),
            "reason": item.get("reason"),
            "issues": item.get("issues", []),
            "zero_series": len(result.get("zero_series", [])),
            "measured_zero_samples": sum(
                z.get("sample_count", 0) for z in result.get("zero_series", [])
            ),
        }
        row["has_evidence"] = bool(
            row["rows"]
            or row["points"]
            or row["measured_zero_samples"]
            or (
                result.get("kind") in {"text", "plain_text"}
                and (result.get("text") or result.get("data"))
            )
        )
        summary.append(row)
    counts = dict(collections.Counter(row["status"] for row in summary))
    sections: dict[str, collections.Counter] = {}
    for row in summary:
        sections.setdefault(row["id"].split(".")[0], collections.Counter())[row["status"]] += 1
    out = {
        "report": str(path),
        "item_count": len(items),
        "statuses": counts,
        "items_with_evidence": sum(row["has_evidence"] for row in summary),
        "runtime": artifact.get("runtime"),
        "sections": {k: dict(v) for k, v in sections.items()},
        "items": summary,
        "catalogs": {
            k: len(artifact[k])
            for k in ("query_texts", "object_ddls", "event_texts")
            if k in artifact
        },
    }
    path.with_name(path.stem + "-audit.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str)
    )
    columns = ["id", "status", "kind", "rows", "series", "points", "reason"]
    lines = ["\t".join(columns)] + [
        "\t".join(str(row.get(k, "")) for k in columns) for row in summary
    ]
    path.with_name(path.stem + "-items.tsv").write_text("\n".join(lines))
    print(json.dumps({"item_count": len(items), "statuses": counts}, indent=2))
    for row in summary:
        if row["status"] == "error":
            print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
