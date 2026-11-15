import io
import json
import unittest
from iue.openrouter import OpenRouterPrice


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class OpenRouterTests(unittest.TestCase):
    def test_fixture(self):
        raw = open("data/fixtures/openrouter_endpoints.json", "rb").read()
        source = OpenRouterPrice(["model"], opener=lambda *a, **k: Response(raw))
        rows = source.endpoints()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].price_in, 1)
        self.assertEqual(rows[0].price_out, 2)
        self.assertIsNone(rows[0].quantization)
        self.assertEqual(source.skipped, 1)

    def test_nonfinite_and_negative_prices_are_skipped(self):
        payload = {"data": {"endpoints": [
            {"provider_name": "negative", "pricing": {"prompt": "-0.1", "completion": "0.2"}},
            {"provider_name": "nan", "pricing": {"prompt": "nan", "completion": "0.2"}},
            {"provider_name": "infinite", "pricing": {"prompt": "1e999", "completion": "0.2"}},
            {"provider_name": "valid", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        ]}}
        raw = json.dumps(payload).encode()
        source = OpenRouterPrice(["model"], opener=lambda *a, **k: Response(raw))
        rows = source.endpoints()
        self.assertEqual([row.provider for row in rows], ["valid"])
        self.assertEqual(source.skipped, 3)


if __name__ == "__main__":
    unittest.main()
