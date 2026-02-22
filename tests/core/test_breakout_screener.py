"""Tests for src.core.screening.breakout_screener module."""

import pandas as pd
import pytest

from src.core.screening.breakout_screener import BreakoutScreener, detect_new_high_breakout


# ============================================================
# Helpers
# ============================================================

def _make_hist(highs: list[float]) -> pd.DataFrame:
    """Build a minimal price history DataFrame with only a High column."""
    return pd.DataFrame({"High": highs})


def _make_hist_full(highs: list[float], closes: list[float] | None = None) -> pd.DataFrame:
    """Build a price history DataFrame with High and Close columns."""
    data = {"High": highs}
    if closes is not None:
        data["Close"] = closes
    else:
        data["Close"] = [h * 0.99 for h in highs]
    return pd.DataFrame(data)


# ============================================================
# TestDetectNewHighBreakout
# ============================================================

class TestDetectNewHighBreakout:
    def test_insufficient_data_returns_none(self):
        """Less than 25 rows → None."""
        hist = _make_hist([100.0] * 24)
        assert detect_new_high_breakout(hist) is None

    def test_exactly_25_rows_accepted(self):
        """Exactly 25 rows is acceptable (boundary)."""
        highs = [100.0] * 22 + [90.0, 90.0, 110.0]  # 25 rows; last 3 include new high
        hist = _make_hist(highs)
        result = detect_new_high_breakout(hist)
        assert result is not None

    def test_missing_high_column_returns_none(self):
        """DataFrame without High column → None."""
        hist = pd.DataFrame({"Close": [100.0] * 30})
        assert detect_new_high_breakout(hist) is None

    def test_empty_dataframe_returns_none(self):
        """Empty DataFrame → None."""
        assert detect_new_high_breakout(pd.DataFrame()) is None

    def test_none_input_returns_none(self):
        """None input → None."""
        assert detect_new_high_breakout(None) is None

    def test_valid_breakout_today(self):
        """52w high touched today (last row) after a quiet period."""
        # 24 quiet rows (max = 90), then new high on the last day = 100
        highs = [90.0] * 24 + [100.0]
        hist = _make_hist(highs)
        result = detect_new_high_breakout(hist)
        assert result is not None
        assert result["is_breakout"] is True
        assert result["breakout_day_offset"] == 0
        assert result["high_52w"] == pytest.approx(100.0)

    def test_valid_breakout_yesterday(self):
        """52w high touched yesterday (second-to-last row)."""
        highs = [90.0] * 24 + [100.0, 95.0]
        hist = _make_hist(highs)
        result = detect_new_high_breakout(hist)
        assert result is not None
        assert result["is_breakout"] is True
        assert result["breakout_day_offset"] == 1

    def test_valid_breakout_day_before_yesterday(self):
        """52w high touched two days ago (third-to-last row)."""
        highs = [90.0] * 24 + [100.0, 95.0, 92.0]
        hist = _make_hist(highs)
        result = detect_new_high_breakout(hist)
        assert result is not None
        assert result["is_breakout"] is True
        assert result["breakout_day_offset"] == 2

    def test_quiet_period_already_touched_high(self):
        """52w high was already touched within the quiet window → is_breakout=False."""
        # quiet period (21 rows before last 3): one of them hits 100
        highs = [90.0] * 2 + [100.0] + [90.0] * 19 + [100.0, 95.0, 93.0]
        hist = _make_hist(highs)
        result = detect_new_high_breakout(hist)
        assert result is not None
        assert result["is_breakout"] is False

    def test_no_recent_touch_no_breakout(self):
        """52w high not touched in last 3 days → is_breakout=False."""
        highs = [90.0] * 24 + [85.0]
        hist = _make_hist(highs)
        result = detect_new_high_breakout(hist)
        assert result is not None
        assert result["is_breakout"] is False

    def test_breakout_day_offset_none_when_not_breakout(self):
        """breakout_day_offset should be None when is_breakout=False."""
        highs = [90.0] * 24 + [85.0]
        hist = _make_hist(highs)
        result = detect_new_high_breakout(hist)
        assert result is not None
        assert result["is_breakout"] is False
        assert result["breakout_day_offset"] is None


# ============================================================
# Mock yahoo client
# ============================================================

def _make_mock_client(
    quotes: list[dict],
    hist_map: dict[str, pd.DataFrame],
    quarterly_map: dict[str, dict | None] | None = None,
):
    """Build a minimal mock yahoo_client object."""
    class MockClient:
        def screen_stocks(self, query, size=250, max_results=250, sort_field=None, sort_asc=False):
            return quotes

        def get_price_history(self, symbol):
            return hist_map.get(symbol)

        def get_quarterly_financials(self, symbol):
            if quarterly_map is None:
                return {"has_quarterly_data": False, "revenue_yoy": None, "operating_income_yoy": None}
            return quarterly_map.get(symbol, {"has_quarterly_data": False, "revenue_yoy": None, "operating_income_yoy": None})

    return MockClient()


def _make_quote(symbol, per=15.0, roe=0.15, name=None):
    """Build a minimal quote dict as returned by yfinance EquityQuery."""
    return {
        "symbol": symbol,
        "shortName": name or symbol,
        "trailingPE": per,
        "returnOnEquity": roe,
        "regularMarketPrice": 1000.0,
        "sector": "Technology",
        "dividendYield": 2.0,
        "priceToBook": 1.5,
        "revenueGrowth": 0.10,
        "earningsGrowth": 0.15,
    }


def _make_breakout_hist(symbol_hint="X"):
    """Build a 25-row hist where the last row is a new 52w high (today)."""
    highs = [90.0] * 24 + [100.0]
    return _make_hist(highs)


def _make_non_breakout_hist():
    """Build a 25-row hist with no recent high touch."""
    highs = [90.0] * 25
    return _make_hist(highs)


# ============================================================
# TestBreakoutScreenerScreen
# ============================================================

class TestBreakoutScreenerScreen:
    def test_only_breakout_stocks_pass(self):
        """Non-breakout stocks are excluded by Phase 2."""
        quotes = [_make_quote("A.T"), _make_quote("B.T")]
        hist_map = {
            "A.T": _make_breakout_hist(),
            "B.T": _make_non_breakout_hist(),
        }
        client = _make_mock_client(quotes, hist_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10, skip_quarterly_filter=True)
        assert len(results) == 1
        assert results[0]["symbol"] == "A.T"

    def test_no_quotes_returns_empty(self):
        """Empty quote list → empty result."""
        client = _make_mock_client([], {})
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10)
        assert results == []

    def test_no_hist_stock_skipped(self):
        """Stocks with no price history are skipped in Phase 2."""
        quotes = [_make_quote("A.T")]
        hist_map = {}  # no history
        client = _make_mock_client(quotes, hist_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10, skip_quarterly_filter=True)
        assert results == []

    def test_quarterly_data_absent_passes_through(self):
        """has_quarterly_data=False (JP stock) → passes Phase 3 filter."""
        quotes = [_make_quote("A.T")]
        hist_map = {"A.T": _make_breakout_hist()}
        quarterly_map = {"A.T": {"has_quarterly_data": False, "revenue_yoy": None, "operating_income_yoy": None}}
        client = _make_mock_client(quotes, hist_map, quarterly_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10)
        assert len(results) == 1
        assert results[0]["symbol"] == "A.T"
        assert results[0]["revenue_yoy"] is None

    def test_quarterly_growth_sufficient_passes(self):
        """Revenue YoY ≥ 10% and operating income YoY ≥ 20% → passes."""
        quotes = [_make_quote("A.T")]
        hist_map = {"A.T": _make_breakout_hist()}
        quarterly_map = {"A.T": {"has_quarterly_data": True, "revenue_yoy": 0.15, "operating_income_yoy": 0.25}}
        client = _make_mock_client(quotes, hist_map, quarterly_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10)
        assert len(results) == 1

    def test_revenue_growth_insufficient_excluded(self):
        """Revenue YoY < 10% → excluded by Phase 3."""
        quotes = [_make_quote("A.T")]
        hist_map = {"A.T": _make_breakout_hist()}
        quarterly_map = {"A.T": {"has_quarterly_data": True, "revenue_yoy": 0.05, "operating_income_yoy": 0.25}}
        client = _make_mock_client(quotes, hist_map, quarterly_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10)
        assert results == []

    def test_operating_income_growth_insufficient_excluded(self):
        """Operating income YoY < 20% → excluded by Phase 3."""
        quotes = [_make_quote("A.T")]
        hist_map = {"A.T": _make_breakout_hist()}
        quarterly_map = {"A.T": {"has_quarterly_data": True, "revenue_yoy": 0.15, "operating_income_yoy": 0.10}}
        client = _make_mock_client(quotes, hist_map, quarterly_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10)
        assert results == []

    def test_min_per_filter_excludes_low_per(self):
        """Stocks with PER < min_per (default 10) are excluded after Phase 1."""
        quotes = [_make_quote("A.T", per=5.0), _make_quote("B.T", per=15.0)]
        hist_map = {
            "A.T": _make_breakout_hist(),
            "B.T": _make_breakout_hist(),
        }
        client = _make_mock_client(quotes, hist_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10, skip_quarterly_filter=True)
        symbols = [r["symbol"] for r in results]
        assert "A.T" not in symbols
        assert "B.T" in symbols

    def test_skip_quarterly_filter_bypasses_phase3(self):
        """skip_quarterly_filter=True skips Phase 3 entirely."""
        quotes = [_make_quote("A.T")]
        hist_map = {"A.T": _make_breakout_hist()}
        # quarterly would fail if evaluated: very low growth
        quarterly_map = {"A.T": {"has_quarterly_data": True, "revenue_yoy": 0.01, "operating_income_yoy": 0.01}}
        client = _make_mock_client(quotes, hist_map, quarterly_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10, skip_quarterly_filter=True)
        assert len(results) == 1

    def test_top_n_limits_result_count(self):
        """top_n limits the number of returned stocks."""
        quotes = [_make_quote(f"S{i}.T") for i in range(10)]
        hist_map = {f"S{i}.T": _make_breakout_hist() for i in range(10)}
        client = _make_mock_client(quotes, hist_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=3, skip_quarterly_filter=True)
        assert len(results) <= 3

    def test_result_fields_exist(self):
        """Returned dicts contain expected fields."""
        quotes = [_make_quote("A.T")]
        hist_map = {"A.T": _make_breakout_hist()}
        client = _make_mock_client(quotes, hist_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10, skip_quarterly_filter=True)
        assert len(results) == 1
        r = results[0]
        for field in ["symbol", "name", "sector", "price", "per", "roe",
                      "high_52w", "breakout_day_offset", "revenue_yoy",
                      "operating_income_yoy", "value_score"]:
            assert field in r, f"Missing field: {field}"

    def test_network_failure_skips_stock(self):
        """get_quarterly_financials returning None (network error) → stock skipped."""
        quotes = [_make_quote("A.T")]
        hist_map = {"A.T": _make_breakout_hist()}
        quarterly_map = {"A.T": None}  # simulate network failure
        client = _make_mock_client(quotes, hist_map, quarterly_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10)
        assert results == []

    def test_results_sorted_by_value_score_descending(self):
        """Results are sorted by value_score descending."""
        # Use different PER values to get different value_scores
        quotes = [_make_quote("A.T", per=25.0), _make_quote("B.T", per=12.0)]
        hist_map = {
            "A.T": _make_breakout_hist(),
            "B.T": _make_breakout_hist(),
        }
        client = _make_mock_client(quotes, hist_map)
        screener = BreakoutScreener(client)
        results = screener.screen(region="jp", top_n=10, skip_quarterly_filter=True)
        if len(results) >= 2:
            scores = [r.get("value_score", 0) for r in results]
            assert scores == sorted(scores, reverse=True)
