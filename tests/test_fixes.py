import contextlib
import http.client
import io
import socket
import threading
import time
import unittest

from demo import SubmissionRateLimiter, make_server
from iue import render
from iue.report import build_reports, scenario
from iue.sources import FakePrice, FakeRent, FakeServing
from iue.vault import MemoryVault


class SlowNumber:
    def __init__(self, value=0):
        self.value = value

    def __add__(self, other):
        value = self.value
        time.sleep(.005)
        return SlowNumber(value + other)


class ReviewerFixTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server('127.0.0.1', 0, {})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, sid=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Content-Length': '0',
            'Origin': f'http://127.0.0.1:{self.server.server_port}',
        }
        if sid:
            headers['Cookie'] = f'demo_sid={sid}'
        connection.request('POST', '/interest', '', headers)
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
        return status

    def test_cookieless_clients_do_not_share_five_request_bucket(self):
        self.assertEqual([self.post() for _ in range(6)], [200, 200, 200, 200, 200, 200])

    def test_global_token_bucket_and_session_table_bound(self):
        limiter = SubmissionRateLimiter(global_capacity=3, refill_per_second=0, max_sessions=2)
        self.assertTrue(limiter.allow('a' * 32))
        self.assertTrue(limiter.allow('b' * 32))
        self.assertTrue(limiter.allow('c' * 32))
        self.assertLessEqual(len(limiter.sessions), 2)
        self.assertFalse(limiter.allow('d' * 32))

    def test_full_vault_evicts_oldest_record(self):
        vault = MemoryVault(max_records=2)
        oldest = vault.put('signup', {'email': 'old@example.test'})
        vault.put('signup', {'email': 'middle@example.test'})
        newest = vault.put('signup', {'email': 'new@example.test'})
        self.assertEqual(vault.count('signup'), 2)
        self.assertFalse(vault.delete(oldest))
        self.assertTrue(vault.delete(newest))

    def test_total_deadline_closes_slow_header_connection(self):
        self.server.request_deadline = .15
        connection = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=1)
        try:
            connection.sendall(b'G')
            started = time.monotonic()
            self.assertEqual(connection.recv(1), b'')
            self.assertLess(time.monotonic() - started, .8)
        finally:
            connection.close()

    def test_handle_error_does_not_log_client_address_or_traceback(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self.server.handle_error(None, ('203.0.113.9', 4567))
        self.assertEqual(captured.getvalue(), '')

    def test_interest_counter_update_is_locked(self):
        self.server.rate_limiter = SubmissionRateLimiter(global_capacity=100, refill_per_second=0, max_sessions=100)
        self.server.rates = self.server.rate_limiter.sessions
        self.server.interest_clicks = SlowNumber()
        workers = [threading.Thread(target=self.post, args=(f'{index:032x}',)) for index in range(20)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(self.server.interest_clicks.value, 20)

    def test_per_chip_table_contains_entry_price_band(self):
        reports = build_reports(FakePrice().endpoints(), FakeRent().quotes(), FakeServing().assumptions())
        report = next(item for item in reports if item.model_id.startswith('Llama'))
        section = render.model_page(report, None).split('<h2>Per-chip break-even</h2>', 1)[1]
        self.assertIn('Entry low (70% utilisation)', section)
        self.assertIn('Entry high (40% utilisation)', section)
        self.assertIn('Entry bands use a 30% target margin', section)
        self.assertGreaterEqual(section.count('$'), 6)

    def test_utilisation_changes_visible_margin_and_entry_price(self):
        reports = build_reports(FakePrice().endpoints(), FakeRent().quotes(), FakeServing().assumptions())
        report = next(item for item in reports if item.model_id.startswith('Llama'))
        low = scenario(report, 'H100-SXM', 2.4, 1.5, u=.25, g=.3)
        high = scenario(report, 'H100-SXM', 2.4, 1.5, u=.75, g=.3)
        self.assertNotEqual(low['margin_min'], high['margin_min'])
        self.assertNotEqual(low['entry_at_u'], high['entry_at_u'])
        page = render.model_page(report, high)
        self.assertIn('75% utilisation', page)
        self.assertIn('margin/GPU-hour at min', page)
        self.assertIn('target-margin entry price', page)

    def test_free_endpoints_are_displayed_and_flagged(self):
        reports = build_reports(FakePrice().endpoints(), FakeRent().quotes(), FakeServing().assumptions())
        report = next(item for item in reports if item.model_id.startswith('Mistral'))
        page = render.model_page(report, None)
        self.assertIn('Northwind Inference', page)
        self.assertIn('free — excluded from market statistics', page)
        self.assertIn('<th>Status</th>', page)

    def test_static_demand_controls_use_configured_destinations(self):
        reports = build_reports(FakePrice().endpoints(), FakeRent().quotes(), FakeServing().assumptions())
        page = render.dashboard_page(
            reports, static=True, demo=False,
            form_action='https://forms.example/submit',
        )
        self.assertIn('action="https://forms.example/submit"', page)
        self.assertIn('name="email"', page)
        self.assertIn('name="model_name"', page)
        self.assertIn('form-action &#x27;self&#x27; https://forms.example', page)

    def test_oversized_parsed_headers_are_rejected(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        headers = {f'X-Test-{index}': 'x' for index in range(51)}
        connection.request('GET', '/', headers=headers)
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        self.assertEqual(response.status, 431)
        self.assertIn(render.BANNER, body)
        self.assertIn('Content-Security-Policy', response.headers)

    def test_parser_errors_use_branded_csp_page(self):
        connection = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2)
        try:
            connection.sendall(b'NOT A VALID REQUEST\r\n\r\n')
            response = b''
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            connection.close()
        text = response.decode('utf-8', errors='replace')
        self.assertIn('Content-Security-Policy:', text)
        self.assertIn(render.BANNER, text)


if __name__ == '__main__':
    unittest.main()
