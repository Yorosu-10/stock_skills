"""BreakoutScreener: 52-week high breakout + growth earnings screening."""

from typing import Optional

import pandas as pd

from src.core.screening.indicators import calculate_value_score
from src.core.screening.query_builder import build_query
from src.core.screening.query_screener import QueryScreener


def detect_new_high_breakout(hist: pd.DataFrame) -> Optional[dict]:
    """Detect whether a stock has recently broken out to a new 52-week high.

    A valid breakout requires:
    1. At least one of the last 3 trading days touched the 52-week high.
    2. None of the 21 trading days before that window touched the 52-week high
       (the "quiet period" – the stock was consolidating/coiling).

    Parameters
    ----------
    hist : pd.DataFrame
        Price history with a ``High`` column.  Must contain at least 25 rows
        (3 recent + 21 quiet + 1 for the 52w high calculation base).

    Returns
    -------
    dict or None
        None if data is insufficient or the ``High`` column is missing.
        Otherwise a dict with:
          - ``is_breakout`` (bool)
          - ``breakout_day_offset`` (int): 0=today, 1=yesterday, 2=day before (only valid when is_breakout=True)
          - ``high_52w`` (float): the 52-week high value
    """
    try:
        if hist is None or hist.empty:
            return None
        if "High" not in hist.columns:
            return None
        if len(hist) < 25:
            return None

        highs = hist["High"]
        high_52w = float(highs.max())

        # Recent window: last 3 rows (indices -3, -2, -1)
        recent_window = highs.iloc[-3:]
        breakout_occurred = any(float(v) >= high_52w for v in recent_window if v == v)

        # Quiet window: rows [-24, -3) = 21 rows before the recent 3
        quiet_window = highs.iloc[-24:-3]
        was_quiet = not any(float(v) >= high_52w for v in quiet_window if v == v)

        is_breakout = breakout_occurred and was_quiet

        # Determine which day the breakout happened (0=today, 1=yesterday, 2=day before)
        breakout_day_offset = None
        if is_breakout:
            for offset in range(3):
                idx = -(1 + offset)
                val = highs.iloc[idx]
                if val == val and float(val) >= high_52w:
                    breakout_day_offset = offset
                    break

        return {
            "is_breakout": is_breakout,
            "breakout_day_offset": breakout_day_offset,
            "high_52w": high_52w,
        }
    except Exception:
        return None


class BreakoutScreener:
    """Screen stocks for 52-week high breakout with growth earnings.

    Three-phase pipeline:
      Phase 1: EquityQuery (PER 10–30, ROE ≥ 10%) → hundreds of candidates
      Phase 2: Price history – 52-week high breakout filter → dozens
      Phase 3: Quarterly income statement – revenue/operating income YoY growth
    """

    DEFAULT_CRITERIA = {
        "min_per": 10,
        "max_per": 30,
        "min_roe": 0.10,
    }

    # Growth thresholds for Phase 3
    MIN_REVENUE_YOY = 0.10       # +10% YoY revenue growth
    MIN_OP_INCOME_YOY = 0.20     # +20% YoY operating income growth

    def __init__(self, yahoo_client):
        self.yahoo_client = yahoo_client

    def screen(
        self,
        region: str = "jp",
        top_n: int = 20,
        fundamental_criteria: Optional[dict] = None,
        skip_quarterly_filter: bool = False,
    ) -> list[dict]:
        """Run the three-phase breakout screening pipeline.

        Parameters
        ----------
        region : str
            Market region code (e.g. 'jp', 'us').
        top_n : int
            Maximum number of results to return.
        fundamental_criteria : dict, optional
            Override the default fundamental criteria.
        skip_quarterly_filter : bool
            When True, skip Phase 3 (quarterly growth filter). Useful for
            markets where quarterly data is rarely available.

        Returns
        -------
        list[dict]
            Breakout stocks sorted by value_score descending.
        """
        criteria = fundamental_criteria if fundamental_criteria is not None else dict(self.DEFAULT_CRITERIA)

        # -----------------------------------------------------------------
        # Phase 1: Fundamental filtering via EquityQuery
        # -----------------------------------------------------------------
        query = build_query(criteria, region=region)

        raw_quotes = self.yahoo_client.screen_stocks(
            query,
            size=250,
            max_results=max(top_n * 5, 250),
            sort_field="intradaymarketcap",
            sort_asc=False,
        )

        if not raw_quotes:
            return []

        fundamentals: list[dict] = []
        for quote in raw_quotes:
            normalized = QueryScreener._normalize_quote(quote)
            # Post-filter: EquityQuery cannot express min_per, so filter here
            min_per = criteria.get("min_per")
            per_val = normalized.get("per")
            if min_per is not None and per_val is not None and per_val < min_per:
                continue
            normalized["value_score"] = calculate_value_score(normalized)
            fundamentals.append(normalized)

        if not fundamentals:
            return []

        # -----------------------------------------------------------------
        # Phase 2: 52-week high breakout filter
        # -----------------------------------------------------------------
        breakout_passed: list[dict] = []
        for stock in fundamentals:
            symbol = stock.get("symbol")
            if not symbol:
                continue

            hist = self.yahoo_client.get_price_history(symbol)
            if hist is None or hist.empty:
                continue

            result = detect_new_high_breakout(hist)
            if result is None:
                continue

            if not result.get("is_breakout"):
                continue

            stock["breakout_day_offset"] = result.get("breakout_day_offset")
            stock["high_52w"] = result.get("high_52w")
            breakout_passed.append(stock)

        if not breakout_passed:
            return []

        # -----------------------------------------------------------------
        # Phase 3: Quarterly growth filter
        # -----------------------------------------------------------------
        if skip_quarterly_filter:
            growth_passed = breakout_passed
        else:
            growth_passed = []
            for stock in breakout_passed:
                symbol = stock.get("symbol")
                if not symbol:
                    continue

                qf = self.yahoo_client.get_quarterly_financials(symbol)

                if qf is None:
                    # Network failure – skip this stock
                    continue

                if not qf.get("has_quarterly_data", False):
                    # Data unavailable (common for JP stocks) – pass through
                    stock["revenue_yoy"] = None
                    stock["operating_income_yoy"] = None
                    growth_passed.append(stock)
                    continue

                rev_yoy = qf.get("revenue_yoy")
                op_yoy = qf.get("operating_income_yoy")

                # If YoY data is present, apply growth thresholds
                rev_ok = rev_yoy is None or rev_yoy >= self.MIN_REVENUE_YOY
                op_ok = op_yoy is None or op_yoy >= self.MIN_OP_INCOME_YOY

                if rev_ok and op_ok:
                    stock["revenue_yoy"] = rev_yoy
                    stock["operating_income_yoy"] = op_yoy
                    growth_passed.append(stock)

        if not growth_passed:
            return []

        # -----------------------------------------------------------------
        # Build final results
        # -----------------------------------------------------------------
        results: list[dict] = []
        for stock in growth_passed:
            results.append({
                "symbol": stock["symbol"],
                "name": stock.get("name"),
                "sector": stock.get("sector"),
                "price": stock.get("price"),
                "per": stock.get("per"),
                "roe": stock.get("roe"),
                "high_52w": stock.get("high_52w"),
                "breakout_day_offset": stock.get("breakout_day_offset"),
                "revenue_yoy": stock.get("revenue_yoy"),
                "operating_income_yoy": stock.get("operating_income_yoy"),
                "value_score": stock.get("value_score", 0.0),
                # Keep annotation fields if present
                "_note_markers": stock.get("_note_markers"),
                "_note_summary": stock.get("_note_summary"),
            })

        results.sort(key=lambda r: -(r.get("value_score") or 0.0))
        return results
