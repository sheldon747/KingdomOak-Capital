#!/usr/bin/env python3
"""
Fetches account data from an IBKR Flex Query and writes the full
performance dashboard - return, drawdown, risk-adjusted ratios, trade
statistics, consistency metrics, and a daily P&L calendar - into
data.json for the website to read.

Requires two secrets, passed as environment variables:
  FLEX_TOKEN     - the token shown when you activate Flex Web Service
                   (Client Portal > Settings > Account Settings > Flex Web Service)
  FLEX_QUERY_ID  - the numeric ID of the Flex Query you create
                   (Reports > Flex Queries)

Optional environment variables:
  PERFORMANCE_START_DATE  - "YYYYMMDD". The exact date trading began,
                             used as the inception baseline instead of
                             a heuristic. See compute_nav_stats.
  DAILY_LOSS_LIMIT_PCT    - the daily drawdown percentage that counts
                             as a circuit-breaker breach. Defaults to
                             -2.0, taken from KingdomOak's SOP circuit
                             breaker threshold. Only the numeric
                             threshold is used here - the SOP's
                             surrounding enforcement narrative and any
                             violation history stay internal and are
                             never pulled onto the public site.

The Flex Query must include:
  1. "Equity Summary by Report Date in Base" (Report Date, Total) -
     the daily NAV series every return/risk/drawdown/consistency
     metric is built from.
  2. "Trades" (Execution) with at least: Conid, Symbol, Date/Time,
     Trade Date, Quantity, Buy/Sell, Open/Close Indicator, Realized
     P/L, Multiplier, Trade Price. UnderlyingSymbol is included
     automatically by IBKR's standard Trades export and is what
     instrument-breakdown stats group on (it's the clean root ticker,
     e.g. "ES", rather than the dated contract code like "ESH6").

Currency-conversion rows (assetCategory="CASH", e.g. USD.CAD) are
excluded from every trade-level statistic - they're not trades.

Position-level P&L is computed by tracking running quantity per
instrument (conid) chronologically; a "trade" is one full cycle from
flat to open to flat again, so scaling out of a position across
several fills counts once, not multiple times.

Nothing here fabricates a number it can't support. Metrics that need
data this script doesn't have (Alpha/Beta vs a benchmark, true
tick-level MFE/MAE) are simply not computed - see the dashboard's own
"needs data" tiles for what's still pending and why.
"""

import os
import sys
import time
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError

SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
GET_STATEMENT_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"

MAX_POLL_ATTEMPTS = 10
POLL_DELAY_SECONDS = 15
EPSILON = 1e-6

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ============================================================
# Flex Web Service fetching (unchanged mechanics)
# ============================================================

def fetch(url: str) -> str:
    # Some public data sources (Stooq, in particular) return 403
    # Forbidden for Python's default urllib User-Agent, treating it as
    # bot traffic. A browser-like header fixes that without changing
    # anything else about the request. Harmless for IBKR's Flex Web
    # Service too, which doesn't care about the header either way.
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
    with urlopen(request, timeout=30) as resp:
        return resp.read().decode("utf-8")


def request_statement(token: str, query_id: str) -> str:
    url = f"{SEND_REQUEST_URL}?t={token}&q={query_id}&v=3"
    xml_text = fetch(url)
    root = ET.fromstring(xml_text)
    status = root.findtext("Status")
    if status != "Success":
        error_code = root.findtext("ErrorCode", "unknown")
        error_msg = root.findtext("ErrorMessage", "no message")
        raise RuntimeError(f"Flex request failed ({error_code}): {error_msg}")
    reference_code = root.findtext("ReferenceCode")
    if not reference_code:
        raise RuntimeError("Flex request succeeded but returned no ReferenceCode")
    return reference_code


def poll_for_statement(token: str, reference_code: str) -> str:
    url = f"{GET_STATEMENT_URL}?q={reference_code}&t={token}&v=3"
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        xml_text = fetch(url)
        if "<FlexStatementResponse" in xml_text and "<ErrorCode>" in xml_text:
            root = ET.fromstring(xml_text)
            error_code = root.findtext("ErrorCode")
            if error_code == "1019":
                time.sleep(POLL_DELAY_SECONDS)
                continue
            error_msg = root.findtext("ErrorMessage", "no message")
            raise RuntimeError(f"Flex statement error ({error_code}): {error_msg}")
        return xml_text
    raise TimeoutError("Flex statement never became ready within the polling window")


# ============================================================
# NAV series: return, drawdown, and every risk-adjusted ratio
# ============================================================

def compute_nav_stats(root: ET.Element) -> dict:
    entries = root.findall(".//EquitySummaryByReportDateInBase")
    if not entries:
        raise ValueError("No EquitySummaryByReportDateInBase rows found.")

    rows = []
    for e in entries:
        report_date = e.get("reportDate")
        total = e.get("total")
        if report_date is None or total is None:
            continue
        rows.append((report_date, float(total)))
    rows.sort(key=lambda r: r[0])

    start_date_override = os.environ.get("PERFORMANCE_START_DATE")
    if start_date_override:
        rows = [r for r in rows if r[0] >= start_date_override]
        if not rows:
            raise ValueError(f"No NAV rows on or after PERFORMANCE_START_DATE={start_date_override}")
    else:
        peak_nav = max(nav for _, nav in rows)
        threshold = peak_nav * 0.5
        first_funded_idx = next((i for i, (_, nav) in enumerate(rows) if nav >= threshold), None)
        if first_funded_idx is None:
            raise ValueError("No NAV values found reaching the funding threshold")
        rows = rows[first_funded_idx:]

    if len(rows) < 2:
        raise ValueError("Not enough NAV data points to compute return/drawdown")

    navs = [nav for _, nav in rows]
    start_nav, end_nav = navs[0], navs[-1]
    return_pct = (end_nav - start_nav) / start_nav * 100.0

    peak = navs[0]
    max_drawdown_pct = 0.0
    for nav in navs:
        if nav > peak:
            peak = nav
        drawdown = (nav - peak) / peak * 100.0
        if drawdown < max_drawdown_pct:
            max_drawdown_pct = drawdown

    return {
        "period_start": rows[0][0],
        "period_end": rows[-1][0],
        "return_pct": round(return_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "nav_series": [[d, round(nav, 2)] for d, nav in rows],
        "_rows": rows,  # internal use only, stripped before writing data.json
    }


def compute_risk_adjusted_stats(rows: list) -> dict:
    """
    Every ratio here is derived purely from day-to-day NAV changes -
    no benchmark, no assumptions beyond the NAV series itself.
    Annualization uses a 252-trading-day convention throughout.
    """
    navs = [nav for _, nav in rows]
    daily_returns = [(navs[i] - navs[i - 1]) / navs[i - 1] for i in range(1, len(navs))]
    n = len(daily_returns)
    if n < 2:
        return {}

    mean_r = sum(daily_returns) / n
    var_r = sum((r - mean_r) ** 2 for r in daily_returns) / n
    std_r = math.sqrt(var_r)
    downside = [r for r in daily_returns if r < 0]
    downside_std = math.sqrt(sum(r ** 2 for r in downside) / n) if downside else 0.0
    ann_factor = math.sqrt(252)

    sharpe = (mean_r / std_r) * ann_factor if std_r > 0 else None
    sortino = (mean_r / downside_std) * ann_factor if downside_std > 0 else None

    gains_sum = sum(r for r in daily_returns if r > 0)
    losses_sum = abs(sum(r for r in daily_returns if r < 0))
    omega = gains_sum / losses_sum if losses_sum > 0 else None

    sorted_r = sorted(daily_returns)
    var_95 = sorted_r[int(0.05 * n)]
    tail_slice = sorted_r[: int(0.05 * n) + 1]
    cvar_95 = sum(tail_slice) / len(tail_slice) if tail_slice else None
    p95 = sorted_r[int(0.95 * n)]
    p5 = sorted_r[int(0.05 * n)]
    tail_ratio = (p95 / abs(p5)) if p5 != 0 else None

    skew = (sum((r - mean_r) ** 3 for r in daily_returns) / n) / (std_r ** 3) if std_r > 0 else None
    kurt = (sum((r - mean_r) ** 4 for r in daily_returns) / n) / (std_r ** 4) - 3 if std_r > 0 else None

    peak = navs[0]
    sq_dd_sum = 0.0
    for v in navs:
        if v > peak:
            peak = v
        dd_pct = (v - peak) / peak * 100
        sq_dd_sum += dd_pct ** 2
    ulcer_index = math.sqrt(sq_dd_sum / len(navs))

    total_return_pct = (navs[-1] - navs[0]) / navs[0] * 100.0
    ulcer_perf_index = (total_return_pct / ulcer_index) if ulcer_index > 0 else None

    max_dd_pct = min(
        ((v - m) / m * 100 for v, m in zip(navs, _running_peak(navs))), default=0.0
    )
    calmar = (total_return_pct / abs(max_dd_pct)) if max_dd_pct != 0 else None
    romad = calmar  # same construction, kept as a separate key for dashboard clarity

    net_profit_dollars = navs[-1] - navs[0]
    max_dd_dollars = abs(max_dd_pct) / 100 * peak
    recovery_factor = abs(net_profit_dollars / max_dd_dollars) if max_dd_dollars != 0 else None

    start_date = datetime.strptime(rows[0][0], "%Y%m%d")
    end_date = datetime.strptime(rows[-1][0], "%Y%m%d")
    years_elapsed = (end_date - start_date).days / 365.25
    total_return_ratio = navs[-1] / navs[0]
    cagr_pct = (total_return_ratio ** (1 / years_elapsed) - 1) * 100 if years_elapsed > 0 else None

    return {
        "sharpe": _r(sharpe), "sortino": _r(sortino), "calmar": _r(calmar), "romad": _r(romad),
        "omega": _r(omega), "var_95_pct": _r(var_95 * 100 if var_95 is not None else None),
        "cvar_95_pct": _r(cvar_95 * 100 if cvar_95 is not None else None),
        "tail_ratio": _r(tail_ratio), "skew": _r(skew), "kurtosis": _r(kurt),
        "ulcer_index": _r(ulcer_index), "ulcer_perf_index": _r(ulcer_perf_index),
        "recovery_factor": _r(recovery_factor), "cagr_pct": _r(cagr_pct),
        "years_elapsed": _r(years_elapsed, 3),
    }


def _running_peak(navs):
    peak = navs[0]
    out = []
    for v in navs:
        if v > peak:
            peak = v
        out.append(peak)
    return out


def _r(x, decimals=2):
    return round(x, decimals) if x is not None else None


# ============================================================
# Trade-level parsing: positions, instrument breakdown, R-multiples
# ============================================================

def parse_fills(root: ET.Element) -> list:
    """Returns one dict per fill, excluding currency-conversion (CASH) rows."""
    fills = []
    for t in root.findall(".//Trade"):
        if t.get("assetCategory") == "CASH":
            continue
        qty_raw = t.get("quantity")
        if qty_raw is None:
            continue
        try:
            qty = float(qty_raw)
        except ValueError:
            continue
        buy_sell = t.get("buySell")
        if buy_sell == "SELL" and qty > 0:
            qty = -qty
        elif buy_sell == "BUY" and qty < 0:
            qty = abs(qty)

        pnl_raw = t.get("fifoPnlRealized")
        pnl = float(pnl_raw) if pnl_raw not in (None, "") else 0.0

        multiplier_raw = t.get("multiplier")
        trade_price_raw = t.get("tradePrice")
        multiplier = float(multiplier_raw) if multiplier_raw not in (None, "") else None
        trade_price = float(trade_price_raw) if trade_price_raw not in (None, "") else None

        fills.append({
            "conid": t.get("conid"),
            "underlying_symbol": (t.get("underlyingSymbol") or t.get("symbol") or "Unknown").strip(),
            "date_time": t.get("dateTime") or t.get("tradeDate") or "",
            "qty": qty,
            "pnl": pnl,
            "multiplier": multiplier,
            "trade_price": trade_price,
        })
    return fills


def _parse_dt(s):
    if ";" in s:
        date_part, time_part = s.split(";")
    else:
        date_part, time_part = s, "000000"
    return datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")


def build_positions(fills: list) -> list:
    """
    Groups fills by instrument (conid) and walks them chronologically,
    tracking running quantity. A position closes (and is recorded) the
    moment running quantity returns to flat. Still-open positions at
    the end of the report window are intentionally excluded - they
    aren't completed trades yet.
    """
    by_conid = {}
    for f in fills:
        by_conid.setdefault(f["conid"], []).append(f)

    positions = []
    for conid, conid_fills in by_conid.items():
        conid_fills.sort(key=lambda f: f["date_time"])
        running_qty = 0.0
        accumulated_pnl = 0.0
        open_dt = None
        entry_notional = 0.0
        underlying = conid_fills[0]["underlying_symbol"]
        for f in conid_fills:
            if open_dt is None:
                open_dt = f["date_time"]
                if f["multiplier"] and f["trade_price"]:
                    entry_notional = abs(f["qty"]) * f["multiplier"] * f["trade_price"]
            running_qty += f["qty"]
            accumulated_pnl += f["pnl"]
            if abs(running_qty) < EPSILON:
                positions.append({
                    "pnl": accumulated_pnl,
                    "open_dt": open_dt,
                    "close_dt": f["date_time"],
                    "underlying_symbol": underlying,
                    "entry_notional": entry_notional,
                })
                accumulated_pnl = 0.0
                running_qty = 0.0
                open_dt = None
                entry_notional = 0.0
    return positions


def compute_trade_stats(positions: list, inception_nav: float) -> dict:
    nonzero = [p for p in positions if abs(p["pnl"]) > EPSILON]
    if not nonzero:
        return {}

    wins = [p for p in nonzero if p["pnl"] > 0]
    losses = [p for p in nonzero if p["pnl"] < 0]
    total_closed = len(nonzero)
    win_rate = len(wins) / total_closed

    result = {
        "total_trades": total_closed,
        "win_rate_pct": round(win_rate * 100.0, 1),
    }

    gross_profit = sum(p["pnl"] for p in wins)
    gross_loss = abs(sum(p["pnl"] for p in losses))
    if gross_loss > 0:
        result["profit_factor"] = round(gross_profit / gross_loss, 2)

    avg_win = gross_profit / len(wins) if wins else None
    avg_loss = -gross_loss / len(losses) if losses else None
    result["avg_win"] = _r(avg_win)
    result["avg_loss"] = _r(avg_loss)
    result["largest_win"] = _r(max((p["pnl"] for p in wins), default=None))
    result["largest_loss"] = _r(min((p["pnl"] for p in losses), default=None))

    expectancy = win_rate * (avg_win or 0) + (1 - win_rate) * (avg_loss or 0)
    result["expectancy"] = _r(expectancy)

    payoff_ratio = (avg_win / abs(avg_loss)) if (avg_win and avg_loss) else None
    result["payoff_ratio"] = _r(payoff_ratio)

    kelly = win_rate - (1 - win_rate) / payoff_ratio if payoff_ratio else None
    result["kelly_pct"] = _r(kelly * 100 if kelly is not None else None)

    def holding_hours(p):
        try:
            return (_parse_dt(p["close_dt"]) - _parse_dt(p["open_dt"])).total_seconds() / 3600.0
        except (ValueError, KeyError):
            return None

    win_holds = [h for h in (holding_hours(p) for p in wins) if h is not None]
    loss_holds = [h for h in (holding_hours(p) for p in losses) if h is not None]
    result["avg_hold_win_hours"] = _r(sum(win_holds) / len(win_holds)) if win_holds else None
    result["avg_hold_loss_hours"] = _r(sum(loss_holds) / len(loss_holds)) if loss_holds else None

    # R-multiples: R is defined as 0.25% of the account's starting
    # (inception) NAV - a fixed dollar risk unit, per the stated
    # 0.25% stop-loss / 0.75% profit-target plan. This does not
    # resize R as the account grows or shrinks; refine later if a
    # dynamically-resized R is wanted instead.
    r_unit = inception_nav * 0.0025
    r_multiples = [p["pnl"] / r_unit for p in nonzero]
    result["R_unit_dollars"] = _r(r_unit)
    result["avg_r_multiple"] = _r(sum(r_multiples) / len(r_multiples), 3)
    result["total_r_value"] = _r(sum(r_multiples), 1)

    # Leverage: notional exposure at trade entry relative to inception
    # NAV, averaged across trades. This is a per-trade snapshot, not a
    # continuous daily leverage series (that would require
    # reconstructing simultaneous open positions across every
    # instrument at every point in time, which Flex execution data
    # alone doesn't give a clean way to do). Only trades with both
    # Multiplier and Trade Price present are included.
    notionals = [p["entry_notional"] for p in nonzero if p.get("entry_notional")]
    if notionals:
        avg_leverage = (sum(notionals) / len(notionals)) / inception_nav
        result["avg_leverage_at_entry"] = _r(avg_leverage, 2)

    return result


def compute_instrument_breakdown(positions: list) -> dict:
    nonzero = [p for p in positions if abs(p["pnl"]) > EPSILON]
    if not nonzero:
        return {}

    by_symbol_pnl, by_symbol_count = {}, {}
    for p in nonzero:
        sym = p["underlying_symbol"]
        by_symbol_pnl[sym] = by_symbol_pnl.get(sym, 0) + p["pnl"]
        by_symbol_count[sym] = by_symbol_count.get(sym, 0) + 1

    most_profitable = max(by_symbol_pnl.items(), key=lambda x: x[1])
    least_profitable = min(by_symbol_pnl.items(), key=lambda x: x[1])
    most_traded = max(by_symbol_count.items(), key=lambda x: x[1])

    return {
        "most_profitable_instrument": [most_profitable[0], _r(most_profitable[1])],
        "least_profitable_instrument": [least_profitable[0], _r(least_profitable[1])],
        "most_traded_instrument": [most_traded[0], most_traded[1]],
    }


# ============================================================
# Daily P&L series: calendar heatmap, consistency, best/worst periods
# ============================================================

def build_daily_series(rows: list, positions: list) -> dict:
    """
    Daily $ P&L comes from NAV-to-NAV differences (captures the whole
    account's movement - commissions, financing, everything), not
    summed realized trade P&L. Trade/win/loss counts per day are
    layered on from each position's close date, for the calendar
    tooltip only.

    Futures markets trade Sunday evening (CME Globex), but IBKR's
    daily NAV snapshot doesn't generate a separate Sunday row - that
    session's activity is folded into the next reporting date. A
    position closed on a Sunday (or any date with no matching NAV
    row) is rolled forward to the next date that does have one, so it
    isn't silently dropped from the calendar/consistency counts.
    """
    daily = {}
    nav_dates_sorted = [d for d, _ in rows]
    # rows[0] is the inception baseline itself - it has no prior day to
    # diff against for a $ P&L figure, but trades CAN close on that
    # exact date, so it still needs an entry (pnl 0.0) or those trades
    # would be silently dropped from every count that follows.
    daily[rows[0][0]] = {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
    for i in range(1, len(rows)):
        date_str, nav = rows[i]
        prev_nav = rows[i - 1][1]
        daily[date_str] = {"pnl": round(nav - prev_nav, 2), "trades": 0, "wins": 0, "losses": 0}

    nav_date_set = set(nav_dates_sorted)

    def roll_forward(date_str):
        if date_str in nav_date_set:
            return date_str
        for candidate in nav_dates_sorted:
            if candidate > date_str:
                return candidate
        return None  # closed after the last NAV row in the report - nothing to attribute to

    for p in positions:
        if abs(p["pnl"]) <= EPSILON:
            continue
        close_date = p["close_dt"].split(";")[0]
        attributed_date = roll_forward(close_date)
        if attributed_date is None or attributed_date not in daily:
            continue
        daily[attributed_date]["trades"] += 1
        if p["pnl"] > 0:
            daily[attributed_date]["wins"] += 1
        else:
            daily[attributed_date]["losses"] += 1

    return daily


def _monday_of(date_str):
    d = datetime.strptime(date_str, "%Y%m%d")
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y%m%d")


def compute_consistency_stats(daily: dict) -> dict:
    dates_sorted = sorted(daily.keys())
    pnls = [daily[d]["pnl"] for d in dates_sorted]
    if not pnls:
        return {}

    green_day_pct = sum(1 for p in pnls if p > 0) / len(pnls) * 100

    weekly = {}
    for d in dates_sorted:
        wk = _monday_of(d)
        weekly[wk] = weekly.get(wk, 0) + daily[d]["pnl"]
    week_keys = sorted(weekly.keys())
    week_vals = [weekly[k] for k in week_keys]
    green_week_pct = sum(1 for v in week_vals if v > 0) / len(week_vals) * 100 if week_vals else None

    monthly = {}
    for d in dates_sorted:
        ym = d[:6]
        monthly[ym] = monthly.get(ym, 0) + daily[d]["pnl"]
    month_keys = sorted(monthly.keys())
    month_vals = [monthly[k] for k in month_keys]
    green_month_pct = sum(1 for v in month_vals if v > 0) / len(month_vals) * 100 if month_vals else None

    def max_streak(vals, positive=True):
        best = cur = 0
        for v in vals:
            cond = v > 0 if positive else v < 0
            cur = cur + 1 if cond else 0
            best = max(best, cur)
        return best

    def current_streak(vals):
        """
        Streak counting backward from the most recent value. Returns
        (count, direction) where direction is "win" or "loss" - or
        (0, None) if the most recent value is exactly flat.
        """
        if not vals:
            return 0, None
        last = vals[-1]
        if last == 0:
            return 0, None
        positive = last > 0
        count = 0
        for v in reversed(vals):
            if (v > 0) == positive and v != 0:
                count += 1
            else:
                break
        return count, ("win" if positive else "loss")

    current_day_streak, current_day_direction = current_streak(pnls)
    current_week_streak, current_week_direction = current_streak(week_vals)
    current_month_streak, current_month_direction = current_streak(month_vals)

    daily_limit_pct = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "-2.0"))

    return {
        "green_day_pct": _r(green_day_pct, 1),
        "green_week_pct": _r(green_week_pct, 1),
        "green_month_pct": _r(green_month_pct, 1),
        "max_win_streak_days": max_streak(pnls, True),
        "max_loss_streak_days": max_streak(pnls, False),
        "max_win_streak_weeks": max_streak(week_vals, True),
        "max_loss_streak_weeks": max_streak(week_vals, False),
        "max_win_streak_months": max_streak(month_vals, True),
        "max_loss_streak_months": max_streak(month_vals, False),
        "current_day_streak": current_day_streak,
        "current_day_streak_direction": current_day_direction,
        "current_week_streak": current_week_streak,
        "current_week_streak_direction": current_week_direction,
        "current_month_streak": current_month_streak,
        "current_month_streak_direction": current_month_direction,
        "_daily_limit_pct": daily_limit_pct,
    }


def compute_daily_loss_breach(rows: list, threshold_pct: float) -> dict:
    """
    Checks close-to-close daily % change against the SOP's circuit
    breaker threshold. This is a day-end approximation, not intraday
    tracking - Flex data only reports end-of-day NAV, so a breach that
    happened and was recovered from within the same day won't show up
    here the way a real-time monitor would catch it.
    """
    navs = [nav for _, nav in rows]
    dates = [d for d, _ in rows]
    pct_changes = [(navs[i] - navs[i - 1]) / navs[i - 1] * 100 for i in range(1, len(navs))]
    breaches = [p for p in pct_changes if p <= threshold_pct]
    return {
        "daily_breach_pct": _r(len(breaches) / len(pct_changes) * 100, 1) if pct_changes else None,
        "daily_breach_count": len(breaches),
        "total_trading_days": len(pct_changes),
        "daily_loss_limit_pct": threshold_pct,
    }


def compute_time_based_stats(daily: dict) -> dict:
    dates_sorted = sorted(daily.keys())
    pnls = [daily[d]["pnl"] for d in dates_sorted]
    if not pnls:
        return {}

    total_pnl = sum(pnls)
    best_day_idx = max(range(len(pnls)), key=lambda i: pnls[i])
    worst_day_idx = min(range(len(pnls)), key=lambda i: pnls[i])

    weekly = {}
    for d in dates_sorted:
        wk = _monday_of(d)
        weekly[wk] = weekly.get(wk, 0) + daily[d]["pnl"]
    week_keys = sorted(weekly.keys())
    week_vals = [weekly[k] for k in week_keys]
    best_week_idx = max(range(len(week_vals)), key=lambda i: week_vals[i])
    worst_week_idx = min(range(len(week_vals)), key=lambda i: week_vals[i])

    monthly = {}
    for d in dates_sorted:
        ym = d[:6]
        monthly[ym] = monthly.get(ym, 0) + daily[d]["pnl"]
    month_keys = sorted(monthly.keys())
    month_vals = [monthly[k] for k in month_keys]
    best_month_idx = max(range(len(month_vals)), key=lambda i: month_vals[i])
    worst_month_idx = min(range(len(month_vals)), key=lambda i: month_vals[i])

    return {
        "total_pnl": _r(total_pnl),
        "avg_daily_pnl": _r(total_pnl / len(pnls)),
        "avg_weekly_pnl": _r(total_pnl / len(week_vals)) if week_vals else None,
        "avg_monthly_pnl": _r(total_pnl / len(month_vals)) if month_vals else None,
        "best_day": [dates_sorted[best_day_idx], _r(pnls[best_day_idx])],
        "worst_day": [dates_sorted[worst_day_idx], _r(pnls[worst_day_idx])],
        "best_week": [week_keys[best_week_idx], _r(week_vals[best_week_idx])],
        "worst_week": [week_keys[worst_week_idx], _r(week_vals[worst_week_idx])],
        "best_month": [month_keys[best_month_idx], _r(month_vals[best_month_idx])],
        "worst_month": [month_keys[worst_month_idx], _r(month_vals[worst_month_idx])],
    }


# ============================================================
# Monthly returns (% table) - unchanged from the prior version
# ============================================================

def compute_monthly_returns(rows: list) -> dict:
    if len(rows) < 2:
        return {}
    baseline_nav = rows[0][1]
    last_nav_by_year_month = {}
    for date_str, nav in rows[1:]:
        year, month = date_str[:4], int(date_str[4:6])
        last_nav_by_year_month[(year, month)] = nav

    results = {}
    prev_nav = baseline_nav
    for (year, month), nav in sorted(last_nav_by_year_month.items()):
        pct = (nav - prev_nav) / prev_nav * 100.0
        results.setdefault(year, {})[MONTH_ABBR[month - 1]] = round(pct, 2)
        prev_nav = nav

    for year, months in results.items():
        compound = 1.0
        for abbr, pct in months.items():
            compound *= (1 + pct / 100.0)
        months["YTD"] = round((compound - 1) * 100.0, 2)
    return results


# ============================================================
# Alpha / Beta vs a public benchmark (not from IBKR - see note)
# ============================================================
#
# Alpha and Beta need a benchmark's own daily returns to compare
# against, not anything from your brokerage account. Pulling that
# from IBKR itself would require its separate Client Portal Web API
# with OAuth 1.0a - a materially heavier setup than the Flex Web
# Service token this script otherwise relies on, and not practical to
# run unattended in GitHub Actions without registering an OAuth app.
#
# Since the S&P 500's daily closing price is public information, this
# pulls it from Stooq (stooq.com), a free historical-data source that
# needs no API key or authentication. If Stooq's endpoint format ever
# changes, this is the one place to fix it - the rest of the script
# doesn't depend on it, and a failure here does not fail the whole run
# (Alpha/Beta are simply omitted from data.json, the same as any other
# metric this script can't currently support).

BENCHMARK_URL = "https://stooq.com/q/d/l/?s=%5Espx&d1={start}&d2={end}&i=d"


# ============================================================
# Goal tracker: path to a target NAV by a target date
# ============================================================
#
# GOAL_AMOUNT and GOAL_DATE are configurable via environment variables
# so the target can change without editing code. Defaults reflect the
# stated goal: $1,000,000 by August 31, 2027.

def compute_goal_tracker(rows: list) -> dict:
    goal_amount = float(os.environ.get("GOAL_AMOUNT", "1000000"))
    goal_date_str = os.environ.get("GOAL_DATE", "20270831")

    latest_date_str, current_nav = rows[-1]
    latest_date = datetime.strptime(latest_date_str, "%Y%m%d")
    goal_date = datetime.strptime(goal_date_str, "%Y%m%d")
    days_remaining = (goal_date - latest_date).days

    result = {
        "goal_amount": goal_amount,
        "goal_date": goal_date_str,
        "goal_progress_pct": _r(current_nav / goal_amount * 100, 2),
        "goal_dollars_remaining": _r(goal_amount - current_nav),
        "goal_days_remaining": days_remaining,
    }

    if days_remaining <= 0 or current_nav <= 0:
        # Deadline already passed, or NAV is non-positive (compounding
        # math toward a positive goal is undefined from zero/negative) -
        # report progress-to-date only, no forward path.
        return result

    required_total_return_pct = (goal_amount / current_nav - 1) * 100
    required_daily_rate = (goal_amount / current_nav) ** (1 / days_remaining) - 1

    result.update({
        "required_total_return_pct": _r(required_total_return_pct),
        "required_daily_pct": _r(required_daily_rate * 100, 4),
        "required_weekly_pct": _r(((1 + required_daily_rate) ** 7 - 1) * 100, 3),
        "required_monthly_pct": _r(((1 + required_daily_rate) ** 30 - 1) * 100, 2),
        "required_dollars_tomorrow": _r(current_nav * required_daily_rate),
        "required_dollars_next_week": _r(current_nav * ((1 + required_daily_rate) ** 7 - 1)),
        "required_dollars_next_month": _r(current_nav * ((1 + required_daily_rate) ** 30 - 1)),
    })

    # Full compounding trajectory from today's NAV to the goal, one
    # point per calendar day, for charting alongside the actual equity
    # curve. This is the pace required if growth were perfectly smooth
    # every single day - real trading never looks like this, it's a
    # reference line, not a prediction.
    trajectory = []
    for n in range(days_remaining + 1):
        d = latest_date + timedelta(days=n)
        required_nav = current_nav * ((1 + required_daily_rate) ** n)
        trajectory.append([d.strftime("%Y%m%d"), _r(required_nav)])
    result["goal_trajectory"] = trajectory

    return result


def fetch_benchmark_closes(start_date: str, end_date: str) -> dict:
    """
    Returns {date_str ("YYYYMMDD"): close_price} for the S&P 500 (^SPX)
    between start_date and end_date (inclusive), both "YYYYMMDD".
    Raises on any failure - the caller decides whether that's fatal.
    """
    url = BENCHMARK_URL.format(start=start_date, end=end_date)
    csv_text = fetch(url)
    lines = csv_text.strip().splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        raise ValueError(f"Unexpected benchmark CSV format from Stooq: {lines[:2]!r}")

    closes = {}
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        date_str = parts[0].replace("-", "")  # "2026-01-20" -> "20260120"
        try:
            closes[date_str] = float(parts[4])  # Close column
        except ValueError:
            continue
    if not closes:
        raise ValueError("Benchmark fetch succeeded but no rows were parsed")
    return closes


def compute_alpha_beta(rows: list) -> dict:
    """
    Beta = covariance(strategy returns, benchmark returns) / variance(benchmark returns)
    Alpha = (mean strategy return - Beta * mean benchmark return), annualized to a %.
    Only dates present in BOTH the account's NAV series and the
    benchmark are used, so a few missing benchmark days (holidays,
    data gaps) don't break the whole calculation - they're just
    excluded from both sides.
    """
    start_date, end_date = rows[0][0], rows[-1][0]
    benchmark_closes = fetch_benchmark_closes(start_date, end_date)

    strategy_returns, benchmark_returns = [], []
    for i in range(1, len(rows)):
        date_str, nav = rows[i]
        prev_nav = rows[i - 1][1]
        prev_date = rows[i - 1][0]
        if date_str not in benchmark_closes or prev_date not in benchmark_closes:
            continue
        strategy_returns.append((nav - prev_nav) / prev_nav)
        benchmark_returns.append(
            (benchmark_closes[date_str] - benchmark_closes[prev_date]) / benchmark_closes[prev_date]
        )

    n = len(strategy_returns)
    if n < 10:  # too few overlapping days for a meaningful regression
        raise ValueError(f"Only {n} overlapping days between account and benchmark - too few for Alpha/Beta")

    mean_s = sum(strategy_returns) / n
    mean_b = sum(benchmark_returns) / n
    covariance = sum((strategy_returns[i] - mean_s) * (benchmark_returns[i] - mean_b) for i in range(n)) / n
    variance_b = sum((b - mean_b) ** 2 for b in benchmark_returns) / n

    if variance_b == 0:
        raise ValueError("Benchmark showed zero variance over this period - cannot compute Beta")

    beta = covariance / variance_b
    alpha_daily = mean_s - beta * mean_b
    alpha_annualized_pct = alpha_daily * 252 * 100.0

    return {
        "alpha_pct": _r(alpha_annualized_pct),
        "beta": _r(beta),
        "benchmark_overlap_days": n,
    }


# ============================================================
# Top-level orchestration
# ============================================================

# ============================================================




def compute_stats(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)

    nav_stats = compute_nav_stats(root)
    rows = nav_stats.pop("_rows")
    inception_nav = rows[0][1]

    fills = parse_fills(root)
    positions = build_positions(fills)

    stats = dict(nav_stats)
    stats.update(compute_risk_adjusted_stats(rows))
    stats.update(compute_trade_stats(positions, inception_nav))
    stats.update(compute_instrument_breakdown(positions))

    daily = build_daily_series(rows, positions)
    stats.update(compute_time_based_stats(daily))

    consistency = compute_consistency_stats(daily)
    daily_limit_pct = consistency.pop("_daily_limit_pct", -2.0)
    stats.update(consistency)
    stats.update(compute_daily_loss_breach(rows, daily_limit_pct))

    # Gain-to-Pain Ratio, monthly convention: sum of monthly gains
    # divided by sum of monthly losses (not the daily version).
    monthly_returns = compute_monthly_returns(rows)
    stats["monthly_returns"] = monthly_returns
    latest_year = max(monthly_returns.keys()) if monthly_returns else None
    if latest_year:
        month_vals = [v for k, v in monthly_returns[latest_year].items() if k != "YTD"]
        gains = sum(v for v in month_vals if v > 0)
        pains = sum(abs(v) for v in month_vals if v < 0)
        stats["gain_to_pain"] = _r(gains / pains) if pains > 0 else None

    stats["daily_pnl"] = {d: v for d, v in daily.items()}
    stats["launch_date"] = stats["period_start"]
    stats.update(compute_goal_tracker(rows))

    # Kinfo leaderboard rank and US Investing Championship status are
    # not available through any API - Kinfo's leaderboard is a
    # JavaScript app with no public data endpoint, and the
    # Championship simply doesn't list accounts that aren't currently
    # profitable. Both are set manually as environment variables
    # (GitHub repo Variables, not Secrets, since none of this is
    # sensitive) and passed straight through here. Update them
    # whenever you check the real values - this script cannot fetch
    # them itself.
    kinfo_rank = os.environ.get("KINFO_RANK")
    if kinfo_rank:
        stats["kinfo_rank"] = kinfo_rank
        stats["kinfo_rank_period"] = os.environ.get("KINFO_RANK_PERIOD", "1 Month")
        stats["kinfo_rank_asof"] = os.environ.get("KINFO_RANK_ASOF", stats["period_end"])

    usic_status = os.environ.get("USIC_STATUS", "not_ranked")
    stats["usic_status"] = usic_status
    if usic_status == "ranked":
        stats["usic_rank"] = os.environ.get("USIC_RANK")
        stats["usic_return_pct"] = os.environ.get("USIC_RETURN_PCT")

    try:
        stats.update(compute_alpha_beta(rows))
    except (URLError, RuntimeError, ValueError) as exc:
        # Alpha/Beta are a nice-to-have, not core to the site - a
        # benchmark fetch hiccup shouldn't take down return, drawdown,
        # or anything else this script computes from your own account.
        print(f"Alpha/Beta not computed this run: {exc}", file=sys.stderr)

    return stats


def main():
    token = os.environ.get("FLEX_TOKEN")
    query_id = os.environ.get("FLEX_QUERY_ID")

    if not token or not query_id:
        print("FLEX_TOKEN and FLEX_QUERY_ID must be set as environment variables.", file=sys.stderr)
        sys.exit(1)

    try:
        reference_code = request_statement(token, query_id)
        xml_text = poll_for_statement(token, reference_code)
        stats = compute_stats(xml_text)
    except (URLError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f"Failed to update performance data: {exc}", file=sys.stderr)
        sys.exit(1)

    import json
    from datetime import timezone

    existing_monthly_returns = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json") as f:
                existing_monthly_returns = json.load(f).get("monthly_returns", {})
        except (json.JSONDecodeError, OSError):
            pass

    stats["monthly_returns"] = {**existing_monthly_returns, **stats["monthly_returns"]}

    output = {
        **stats,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote data.json with {len(output)} top-level fields")


if __name__ == "__main__":
    main()
