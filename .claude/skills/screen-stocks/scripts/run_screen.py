#!/usr/bin/env python3
"""Entry point for the screen-stocks skill.

Supports two modes:
  --mode query  (default): Uses yfinance EquityQuery -- no symbol list needed.
  --mode legacy          : Uses the original ValueScreener
                           with predefined symbol lists per market.
"""

import argparse
import csv
import datetime
import re
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from scripts.common import try_import, HAS_HISTORY_STORE, HAS_GRAPH_QUERY as _HAS_GQ
from src.data import yahoo_client
from src.core.screening.screener import ValueScreener, QueryScreener, PullbackScreener, AlphaScreener, TrendingScreener, GrowthScreener, BreakoutScreener
from src.output.formatter import format_markdown, format_query_markdown, format_pullback_markdown, format_alpha_markdown, format_trending_markdown, format_growth_markdown, format_breakout_markdown
from src.markets.japan import JapanMarket
from src.markets.us import USMarket
from src.markets.asean import ASEANMarket

# Module availability from common.py (KIK-448); import specific functions when available
HAS_HISTORY = HAS_HISTORY_STORE
if HAS_HISTORY:
    from src.data.history_store import save_screening

HAS_SR_FORMAT, _sf = try_import("src.output.formatter", "format_shareholder_return_markdown")
if HAS_SR_FORMAT: format_shareholder_return_markdown = _sf["format_shareholder_return_markdown"]

HAS_GRAPH_QUERY = _HAS_GQ
if HAS_GRAPH_QUERY:
    from src.data.graph_query import get_screening_frequency

HAS_ANNOTATOR, _an = try_import("src.data.screen_annotator", "annotate_results")
if HAS_ANNOTATOR: annotate_results = _an["annotate_results"]

HAS_SCREENING_CTX, _sctx = try_import(
    "src.data.screening_context", "get_screening_graph_context"
)
if HAS_SCREENING_CTX:
    get_screening_graph_context = _sctx["get_screening_graph_context"]

HAS_SCREENING_SUMMARY, _ssum = try_import(
    "src.output.screening_summary_formatter", "format_screening_summary"
)
if HAS_SCREENING_SUMMARY:
    format_screening_summary = _ssum["format_screening_summary"]


# Legacy market classes
MARKETS = {
    "japan": JapanMarket,
    "us": USMarket,
    "asean": ASEANMarket,
}

# Mapping from user-facing region names to yfinance region codes.
# Single-region entries map to one code; multi-region entries expand to a list.
REGION_EXPAND = {
    "japan": ["jp"],
    "jp": ["jp"],
    "us": ["us"],
    "asean": ["sg", "th", "my", "id", "ph"],
    "sg": ["sg"],
    "singapore": ["sg"],
    "th": ["th"],
    "thailand": ["th"],
    "my": ["my"],
    "malaysia": ["my"],
    "id": ["id"],
    "indonesia": ["id"],
    "ph": ["ph"],
    "philippines": ["ph"],
    "hk": ["hk"],
    "hongkong": ["hk"],
    "kr": ["kr"],
    "korea": ["kr"],
    "tw": ["tw"],
    "taiwan": ["tw"],
    "cn": ["cn"],
    "china": ["cn"],
    "all": ["jp", "us", "sg", "th", "my", "id", "ph"],
}

REGION_NAMES = {
    "jp": "日本株",
    "us": "米国株",
    "sg": "シンガポール株",
    "th": "タイ株",
    "my": "マレーシア株",
    "id": "インドネシア株",
    "ph": "フィリピン株",
    "hk": "香港株",
    "kr": "韓国株",
    "tw": "台湾株",
    "cn": "中国株",
}

VALID_SECTORS = [
    "Technology",
    "Financial Services",
    "Healthcare",
    "Consumer Cyclical",
    "Industrials",
    "Communication Services",
    "Consumer Defensive",
    "Energy",
    "Basic Materials",
    "Real Estate",
    "Utilities",
]


def _resolve_ja_name(symbol: str) -> str:
    """Yahoo Finance Japan から日本語社名を取得する（JP株のみ）。失敗時は空文字を返す。"""
    if not symbol.endswith(".T"):
        return ""
    code = symbol[:-2]  # ".T" を除く
    try:
        import requests
        url = f"https://finance.yahoo.co.jp/quote/{code}"
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ja,en;q=0.9"}
        r = requests.get(url, headers=headers, timeout=10)
        m = re.search(r"<title[^>]*>(.+?)【", r.text)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


def _save_breakout_csv(results: list, region_code: str) -> str:
    """Breakout スクリーニング結果を CSV に保存する（銘柄名は日本語）。

    Parameters
    ----------
    results : list[dict]
        BreakoutScreener.screen() の返り値。
    region_code : str
        yfinance リージョンコード（例: 'jp'）。

    Returns
    -------
    str
        保存先ファイルパス。
    """
    _DAY_LABELS = {0: "今日", 1: "昨日", 2: "一昨日"}
    date_str = datetime.date.today().strftime("%Y%m%d")

    # プロジェクトルート / data / screening_results
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    out_dir = os.path.join(project_root, "data", "screening_results")
    os.makedirs(out_dir, exist_ok=True)
    filepath = os.path.join(out_dir, f"breakout_{region_code}_{date_str}.csv")

    fields = [
        "順位", "シンボル", "銘柄名", "株価", "52週高値",
        "ブレイクタイミング", "PER", "ROE", "売上YoY", "営業利益YoY", "スコア",
    ]

    print("  日本語銘柄名を取得中...", end="", flush=True)
    rows = []
    for rank, r in enumerate(results, 1):
        sym = r.get("symbol", "")
        ja_name = _resolve_ja_name(sym) or r.get("name", "")
        time.sleep(0.3)

        offset = r.get("breakout_day_offset")
        day_str = _DAY_LABELS.get(offset, "-") if offset is not None else "-"

        roe = r.get("roe")
        rev = r.get("revenue_yoy")
        op = r.get("operating_income_yoy")

        rows.append({
            "順位": rank,
            "シンボル": sym,
            "銘柄名": ja_name,
            "株価": r.get("price", ""),
            "52週高値": r.get("high_52w", ""),
            "ブレイクタイミング": day_str,
            "PER": r.get("per", ""),
            "ROE": f"{roe * 100:.1f}%" if roe is not None else "-",
            "売上YoY": f"{rev * 100:.1f}%" if rev is not None else "N/A",
            "営業利益YoY": f"{op * 100:.1f}%" if op is not None else "N/A",
            "スコア": r.get("value_score", ""),
        })
    print(" 完了")

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    return filepath


def _annotate(results):
    """Apply screen annotations (KIK-418/419). Returns (results, excluded_count)."""
    if not HAS_ANNOTATOR or not results:
        return results, 0
    try:
        return annotate_results(results)
    except Exception:
        return results, 0


def _print_recurring_picks(results):
    """Print recurring picks highlight if graph data available (KIK-406)."""
    if not HAS_GRAPH_QUERY or not results:
        return
    try:
        symbols = [r.get("symbol") for r in results if r.get("symbol")]
        if not symbols:
            return
        freq = get_screening_frequency(symbols)
        # Only show symbols that appeared 2+ times (current run counts as new)
        recurring = {s: c for s, c in freq.items() if c >= 2}
        if recurring:
            print("**再出現銘柄** (過去のスクリーニングにも登場):")
            for sym, cnt in sorted(recurring.items(), key=lambda x: -x[1]):
                print(f"  - {sym}: 過去{cnt}回出現")
            print()
    except Exception:
        pass


def _build_graphrag_prompt(context: dict, symbols: list) -> str:
    """Build LLM prompt from graph context for screening summary (KIK-452)."""
    lines = [
        "以下のナレッジグラフコンテキストをもとに、スクリーニング結果の"
        "投資判断に役立つ簡潔なサマリーを1〜3文で日本語で生成してください。\n"
    ]
    for sector, data in context.get("sector_research", {}).items():
        pos = "、".join(data.get("catalysts_pos", [])[:2])
        neg = "、".join(data.get("catalysts_neg", [])[:2])
        lines.append(
            f"セクター {sector}: ポジ材料={pos or 'なし'}, ネガ材料={neg or 'なし'}"
        )
    for sym, notes in context.get("symbol_notes", {}).items():
        for n in notes[:1]:
            lines.append(
                f"{sym}: {n.get('type', '')} - {n.get('content', '')[:60]}"
            )
    lines.append("\nサマリー（1〜3文）:")
    return "\n".join(lines)


def _print_graphrag_context(results):
    """Print GraphRAG context from knowledge graph (KIK-452)."""
    if not HAS_SCREENING_CTX or not HAS_SCREENING_SUMMARY or not results:
        return
    try:
        symbols = [r.get("symbol") for r in results if r.get("symbol")]
        sectors = list({r.get("sector") for r in results if r.get("sector")})
        context = get_screening_graph_context(symbols, sectors)
        if not context.get("has_data"):
            return
        llm_text = ""
        try:
            from src.data import grok_client as gc
            if gc.is_available():
                prompt = _build_graphrag_prompt(context, symbols)
                llm_text = gc.synthesize_text(prompt)
        except Exception:
            pass
        summary = format_screening_summary(context, llm_text)
        if summary:
            print(summary)
    except Exception:
        pass


def run_trending_mode(args):
    """Run trending stock screening using Grok X search."""
    try:
        from src.data import grok_client as gc
        if not gc.is_available():
            print("Error: trending preset requires XAI_API_KEY environment variable.")
            print("Set: export XAI_API_KEY=your-api-key")
            sys.exit(1)
    except ImportError:
        print("Error: grok_client module not available.")
        sys.exit(1)

    region_key = args.region.lower()
    first_region = REGION_EXPAND.get(region_key, [region_key])[0]
    region_name = REGION_NAMES.get(first_region, region_key.upper())
    theme_label = f" [{args.theme}]" if args.theme else ""

    print(f"\n## {region_name} - Xトレンド銘柄{theme_label} スクリーニング結果\n")
    print("Step 1: X (Twitter) でトレンド銘柄を検索中...")

    screener = TrendingScreener(yahoo_client, gc)
    results, market_context = screener.screen(
        region=region_key, theme=args.theme, top_n=args.top,
    )

    results, excluded = _annotate(results)
    print(f"Step 2: {len(results)}銘柄のファンダメンタルズを取得・スコアリング完了\n")
    if excluded:
        print(f"※ 直近売却済み {excluded}銘柄を除外\n")
    print(format_trending_markdown(results, market_context))

    _print_recurring_picks(results)
    _print_graphrag_context(results)

    if HAS_HISTORY and results:
        try:
            save_screening(preset="trending", region=region_key, results=results)
        except Exception as e:
            print(f"Warning: 履歴保存失敗: {e}", file=sys.stderr)
    print()


def run_query_mode(args):
    """Run screening using EquityQuery (default mode)."""
    region_key = args.region.lower()
    regions = REGION_EXPAND.get(region_key)
    if regions is None:
        # Treat as raw 2-letter region code
        regions = [region_key]

    # trending preset uses TrendingScreener (Grok-based)
    if args.preset == "trending":
        run_trending_mode(args)
        return

    # pullback preset uses PullbackScreener
    if args.preset == "pullback":
        screener = PullbackScreener(yahoo_client)
        for region_code in regions:
            region_name = REGION_NAMES.get(region_code, region_code.upper())
            print(f"\n## {region_name} - 押し目買い スクリーニング結果\n")
            print("Step 1: ファンダメンタルズ条件で絞り込み中...")
            results = screener.screen(region=region_code, top_n=args.top)
            results, excluded = _annotate(results)
            print(f"Step 2-3 完了: {len(results)}銘柄が条件に合致\n")
            if excluded:
                print(f"※ 直近売却済み {excluded}銘柄を除外\n")
            print(format_pullback_markdown(results))
            _print_recurring_picks(results)
            _print_graphrag_context(results)
            if HAS_HISTORY and results:
                try:
                    save_screening(preset="pullback", region=region_code, results=results)
                except Exception as e:
                    print(f"Warning: 履歴保存失敗: {e}", file=sys.stderr)
            print()
        return

    # breakout preset uses BreakoutScreener
    if args.preset == "breakout":
        screener = BreakoutScreener(yahoo_client)
        for region_code in regions:
            region_name = REGION_NAMES.get(region_code, region_code.upper())
            print(f"\n## {region_name} - 新高値ブレイク スクリーニング結果\n")
            print("Phase 1: ファンダメンタルズ条件で絞り込み中 (EquityQuery)...")
            print("Phase 2: 52週高値ブレイク判定中（価格履歴取得）...")
            print("Phase 3: 四半期成長フィルタ中...")
            results = screener.screen(region=region_code, top_n=args.top)
            results, excluded = _annotate(results)
            print(f"完了: {len(results)}銘柄が条件に合致\n")
            if excluded:
                print(f"※ 直近売却済み {excluded}銘柄を除外\n")
            print(format_breakout_markdown(results))
            _print_recurring_picks(results)
            _print_graphrag_context(results)
            if HAS_HISTORY and results:
                try:
                    save_screening(preset="breakout", region=region_code, results=results)
                except Exception as e:
                    print(f"Warning: 履歴保存失敗: {e}", file=sys.stderr)
            if results:
                try:
                    csv_path = _save_breakout_csv(results, region_code)
                    print(f"💾 CSV保存: {csv_path}")
                except Exception as e:
                    print(f"Warning: CSV保存失敗: {e}", file=sys.stderr)
            print()
        return

    # growth preset uses GrowthScreener
    if args.preset == "growth":
        screener = GrowthScreener(yahoo_client)
        for region_code in regions:
            region_name = REGION_NAMES.get(region_code, region_code.upper())
            sector_label = f" [{args.sector}]" if args.sector else ""
            print(f"\n## {region_name} - 純成長株{sector_label} スクリーニング結果\n")
            print("Step 1: 成長条件で絞り込み中 (EquityQuery)...")
            results = screener.screen(region=region_code, top_n=args.top, sector=args.sector)
            results, excluded = _annotate(results)
            print(f"Step 2: {len(results)}銘柄のEPS成長率を取得・ソート完了\n")
            if excluded:
                print(f"※ 直近売却済み {excluded}銘柄を除外\n")
            print(format_growth_markdown(results))
            _print_recurring_picks(results)
            _print_graphrag_context(results)
            if HAS_HISTORY and results:
                try:
                    save_screening(preset="growth", region=region_code, results=results, sector=args.sector)
                except Exception as e:
                    print(f"Warning: 履歴保存失敗: {e}", file=sys.stderr)
            print()
        return

    # alpha preset uses AlphaScreener
    if args.preset == "alpha":
        screener = AlphaScreener(yahoo_client)
        for region_code in regions:
            region_name = REGION_NAMES.get(region_code, region_code.upper())
            print(f"\n## {region_name} - アルファシグナル スクリーニング結果\n")
            print("Step 1: 割安足切り (EquityQuery)...")
            results = screener.screen(region=region_code, top_n=args.top)
            results, excluded = _annotate(results)
            print(f"Step 2-4 完了: {len(results)}銘柄がアルファ条件に合致\n")
            if excluded:
                print(f"※ 直近売却済み {excluded}銘柄を除外\n")
            print(format_alpha_markdown(results))
            _print_recurring_picks(results)
            _print_graphrag_context(results)
            if HAS_HISTORY and results:
                try:
                    save_screening(preset="alpha", region=region_code, results=results)
                except Exception as e:
                    print(f"Warning: 履歴保存失敗: {e}", file=sys.stderr)
            print()
        return

    screener = QueryScreener(yahoo_client)

    for region_code in regions:
        region_name = REGION_NAMES.get(region_code, region_code.upper())
        sector_label = f" [{args.sector}]" if args.sector else ""

        if args.with_pullback:
            results = screener.screen(
                region=region_code,
                preset=args.preset,
                sector=args.sector,
                top_n=args.top,
                with_pullback=True,
            )
            results, excluded = _annotate(results)
            pullback_label = " + 押し目フィルタ"
            print(f"\n## {region_name} - {args.preset}{sector_label}{pullback_label} スクリーニング結果 (EquityQuery)\n")
            if excluded:
                print(f"※ 直近売却済み {excluded}銘柄を除外\n")
            print(format_pullback_markdown(results))
            _print_recurring_picks(results)
            _print_graphrag_context(results)
            if HAS_HISTORY and results:
                try:
                    save_screening(preset=args.preset, region=region_code, results=results, sector=args.sector)
                except Exception as e:
                    print(f"Warning: 履歴保存失敗: {e}", file=sys.stderr)
        else:
            results = screener.screen(
                region=region_code,
                preset=args.preset,
                sector=args.sector,
                top_n=args.top,
            )
            results, excluded = _annotate(results)
            print(f"\n## {region_name} - {args.preset}{sector_label} スクリーニング結果 (EquityQuery)\n")
            if excluded:
                print(f"※ 直近売却済み {excluded}銘柄を除外\n")
            if args.preset == "shareholder-return" and HAS_SR_FORMAT:
                print(format_shareholder_return_markdown(results))
            else:
                print(format_query_markdown(results))
            _print_recurring_picks(results)
            _print_graphrag_context(results)
            if HAS_HISTORY and results:
                try:
                    save_screening(preset=args.preset, region=region_code, results=results, sector=args.sector)
                except Exception as e:
                    print(f"Warning: 履歴保存失敗: {e}", file=sys.stderr)
        print()


def run_legacy_mode(args):
    """Run screening using the original ValueScreener."""
    print(
        "⚠️  [DEPRECATED] --mode legacy は非推奨です。"
        " QueryScreener (デフォルト) を使用してください。"
        " --mode legacy は将来削除予定です。"
    )
    # Map region to legacy market names
    region_to_market = {
        "japan": "japan",
        "jp": "japan",
        "us": "us",
        "asean": "asean",
        "all": "all",
    }
    market_key = region_to_market.get(args.region.lower())
    if market_key is None:
        print(f"Error: Legacy mode only supports japan/us/asean/all. Got: {args.region}")
        print("Use --mode query for other regions.")
        sys.exit(1)

    if market_key == "all":
        markets_to_run = list(MARKETS.items())
    else:
        if market_key not in MARKETS:
            print(f"Error: Unknown market '{market_key}'")
            sys.exit(1)
        markets_to_run = [(market_key, MARKETS[market_key])]

    client = yahoo_client

    for market_name, market_cls in markets_to_run:
        market = market_cls()

        screener = ValueScreener(client, market)
        results = screener.screen(preset=args.preset, top_n=args.top)
        results, excluded = _annotate(results)
        print(f"\n## {market.name} - {args.preset} スクリーニング結果\n")
        if excluded:
            print(f"※ 直近売却済み {excluded}銘柄を除外\n")
        print(format_markdown(results))
        if HAS_HISTORY and results:
            try:
                save_screening(preset=args.preset, region=market_name, results=results)
            except Exception as e:
                print(f"Warning: 履歴保存失敗: {e}", file=sys.stderr)
        print()


def main():
    parser = argparse.ArgumentParser(description="割安株スクリーニング")

    # --region is the primary argument; --market is kept for backward compatibility
    parser.add_argument(
        "--region",
        default=None,
        help="Region/market to screen (e.g. japan, us, asean, sg, hk, kr, tw, cn)",
    )
    parser.add_argument(
        "--market",
        default=None,
        help="(Legacy) Alias for --region. Kept for backward compatibility.",
    )
    parser.add_argument(
        "--preset",
        default="value",
        choices=["value", "high-dividend", "growth", "growth-value", "deep-value", "quality", "pullback", "alpha", "trending", "long-term", "shareholder-return", "breakout"],
    )
    parser.add_argument(
        "--sector",
        default=None,
        help=f"Sector filter. Options: {', '.join(VALID_SECTORS)}",
    )
    parser.add_argument(
        "--theme",
        default=None,
        help="Theme filter for trending preset (e.g., AI, semiconductor, EV)",
    )
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument(
        "--with-pullback",
        action="store_true",
        default=False,
        help="任意プリセットにテクニカル押し目フィルタを追加適用",
    )
    parser.add_argument(
        "--mode",
        default="query",
        choices=["query", "legacy"],
        help="Screening mode: 'query' (EquityQuery, default) or 'legacy' (symbol list based)",
    )

    args = parser.parse_args()

    # Resolve --region from --market if --region not given
    if args.region is None:
        args.region = args.market if args.market else "japan"

    # Normalize region
    args.region = args.region.lower()

    # Validate sector
    if args.sector is not None:
        # Allow case-insensitive matching
        matched = None
        for s in VALID_SECTORS:
            if s.lower() == args.sector.lower():
                matched = s
                break
        if matched is None:
            print(f"Warning: Unknown sector '{args.sector}'. Valid sectors:")
            for s in VALID_SECTORS:
                print(f"  - {s}")
            sys.exit(1)
        args.sector = matched

    # pullback preset always uses query mode (needs EquityQuery + technical analysis)
    if args.preset == "pullback" and args.mode == "legacy":
        print("Note: pullback preset requires query mode. Switching to --mode query.")
        args.mode = "query"

    if args.preset == "alpha" and args.mode == "legacy":
        print("Note: alpha preset requires query mode. Switching to --mode query.")
        args.mode = "query"

    if args.preset == "growth" and args.mode == "legacy":
        print("Note: growth preset requires query mode. Switching to --mode query.")
        args.mode = "query"

    if args.preset == "trending" and args.mode == "legacy":
        print("Note: trending preset requires query mode. Switching to --mode query.")
        args.mode = "query"

    if args.preset == "breakout" and args.mode == "legacy":
        print("Note: breakout preset requires query mode. Switching to --mode query.")
        args.mode = "query"

    if args.mode == "query":
        run_query_mode(args)
    else:
        run_legacy_mode(args)


if __name__ == "__main__":
    main()
