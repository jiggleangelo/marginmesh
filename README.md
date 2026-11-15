# marginmesh

Prototype: marginmesh: calculate break-even and target-margin prices for open models

**Use it free, no account:** https://app-marginmesh.manykiln.com/  
**Try the demo (made-up data):** https://try-marginmesh.manykiln.com/  
**Product page:** https://marginmesh.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 51 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# Inference Unit Economics Terminal (IUE)

IUE is an offline-first market report and inference-cost calculator. The hosted demo uses fictional providers, made-up prices and modelled serving assumptions. Nothing is sold.

## Run the hosted demo

```sh
python3 demo.py
```

Open `http://127.0.0.1:8080`. The standard hosting contract is:

```sh
DEMO_HOST=0.0.0.0 PORT=8080 \
PUBLIC_BASE_URL=https://try-marginmesh.manykiln.com \
DEMO_HTTPS_TERMINATED=true python3 demo.py
```

`DEMO_HOST` defaults to `127.0.0.1`; `PORT` defaults to `8080`. The server validates the Host header and requires a matching Origin or Referer for every POST. When `PUBLIC_BASE_URL` is set, its host is enforced, links are absolute and cookies are Secure. `/healthz` is exempt from host enforcement. The demo performs no outbound requests. Every response closes its connection. A three-second socket timeout and a ten-second total wall-clock request deadline cover both headers and bodies.

The application rejects more than 50 headers or more than 16 KiB of parsed header data. An internet deployment must keep a reverse proxy in front that rejects oversized headers before forwarding and applies connection and request-rate limits.

Demo signups, model requests and interest actions are count-only interest signals. Submitted values cannot be read back or used to contact anyone. The in-memory vault is bounded and loses all records on restart.

## Usable app

`app.py` is the private-workspace, self-serve marginmesh application. It uses only deterministic formulas and performs no network, AI, payment, analytics or third-party messaging operations.

Run it locally:

```sh
DATA_DIR=./data_app python3 app.py
```

Then open `http://127.0.0.1:8080`. Production configuration follows the shared hosting contract:

```sh
APP_HOST=0.0.0.0 PORT=8080 \
PUBLIC_BASE_URL=https://app-marginmesh.manykiln.com \
APP_HTTPS_TERMINATED=true \
DATA_DIR=/persistent/marginmesh \
APP_DATA_CAP_MB=200 \
python3 app.py
```

`DATA_DIR` is the app's only writable location. A workspace is addressed by a random URL token; only its SHA-256 digest is stored on disk. Bookmark the workspace URL because it cannot be recovered. State is written atomically and persists across restarts. Export the full workspace as JSON before relying on it, or delete the workspace immediately from its dashboard.

Market input may be pasted or uploaded as UTF-8 CSV with `model,provider,price_in,price_out`, or as an OpenRouter-style endpoints JSON document previously downloaded by the user. Serving CSV requires `chip,usd_per_gpu_hour` and either `m_tokens_per_gpu_hour` or both `gpus_per_replica,tps_per_replica`. Optional fields and limits are explained in the UI. “Try with sample data” uses clearly labelled, wholly made-up records.

The app accepts at most 1 MB per request, 20 models, 200 endpoints per model, 1,000 endpoints total, 100 serving rows and 10 scenarios per model. Each workspace is capped at 5 MB. Idle workspaces expire after 90 days; untouched empty workspaces expire after 24 hours. A sweep runs at startup and daily.

## Test

```sh
python3 -m unittest discover -s tests
```

The suite uses only Python's standard library and loopback HTTP.

## Build the static report

```sh
python3 build.py --source fake --out out
python3 build.py --source live --out out --models data/top_models.txt
```

The fake build is offline. The live build is the only mode that accesses OpenRouter. Set `IUE_PUBLIC_BASE_URL` for canonical URLs. Static form controls remain part of the legacy static builder; `app.py` contains no signup, contact, payment or external form workflow.

## Data and licence status

- Demo JSON: wholly made-up and labelled `MADE-UP DATA FOR DEMO`.
- OpenRouter: live-price adapter only. Verify current fields, rate limits, terms and derived-stat republication rights before use.
- `data/rent.csv`: placeholder rows, not defaults for `app.py`.
- `data/serving.csv`: estimates, not defaults for `app.py`.
- `data/fixtures/openrouter_endpoints.json`: a tiny hand-written test fixture (248 bytes, one made-up "Fixture Provider" and one deliberately malformed row). It is not a copy of OpenRouter data and is used only by `tests/test_openrouter.py`.
- Artificial Analysis, Silicon Data and Ornn: excluded or unused.

## Important limitations

R is a modelled throughput assumption, not a promise. Actual throughput varies with serving software, batching, sequence lengths, quantisation and workload. Break-even utilisation scales as `1/R`: if R is overestimated by 20% (the estimate is 1.2 times the true value), the computed break-even utilisation is 1/1.2, about 17% below the true value; if R is underestimated by 20% (0.8 times the true value), it is 1/0.8, 25% above. Outputs are directional and are not investment advice.
