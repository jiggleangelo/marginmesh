import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from iue import csvout, econ, render
from iue.openrouter import OpenRouterPrice
from iue.report import build_reports, scenario
from iue.sources import CsvRent, CsvServing, FakePrice, FakeRent, FakeServing


def _verified_url(value):
    parsed = urlparse(str(value).strip())
    return parsed.scheme == "https" and bool(parsed.netloc)


def _validate_live_sources(rents, serving):
    bad_rent = [quote.chip for quote in rents if not _verified_url(quote.source) or "placeholder" in quote.source.lower()]
    bad_serving = [f"{row.model_id}/{row.chip}" for row in serving if not _verified_url(row.reference) or row.confidence.lower() in ("placeholder", "estimate")]
    if bad_rent or bad_serving:
        details = []
        if bad_rent:
            details.append("rent sources: " + ", ".join(bad_rent[:5]))
        if bad_serving:
            details.append("serving sources: " + ", ".join(bad_serving[:5]))
        raise SystemExit("live build blocked: replace placeholder or non-URL sources (" + "; ".join(details) + ")")


def _output_path(root, out):
    target = Path(out).expanduser().resolve()
    if target in (Path("/").resolve(), root.resolve()):
        raise SystemExit("--out must be a dedicated output directory")
    return target


def build(source, out, models=None):
    root = Path(__file__).resolve().parent
    network_errors = []
    if source == "fake":
        endpoints = FakePrice(root / "data/sample_endpoints.json").endpoints()
        rents = FakeRent(root / "data/sample_rent.json").quotes()
        serving = FakeServing(root / "data/sample_serving.json").assumptions()
        skipped = 0
    else:
        if not models:
            raise SystemExit("--models is required for live builds")
        model_path = Path(models).expanduser().resolve()
        ids = [line.strip() for line in model_path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        adapter = OpenRouterPrice(ids)
        endpoints = adapter.endpoints()
        skipped = adapter.skipped
        network_errors = adapter.errors
        rents = CsvRent(root / "data/rent.csv").quotes()
        serving = CsvServing(root / "data/serving.csv").assumptions()
        _validate_live_sources(rents, serving)
        total = len(endpoints) + skipped
        if total and skipped / total > .10:
            raise SystemExit(f"live build blocked: {skipped}/{total} endpoint rows were invalid")
        if not endpoints:
            raise SystemExit("live build blocked: no endpoint data was retrieved")
    reports = build_reports(endpoints, rents, serving)
    out = _output_path(root, out)
    (out / "model").mkdir(parents=True, exist_ok=True)
    demo = source == "fake"
    public_base = os.environ.get("IUE_PUBLIC_BASE_URL", "").rstrip("/")
    form_action = os.environ.get("IUE_FORM_ACTION") or None
    (out / "index.html").write_text(
        render.dashboard_page(reports, public_base, static=True, demo=demo,
                              form_action=form_action), encoding="utf-8"
    )
    (out / "methodology.html").write_text(
        render.methodology_page(public_base, demo=demo, static=True), encoding="utf-8"
    )
    (out / "export.csv").write_text(csvout.dashboard(reports), encoding="utf-8")
    for report in reports:
        choice = next((chip for chip in report.chips if chip["rent"] is not None), None)
        current = scenario(report, choice["chip"], choice["rent"], choice["r"]) if choice else None
        if current is not None:
            current["k"] = 3
        data = {
            "model": report.model_id,
            "endpoints": [{"price_in": endpoint.price_in, "price_out": endpoint.price_out}
                          for endpoint in report.endpoints if endpoint.price_in or endpoint.price_out],
        }
        blob = json.dumps(data, separators=(",", ":"))
        embedded = f'<div id="iue-data" hidden data-json="{render.esc(blob)}"></div><script src="../calc.js"></script>'
        page = render.model_page(report, current, base=public_base, static=True, demo=demo, embedded_json=embedded)
        name = render.slug(report.model_id)
        (out / "model" / f"{name}.html").write_text(page, encoding="utf-8")
        (out / "model" / f"{name}.csv").write_text(
            csvout.economics(report.model_id, [current] if current else []), encoding="utf-8"
        )
    vector = {
        "c": 2.5, "r": 3.6, "p": 1.2, "u": .6, "g": .3,
        "break_even": econ.break_even(2.5, 3.6, 1.2),
        "margin": econ.margin(2.5, 3.6, 1.2, .6),
        "entry": econ.entry_price(2.5, 3.6, .6, .3),
    }
    (out / "test_vectors.json").write_text(json.dumps(vector, indent=2), encoding="utf-8")
    (out / "calc.js").write_text((root / "static/calc.js").read_text(encoding="utf-8"), encoding="utf-8")
    (out / "selftest.js").write_text((root / "static/selftest.js").read_text(encoding="utf-8"), encoding="utf-8")
    selftest = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'; style-src \'self\' \'unsafe-inline\'; frame-ancestors \'none\'; base-uri \'none\'">'
                '</head><body><div class="banner">' + render.BANNER +
                '</div><pre id="result">Running…</pre><script src="calc.js"></script><script src="selftest.js"></script></body></html>')
    (out / "calc_selftest.html").write_text(selftest, encoding="utf-8")
    sources = [{
        "source": "made-up demo samples" if demo else "OpenRouter and verified curated CSV",
        "rows": len(endpoints),
        "skipped_rows": skipped,
        "licence_status": "demo only" if demo else "verification required",
        "retrieval_errors": network_errors,
    }]
    (out / "data_sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
    print(f"built {len(reports)} reports; skipped {skipped} endpoint rows; retrieval errors {len(network_errors)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("fake", "live"), default="fake")
    parser.add_argument("--out", default="out")
    parser.add_argument("--models")
    args = parser.parse_args()
    build(args.source, args.out, args.models)
