import contextlib
import http.client
import io
import os
import re
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, unquote, urlencode

from build import build
from demo import make_server
from iue import render
from iue.report import build_reports
from iue.sources import FakePrice, FakeRent, FakeServing


class LinkTargets(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.targets = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ('href', 'action', 'formaction', 'src') and value:
                self.targets.append(value)


class PaymentAbsenceTests(unittest.TestCase):
    def assert_no_payment_feature(self, content, label):
        lowered = content.lower()
        for forbidden in ('stripe', 'checkout', '$99/mo', 'paid pilot', 'paid plan', 'paid-plan'):
            self.assertNotIn(forbidden, lowered, label)
        links = LinkTargets()
        links.feed(content)
        for target in links.targets:
            decoded = unquote(target).lower()
            self.assertNotRegex(
                decoded,
                r'(?:^|[/?.&#=_-])(?:pilot|checkout|payment|payments|billing|subscribe|subscription|pricing|plans?)(?:$|[/?.&#=_-])',
                f'{label}: {target}',
            )

    def test_pages_and_static_build_have_no_checkout_feature(self):
        reports = build_reports(FakePrice().endpoints(), FakeRent().quotes(), FakeServing().assumptions())
        pages = [('README.md', Path('README.md').read_text(encoding='utf-8'))]
        for static in (False, True):
            for demo in (False, True):
                pages.append((
                    f'dashboard static={static} demo={demo}',
                    render.dashboard_page(reports, static=static, demo=demo),
                ))
                pages.append((
                    f'methodology static={static} demo={demo}',
                    render.methodology_page(static=static, demo=demo),
                ))
                for report in reports:
                    pages.append((
                        f'model {report.model_id} static={static} demo={demo}',
                        render.model_page(report, None, static=static, demo=demo),
                    ))
        pages.append((
            'static external forms',
            render.dashboard_page(
                reports, static=True, demo=False,
                form_action='https://forms.example/submit',
            ),
        ))

        server = make_server('127.0.0.1', 0, {})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, fields=None, expected=200):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            body = urlencode(fields or {}) if method == 'POST' else None
            headers = {}
            if method == 'POST':
                headers = {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': f'http://127.0.0.1:{server.server_port}',
                }
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                content = response.read().decode('utf-8')
                self.assertEqual(response.status, expected, f'{method} {path}')
                self.assertIsNone(response.getheader('Location'), f'{method} {path}')
                pages.append((f'{method} {path}', content))
                return content
            finally:
                connection.close()

        try:
            for path in ('/', '/methodology', '/interest', '/healthz', '/export.csv'):
                request('GET', path)
            for report in reports:
                model_id = quote(report.model_id, safe='')
                request('GET', f'/model?id={model_id}')
                request('GET', f'/model.csv?id={model_id}')
            model_id = quote('Llama-3.3-70B (sample)', safe='')
            request('GET', f'/model?id={model_id}&k=21', expected=400)
            request('GET', '/missing', expected=404)
            for path in ('/signup', '/request', '/delete'):
                request('GET', path, expected=405)
            request('GET', '/pilot', expected=404)
            request('POST', '/pilot', expected=404)
            self.assertEqual(server.interest_clicks, 0)
            request('POST', '/interest')
            self.assertEqual(server.interest_clicks, 1)
            receipt_page = request('POST', '/signup')
            receipt = re.search(r'<code>([0-9a-f]{24})</code>', receipt_page)
            self.assertIsNotNone(receipt)
            request('POST', '/delete', {'receipt': receipt.group(1)})
            self.assertEqual(server.vault.count('signup'), 0)
            request('POST', '/request', {'sample_model': reports[0].model_id})
            self.assertEqual(server.vault.count('request'), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        for form_action in ('', 'https://forms.example/submit'):
            with tempfile.TemporaryDirectory() as directory:
                environment = {
                    'IUE_PUBLIC_BASE_URL': 'https://iue.example',
                    'IUE_FORM_ACTION': form_action,
                }
                with patch.dict(os.environ, environment, clear=True):
                    with contextlib.redirect_stdout(io.StringIO()):
                        build('fake', directory)
                for path in Path(directory).rglob('*'):
                    if path.is_file():
                        pages.append((
                            f'build forms={bool(form_action)} {path.relative_to(directory)}',
                            path.read_text(encoding='utf-8'),
                        ))

        for label, content in pages:
            with self.subTest(page=label):
                self.assert_no_payment_feature(content, label)


if __name__ == '__main__':
    unittest.main()
