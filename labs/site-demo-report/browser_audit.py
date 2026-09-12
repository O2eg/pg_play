"""Open the rendered report in headless Chromium and check graph, charts and the plan viewer.

Usage: browser_audit.py REPORT.html   (needs a Python with playwright + chromium installed)
Writes REPORT-browser.json and REPORT-plan.png next to the HTML; exits non-zero on JS errors.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PLANS_ITEM = "server_log.auto_explain_plans"


def main(path: Path) -> None:
    out = path.with_name(path.stem + "-browser.json")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        requests: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on(
            "request",
            lambda r: requests.append(r.url) if r.url.startswith(("http:", "https:")) else None,
        )
        page.goto(path.as_uri(), wait_until="load", timeout=60000)
        page.wait_for_selector("#diagnosticGraph .dg-node", timeout=30000)
        counts = page.evaluate(
            "() => ({roots: window.pgDiagReport.diagnosticGraph.roots, "
            "statuses: window.pgDiagReport.diagnosticGraph.coverage.statusCounts, "
            "charts: echartsCharts.length, "
            "items: document.querySelectorAll('[id^=\"item-\"]').length})"
        )
        page.evaluate(
            "() => { const el = document.getElementById('item-" + PLANS_ITEM + "'); if (el) { "
            "for (let n = el; n; n = n.parentElement) if (n.tagName === 'DETAILS') n.open = true; "
            "el.scrollIntoView(); } }"
        )
        page.wait_for_timeout(400)
        data = page.evaluate(
            "() => { const e = echartsCharts.find(e => e.item.item_id === '" + PLANS_ITEM + "'); "
            "if (!e) return {found: false}; const series = e.chart.getOption().series; "
            "const points = series.flatMap(s => s.data || []); "
            "return {found: true, series: series.length, points: points.length}; }"
        )
        opened = page.evaluate(
            "() => { const e = echartsCharts.find(e => e.item.item_id === '" + PLANS_ITEM + "'); "
            "if (!e) return false; for (const s of e.chart.getOption().series) "
            "for (const d of s.data || []) { openQueryPlanViewerFromChart({data: d}); "
            "const m = document.getElementById('planViewerModal'); "
            "if (m && !m.hidden && getComputedStyle(m).display !== 'none') return true; } "
            "return false; }"
        )
        page.wait_for_timeout(300)
        detail = {
            "opened": opened,
            "plan_rows": page.locator("#planViewerModal tr.pv-row").count(),
            "modal_text": page.locator("#planViewerModal").inner_text()[:180] if opened else "",
        }
        formats = page.evaluate(
            "() => { const entry = echartsCharts.find(e => e.item.item_id === '"
            + PLANS_ITEM
            + "'); "
            "const out = {}; if (!entry) return out; "
            "for (const s of entry.chart.getOption().series) for (const d of s.data || []) { "
            "const f = d.pgDiagViewer?.plan_format; if (!f || out[f]) continue; "
            "openQueryPlanViewerFromChart({data: d}); "
            "out[f] = {rows: document.querySelectorAll('#planViewerModal tr.pv-row').length, "
            "error: document.querySelector('#planViewerModal .plan-viewer-error')?.textContent "
            "|| null}; } return out; }"
        )
        page.screenshot(path=str(path.with_name(path.stem + "-plan.png")))
        result = {
            "html": str(path),
            "graph": counts,
            "auto_explain_chart": data,
            "plan_viewer": detail,
            "plan_formats": formats,
            "errors": errors,
            "external_requests": requests,
        }
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(json.dumps(result, ensure_ascii=False))
        browser.close()
        assert not errors, errors
        if data.get("points", 0):
            assert opened and detail["plan_rows"] > 0, detail
        assert all(x["rows"] > 0 and not x["error"] for x in formats.values()), formats


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
