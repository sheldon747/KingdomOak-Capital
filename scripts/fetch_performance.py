#!/usr/bin/env python3
"""
Fetches account data from an IBKR Flex Query and writes return, max
drawdown, win rate, and profit factor into data.json for the website
to read.

Requires two secrets, passed as environment variables:
  FLEX_TOKEN     - the token shown when you activate Flex Web Service
                   (Client Portal > Settings > Account Settings > Flex Web Service)
  FLEX_QUERY_ID  - the numeric ID of the Flex Query you create
                   (Reports > Flex Queries)

The Flex Query itself must include TWO sections:
  1. "Equity Summary by Report Date in Base" - used for return and max
     drawdown (a daily NAV series over whatever period you configure,
     e.g. "Year to Date").
  2. "Trades" (the Trade Confirmation / Executions section) - used for
     win rate and profit factor. Fills for the same instrument are
     grouped chronologically and tracked by running position size;
     realized P&L accumulates while a position is open, and a "trade"
     is recorded when the position returns to flat. Scaling out of one
     position across several fills counts as one trade, not several.

If the Trades section is missing or empty, win rate and profit factor
are simply omitted from data.json rather than guessed at - the site's
JS hides those two stat boxes when the fields aren't present.

Never hardcode FLEX_TOKEN or FLEX_QUERY_ID in this file. In GitHub
Actions they are injected as env vars from repository secrets (see
the workflow file).
"""

import os
import sys
import time
import xml.etree.ElementTree as ET
from urllib.request import urlopen
from urllib.error import URLError

SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
GET_STATEMENT_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"

MAX_POLL_ATTEMPTS = 10
POLL_DELAY_SECONDS = 15


def fetch(url: str) -> str:
    with urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def request_statement(token: str, query_id: str) -> str:
    """Kicks off the Flex report and returns a reference code to poll for the result."""
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
    """IBKR generates the report asynchronously; poll until it's ready."""
    url = f"{GET_STATEMENT_URL}?q={reference_code}&t={token}&v=3"

    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        xml_text = fetch(url)

        # A "still generating" response is a small <FlexStatementResponse> with
        # an error code (typically 1019) rather than the actual report.
        if "<FlexStatementResponse" in xml_text and "<ErrorCode>" in xml_text:
            root = ET.fromstring(xml_text)
            error_code = root.findtext("ErrorCode")
            if error_code == "1019":  # statement generation in progress
                time.sleep(POLL_DELAY_SECONDS)
                continue
            error_msg = root.findtext("ErrorMessage", "no message")
            raise RuntimeError(f"Flex statement error ({error_code}): {error_msg}")

        return xml_text

    raise TimeoutError("Flex statement never became ready within the polling window")


def compute_nav_stats(root: ET.Element) -> dict:
    entries = root.findall(".//EquitySummaryByReportDateInBase")
    if not entries:
        raise ValueError(
            "No EquitySummaryByReportDateInBase rows found. "
            "Make sure the Flex Query includes that section."
        )

    rows = []
    for e in entries:
        report_date = e.get("reportDate")
        total = e.get("total")
        if report_date is None or total is None:
            continue
        rows.append((report_date, float(total)))

    rows.sort(key=lambda r: r[0])

    # Flex Query periods like "Year to Date" often include a leading
    # reference row (e.g. prior year-end) and any dates before the
    # account was actually funded, where total == 0 or still ramping
    # up through a multi-day deposit. Using an early, tiny NAV as the
    # starting point produces a technically-correct but meaningless
    # percentage (e.g. a $36 starting balance growing to $56,000 reads
    # as a six-figure percentage return).
    #
    # If PERFORMANCE_START_DATE (format YYYYMMDD) is set, that exact
    # date is used as day one. For this account, deposits completed
    # before 2026-01-20 and trading began that day, so set this
    # secret to "20260120" once wiring up GitHub Actions rather than
    # relying on the heuristic below (confirmed against the account's
    # actual deposit history rather than inferred from NAV shape).
    # Without it, as a fallback default, the first day where NAV
    # reaches at least 50% of the period's peak NAV is used as a
    # rough stand-in for "fully funded" - a heuristic, not a precise
    # answer, and worth re-verifying each year once "Year to Date"
    # periods reset in January.
    start_date_override = os.environ.get("PERFORMANCE_START_DATE")
    if start_date_override:
        rows = [r for r in rows if r[0] >= start_date_override]
        if not rows:
            raise ValueError(
                f"No NAV rows on or after PERFORMANCE_START_DATE={start_date_override}"
            )
    else:
        peak_nav = max(nav for _, nav in rows)
        threshold = peak_nav * 0.5
        first_funded_idx = next(
            (i for i, (_, nav) in enumerate(rows) if nav >= threshold), None
        )
        if first_funded_idx is None:
            raise ValueError("No NAV values found reaching the funding threshold")
        rows = rows[first_funded_idx:]

    if len(rows) < 2:
        raise ValueError("Not enough NAV data points to compute return/drawdown")

    navs = [nav for _, nav in rows]
    start_nav = navs[0]
    end_nav = navs[-1]
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
    }


def compute_trade_stats(root: ET.Element) -> dict:
    """
    Returns win rate, profit factor, and closed-position count from the
    Trades section, or an empty dict if that section isn't present.

    Counts per POSITION, not per fill: trades for the same instrument
    are grouped and processed in chronological order, tracking the
    running quantity. Realized P&L is accumulated while a position is
    open; when the running quantity returns to flat (0), that closes
    one "trade" and the accumulated P&L is recorded as its result.
    This means scaling out of one position in three fills counts as
    one trade, not three.
    """
    trades = root.findall(".//Trade")
    if not trades:
        return {}

    def signed_qty(t: ET.Element):
        qty_raw = t.get("quantity")
        if qty_raw is None:
            return None
        try:
            qty = float(qty_raw)
        except ValueError:
            return None
        # Some Flex configurations report quantity as an unsigned
        # magnitude with a separate buySell field - normalize to signed.
        buy_sell = t.get("buySell")
        if buy_sell == "SELL" and qty > 0:
            qty = -qty
        elif buy_sell == "BUY" and qty < 0:
            qty = abs(qty)
        return qty

    # Group fills by instrument (conid is the stable IBKR identifier;
    # fall back to symbol if conid isn't present).
    by_instrument = {}
    for t in trades:
        key = t.get("conid") or t.get("symbol")
        date_time = t.get("dateTime") or t.get("tradeDate") or ""
        qty = signed_qty(t)
        pnl_raw = t.get("fifoPnlRealized")
        pnl = float(pnl_raw) if pnl_raw not in (None, "") else 0.0
        if key is None or qty is None:
            continue
        by_instrument.setdefault(key, []).append((date_time, qty, pnl))

    position_pnls = []
    EPSILON = 1e-6  # guards against float rounding never quite hitting zero

    for key, fills in by_instrument.items():
        fills.sort(key=lambda f: f[0])
        running_qty = 0.0
        accumulated_pnl = 0.0
        for _, qty, pnl in fills:
            running_qty += qty
            accumulated_pnl += pnl
            if abs(running_qty) < EPSILON:
                position_pnls.append(accumulated_pnl)
                accumulated_pnl = 0.0
                running_qty = 0.0
        # Any fills left with accumulated_pnl but an open running_qty at
        # the end of the report window are a still-open position - not
        # yet a completed trade, so intentionally left out of the count.

    nonzero = [p for p in position_pnls if abs(p) > EPSILON]
    if not nonzero:
        return {}

    wins = [p for p in nonzero if p > 0]
    losses = [p for p in nonzero if p < 0]
    total_closed = len(nonzero)

    result = {
        "total_trades": total_closed,
        "win_rate_pct": round(len(wins) / total_closed * 100.0, 1),
    }

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        result["profit_factor"] = round(gross_profit / gross_loss, 2)
    # If there are no losing positions, profit factor is undefined
    # (division by zero) rather than infinite - omit it instead of
    # showing a misleading number.

    return result


def compute_stats(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    stats = compute_nav_stats(root)
    stats.update(compute_trade_stats(root))
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
    from datetime import datetime, timezone

    output = {
        **stats,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote data.json: {output}")


if __name__ == "__main__":
    main()
