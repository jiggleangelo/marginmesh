import math
import unittest
from iue import econ


class EconTests(unittest.TestCase):
    def test_golden(self):
        self.assertAlmostEqual(econ.rate_million(8000, 8), 3.6, places=6)
        self.assertAlmostEqual(econ.break_even(2.5, 3.6, 1.2), .5787037037, places=6)
        self.assertAlmostEqual(econ.margin(2.5, 3.6, 1.2, .6), .092, places=6)
        self.assertAlmostEqual(econ.entry_price(2.5, 3.6, .6, .3), 1.6534391534, places=6)
    def test_edges(self):
        self.assertEqual(econ.percentage_or_not_viable(1.1), "not viable on this chip at this price")
        self.assertLessEqual(*econ.entry_band(2, 3, .4, .7, .3))
        self.assertEqual(econ.verdict(1, 2, 3), "room to undercut")
        self.assertEqual(econ.verdict(2.5, 2, 3), "competitive at median")
        self.assertEqual(econ.verdict(4, 2, 3), "priced out")
        self.assertEqual(econ.blended_price(2, 6, 0), 6)
        self.assertEqual(econ.blended_price(2, 6, 3), 3)
    def test_validation(self):
        bad = [float("nan"), float("inf"), 0, -1]
        for value in bad:
            with self.assertRaises(econ.InputError): econ.entry_price(value, 2, .5, .3)
        for u in (0, 1.1):
            with self.assertRaises(econ.InputError): econ.entry_price(1, 2, u, .3)
        for g in (-.1, .951):
            with self.assertRaises(econ.InputError): econ.entry_price(1, 2, .5, g)
        for k in (-1, 21):
            with self.assertRaises(econ.InputError): econ.blended_price(1, 2, k)


if __name__ == "__main__": unittest.main()
