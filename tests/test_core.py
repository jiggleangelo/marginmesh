import json
import tempfile
import unittest
from pathlib import Path
from iue import csvout, render
from iue.models import Endpoint
from iue.report import build_reports, market_report
from iue.sources import FakePrice, FakeRent, FakeServing, CsvRent
from iue.vault import MemoryVault


class CoreTests(unittest.TestCase):
    def test_samples_and_report(self):
        endpoints=FakePrice().endpoints(); rents=FakeRent().quotes(); serving=FakeServing().assumptions()
        self.assertEqual(len({x.model_id for x in endpoints}), 8)
        reports=build_reports(endpoints,rents,serving)
        llama=next(x for x in reports if x.model_id.startswith("Llama"))
        self.assertEqual(llama.provider_count,7); self.assertEqual(llama.quant_flag,"mixed"); self.assertAlmostEqual(llama.spread_ratio,3.1,places=1)
        mistral=next(x for x in reports if x.model_id.startswith("Mistral")); self.assertEqual(mistral.free_count,1)
        kimi=next(x for x in reports if x.model_id.startswith("Kimi")); self.assertEqual(kimi.chips,[])
        qwen=next(x for x in reports if x.model_id.startswith("Qwen")); self.assertEqual(qwen.quant_flag,"undisclosed"); self.assertEqual(qwen.uptime_flag,"low")
        self.assertIn("model_id,provider_count",csvout.dashboard(reports))
    def test_stale(self):
        e=Endpoint("x","p",1,1,"fp8",1,None,None,"2026-01-01T00:00:00Z")
        self.assertTrue(market_report("x",[e],as_of="2026-01-04T00:00:01Z").stale_flag)
    def test_vault_private(self):
        v=MemoryVault(); a=v.put("signup",{"email":"secret@example.test"}); b=v.put("signup",{"email":"other@example.test"})
        self.assertNotEqual(a,b); self.assertEqual(v.count("signup"),2); self.assertNotIn("secret",repr(v))
        self.assertFalse(any(x in dir(v) for x in ("get","list","export")))
    def test_render_escape(self):
        page=render.message_page("x","<script>alert(1)</script>")
        self.assertIn(render.BANNER,page); self.assertNotIn("<script>alert",page); self.assertIn("&lt;script&gt;",page)
    def test_csv_error_line(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"x.csv"; p.write_text("chip,usd_per_gpu_hour,kind,source,observed_at\nH100,nope,on-demand,,now\n")
            with self.assertRaisesRegex(ValueError,"line 2"): CsvRent(p).quotes()


if __name__ == "__main__": unittest.main()
