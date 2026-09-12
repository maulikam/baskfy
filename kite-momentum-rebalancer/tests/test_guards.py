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

    def test_gateway_blocks_before_any_broker_call(self):
        # AF 3.5: untouchables return BLOCKED so one SGB leg cannot abort a batch.
        res = asyncio.run(self.gw.place(symbol="SGBDE31III", qty=1, side="SELL", price=7400.0))
        self.assertEqual(res["status"], "BLOCKED")
        res = asyncio.run(self.gw.place(symbol="RRKABEL", qty=1, side="BUY",
                                        price=2275.0, series="GB"))
        self.assertEqual(res["status"], "BLOCKED")

    def test_gateway_accepts_ordinary_equity(self):
        # DRY_RUN is pinned rather than inherited from .env. Reading the ambient value
        # meant this only ever exercised the simulation short-circuit, so it passed while
        # the desk was in DRY_RUN and failed the moment it went live — false confidence
        # at exactly the point it mattered.
        from app import config as C
        prev = C.DRY_RUN
        C.DRY_RUN = True
        try:
            res = asyncio.run(self.gw.place(symbol="RRKABEL", qty=10, side="BUY",
                                            price=2275.0, series="EQ"))
        finally:
            C.DRY_RUN = prev
        self.assertEqual(res["status"], "DRY_RUN")
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
        from app import config as C
        prev = C.DRY_RUN
        C.DRY_RUN = True
        try:
            res = asyncio.run(self.gw.place(symbol="DIXON", qty=10, side="BUY",
                                            price=639.4))
        finally:
            C.DRY_RUN = prev
        self.assertEqual(res["status"], "DRY_RUN")


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

class TestLiveModeRouting(unittest.TestCase):
    """What happens when DRY_RUN is OFF.

    Every other gateway test pinned DRY_RUN on, so the simulation short-circuit was the
    only path ever exercised. That left the live path — the one that actually reaches the
    broker — untested, which is the wrong way round: the dangerous path deserves the
    coverage.
    """

    def setUp(self):
        from app.core.gateway import OrderGateway
        from app.core.risk import RiskConfig, RiskManager

        class RecordingKC:
            def __init__(self):
                self.sent = []
                self.VARIETY_REGULAR = "regular"
                self.TRANSACTION_TYPE_BUY = "BUY"
                self.TRANSACTION_TYPE_SELL = "SELL"
                self.PRODUCT_CNC = "CNC"
                self.PRODUCT_MIS = "MIS"
                self.ORDER_TYPE_LIMIT = "LIMIT"
                self.ORDER_TYPE_MARKET = "MARKET"
                self.VALIDITY_DAY = "DAY"

            def place_order(self, **kw):
                self.sent.append(kw)
                return "ORDER123"

        self.kc = RecordingKC()
        self.gw = OrderGateway(self.kc,
                               RiskManager(RiskConfig(max_position_value=1e12)))

    def _live(self, **kw):
        from app import config as C
        prev = C.DRY_RUN
        C.DRY_RUN = False
        try:
            return asyncio.run(self.gw.place(**kw))
        finally:
            C.DRY_RUN = prev

    def test_a_legitimate_cnc_order_reaches_the_broker_when_live(self):
        res = self._live(symbol="DIXON", qty=10, side="BUY", price=639.4)
        self.assertEqual(len(self.kc.sent), 1)
        self.assertNotEqual(res.get("status"), "BLOCKED")

    def test_an_untouchable_instrument_never_reaches_the_broker_when_live(self):
        """The guard must not depend on DRY_RUN for its protection."""
        from app.core.guards import UntouchableInstrumentError
        try:
            res = self._live(symbol="SGBDE31III-GB", qty=1, side="SELL", price=15306.0)
            self.assertEqual(res.get("status"), "BLOCKED")
        except UntouchableInstrumentError:
            pass                      # raising before any network call is also correct
        self.assertEqual(self.kc.sent, [], "a protected instrument was sent to the broker")

    def test_a_gated_product_never_reaches_the_broker_when_live(self):
        res = self._live(symbol="DIXON", qty=10, side="BUY", product="MIS", price=639.4)
        self.assertEqual(res["status"], "BLOCKED")
        self.assertEqual(self.kc.sent, [], "an MIS order was sent while the gate is off")

    def test_the_order_sent_live_is_cnc_and_delivery(self):
        self._live(symbol="DIXON", qty=10, side="BUY", price=639.4)
        sent = self.kc.sent[0]
        self.assertEqual(sent.get("product"), "CNC")
        self.assertEqual(sent.get("exchange"), "NSE")
        self.assertEqual(sent.get("quantity"), 10)

if __name__ == "__main__":
    unittest.main(verbosity=2)
