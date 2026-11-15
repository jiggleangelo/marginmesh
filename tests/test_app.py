import http.client
import json
import tempfile
import threading
import unittest
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urlparse

from app import NOTICE, make_server


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server('127.0.0.1', 0, {'DATA_DIR': self.temp.name})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, fields=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        body = None
        supplied = dict(headers or {})
        if fields is not None:
            body = urlencode(fields)
            supplied.setdefault('Content-Type', 'application/x-www-form-urlencoded')
            supplied.setdefault('Origin', self.origin)
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        content = response.read()
        result = response.status, dict(response.headers), content
        connection.close()
        return result

    def create(self, sample='0', referer=False):
        headers = {'Referer': self.origin + '/'} if referer else None
        status, response_headers, body = self.request('POST', '/w/new', {'sample': sample}, headers)
        self.assertEqual(status, 303)
        return urlparse(response_headers['Location']).path

    def test_security_create_and_notice(self):
        self.assertEqual(self.request('GET', '/healthz')[2], b'ok')
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.test'})[0], 421)
        self.assertEqual(self.request('POST', '/w/new', {'sample': '0'}, {'Origin': 'https://evil.test'})[0], 403)
        path = self.create(referer=True)
        status, headers, body = self.request('GET', path)
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('This link is the only key', page)
        self.assertIn(NOTICE, unescape(page))

    def test_import_calculate_export_persist_delete(self):
        path = self.create()
        token = path.rsplit('/', 1)[1]
        prices = ('model,provider,price_in,price_out,quant,uptime_pct\n'
                  'golden,Provider A,1.2,1.2,fp8,99.9\n'
                  'golden,<script>,2.4,2.4,fp8,99\n'
                  'free,Provider B,0,0,,98\n'
                  'bad,Provider C,nope,1,,99\n')
        status, headers, body = self.request('POST', path + '/import/prices',
                                              {'format': 'csv', 'unit': 'per_m', 'mode': 'append', 'text': prices})
        self.assertEqual(status, 303)
        chips = ('chip,usd_per_gpu_hour,m_tokens_per_gpu_hour,model,confidence,source\n'
                 'H100,2.5,3.6,golden,measured,my benchmark\n')
        self.assertEqual(self.request('POST', path + '/import/chips', {'mode': 'append', 'text': chips})[0], 303)
        page = self.request('GET', path)[2].decode()
        self.assertIn('golden', page)
        model_path = path + '/m/golden?chip=H100&rent=2.5&r=3.6&u=.6&g=.3&k=3&ulo=.4&uhi=.7'
        status, headers, body = self.request('GET', model_path)
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('58%', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertEqual(self.request('GET', path + '/m/golden?u=1.1')[0], 400)
        fields = {'name': '=saved', 'chip': 'H100', 'rent': '2.5', 'r': '3.6', 'u': '.6',
                  'g': '.3', 'k': '3', 'ulo': '.4', 'uhi': '.7'}
        self.assertEqual(self.request('POST', path + '/m/golden/scenario', fields)[0], 303)
        exported = json.loads(self.request('GET', path + '/export')[2])
        self.assertTrue(exported['endpoints'])
        self.assertTrue(exported['chips'])
        self.assertTrue(exported['scenarios'])
        for file in Path(self.temp.name).rglob('*'):
            if file.is_file():
                self.assertNotIn(token, file.read_text(encoding='utf-8'))
        self.assertEqual(self.request('POST', path + '/delete', {})[0], 303)
        self.assertEqual(self.request('GET', path)[0], 404)
        self.assertFalse(self.server.store.path(token).exists())

    def test_sample_and_expiry(self):
        path = self.create('1')
        self.assertIn('Made-up sample', self.request('GET', path)[2].decode())
        exported = json.loads(self.request('GET', path + '/export')[2])
        self.assertEqual(exported['_note'], 'Made-up sample')
        token = path.rsplit('/', 1)[1]
        meta = self.server.store.path(token) / 'meta.json'
        data = json.loads(meta.read_text())
        data['last_used'] = 0
        meta.write_text(json.dumps(data))
        self.server.store.sweep()
        self.assertEqual(self.request('GET', path)[0], 404)

    def test_body_cap(self):
        path = self.create()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        connection.request('POST', path + '/import/prices', body=None,
                           headers={'Origin': self.origin, 'Content-Type': 'application/x-www-form-urlencoded',
                                    'Content-Length': str(1100000)})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        connection.close()


if __name__ == '__main__':
    unittest.main()
