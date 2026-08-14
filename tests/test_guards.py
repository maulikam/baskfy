"""Guard tests — the SGB / G-sec hard block must fire below every other layer.

Run:  python -m unittest tests.test_guards -v
"""
from __future__ import annotations

import asyncio
import unittest

from app.core.guards import (
    UntouchableInstrumentError,
    assert_tradeable,
    filter_tradeable,
)


class TestUntouchableInstruments(unittest.TestCase):
    def test_named_sgb_is_blocked(self):
        """The exact symbol from config.EXCLUDED_SYMBOLS / UNTOUCHABLE_SYMBOLS."""
        with self.assertRaises(UntouchableInstrumentError):
            assert_tradeable("SGBDE31III")

    def test_any_sgb_prefix_is_blocked(self):
        """Every Sovereign Gold Bond tranche, not just the one we hold."""
        for sym in ("SGBAUG28", "SGBJUN31IV", "SGBMAR30X", "SGB", "sgbnov29ii"):
            with self.subTest(symbol=sym):
                with self.assertRaises(UntouchableInstrumentError):
                    assert_tradeable(sym)

    def test_real_account_sgb_symbol_is_blocked(self):
        """REGRESSION: the live holding is 'SGBDE31III-GB' (with the -GB suffix),
        NOT the bare 'SGBDE31III' listed in UNTOUCHABLE_SYMBOLS/EXCLUDED_SYMBOLS.
        Both exact-match sets miss it — only the SGB prefix rule catches it.
        If the prefix rule is ever narrowed, a Rs 60L position becomes tradeable."""
        with self.assertRaises(UntouchableInstrumentError):
            assert_tradeable("SGBDE31III-GB")
        self.assertEqual(filter_tradeable(["SGBDE31III-GB", "RRKABEL"]), ["RRKABEL"])

    def test_gb_series_is_blocked(self):
        """Series GB (G-sec) is blocked regardless of how ordinary the symbol looks."""
        with self.assertRaises(UntouchableInstrumentError):
            assert_tradeable("RRKABEL", series="GB")
        with self.assertRaises(UntouchableInstrumentError):
            assert_tradeable("ANYTHING", series="gb")

    def test_gs_series_is_blocked(self):
        with self.assertRaises(UntouchableInstrumentError):
            assert_tradeable("SOMEGSEC", series="GS")

    def test_whitespace_and_case_do_not_bypass(self):
        for sym in ("  sgbde31iii  ", "SgBdE31III", " SGBAUG28"):
            with self.subTest(symbol=sym):
                with self.assertRaises(UntouchableInstrumentError):
                    assert_tradeable(sym)

    def test_rrkabel_passes(self):
        """A normal EQ equity must sail through — with and without series."""
        assert_tradeable("RRKABEL")
        assert_tradeable("RRKABEL", series="EQ")
        assert_tradeable("rrkabel", series="EQ")

    def test_other_ordinary_symbols_pass(self):
        for sym in ("DIXON", "POLYCAB", "HAL", "SBIN"):
            with self.subTest(symbol=sym):
                assert_tradeable(sym, series="EQ")

    def test_filter_tradeable_drops_only_untouchables(self):
        got = filter_tradeable(["RRKABEL", "SGBDE31III", "DIXON", "SGBAUG28", "POLYCAB"])
        self.assertEqual(got, ["RRKABEL", "DIXON", "POLYCAB"])


class TestGatewayEnforcesGuard(unittest.TestCase):
    """The guard must fire inside the gateway before any network call is made."""

    def setUp(self):
        from app.core.gateway import OrderGateway
        from app.core.risk import RiskManager

        class ExplodingKC:
            def place_order(self, **kw):
                raise AssertionError("network call attempted for a blocked instrument")

        self.gw = OrderGateway(ExplodingKC(), RiskManager())

    def test_gateway_raises_before_any_broker_call(self):
        with self.assertRaises(UntouchableInstrumentError):
            asyncio.run(self.gw.place(symbol="SGBDE31III", qty=1, side="SELL", price=7400.0))
        with self.assertRaises(UntouchableInstrumentError):
            asyncio.run(self.gw.place(symbol="RRKABEL", qty=1, side="BUY",
                                      price=2275.0, series="GB"))

    def test_gateway_accepts_ordinary_equity(self):
        res = asyncio.run(self.gw.place(symbol="RRKABEL", qty=10, side="BUY",
                                        price=2275.0, series="EQ"))
        # DRY_RUN=true in .env → simulated, never routed to the broker.
        self.assertIn(res["status"], ("DRY_RUN", "PLACED"))
        self.assertEqual(res["symbol"], "RRKABEL")


class TestProductGates(unittest.TestCase):
    """CNC-only. MIS and F&O must be refused by a real switch, not by an AttributeError."""

    def setUp(self):
        from app.core.gateway import OrderGateway
        from app.core.risk import RiskConfig, RiskManager

        class ExplodingKC:
            def place_order(self, **kw):
                raise AssertionError("a gated product must never reach the broker")

        self.gw = OrderGateway(ExplodingKC(),
                               RiskManager(RiskConfig(max_position_value=1e12)))

    def test_config_defines_both_gates_and_both_default_off(self):
        from app import config as C
        self.assertIs(C.INTRADAY_ENABLED, False)
        self.assertIs(C.OPTIONS_ENABLED, False)

    def test_mis_is_blocked_cleanly(self):
        res = asyncio.run(self.gw.place(symbol="DIXON", qty=10, side="BUY",
                                        product="MIS", price=639.4))
        self.assertEqual(res["status"], "BLOCKED")
        self.assertIn("INTRADAY_ENABLED", res["error"])

    def test_fno_is_blocked_cleanly(self):
        res = asyncio.run(self.gw.place(symbol="NIFTY26AUGCE", qty=50, side="BUY",
                                        product="NRML", price=120.0, exchange="NFO"))
        self.assertEqual(res["status"], "BLOCKED")
        self.assertIn("OPTIONS_ENABLED", res["error"])

    def test_cnc_still_passes(self):
        res = asyncio.run(self.gw.place(symbol="DIXON", qty=10, side="BUY", price=639.4))
        self.assertIn(res["status"], ("DRY_RUN", "PLACED"))


class TestScoringExcludesSGB(unittest.TestCase):
    """Second, independent layer: the scan filter must reject it too."""

    def test_excluded_symbol_never_scored(self):
        import pandas as pd
        from app.scoring import load_scan, score

        d = score(load_scan("data/uploads/sample_scan.csv"))
        row = d.loc[d.symbol == "SGBDE31III"]
        self.assertFalse(row.empty, "sample scan must contain SGBDE31III")
        self.assertIn("excluded_instrument", row.iloc[0]["reject"])
        self.assertTrue(pd.isna(row.iloc[0]["SCORE"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
