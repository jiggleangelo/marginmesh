import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .models import Endpoint


class OpenRouterPrice:
    def __init__(self, models, opener=None, base="https://openrouter.ai/api/v1/models"):
        self.models = models
        self.opener = opener or urllib.request.urlopen
        self.base = base
        self.skipped = 0
        self.errors = []

    def endpoints(self):
        out = []
        for model_id in self.models:
            url = f"{self.base}/{urllib.parse.quote(model_id, safe='')}/endpoints"
            try:
                with self.opener(url, timeout=20) as response:
                    payload = json.load(response)
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
                self.errors.append({"model_id": model_id, "error": type(exc).__name__})
                continue
            rows = payload.get("data", {}).get("endpoints", payload.get("endpoints", []))
            for row in rows:
                try:
                    pricing = row.get("pricing", row)
                    pin = _million_price(pricing["prompt"])
                    pout = _million_price(pricing["completion"])
                    out.append(Endpoint(
                        model_id,
                        row.get("provider_name") or row.get("name") or "unknown",
                        pin,
                        pout,
                        row.get("quantization"),
                        row.get("context_length"),
                        _float(row.get("uptime_pct")),
                        _float(row.get("throughput_tps")),
                        row.get("observed_at") or datetime.now(timezone.utc).isoformat(),
                    ))
                except (KeyError, TypeError, ValueError, OverflowError):
                    self.skipped += 1
        return out


def _million_price(value):
    price = float(value) * 1_000_000
    if not math.isfinite(price) or price < 0:
        raise ValueError("price must be finite and non-negative")
    return price


def _float(value):
    if value in (None, ""):
        return None
    result = float(value)
    return result if math.isfinite(result) else None
