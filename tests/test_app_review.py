import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlparse

from app import make_server
from iue import csvout
from iue.userinput import InputFileError, parse_openrouter_json, parse_prices_csv
from iue.workspace import Conflict, WorkspaceStore


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server('127.0.0.1', 0, {'DATA_DIR': self.temp.name})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def request(self, method, path, fields=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        body = urlencode(fields) if fields is not None else None
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': self.origin} if fields is not None else {}
        connection.request(method, path, body, headers)
        response = connection.getresponse(); data = response.read(); result = response.status, dict(response.headers), data
        connection.close(); return result

    def create(self, sample='0'):
        status, headers, _ = self.request('POST', '/w/new', {'sample': sample})
        self.assertEqual(status, 303); return urlparse(headers['Location']).path

    def import_model_and_chip(self):
        path = self.create()
        prices = 'model,provider,price_in,price_out\ngolden,P,1,2\n'
        chips = 'chip,usd_per_gpu_hour,m_tokens_per_gpu_hour,model,source\nH100,2,3,golden,private note\n'
        self.assertEqual(self.request('POST', path + '/import/prices', {'format': 'csv', 'text': prices})[0], 303)
        self.assertEqual(self.request('POST', path + '/import/chips', {'text': chips})[0], 303)
        return path

    def test_model_delete_removes_model_specific_confidential_chips(self):
        path = self.import_model_and_chip()
        self.assertEqual(self.request('POST', path + '/m/golden/delete', {})[0], 303)
        exported = json.loads(self.request('GET', path + '/export')[2])
        self.assertEqual(exported['chips'], [])
        self.assertNotIn('private note', json.dumps(exported))

    def test_sample_is_unwritten_and_expires_after_one_day(self):
        path = self.create('1'); token = path.rsplit('/', 1)[1]
        meta_path = self.server.store.path(token) / 'meta.json'; meta = json.loads(meta_path.read_text())
        self.assertFalse(meta['written'])
        self.server.store.sweep(now=meta['created'] + 86401)
        self.assertEqual(self.request('GET', path)[0], 404)

    def test_bad_openrouter_json_is_rejected_without_bricking_workspace(self):
        path = self.create()
        bad = {'data': {'endpoints': [{'provider_name': 'P', 'pricing': {'prompt': '0.1', 'completion': '0.2'}, 'observed_at': {'bad': True}}]}}
        self.assertEqual(self.request('POST', path + '/import/prices', {'format': 'openrouter_json', 'model': 'm', 'text': json.dumps(bad)})[0], 400)
        self.assertEqual(self.request('GET', path)[0], 200)
        self.assertEqual(self.request('POST', path + '/import/prices', {'format': 'openrouter_json', 'model': 'm', 'text': '[]'})[0], 400)

    def test_bom_csv_header_is_accepted(self):
        rows, skipped, _ = parse_prices_csv('\ufeffmodel,provider,price_in,price_out\nm,p,1,2\n')
        self.assertEqual((len(rows), skipped), (1, 0))

    def test_excel_mime_upload_is_accepted(self):
        path = self.create(); boundary = 'BOUNDARY'
        payload = ('--BOUNDARY\r\nContent-Disposition: form-data; name="format"\r\n\r\ncsv\r\n'
                   '--BOUNDARY\r\nContent-Disposition: form-data; name="file"; filename="prices.csv"\r\n'
                   'Content-Type: application/vnd.ms-excel\r\n\r\nmodel,provider,price_in,price_out\r\nm,p,1,2\r\n--BOUNDARY--\r\n').encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        connection.request('POST', path + '/import/prices', payload, {'Origin': self.origin, 'Content-Type': 'multipart/form-data; boundary=' + boundary, 'Content-Length': str(len(payload))})
        response = connection.getresponse(); response.read(); self.assertEqual(response.status, 303); connection.close()

    def test_stale_save_is_rejected_instead_of_losing_update(self):
        store = WorkspaceStore(Path(self.temp.name) / 'optimistic'); token = store.create()
        first = store.read(token); second = store.read(token); first['chips'] = [{'chip': 'A'}]; store.save(token, first)
        second['chips'] = [{'chip': 'B'}]
        with self.assertRaises(Conflict): store.save(token, second)
        self.assertEqual(store.read(token)['chips'], [{'chip': 'A'}])

    def test_serving_rows_are_visible_and_individually_deletable(self):
        path = self.import_model_and_chip(); page = self.request('GET', path)[2].decode()
        self.assertIn('private note', page); self.assertIn('/chips/0/delete', page)
        self.assertEqual(self.request('POST', path + '/chips/0/delete', {})[0], 303)
        self.assertEqual(json.loads(self.request('GET', path + '/export')[2])['chips'], [])

    def test_expiry_policy_and_confirmation_pages_are_visible(self):
        path = self.import_model_and_chip(); page = self.request('GET', path)[2].decode()
        self.assertIn('expire after 90 days', page); self.assertIn('expire after 24 hours', page)
        self.assertIn('/delete/confirm', page)
        self.assertIn('Confirm permanent deletion', self.request('GET', path + '/delete/confirm')[2].decode())
        self.assertIn('Confirm model deletion', self.request('GET', path + '/m/golden/delete/confirm')[2].decode())

    def test_arbitrary_message_query_is_not_reflected(self):
        self.assertNotIn('workspace compromised', self.request('GET', '/?msg=workspace+compromised')[2].decode())
        path = self.create(); self.assertNotIn('workspace compromised', self.request('GET', path + '?msg=workspace+compromised')[2].decode())

    def test_json_unit_note_and_restored_columns(self):
        path = self.import_model_and_chip(); dashboard = self.request('GET', path)[2].decode()
        self.assertIn('unit selector applies to CSV only', dashboard)
        model = self.request('GET', path + '/m/golden')[2].decode()
        for label in ('Blended', 'Context', 'Throughput', 'Entry low', 'Entry high'):
            self.assertIn(label, model)

    def test_formula_guard_handles_leading_tab_and_carriage_return(self):
        rendered = csvout._write(['value'], [['\t=SUM(A1:A2)'], ['\r+1']])
        self.assertIn("'\t=SUM", rendered); self.assertIn("'\r+1", rendered)


if __name__ == '__main__':
    unittest.main()
