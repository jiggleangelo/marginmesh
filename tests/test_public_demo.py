import http.client
import threading
import unittest
import urllib.parse

from demo import make_server
from iue.render import BANNER


class PublicDemoPolicyTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server('127.0.0.1', 0, {})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        return response.status, body

    def get(self, path):
        status, body = self.request('GET', path)
        self.assertEqual(status, 200)
        return body

    def test_demo_pages_have_banner_and_no_personal_data_inputs(self):
        model_id = urllib.parse.quote('Llama-3.3-70B (sample)')
        pages = [self.get('/'), self.get('/methodology'), self.get(f'/model?id={model_id}'), self.get('/interest')]
        for page in pages:
            lowered = page.lower()
            self.assertIn(BANNER, page)
            self.assertNotRegex(lowered, r'<input[^>]+type=["\']email["\']')
            self.assertNotRegex(lowered, r'<input[^>]+name=["\'](?:email|phone|name)["\']')
            self.assertNotIn('stripe', lowered)
            self.assertNotIn('/pilot', lowered)
            self.assertNotIn('checkout', lowered)
            self.assertNotIn('$99/mo', lowered)

    def test_removed_pilot_route_is_not_available(self):
        status, body = self.request('GET', '/pilot')
        self.assertEqual(status, 404)
        self.assertNotIn('checkout', body.lower())


if __name__ == '__main__':
    unittest.main()
