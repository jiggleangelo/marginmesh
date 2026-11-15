from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

from .econ import (
    blended_price,
    break_even,
    entry_band,
    entry_price,
    margin,
    match_utilisation,
    number,
    rate_million,
    verdict,
)
from .models import ModelReport


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def market_report(model_id, endpoints, k=3, as_of=None):
    rows = [x for x in endpoints if x.model_id == model_id]
    paid = [(x, blended_price(x.price_in, x.price_out, k)) for x in rows if x.price_in or x.price_out]
    prices = sorted(price for _, price in paid)
    quants = [x.quantization for x, _ in paid]
    disclosed = {q for q in quants if q and q.lower() != "unknown"}
    quant = "undisclosed" if any(not q or q.lower() == "unknown" for q in quants) else "mixed" if len(disclosed) > 1 else "ok"
    uptimes = [x.uptime_pct for x, _ in paid if x.uptime_pct is not None]
    uptime = "n/a" if not uptimes else "low" if min(uptimes) < 99 else "ok"
    now = _time(as_of) if isinstance(as_of, str) else as_of or datetime.now(timezone.utc)
    stale = any((now - _time(x.observed_at)).total_seconds() > 172800 for x in rows)
    lo, mid, hi = (prices[0], median(prices), prices[-1]) if prices else (None, None, None)
    return ModelReport(
        model_id, rows, len({x.provider for x, _ in paid}), len(rows) - len(paid),
        lo, mid, hi, hi / lo if lo else None, (hi - lo) / mid if mid else None,
        quant, uptime, stale,
    )


def build_reports(endpoints, rents, assumptions, k=3, as_of=None):
    rent_by_chip = defaultdict(list)
    for quote in rents:
        rent_by_chip[quote.chip].append(quote.usd_per_gpu_hour)
    assumption_by_model = defaultdict(list)
    for assumption in assumptions:
        assumption_by_model[assumption.model_id].append(assumption)
    reports = []
    for model_id in sorted({x.model_id for x in endpoints}):
        report = market_report(model_id, endpoints, k, as_of)
        for assumption in assumption_by_model[model_id]:
            prices = rent_by_chip[assumption.chip]
            rent = median(prices) if prices else None
            rate = rate_million(assumption.tps_per_replica, assumption.gpus_per_replica)
            report.chips.append({
                "chip": assumption.chip,
                "rent": rent,
                "r": rate,
                "quant": assumption.quant,
                "confidence": assumption.confidence,
                "reference": assumption.reference,
            })
        reports.append(report)
    return reports


def scenario(report, chip, c, r, u=.6, g=.3, u_lo=.4, u_hi=.7):
    if c is None:
        return {
            "chip": chip, "rent": None, "r": r, "u": u, "g": g,
            "u_lo": u_lo, "u_hi": u_hi, "break_even_min": None,
            "break_even_median": None, "margin_min": None,
            "margin_median": None, "entry_at_u": None,
            "entry_low": None, "entry_high": None,
            "u_match": None, "verdict": "no rent quote",
        }
    c = number("rent", c, low=0, low_open=True)
    r = number("R", r, low=0, low_open=True)
    u = number("utilisation", u, low=0, high=1, low_open=True)
    g = number("target margin", g, low=0, high=.95)
    low, high = entry_band(c, r, u_lo, u_hi, g)
    at_utilisation = entry_price(c, r, u, g)
    if report.price_min is None or report.price_median is None:
        be_min = be_median = matched = None
        margin_min = margin_median = None
        outcome = "no market price"
    else:
        be_min = break_even(c, r, report.price_min)
        be_median = break_even(c, r, report.price_median)
        margin_min = margin(c, r, report.price_min, u)
        margin_median = margin(c, r, report.price_median, u)
        matched = match_utilisation(c, r, report.price_min, g)
        outcome = verdict(low, report.price_min, report.price_median)
    return {
        "chip": chip, "rent": c, "r": r, "u": u, "g": g,
        "u_lo": u_lo, "u_hi": u_hi, "break_even_min": be_min,
        "break_even_median": be_median, "margin_min": margin_min,
        "margin_median": margin_median, "entry_at_u": at_utilisation,
        "entry_low": low, "entry_high": high,
        "u_match": matched, "verdict": outcome,
    }
