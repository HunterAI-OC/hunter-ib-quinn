"""
tests/test_ranking.py

Unit tests for the Quinn ranking engine.
Run: python -m pytest tests/test_ranking.py -v
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ib_quinn import QuinnEngine, OptionContract


def make_contract(
    symbol="PLTR",
    strike=25.0,
    expiry_delta=1,
    right="CALL",
    delta=0.52,
    gamma=0.10,
    theta=-0.05,
    iv=48.0,
    bid=1.85,
    ask=1.90,
    oi=5000,
    volume=3000,
) -> OptionContract:
    expiry = (date.today() + timedelta(days=expiry_delta)).strftime("%Y%m%d")
    return OptionContract(
        symbol=symbol,
        strike=strike,
        expiry=expiry,
        right=right,
        delta=delta,
        gamma=gamma,
        theta=theta,
        iv=iv,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2,
        oi=oi,
        volume=volume,
    )


class FakeContext:
    """Minimal ctx stand-in for tests that only test rank_contracts."""
    pass


class TestDTECalculation:
    """DTE must be calculated from expiry date, not hardcoded."""

    def setup_method(self):
        self.engine = QuinnEngine(
            ctx=FakeContext(),
            zmq_host="127.0.0.1",
            tick_port=5555,
            chain_port=5556,
            rep_port=5560,
            refresh_interval=60,
            log_dir=Path("/tmp"),
        )

    def test_dte_0(self):
        today = date.today().strftime("%Y%m%d")
        assert self.engine._calc_dte(today) == 0

    def test_dte_1(self):
        tomorrow = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
        assert self.engine._calc_dte(tomorrow) == 1

    def test_dte_4(self):
        four_days = (date.today() + timedelta(days=4)).strftime("%Y%m%d")
        assert self.engine._calc_dte(four_days) == 4

    def test_dte_expired(self):
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        assert self.engine._calc_dte(yesterday) is None

    def test_dte_invalid_format(self):
        assert self.engine._calc_dte("NOT_A_DATE") is None


class TestSpreadFilter:
    """Spread rules per SPEC-QUINN §3.1 table."""

    def setup_method(self):
        self.engine = QuinnEngine(
            ctx=FakeContext(),
            zmq_host="127.0.0.1",
            tick_port=5555,
            chain_port=5556,
            rep_port=5560,
            refresh_interval=60,
            log_dir=Path("/tmp"),
        )

    def test_spread_low_stock_ok(self):
        c = make_contract(bid=1.80, ask=1.85)   # spread = 0.05
        assert self.engine._spread_cost_acceptable(c, stock_price=2.50) is True

    def test_spread_low_stock_too_wide(self):
        c = make_contract(bid=1.80, ask=1.86)   # spread = 0.06 > 0.05
        assert self.engine._spread_cost_acceptable(c, stock_price=2.50) is False

    def test_spread_high_stock_ok(self):
        c = make_contract(bid=3.80, ask=3.90)   # spread = 0.10
        assert self.engine._spread_cost_acceptable(c, stock_price=50.0) is True

    def test_spread_high_stock_too_wide(self):
        c = make_contract(bid=3.80, ask=3.91)   # spread = 0.11 > 0.10
        assert self.engine._spread_cost_acceptable(c, stock_price=50.0) is False


class TestRankingAlgorithm:
    """Ranking per SPEC-QUINN §3.3."""

    def setup_method(self):
        self.engine = QuinnEngine(
            ctx=FakeContext(),
            zmq_host="127.0.0.1",
            tick_port=5555,
            chain_port=5556,
            rep_port=5560,
            refresh_interval=60,
            log_dir=Path("/tmp"),
        )
        self.stock_price = 23.47

    def test_delta_closest_to_050_wins(self):
        """Score rewards delta closest to 0.50."""
        contracts = [
            make_contract(strike=25.0, delta=0.52),   # closer to 0.50
            make_contract(strike=22.5, delta=0.72),   # farther
        ]
        rankings = self.engine.rank_contracts(contracts, "long", self.stock_price)
        assert rankings.contracts[0].strike == 25.0
        assert rankings.contracts[0].rank == 1

    def test_calls_only_for_long(self):
        contracts = [
            make_contract(right="CALL", delta=0.50),
            make_contract(right="PUT",  delta=0.50),
        ]
        rankings = self.engine.rank_contracts(contracts, "long", self.stock_price)
        assert all(c.right == "CALL" for c in rankings.contracts)

    def test_puts_only_for_short(self):
        contracts = [
            make_contract(right="CALL", delta=0.50),
            make_contract(right="PUT",  delta=0.50),
        ]
        rankings = self.engine.rank_contracts(contracts, "short", self.stock_price)
        assert all(c.right == "PUT" for c in rankings.contracts)

    def test_dte_0_preferred_over_dte_4(self):
        """DTE_penalty: 0 for 0 DTE, 1 for 4 DTE. Lower score wins."""
        contracts = [
            make_contract(expiry_delta=0, delta=0.50),   # DTE=0
            make_contract(expiry_delta=4, delta=0.50),   # DTE=4
        ]
        rankings = self.engine.rank_contracts(contracts, "long", self.stock_price)
        assert rankings.contracts[0].expiry == date.today().strftime("%Y%m%d")

    def test_oi_filter_eliminates_low_oi(self):
        contracts = [
            make_contract(oi=5000),   # passes
            make_contract(oi=1999),   # fails OI_MIN
        ]
        rankings = self.engine.rank_contracts(contracts, "long", self.stock_price)
        assert len(rankings.contracts) == 1
        assert rankings.contracts[0].oi == 5000

    def test_volume_filter_eliminates_low_volume(self):
        contracts = [
            make_contract(volume=3000),  # passes
            make_contract(volume=499),   # fails VOLUME_MIN
        ]
        rankings = self.engine.rank_contracts(contracts, "long", self.stock_price)
        assert len(rankings.contracts) == 1
        assert rankings.contracts[0].volume == 3000

    def test_dte_5_rejected(self):
        contracts = [
            make_contract(expiry_delta=5),   # exceeds DTE_MAX
            make_contract(expiry_delta=1),
        ]
        rankings = self.engine.rank_contracts(contracts, "long", self.stock_price)
        assert all(self.engine._calc_dte(c.expiry) <= 4 for c in rankings.contracts)

    def test_rank_assigned_sequentially(self):
        contracts = [
            make_contract(strike=25.0, delta=0.50),
            make_contract(strike=24.0, delta=0.50),
            make_contract(strike=26.0, delta=0.50),
        ]
        rankings = self.engine.rank_contracts(contracts, "long", self.stock_price)
        assert [c.rank for c in rankings.contracts] == [1, 2, 3]
        # All should have scores
        assert all(c.score >= 0 for c in rankings.contracts)

    def test_empty_chain_returns_empty_rankings(self):
        rankings = self.engine.rank_contracts([], "long", self.stock_price)
        assert rankings.contracts == []


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
