import os
import re
import socket
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from iue import csvout, econ, render
from iue.report import build_reports, market_report, scenario
from iue.sources import FakePrice, FakeRent, FakeServing
from iue.vault import MemoryVault

MAX_BODY = 8192
MAX_REQUEST_LINE = 8192
MAX_QUERY = 4096
MAX_HEADERS = 50
MAX_HEADER_BYTES = 16384
REQUEST_DEADLINE = 10


class RateBuckets(OrderedDict):
    def __init__(self, clock, window):
        super().__init__()
        self.clock = clock
        self.window = window

    def __contains__(self, key):
        if not super().__contains__(key):
            return False
        bucket = super().__getitem__(key)
        now = self.clock()
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if not bucket:
            super().__delitem__(key)
            return False
        return True


class SubmissionRateLimiter:
    def __init__(self, global_capacity=60, refill_per_second=1, session_limit=5,
                 window=600, max_sessions=1024, clock=time.monotonic):
        if global_capacity <= 0 or refill_per_second < 0 or session_limit <= 0 or max_sessions <= 0:
            raise ValueError('rate-limit settings must be positive')
        self.global_capacity = float(global_capacity)
        self.refill_per_second = float(refill_per_second)
        self.session_limit = int(session_limit)
        self.window = float(window)
        self.max_sessions = int(max_sessions)
        self.clock = clock
        self.tokens = float(global_capacity)
        self.updated = clock()
        self.sessions = RateBuckets(clock, window)
        self.lock = threading.Lock()

    def allow(self, sid, anonymous=False, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            elapsed = max(0.0, now - self.updated)
            self.tokens = min(self.global_capacity, self.tokens + elapsed * self.refill_per_second)
            self.updated = now
            if anonymous:
                if self.tokens < 1:
                    return False
                self.tokens -= 1
                return True
            key = ('sid', sid)
            if key in self.sessions:
                bucket = self.sessions[key]
                if len(bucket) >= self.session_limit:
                    return False
            else:
                if len(self.sessions) >= self.max_sessions:
                    self.sessions.popitem(last=False)
                bucket = deque()
                self.sessions[key] = bucket
            if self.tokens < 1:
                if not bucket:
                    del self.sessions[key]
                return False
            bucket.append(now)
            self.sessions.move_to_end(key)
            self.tokens -= 1
            return True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, max_threads=32):
        self._slots = threading.BoundedSemaphore(max_threads)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\nContent-Security-Policy: default-src \'self\'; frame-ancestors \'none\'\r\n\r\n')
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 3

    def setup(self):
        super().setup()
        self.connection.settimeout(self.timeout)
        self._deadline_timer = threading.Timer(self.server.request_deadline, self._expire_request)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def finish(self):
        timer = getattr(self, '_deadline_timer', None)
        if timer is not None:
            timer.cancel()
        super().finish()

    def _expire_request(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        # Parser errors can occur before a valid request version is recorded.
        # BaseHTTPRequestHandler defaults those requests to HTTP/0.9, which
        # suppresses the status line and all response headers. Use the server's
        # protocol for the controlled error response so CSP and other security
        # headers are still transmitted.
        if self.request_version == self.default_request_version:
            self.request_version = self.protocol_version
        self._error(code, message or self.responses.get(code, ('Error',))[0])

    def parse_request(self):
        if not super().parse_request():
            return False
        if len(self.raw_requestline) > MAX_REQUEST_LINE:
            self._error(414, 'Request line is too long')
            return False
        header_bytes = sum(len(key.encode('utf-8')) + len(value.encode('utf-8')) + 4
                           for key, value in self.headers.raw_items())
        if len(self.headers) > MAX_HEADERS or header_bytes > MAX_HEADER_BYTES:
            self._error(431, 'Request headers are too large')
            return False
        return True

    def _security(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Cache-Control', 'no-store')
        if self.server.https:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')

    def _send(self, status, data, content_type='text/html; charset=utf-8', cookie=False,
              disposition=None, headers=None):
        data = data.encode('utf-8') if isinstance(data, str) else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self._security()
        if content_type.startswith('text/html'):
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        if disposition:
            self.send_header('Content-Disposition', disposition)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if cookie:
            value = f'demo_sid={self.sid}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400'
            if self.server.secure:
                value += '; Secure'
            self.send_header('Set-Cookie', value)
        self.close_connection = True
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass

    def _sid(self):
        cookies = self.headers.get('Cookie', '')
        found = re.search(r'(?:^|;\s*)demo_sid=([0-9a-f]{32})(?:;|$)', cookies)
        if found:
            self.sid = found.group(1)
            return False
        import secrets
        self.sid = secrets.token_hex(16)
        return True

    def _host_ok(self):
        host = self.headers.get('Host', '').lower().rstrip('.')
        return bool(host) and host in self.server.allowed_hosts

    def _error(self, status, message, allow=None):
        headers = {'Allow': allow} if allow else None
        self._send(status, render.message_page(str(status), str(message)[:80], self.server.base), headers=headers)

    def _request_origin_ok(self):
        expected = self.server.public_origin
        if not expected:
            scheme = 'https' if self.server.secure else 'http'
            expected = f"{scheme}://{self.headers.get('Host', '').lower().rstrip('.')}"
        supplied = self.headers.get('Origin')
        parsed = urlparse(supplied) if supplied else urlparse(self.headers.get('Referer', ''))
        if not parsed.scheme or not parsed.netloc:
            return False
        actual = f"{parsed.scheme.lower()}://{parsed.netloc.lower().rstrip('.')}"
        return actual == expected

    def _rate_ok(self, anonymous=False):
        return self.server.rate_limiter.allow(self.sid, anonymous)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/healthz':
            return self._send(200, 'ok', 'text/plain; charset=utf-8')
        if len(parsed.query.encode('utf-8')) > MAX_QUERY:
            return self._error(414, 'Query string is too long')
        if not self._host_ok():
            return self._error(421, 'Misdirected Request')
        fresh = self._sid()
        try:
            params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            return self._error(400, 'Too many query fields')
        if parsed.path == '/':
            return self._send(200, render.dashboard_page(self.server.reports, self.server.base), cookie=fresh)
        if parsed.path == '/methodology':
            return self._send(200, render.methodology_page(self.server.base), cookie=fresh)
        if parsed.path == '/export.csv':
            return self._send(200, csvout.dashboard(self.server.reports), 'text/csv; charset=utf-8', disposition='attachment; filename="iue-dashboard.csv"')
        if parsed.path == '/model':
            return self._model(params, fresh, False)
        if parsed.path == '/model.csv':
            return self._model(params, fresh, True)
        if parsed.path == '/interest':
            body = ('<h1>Demo interest signal</h1><p>This records one anonymous, count-only sample signal. Do not enter personal data.</p>'
                    '<form method="post" action="' + render.esc(render.join_url(self.server.base, 'interest')) + '"><button name="sample_interest" value="made-up-demo-signal">Count sample interest</button></form>')
            return self._send(200, render.layout('Demo interest', body, self.server.base, canonical_path='/interest'), cookie=fresh)
        if parsed.path in ('/signup', '/request', '/delete'):
            return self._error(405, 'Method not allowed', 'POST')
        return self._error(404, f'Unknown route: {parsed.path[:80]}')

    def _default_scenario(self, report, choice=None):
        choice = choice or next((x for x in report.chips if x['rent'] is not None), None)
        if not choice:
            return None
        try:
            result = scenario(report, choice['chip'], choice['rent'], choice['r'])
            result['k'] = 3
            return result
        except (econ.InputError, ArithmeticError):
            return None

    def _model(self, params, fresh, csv_mode):
        model_id = params.get('id', [''])[0]
        if model_id not in self.server.by_id:
            return self._error(404, f'Unknown model: {model_id[:80]}')
        base_report = self.server.by_id[model_id]
        raw = {key: values[0] for key, values in params.items() if values}
        try:
            k = econ.number('input:output ratio', raw.get('k', 3), low=0, high=20)
            report = market_report(model_id, self.server.endpoints, k)
            report.chips = base_report.chips
            if not report.chips:
                if csv_mode:
                    return self._send(200, csvout.economics(model_id, []), 'text/csv; charset=utf-8', disposition='attachment; filename="iue-model.csv"')
                return self._send(200, render.model_page(report, None, base=self.server.base), cookie=fresh)
            chip = raw.get('chip', report.chips[0]['chip'])
            choice = next((x for x in report.chips if x['chip'] == chip), None)
            if not choice:
                raise econ.InputError('unknown chip')
            if choice['rent'] is None and 'rent' not in raw:
                if csv_mode:
                    return self._send(200, csvout.economics(model_id, []), 'text/csv; charset=utf-8', disposition='attachment; filename="iue-model.csv"')
                return self._send(200, render.model_page(report, None, base=self.server.base), cookie=fresh)
            c = econ.number('rent', raw.get('rent', choice['rent']), low=0, low_open=True)
            r = econ.number('R', raw.get('r', choice['r']), low=0, low_open=True)
            u = econ.number('utilisation', raw.get('u', .6), low=0, high=1, low_open=True)
            g = econ.number('target margin', raw.get('g', .3), low=0, high=.95)
            ulo = econ.number('low utilisation', raw.get('ulo', .4), low=0, high=1, low_open=True)
            uhi = econ.number('high utilisation', raw.get('uhi', .7), low=0, high=1, low_open=True)
            current = scenario(report, chip, c, r, u, g, ulo, uhi)
            current['k'] = k
        except econ.InputError as exc:
            return self._invalid_model(base_report, raw, fresh, str(exc))
        except ArithmeticError:
            return self._invalid_model(base_report, raw, fresh, 'Calculator values are outside the supported numeric range')
        if csv_mode:
            return self._send(200, csvout.economics(model_id, [current]), 'text/csv; charset=utf-8', disposition='attachment; filename="iue-model.csv"')
        return self._send(200, render.model_page(report, current, base=self.server.base), cookie=fresh)

    def _invalid_model(self, report, raw, fresh, message):
        choice = next((x for x in report.chips if x['chip'] == raw.get('chip')), None)
        current = self._default_scenario(report, choice)
        return self._send(400, render.model_page(report, current, message, self.server.base, form_values=raw), cookie=fresh)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._host_ok():
            return self._error(421, 'Misdirected Request')
        allowed = ('/signup', '/request', '/interest', '/delete')
        if parsed.path not in allowed:
            if parsed.path in ('/', '/model', '/methodology', '/healthz', '/export.csv'):
                return self._error(405, 'Method not allowed', 'GET')
            return self._error(404, f'Unknown route: {parsed.path[:80]}')
        if not self._request_origin_ok():
            return self._error(403, 'Origin or Referer rejected')
        fresh = self._sid()
        if not self._rate_ok(anonymous=fresh):
            return self._error(429, 'Submission rate limit exceeded')
        if self.headers.get('Transfer-Encoding'):
            return self._error(400, 'Transfer-Encoding is not supported')
        length = self.headers.get('Content-Length')
        if length is None:
            return self._error(411, 'Content-Length required')
        try:
            size = int(length)
        except ValueError:
            return self._error(400, 'Invalid Content-Length')
        if size < 0:
            return self._error(400, 'Invalid Content-Length')
        if size > MAX_BODY:
            return self._error(413, 'Request body is too large')
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
            return self._error(415, 'Form encoding required')
        try:
            raw_body = self.rfile.read(size)
        except socket.timeout:
            return self._error(408, 'Request body timed out')
        if len(raw_body) != size:
            return self._error(400, 'Incomplete request body')
        try:
            fields = parse_qs(raw_body.decode('utf-8'), keep_blank_values=True, max_num_fields=10)
        except (UnicodeDecodeError, ValueError):
            return self._error(400, 'Invalid form body')
        values = {key: entries[0].strip() for key, entries in fields.items() if entries}
        if parsed.path == '/interest':
            with self.server.interest_lock:
                self.server.interest_clicks += 1
            return self._send(200, render.message_page('Sample interest received', 'One anonymous demo signal was counted.', self.server.base), cookie=fresh)
        if parsed.path == '/delete':
            receipt = values.get('receipt', '')
            if not re.fullmatch(r'[0-9a-f]{24}', receipt):
                return self._error(400, 'Invalid receipt')
            deleted = self.server.vault.delete(receipt)
            message = 'The record was deleted.' if deleted else 'No retained record matched that receipt.'
            return self._send(200, render.message_page('Deletion request', message, self.server.base), cookie=fresh)
        if parsed.path == '/signup':
            stored = {'sample_interest': 'made-up-demo-signal'}
            kind = 'signup'
        else:
            model_id = values.get('sample_model') or values.get('model_name', '')
            if model_id not in self.server.by_id:
                return self._error(400, 'Choose one of the made-up sample models')
            stored = {'sample_model': model_id}
            kind = 'request'
        receipt = self.server.vault.put(kind, stored)
        body = (f'<h1>Received</h1><p>Your deletion receipt is <code>{render.esc(receipt)}</code>.</p>'
                '<p>The count-only sample signal is retained for at most 24 hours or until this process restarts.</p>'
                f'<form method="post" action="{render.esc(render.join_url(self.server.base, "delete"))}">'
                f'<input type="hidden" name="receipt" value="{render.esc(receipt)}">'
                '<button>Delete this sample signal now</button></form>')
        return self._send(200, render.layout('Received', body, self.server.base, canonical_path='/'), cookie=fresh)


def make_server(host='127.0.0.1', port=8080, environ=None):
    environ = os.environ if environ is None else environ
    root = Path(__file__).resolve().parent
    endpoints = FakePrice(root / 'data/sample_endpoints.json').endpoints()
    rents = FakeRent(root / 'data/sample_rent.json').quotes()
    serving = FakeServing(root / 'data/sample_serving.json').assumptions()
    server = Server((host, int(port)), Handler, int(environ.get('DEMO_MAX_THREADS', '32')))
    server.endpoints = endpoints
    server.reports = build_reports(endpoints, rents, serving)
    server.by_id = {report.model_id: report for report in server.reports}
    server.vault = MemoryVault(max_records=1000, retention_seconds=86400)
    server.interest_clicks = 0
    server.interest_lock = threading.Lock()
    capacity = int(environ.get('DEMO_GLOBAL_POST_BURST', '60'))
    per_minute = float(environ.get('DEMO_GLOBAL_POSTS_PER_MINUTE', '60'))
    max_sessions = int(environ.get('DEMO_RATE_BUCKETS', '1024'))
    server.rate_limiter = SubmissionRateLimiter(capacity, per_minute / 60, max_sessions=max_sessions)
    server.rate_lock = server.rate_limiter.lock
    server.rates = server.rate_limiter.sessions
    server.request_deadline = float(environ.get('DEMO_REQUEST_DEADLINE', str(REQUEST_DEADLINE)))
    if server.request_deadline <= 0:
        server.server_close()
        raise ValueError('DEMO_REQUEST_DEADLINE must be positive')
    public = environ.get('PUBLIC_BASE_URL', '').rstrip('/')
    parsed = urlparse(public) if public else None
    if public and (parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path):
        server.server_close()
        raise ValueError('PUBLIC_BASE_URL must be an http(s) origin without a path')
    server.public_base = public
    server.public_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower().rstrip('.')}" if parsed else ''
    server.base = public
    server.secure = environ.get('DEMO_HTTPS_TERMINATED', '').lower() == 'true' or bool(parsed and parsed.scheme == 'https')
    server.https = server.secure
    if parsed:
        server.allowed_hosts = {parsed.netloc.lower().rstrip('.')}
    else:
        actual_port = server.server_port
        configured = host.lower().rstrip('.')
        server.allowed_hosts = {f'{configured}:{actual_port}', configured}
        if configured in ('127.0.0.1', '0.0.0.0', 'localhost'):
            server.allowed_hosts.update({f'127.0.0.1:{actual_port}', f'localhost:{actual_port}'})
    return server


if __name__ == '__main__':
    bind_host = os.environ.get('DEMO_HOST', '127.0.0.1')
    bind_port = int(os.environ.get('PORT', '8080'))
    app = make_server(bind_host, bind_port)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server_close()
