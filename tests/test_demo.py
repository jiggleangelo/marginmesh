import contextlib
import http.client
import io
import threading
import time
import unittest
import urllib.parse
from collections import deque
from pathlib import Path

from demo import make_server
from iue import render
from iue.render import BANNER
from iue.report import build_reports
from iue.sources import FakePrice, FakeRent, FakeServing


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server("127.0.0.1", 0, {})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        self.conn.request(method, path, body=body, headers=headers or {})
        response = self.conn.getresponse()
        return response, response.read().decode()

    def post_headers(self, sid=None):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": "0",
            "Origin": f"http://127.0.0.1:{self.server.server_port}",
        }
        if sid:
            headers["Cookie"] = f"demo_sid={sid}"
        return headers

    def test_flow(self):
        r, b = self.request("GET", "/healthz")
        self.assertEqual((r.status, b), (200, "ok"))
        r, b = self.request("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(BANNER, b)
        mid = urllib.parse.quote("Llama-3.3-70B (sample)")
        r, b = self.request("GET", f"/model?id={mid}&chip=H100-SXM&rent=1.9&r=1.5&u=.6&g=.4&k=3&ulo=.5&uhi=.75")
        self.assertEqual(r.status, 200)
        self.assertIn("competitive at median", b)
        self.assertIn("70%", b)
        r, b = self.request("GET", f"/model?id={mid}&k=21")
        self.assertEqual(r.status, 400)
        self.assertIn('class="error"', b)
        r, b = self.request("GET", f"/model.csv?id={mid}&chip=H100-SXM&rent=1.9&r=1.5&u=.6&g=.4&ulo=.5&uhi=.75")
        self.assertEqual(r.status, 200)
        self.assertIn("1.9,1.5,0.6,0.4", b)
        headers = self.post_headers()
        body = "model_name=Kimi-K2+%28sample%29&email=fake%40example.test"
        r, b = self.request("POST", "/request", body, {**headers, "Content-Length": str(len(body))})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.server.vault.count("request"), 1)
        body = "email=fake%40example.test&consent=on"
        r, b = self.request("POST", "/signup", body, {**headers, "Content-Length": str(len(body))})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.server.vault.count("signup"), 1)
        self.assertEqual(self.request("GET", "/interest")[0].status, 200)
        r, b = self.request("POST", "/interest", "", headers)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.server.interest_clicks, 1)
        r, b = self.request("POST", "/interest", None, {**headers, "Content-Length": "71680"})
        self.assertEqual(r.status, 413)
        payload = urllib.parse.quote("<script>alert(1)</script>")
        r, b = self.request("GET", f"/model?id={payload}")
        self.assertEqual(r.status, 404)
        self.assertNotIn("<script>alert", b)
        self.assertIn("&lt;script&gt;", b)
        self.assertEqual(self.request("GET", "/missing")[0].status, 404)

    def test_request_logging_does_not_disclose_client_or_path(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            r, _ = self.request("GET", "/missing-private-selection")
        self.assertEqual(r.status, 404)
        self.assertNotIn("127.0.0.1", captured.getvalue())
        self.assertNotIn("missing-private-selection", captured.getvalue())

    def test_rate_limit_is_per_session_not_proxy_address(self):
        for index in range(6):
            sid = f"{index + 1:032x}"
            r, _ = self.request("POST", "/interest", "", self.post_headers(sid))
            self.assertEqual(r.status, 200)
        sid = "f" * 32
        statuses = [self.request("POST", "/interest", "", self.post_headers(sid))[0].status for _ in range(6)]
        self.assertEqual(statuses, [200, 200, 200, 200, 200, 429])

    def test_expired_rate_buckets_are_deleted(self):
        self.server.rates[("sid", "a" * 32)] = deque([time.monotonic() - 601])
        r, _ = self.request("POST", "/interest", "", self.post_headers("b" * 32))
        self.assertEqual(r.status, 200)
        self.assertNotIn(("sid", "a" * 32), self.server.rates)

    def test_float_underflow_returns_controlled_400(self):
        mid = urllib.parse.quote("Llama-3.3-70B (sample)")
        r, body = self.request("GET", f"/model?id={mid}&r=1e-300&ulo=1e-300&uhi=1e-300")
        self.assertEqual(r.status, 400)
        self.assertIn("supported numeric range", body)

    def test_every_response_closes_and_timeout_is_short(self):
        r, _ = self.request("GET", "/healthz")
        self.assertEqual(r.getheader("Connection"), "close")
        self.assertLessEqual(self.server.RequestHandlerClass.timeout, 3)

    def test_count_only_copy_matches_storage(self):
        page = render.dashboard_page(self.server.reports)
        self.assertIn("Only aggregate submission counts are usable", page)
        self.assertIn("cannot be used to contact you", page)
        self.assertNotIn("Purpose: respond", page)
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("count-only interest signals", readme)
        self.assertIn("cannot be read back or used to contact anyone", readme)

    def test_methodology_displays_demo_data_dates(self):
        r, body = self.request("GET", "/methodology")
        self.assertEqual(r.status, 200)
        self.assertIn("2026-10-21", body)
        self.assertIn("2026-10-25", body)

    def test_model_page_has_per_chip_break_even_table(self):
        reports = build_reports(FakePrice().endpoints(), FakeRent().quotes(), FakeServing().assumptions())
        report = next(item for item in reports if item.model_id.startswith("Llama"))
        page = render.model_page(report, None)
        section = page.split("<h2>Per-chip break-even</h2>", 1)[1]
        self.assertIn("At market minimum", section)
        self.assertIn("H100-SXM", section)
        self.assertIn("L40S", section)

    def test_static_model_canonical_uses_html_path(self):
        reports = build_reports(FakePrice().endpoints(), FakeRent().quotes(), FakeServing().assumptions())
        report = next(item for item in reports if item.model_id.startswith("Llama"))
        page = render.model_page(report, None, base="https://iue.example", static=True)
        self.assertIn('href="https://iue.example/model/llama-3-3-70b-sample.html"', page)
        self.assertNotIn('rel="canonical" href="https://iue.example/model?id=', page)

    def test_host_contract(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = make_server("127.0.0.1", 0, {"PUBLIC_BASE_URL": "https://try-iue.example", "DEMO_HTTPS_TERMINATED": "true"})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        r, b = self.request("GET", "/healthz", headers={"Host": "try-iue.example"})
        self.assertEqual(r.status, 200)
        r, b = self.request("GET", "/", headers={"Host": "evil.test"})
        self.assertEqual(r.status, 421)
        r, b = self.request("GET", "/", headers={"Host": "try-iue.example"})
        self.assertEqual(r.status, 200)
        self.assertIn("Secure", r.getheader("Set-Cookie"))
        self.assertIn("https://try-iue.example/model", b)


if __name__ == "__main__":
    unittest.main()
