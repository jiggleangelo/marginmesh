import json
import os
import re
import socket
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, quote, urlencode, urlparse

from iue import csvout, econ
from iue.models import Endpoint
from iue.render import esc
from iue.report import market_report, scenario
from iue.sources import FakePrice, FakeRent, FakeServing
from iue.userinput import InputFileError, parse_chips_csv, parse_openrouter_json, parse_prices_csv
from iue.workspace import CapacityError, DataFull, Missing, RateLimit, StoreError, WorkspaceStore

MAX_BODY = 1024 * 1024
NOTICE = ('Operated by AI agents. Early version: keep your own copy of anything important. '
          "Do not enter other people's personal data or pricing you are contractually bound to keep confidential. "
          'Nothing is charged.')
FOOTER = 'All numbers are computed by fixed formulas (see Methodology); no AI model is used. Directional estimates, not investment advice.'
HONESTY = 'R is a modelled assumption. Real throughput depends on the serving stack, batching, sequence lengths and quantisation.'
CSS = 'body{font:16px system-ui;max-width:1200px;margin:auto;padding:0 18px;color:#172033}.banner{background:#ffe8a3;padding:12px;margin:0 -18px 18px;font-weight:650}nav{display:flex;gap:16px;flex-wrap:wrap}table{border-collapse:collapse;width:100%;margin:12px 0}th,td{border:1px solid #ccd3dd;padding:7px;text-align:left}th{background:#eef2f7}.card{border:1px solid #ccd3dd;padding:13px;margin:13px 0}.error{color:#9c1625}.badge{background:#fff4cc;padding:4px 8px}label{display:block;margin:8px 0}input,select,textarea,button{padding:7px;max-width:100%;box-sizing:border-box}textarea{width:100%;min-height:9em}.muted{color:#596273}footer{border-top:1px solid #ccd3dd;margin-top:28px;padding:18px 0}'
MESSAGES = {'workspace_deleted': 'Workspace deleted.', 'prices_imported': 'Market prices imported.', 'chips_imported': 'Serving rows imported.', 'chip_deleted': 'Serving row deleted.', 'model_deleted': 'Model and its model-specific serving rows deleted.'}


def slug(value):
    return re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-') or 'model'


def reports_for(state):
    endpoints = [Endpoint(**row) for row in state.get('endpoints', [])]
    reports = []
    for model in state.get('models', [])[:20]:
        report = market_report(model['id'], endpoints)
        applicable = [x for x in state.get('chips', [])[:100] if not x.get('model') or x.get('model') == model['id']]
        by_chip = {}
        for row in applicable:
            by_chip.setdefault(row['chip'], []).append(row)
        for chip, rows in by_chip.items():
            rents = [x['rent'] for x in applicable if x['chip'] == chip]
            chosen = rows[-1]
            report.chips.append({'chip': chip, 'rent': median(rents), 'r': median([x['r'] for x in rows]),
                                 'quant': chosen.get('quant', ''), 'confidence': chosen.get('confidence', 'estimate'),
                                 'reference': chosen.get('source', '')})
        reports.append(report)
    return reports


def sample_state(root):
    endpoints = [dict(model_id=x.model_id, provider=x.provider, price_in=x.price_in, price_out=x.price_out,
                      quantization=x.quantization, context_len=x.context_len, uptime_pct=x.uptime_pct,
                      throughput_tps=x.throughput_tps, observed_at=x.observed_at)
                 for x in FakePrice(root / 'data/sample_endpoints.json').endpoints()]
    rents = FakeRent(root / 'data/sample_rent.json').quotes()
    serving = FakeServing(root / 'data/sample_serving.json').assumptions()
    rent_map = {}
    for row in rents:
        rent_map.setdefault(row.chip, []).append(row.usd_per_gpu_hour)
    chips = [{'chip': x.chip, 'rent': median(rent_map[x.chip]), 'r': econ.rate_million(x.tps_per_replica, x.gpus_per_replica),
              'model': x.model_id, 'quant': x.quant, 'source': x.reference, 'confidence': x.confidence} for x in serving]
    models = []
    for name in dict.fromkeys(x['model_id'] for x in endpoints):
        base = slug(name); mid = base; suffix = 2
        while any(x['mid'] == mid for x in models):
            mid, suffix = f'{base}-{suffix}', suffix + 1
        models.append({'id': name, 'mid': mid})
    return {'models': models, 'endpoints': endpoints, 'chips': chips, 'scenarios': {},
            'created_at': time.time(), 'sample': True, 'version': 1}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, max_threads=32):
        self._slots = threading.BoundedSemaphore(max_threads)
        self.stop_sweep = threading.Event()
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        pass

    def server_close(self):
        self.stop_sweep.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 3

    def setup(self):
        super().setup(); self.connection.settimeout(self.timeout)
        self.timer = threading.Timer(self.server.request_deadline, self._expire); self.timer.daemon = True; self.timer.start()

    def finish(self):
        if hasattr(self, 'timer'): self.timer.cancel()
        super().finish()

    def _expire(self):
        try: self.connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass

    def log_message(self, *args): pass

    def parse_request(self):
        if not super().parse_request(): return False
        if len(self.raw_requestline) > 8192: self._error(414, 'Request line is too long'); return False
        size = sum(len(k.encode()) + len(v.encode()) + 4 for k, v in self.headers.raw_items())
        if len(self.headers) > 50 or size > 16384: self._error(431, 'Request headers are too large'); return False
        return True

    def send_error(self, code, message=None, explain=None):
        if self.request_version == self.default_request_version: self.request_version = self.protocol_version
        self._error(code, message or 'Request rejected')

    def _headers(self, content_type, length, disposition=None):
        self.send_header('Content-Type', content_type); self.send_header('Content-Length', str(length))
        self.send_header('X-Content-Type-Options', 'nosniff'); self.send_header('Referrer-Policy', 'same-origin'); self.send_header('Cache-Control', 'no-store')
        if self.server.secure: self.send_header('Strict-Transport-Security', 'max-age=31536000')
        if content_type.startswith('text/html'): self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        if disposition: self.send_header('Content-Disposition', disposition)
        self.send_header('Connection', 'close'); self.close_connection = True

    def _send(self, status, data, content_type='text/html; charset=utf-8', disposition=None, location=None):
        data = data.encode() if isinstance(data, str) else data
        self.send_response(status)
        if location: self.send_header('Location', location)
        self._headers(content_type, len(data), disposition); self.end_headers()
        try: self.wfile.write(data)
        except (OSError, BrokenPipeError, socket.timeout): pass

    def _url(self, path):
        path = '/' + path.lstrip('/')
        return self.server.base + path if self.server.base else path

    def _page(self, title, body, workspace=None):
        nav = f'<nav><a href="{esc(self._url("/"))}">Home</a><a href="{esc(self._url("/methodology"))}">Methodology</a>'
        if workspace: nav += f'<a href="{esc(self._url("/w/" + workspace))}">Workspace</a>'
        nav += '</nav>'
        badge = '<p><span class="badge">Made-up sample</span></p>' if workspace and getattr(self, 'state', {}).get('sample') else ''
        return f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{esc(title)}</title><style>{CSS}</style></head><body><div class="banner">{esc(NOTICE)} <a href="https://manykiln.com/privacy">Privacy</a></div>{nav}{badge}{body}<footer>{esc(FOOTER)}</footer></body></html>'

    def _error(self, status, message, details=None):
        extra = ''.join(f'<li>{esc(x)}</li>' for x in (details or [])[:5])
        body = f'<h1>{status}</h1><p class="error">{esc(str(message)[:300])}</p>' + (f'<ul>{extra}</ul>' if extra else '')
        self._send(status, self._page(str(status), body, getattr(self, 'token', None)))

    def _host_ok(self): return self.headers.get('Host', '').lower().rstrip('.') in self.server.allowed_hosts

    def _origin_ok(self):
        expected = self.server.public_origin
        if not expected: expected = ('https' if self.server.secure else 'http') + '://' + self.headers.get('Host', '').lower().rstrip('.')
        origin = self.headers.get('Origin')
        if origin:
            parsed = urlparse(origin); return f'{parsed.scheme.lower()}://{parsed.netloc.lower().rstrip(".")}' == expected
        referer = self.headers.get('Referer', '')
        return referer == expected or referer.startswith(expected + '/')

    def _body(self):
        if self.headers.get('Transfer-Encoding'): raise InputFileError('Transfer-Encoding is not supported')
        value = self.headers.get('Content-Length')
        if value is None: raise InputFileError('Content-Length is required')
        try: size = int(value)
        except ValueError: raise InputFileError('Invalid Content-Length')
        if size < 0: raise InputFileError('Invalid Content-Length')
        if size > MAX_BODY:
            error = InputFileError('Request body is larger than 1 MB'); error.status = 413; raise error
        raw = self.rfile.read(size)
        if len(raw) != size: raise InputFileError('Incomplete request body')
        ctype = self.headers.get('Content-Type', '')
        if ctype.split(';', 1)[0].lower() == 'application/x-www-form-urlencoded':
            try: parsed = parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=20)
            except (UnicodeDecodeError, ValueError): raise InputFileError('Invalid form body')
            return {k: v[0] for k, v in parsed.items()}
        if ctype.lower().startswith('multipart/form-data'):
            try: message = BytesParser(policy=policy.default).parsebytes(b'Content-Type: ' + ctype.encode('ascii', 'strict') + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw)
            except (UnicodeError, ValueError): raise InputFileError('Invalid multipart body')
            fields = {}
            for part in message.iter_parts():
                name = part.get_param('name', header='content-disposition')
                if not name: continue
                if part.get_filename():
                    kind = part.get_content_type().lower()
                    if not (kind.startswith('text/') or kind in ('application/json', 'application/csv', 'application/octet-stream', 'application/vnd.ms-excel')):
                        raise InputFileError('Only text files are accepted')
                try: fields[name] = (part.get_payload(decode=True) or b'').decode('utf-8-sig', errors='strict')
                except UnicodeDecodeError: raise InputFileError('Uploaded text must be UTF-8')
            return fields
        raise InputFileError('Form encoding or multipart upload required')

    def _workspace(self, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', token): raise Missing('Workspace not found')
        self.server.store.resolve(token); self.token = token; self.state = self.server.store.read(token); return self.state

    def _model(self, state, mid):
        found = next((x for x in state.get('models', []) if x.get('mid') == mid), None)
        if not found: raise Missing('Model not found')
        reports = self.server.store.cached(self.token, state, reports_for)
        return found, next(x for x in reports if x.model_id == found['id'])

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/healthz': return self._send(200, 'ok', 'text/plain; charset=utf-8')
        if len(parsed.query.encode()) > 4096: return self._error(414, 'Query string is too long')
        if not self._host_ok(): return self._error(421, 'Misdirected Request')
        try: params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
        except ValueError: return self._error(400, 'Too many query fields')
        one = {k: v[0] for k, v in params.items()}
        try:
            if parsed.path == '/': return self._landing(one.get('msg', ''))
            if parsed.path == '/methodology': return self._methodology()
            match = re.fullmatch(r'/w/([^/]+)', parsed.path)
            if match: return self._dashboard(self._workspace(match.group(1)), one.get('msg', ''))
            match = re.fullmatch(r'/w/([^/]+)/delete/confirm', parsed.path)
            if match:
                self._workspace(match.group(1)); body = f'<h1>Delete workspace?</h1><p>This permanently removes all workspace data.</p><form method="post" action="{esc(self._url(f"/w/{self.token}/delete"))}"><button>Confirm permanent deletion</button></form>'
                return self._send(200, self._page('Confirm deletion', body, self.token))
            match = re.fullmatch(r'/w/([^/]+)/m/([^/]+)/delete/confirm', parsed.path)
            if match:
                state = self._workspace(match.group(1)); model, _ = self._model(state, match.group(2)); body = f'<h1>Delete {esc(model["id"])}?</h1><p>This also removes its endpoints, scenarios and model-specific serving rows.</p><form method="post" action="{esc(self._url(f"/w/{self.token}/m/{model["mid"]}/delete"))}"><button>Confirm model deletion</button></form>'
                return self._send(200, self._page('Confirm deletion', body, self.token))
            match = re.fullmatch(r'/w/([^/]+)/export', parsed.path)
            if match:
                state = self._workspace(match.group(1)); exported = dict(state)
                if state.get('sample'): exported['_note'] = 'Made-up sample'
                return self._send(200, json.dumps(exported, ensure_ascii=False, indent=2), 'application/json; charset=utf-8', 'attachment; filename="marginmesh-workspace.json"')
            match = re.fullmatch(r'/w/([^/]+)/export\.csv', parsed.path)
            if match:
                state = self._workspace(match.group(1)); return self._send(200, csvout.dashboard(self.server.store.cached(self.token, state, reports_for)), 'text/csv; charset=utf-8', 'attachment; filename="marginmesh-dashboard.csv"')
            match = re.fullmatch(r'/w/([^/]+)/m/([^/.]+)\.csv', parsed.path)
            if match:
                state = self._workspace(match.group(1)); model, report = self._model(state, match.group(2)); current, error = self._calculate(report, one); rows = [x['values'] for x in state.get('scenarios', {}).get(model['mid'], [])]
                if not error: rows.append(current)
                return self._send(200, csvout.economics(model['id'], rows), 'text/csv; charset=utf-8', 'attachment; filename="marginmesh-scenarios.csv"')
            match = re.fullmatch(r'/w/([^/]+)/m/([^/]+)', parsed.path)
            if match:
                state = self._workspace(match.group(1)); model, report = self._model(state, match.group(2)); return self._model_page(model, report, one)
            return self._error(404, 'Page not found')
        except Missing as exc: return self._error(404, exc)
        except StoreError as exc: return self._error(exc.status, exc)
        except (ValueError, TypeError, AttributeError, RecursionError, ArithmeticError) as exc: return self._error(400, 'Stored or supplied data is invalid')

    def _landing(self, code=''):
        msg = f'<p class="card">{esc(MESSAGES[code])}</p>' if code in MESSAGES else ''
        body = f'<h1>marginmesh</h1>{msg}<p>Paste provider token prices and your own GPU economics to find break-even utilisation, target-margin pricing and whether you can undercut the market.</p><p>Accepted inputs: prices CSV, an OpenRouter-style endpoints JSON document downloaded by you, and serving CSV. Requests are limited to 1 MB.</p><form method="post" action="{esc(self._url("/w/new"))}"><button name="sample" value="0">Start a workspace</button> <button name="sample" value="1">Try with sample data</button></form><p class="muted">The sample is made up and is only a prefill.</p>'
        self._send(200, self._page('marginmesh', body))

    def _methodology(self):
        body = f'<h1>Methodology</h1><p>Prices are USD per million tokens. Blended price = (k × input + output)/(k+1). R is million tokens per GPU-hour.</p><ul><li>Margin/GPU-hour = p × R × u − c</li><li>Break-even utilisation = c/(p × R)</li><li>Entry price = c/(R × u × (1−g))</li><li>Match utilisation = c/(minimum price × R × (1−g))</li></ul><p>Free endpoints are shown but excluded from market statistics. Rent is the median of supplied rows. Values over 100% utilisation are shown as not viable.</p><p><b>Sensitivity:</b> if R is overestimated by 20%, break-even utilisation is understated by 25%.</p><p>{esc(HONESTY)}</p><p>The sample data is made up. No network or AI model is used.</p>'
        self._send(200, self._page('Methodology', body))

    def _dashboard(self, state, code=''):
        reports = self.server.store.cached(self.token, state, reports_for); by_id = {x['id']: x for x in state.get('models', [])}; rows = []
        money = lambda x: 'n/a' if x is None else f'${x:.3f}'
        for report in reports:
            model = by_id[report.model_id]; spread = 'n/a' if report.spread_ratio is None else f'{report.spread_ratio:.1f}×'
            rows.append(f'<tr><td><a href="{esc(self._url(f"/w/{self.token}/m/{model["mid"]}"))}">{esc(report.model_id)}</a></td><td>{report.provider_count}</td><td>{report.free_count}</td><td>{money(report.price_min)}</td><td>{money(report.price_median)}</td><td>{money(report.price_max)}</td><td>{spread}</td><td>{esc(report.quant_flag)}</td><td>{esc(report.uptime_flag)}</td><td>{"yes" if report.stale_flag else "no"}</td></tr>')
        chip_rows = ''.join(f'<tr><td>{i + 1}</td><td>{esc(x.get("chip", ""))}</td><td>{money(x.get("rent"))}</td><td>{esc(x.get("r", ""))}</td><td>{esc(x.get("model") or "all models")}</td><td>{esc(x.get("quant", ""))}</td><td>{esc(x.get("confidence", ""))}</td><td>{esc(x.get("source", ""))}</td><td><form method="post" action="{esc(self._url(f"/w/{self.token}/chips/{i}/delete"))}"><button>Delete row</button></form></td></tr>' for i, x in enumerate(state.get('chips', []))) or '<tr><td colspan="9">No serving rows</td></tr>'
        msg = f'<p class="card">{esc(MESSAGES[code])}</p>' if code in MESSAGES else ''
        body = f'''<h1>Workspace</h1>{msg}<p class="card"><b>This link is the only key. Bookmark it. It cannot be recovered.</b><br>Idle workspaces expire after 90 days. Empty or untouched workspaces, including untouched samples, expire after 24 hours. Export your own backup.</p>
<table><tr><th>Model</th><th>Providers</th><th>Free</th><th>Min</th><th>Median</th><th>Max</th><th>Spread</th><th>Quant</th><th>Uptime</th><th>Age flag</th></tr>{''.join(rows)}</table>
<h2>Import market prices</h2><form method="post" enctype="multipart/form-data" action="{esc(self._url(f'/w/{self.token}/import/prices'))}"><label>Format <select name="format"><option value="csv">CSV</option><option value="openrouter_json">OpenRouter JSON</option></select></label><label>Unit <select name="unit"><option value="per_m">USD per million tokens</option><option value="per_token">USD per token</option></select></label><p class="muted">The unit selector applies to CSV only. OpenRouter JSON prices are always interpreted as USD per token.</p><label>Model (required for JSON) <input name="model" maxlength="80"></label><label>Mode <select name="mode"><option>append</option><option>replace</option></select></label><label>Paste text<textarea name="text"></textarea></label><label>Or upload a UTF-8 text file <input type="file" name="file" accept="text/csv,application/vnd.ms-excel,application/json,text/plain"></label><button>Import prices</button></form>
<h2 id="chips">Serving rows / chips</h2><table><tr><th>#</th><th>Chip</th><th>Rent/h</th><th>R</th><th>Model</th><th>Quant</th><th>Confidence</th><th>Source</th><th>Action</th></tr>{chip_rows}</table><form method="post" enctype="multipart/form-data" action="{esc(self._url(f'/w/{self.token}/import/chips'))}"><label>Mode <select name="mode"><option>append</option><option>replace</option></select></label><label>Paste serving CSV<textarea name="text"></textarea></label><label>Or upload a UTF-8 CSV <input type="file" name="file" accept="text/csv,application/vnd.ms-excel,text/plain"></label><button>Import chips</button></form><p class="muted">Serving CSV requires chip, usd_per_gpu_hour and either m_tokens_per_gpu_hour or both gpus_per_replica and tps_per_replica. Optional: model, quant, source, confidence.</p><p><a href="{esc(self._url(f'/w/{self.token}/export'))}">Export everything (JSON)</a> · <a href="{esc(self._url(f'/w/{self.token}/export.csv'))}">Dashboard CSV</a></p><p><a href="{esc(self._url(f'/w/{self.token}/delete/confirm'))}">Delete this workspace…</a></p>'''
        self._send(200, self._page('Workspace', body, self.token))

    def _calculate(self, report, values):
        defaults = {'chip': report.chips[0]['chip'] if report.chips else 'example-chip', 'rent': report.chips[0]['rent'] if report.chips else 2.5, 'r': report.chips[0]['r'] if report.chips else 3.6, 'u': .6, 'g': .3, 'k': 3, 'ulo': .4, 'uhi': .7}
        raw = {key: values.get(key, default) for key, default in defaults.items()}
        try:
            k = econ.number('input:output ratio', raw['k'], low=0, high=20); dynamic = market_report(report.model_id, report.endpoints, k)
            result = scenario(dynamic, str(raw['chip'])[:200], econ.number('rent', raw['rent'], low=0, low_open=True), econ.number('R', raw['r'], low=0, low_open=True), econ.number('utilisation', raw['u'], low=0, high=1, low_open=True), econ.number('target margin', raw['g'], low=0, high=.95), econ.number('band low', raw['ulo'], low=0, high=1, low_open=True), econ.number('band high', raw['uhi'], low=0, high=1, low_open=True)); result['k'] = k
            return result, None
        except (econ.InputError, ArithmeticError) as exc: return raw, str(exc) if isinstance(exc, econ.InputError) else 'Calculator values are outside the supported numeric range'

    def _model_page(self, model, report, values):
        current, error = self._calculate(report, values); money = lambda x: 'n/a' if x is None else f'${x:.3f}'
        provider_rows = []
        for endpoint in report.endpoints[:200]:
            free = not (endpoint.price_in or endpoint.price_out); blended = None if free else econ.blended_price(endpoint.price_in, endpoint.price_out, current.get('k', 3) if not error else 3)
            provider_rows.append(f'<tr><td>{esc(endpoint.provider)}</td><td>{"free — excluded from market statistics" if free else "paid"}</td><td>{money(endpoint.price_in)}</td><td>{money(endpoint.price_out)}</td><td>{"free" if free else money(blended)}</td><td>{esc(endpoint.quantization or "undisclosed")}</td><td>{esc(endpoint.context_len if endpoint.context_len is not None else "n/a")}</td><td>{esc(endpoint.uptime_pct if endpoint.uptime_pct is not None else "n/a")}</td><td>{esc(endpoint.throughput_tps if endpoint.throughput_tps is not None else "n/a")}</td></tr>')
        serving = ''.join(f'<tr><td>{esc(x["chip"])}</td><td>{money(x["rent"])}</td><td>{x["r"]:.3f}</td><td>{esc(x["quant"] or "n/a")}</td><td>{esc(x["confidence"])}</td><td>{esc(x["reference"])}</td></tr>' for x in report.chips)
        economics = []
        for row in report.chips:
            try:
                a = econ.percentage_or_not_viable(econ.break_even(row['rent'], row['r'], report.price_min)) if report.price_min else 'n/a'; b = econ.percentage_or_not_viable(econ.break_even(row['rent'], row['r'], report.price_median)) if report.price_median else 'n/a'; low, high = econ.entry_band(row['rent'], row['r'])
            except (econ.InputError, ArithmeticError): a = b = 'invalid assumption'; low = high = None
            economics.append(f'<tr><td>{esc(row["chip"])}</td><td>{esc(a)}</td><td>{esc(b)}</td><td>{money(low)}</td><td>{money(high)}</td></tr>')
        fields = ''.join(f'<label>{label} <input name="{name}" value="{esc(current.get(name if name not in ("ulo", "uhi") else "u_" + name[1:], current.get(name, "")))}"></label>' for name, label in [('chip','Chip'),('rent','Rent'),('r','R'),('u','Utilisation'),('g','Target margin'),('k','Input:output ratio'),('ulo','Band low'),('uhi','Band high')])
        if error: result = f'<p class="error">{esc(error)}</p><div class="card">Results unavailable until corrected.</div>'; status = 400
        else:
            pct = lambda x: 'no market price' if x is None else econ.percentage_or_not_viable(x)
            result = f'<div class="card">Margin per GPU-hour: min {money(current.get("margin_min"))}, median {money(current.get("margin_median"))}. Break-even at min {esc(pct(current.get("break_even_min")))}; median {esc(pct(current.get("break_even_median")))}. Entry band {money(current.get("entry_low"))}–{money(current.get("entry_high"))}. Price at selected utilisation {money(current.get("entry_at_u"))}. Match utilisation {esc(pct(current.get("u_match")))}. <b>{esc(current.get("verdict"))}</b>.</div>'; status = 200
        saved = self.state.get('scenarios', {}).get(model['mid'], []); saved_html = ''.join(f'<li>{esc(x["name"])} <form style="display:inline" method="post" action="{esc(self._url(f"/w/{self.token}/m/{model["mid"]}/scenario/{index}/delete"))}"><button>Delete</button></form></li>' for index, x in enumerate(saved)) or '<li>No saved scenarios</li>'
        hidden = ''.join(f'<input type="hidden" name="{key}" value="{esc(current.get(key if key not in ("ulo", "uhi") else "u_" + key[1:], current.get(key, "")))}">' for key in ('chip','rent','r','u','g','k','ulo','uhi'))
        body = f'<h1>{esc(model["id"])}</h1><h2>Providers</h2><table><tr><th>Provider</th><th>Status</th><th>Input</th><th>Output</th><th>Blended</th><th>Quant</th><th>Context</th><th>Uptime %</th><th>Throughput</th></tr>{"".join(provider_rows)}</table><h2>Serving table</h2><table><tr><th>Chip</th><th>Median rent/h</th><th>M tokens/GPU-h</th><th>Quant</th><th>Confidence</th><th>Source note</th></tr>{serving}</table><h2>Per-chip break-even</h2><table><tr><th>Chip</th><th>At minimum</th><th>At median</th><th>Entry low (70% utilisation)</th><th>Entry high (40% utilisation)</th></tr>{"".join(economics)}</table><p class="muted">Entry bands use a 30% target margin. {esc(HONESTY)}</p><h2>Calculator</h2><form method="get" action="{esc(self._url(f"/w/{self.token}/m/{model["mid"]}"))}">{fields}<button>Recalculate</button></form>{result}<h2>Saved scenarios</h2><ul>{saved_html}</ul><form method="post" action="{esc(self._url(f"/w/{self.token}/m/{model["mid"]}/scenario"))}"><label>Scenario name <input name="name" maxlength="60" required></label>{hidden}<button>Save current scenario</button></form><p><a href="{esc(self._url(f"/w/{self.token}/m/{model["mid"]}.csv"))}">Download saved and current scenarios (CSV)</a></p><p><a href="{esc(self._url(f"/w/{self.token}/m/{model["mid"]}/delete/confirm"))}">Delete this model…</a></p>'
        self._send(status, self._page(model['id'], body, self.token))

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._host_ok(): return self._error(421, 'Misdirected Request')
        if not self._origin_ok(): return self._error(403, 'Origin or Referer rejected')
        try:
            fields = self._body()
            if parsed.path == '/w/new':
                sample = fields.get('sample') == '1'; token = self.server.store.create(sample_state(self.server.root) if sample else None, sample); return self._send(303, b'', location=self._url('/w/' + token))
            match = re.fullmatch(r'/w/([^/]+)/(.*)', parsed.path)
            if not match: return self._error(404, 'Page not found')
            state = self._workspace(match.group(1)); tail = match.group(2)
            if tail == 'delete': self.server.store.delete(self.token); return self._send(303, b'', location=self._url('/?msg=workspace_deleted'))
            if tail == 'import/prices':
                text = fields.get('file') or fields.get('text', '')
                if not text.strip(): raise InputFileError('Paste text or choose a file')
                if fields.get('format', 'csv') == 'openrouter_json': rows, skipped, errors = parse_openrouter_json(text, fields.get('model', ''))
                else: rows, skipped, errors = parse_prices_csv(text, fields.get('unit', 'per_m'))
                names = list(dict.fromkeys(x['model_id'] for x in rows)); existing_names = {x['id'] for x in state.get('models', [])}
                if len(existing_names | set(names)) > 20: raise CapacityError('A workspace can contain at most 20 models')
                if fields.get('mode') == 'replace': state['endpoints'] = [x for x in state.get('endpoints', []) if x['model_id'] not in names]
                counts = {}
                for row in state.get('endpoints', []): counts[row['model_id']] = counts.get(row['model_id'], 0) + 1
                accepted = []
                for row in rows:
                    if counts.get(row['model_id'], 0) >= 200: skipped += 1; continue
                    accepted.append(row); counts[row['model_id']] = counts.get(row['model_id'], 0) + 1
                if len(state.get('endpoints', [])) + len(accepted) > 1000: raise CapacityError('A workspace can contain at most 1000 endpoints')
                state.setdefault('endpoints', []).extend(accepted)
                for name in names:
                    if name not in existing_names:
                        base = slug(name); mid = base; suffix = 2
                        while any(x['mid'] == mid for x in state.setdefault('models', [])): mid, suffix = f'{base}-{suffix}', suffix + 1
                        state['models'].append({'id': name, 'mid': mid})
                self.server.store.save(self.token, state); return self._send(303, b'', location=self._url(f'/w/{self.token}?msg=prices_imported'))
            if tail == 'import/chips':
                text = fields.get('file') or fields.get('text', '')
                if not text.strip(): raise InputFileError('Paste text or choose a file')
                rows, skipped, errors = parse_chips_csv(text)
                if fields.get('mode') == 'replace':
                    models = {x.get('model', '') for x in rows}; state['chips'] = [x for x in state.get('chips', []) if x.get('model', '') not in models]
                if len(state.get('chips', [])) + len(rows) > 100: raise CapacityError('A workspace can contain at most 100 chip rows')
                state.setdefault('chips', []).extend(rows); self.server.store.save(self.token, state); return self._send(303, b'', location=self._url(f'/w/{self.token}?msg=chips_imported'))
            chip_delete = re.fullmatch(r'chips/(\d+)/delete', tail)
            if chip_delete:
                index = int(chip_delete.group(1)); rows = state.setdefault('chips', [])
                if index >= len(rows): raise Missing('Serving row not found')
                del rows[index]; self.server.store.save(self.token, state); return self._send(303, b'', location=self._url(f'/w/{self.token}?msg=chip_deleted#chips'))
            scenario_match = re.fullmatch(r'm/([^/]+)/scenario', tail)
            if scenario_match:
                model, report = self._model(state, scenario_match.group(1)); name = fields.get('name', '').strip()
                if not name or len(name) > 60: raise InputFileError('Scenario name must be 1 to 60 characters')
                values, error = self._calculate(report, fields)
                if error: raise InputFileError(error)
                saved = state.setdefault('scenarios', {}).setdefault(model['mid'], [])
                if len(saved) >= 10: raise CapacityError('This model already has 10 scenarios; delete one first')
                saved.append({'name': name, 'values': values, 'created_at': time.time()}); self.server.store.save(self.token, state); return self._send(303, b'', location=self._url(f'/w/{self.token}/m/{model["mid"]}'))
            delete_scenario = re.fullmatch(r'm/([^/]+)/scenario/(\d+)/delete', tail)
            if delete_scenario:
                model, report = self._model(state, delete_scenario.group(1)); rows = state.setdefault('scenarios', {}).setdefault(model['mid'], []); index = int(delete_scenario.group(2))
                if index >= len(rows): raise Missing('Scenario not found')
                del rows[index]; self.server.store.save(self.token, state); return self._send(303, b'', location=self._url(f'/w/{self.token}/m/{model["mid"]}'))
            delete_model = re.fullmatch(r'm/([^/]+)/delete', tail)
            if delete_model:
                model, report = self._model(state, delete_model.group(1)); state['models'] = [x for x in state['models'] if x['mid'] != model['mid']]; state['endpoints'] = [x for x in state['endpoints'] if x['model_id'] != model['id']]; state['chips'] = [x for x in state.get('chips', []) if x.get('model') != model['id']]; state.setdefault('scenarios', {}).pop(model['mid'], None); self.server.store.save(self.token, state); return self._send(303, b'', location=self._url(f'/w/{self.token}?msg=model_deleted'))
            return self._error(404, 'Page not found')
        except InputFileError as exc: return self._error(getattr(exc, 'status', 400), exc, exc.errors)
        except Missing as exc: return self._error(404, exc)
        except StoreError as exc: return self._error(exc.status, exc)
        except (UnicodeError, ValueError, TypeError, AttributeError, RecursionError) as exc: return self._error(400, 'Invalid input')


def make_server(host='127.0.0.1', port=8080, environ=None):
    environ = os.environ if environ is None else environ; root = Path(__file__).resolve().parent; data_dir = Path(environ.get('DATA_DIR', './data_app')).resolve(); store = WorkspaceStore(data_dir, environ.get('APP_DATA_CAP_MB', '200')); store.sweep(); server = Server((host, int(port)), Handler, 32); server.root = root; server.store = store; server.request_deadline = 10
    public = environ.get('PUBLIC_BASE_URL', '').rstrip('/'); parsed = urlparse(public) if public else None
    if public and (parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path): server.server_close(); raise ValueError('PUBLIC_BASE_URL must be an http(s) origin without a path')
    server.base = public; server.public_origin = f'{parsed.scheme.lower()}://{parsed.netloc.lower().rstrip(".")}' if parsed else ''; server.secure = environ.get('APP_HTTPS_TERMINATED', '').lower() == 'true' or bool(parsed and parsed.scheme == 'https')
    if parsed: server.allowed_hosts = {parsed.netloc.lower().rstrip('.')}
    else:
        actual = server.server_port; configured = host.lower().rstrip('.'); server.allowed_hosts = {configured, f'{configured}:{actual}', f'127.0.0.1:{actual}', f'localhost:{actual}'}
    def sweeper():
        while not server.stop_sweep.wait(86400): store.sweep()
    threading.Thread(target=sweeper, daemon=True).start(); return server


if __name__ == '__main__':
    bind_host = os.environ.get('APP_HOST', '127.0.0.1'); bind_port = int(os.environ.get('PORT', '8080')); app = make_server(bind_host, bind_port); print(f'listening on {bind_host}:{app.server_port}', flush=True)
    try: app.serve_forever()
    except KeyboardInterrupt: pass
    finally: app.server_close()
