from dataclasses import dataclass, field


@dataclass(frozen=True)
class Endpoint:
    model_id: str
    provider: str
    price_in: float
    price_out: float
    quantization: str | None
    context_len: int | None
    uptime_pct: float | None
    throughput_tps: float | None
    observed_at: str


@dataclass(frozen=True)
class RentQuote:
    chip: str
    usd_per_gpu_hour: float
    kind: str
    source: str
    observed_at: str


@dataclass(frozen=True)
class ServingAssumption:
    model_id: str
    chip: str
    quant: str
    gpus_per_replica: int
    tps_per_replica: float
    reference: str
    confidence: str


@dataclass
class ModelReport:
    model_id: str
    endpoints: list[Endpoint]
    provider_count: int
    free_count: int
    price_min: float | None
    price_median: float | None
    price_max: float | None
    spread_ratio: float | None
    spread_pct: float | None
    quant_flag: str
    uptime_flag: str
    stale_flag: bool
    chips: list[dict] = field(default_factory=list)
