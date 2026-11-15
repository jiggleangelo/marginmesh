import csv
import json
from abc import ABC, abstractmethod
from pathlib import Path
from .models import Endpoint, RentQuote, ServingAssumption


class PriceSource(ABC):
    @abstractmethod
    def endpoints(self): ...


class RentSource(ABC):
    @abstractmethod
    def quotes(self): ...


class ServingSource(ABC):
    @abstractmethod
    def assumptions(self): ...


def _records(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("_note") != "MADE-UP DATA FOR DEMO":
        raise ValueError("sample data must carry the demo note")
    return data["records"]


class FakePrice(PriceSource):
    def __init__(self, path="data/sample_endpoints.json"): self.path = path
    def endpoints(self): return [Endpoint(**x) for x in _records(self.path)]


class FakeRent(RentSource):
    def __init__(self, path="data/sample_rent.json"): self.path = path
    def quotes(self): return [RentQuote(**x) for x in _records(self.path)]


class FakeServing(ServingSource):
    def __init__(self, path="data/sample_serving.json"): self.path = path
    def assumptions(self): return [ServingAssumption(**x) for x in _records(self.path)]


def _csv(path, cls, conversions):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for line, row in enumerate(csv.DictReader(f), 2):
            try:
                if "source" in row and not row["source"].strip():
                    raise ValueError("source is required")
                for key, converter in conversions.items():
                    row[key] = converter(row[key])
                rows.append(cls(**row))
            except Exception as exc:
                raise ValueError(f"{path}: line {line}: {exc}") from exc
    return rows


class CsvRent(RentSource):
    def __init__(self, path="data/rent.csv"): self.path = path
    def quotes(self): return _csv(self.path, RentQuote, {"usd_per_gpu_hour": float})


class CsvServing(ServingSource):
    def __init__(self, path="data/serving.csv"): self.path = path
    def assumptions(self):
        return _csv(self.path, ServingAssumption, {"gpus_per_replica": int, "tps_per_replica": float})
