"""
chartink_gainers.py — daily NSE 10%+ gainers scan via Chartink, pushed to Telegram.

Drop-in module for the existing growwapi automation on Groww Cloud.
Runs the scan server-side at Chartink and posts a formatted table to the same
Telegram bot already used for the 15:35 IST alerts.

Usage:
    from chartink_gainers import run_daily_gainers
    run_daily_gainers()                       # CLAUSE_CORE: every 10%+ mover
    run_daily_gainers(clause=CLAUSE_TIGHT)    # adds VCP-style quality filters

Env vars expected (same ones the existing script uses):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import os
import re
import time
import logging
from typing import List, Dict, Optional

import requests

log = logging.getLogger(__name__)

CHARTINK_BASE = "https://chartink.com"
SCREENER_URL = f"{CHARTINK_BASE}/screener/"
PROCESS_URL = f"{CHARTINK_BASE}/screener/process"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------
# Scan clauses
# --------------------------------------------------------------------------

# Core: up 10%+ on the day, volume > 1 lakh.
#
# WHY 1.0995 AND NOT 1.1:
# A stock locked at its 10% upper circuit closes at exactly 1.1x the previous
# close, and "close > prev * 1.1" is strictly false for it — so a plain 1.1
# silently drops every circuit-locked name, which are the ones most worth
# seeing. Tick-size rounding also makes circuit closes print as 9.98% or 9.99%
# rather than a clean 10.00%. The 1.0995 threshold (>= +9.95%) catches all of
# them. Chartink has no ">=" guarantee in clause text, so the tolerance is
# built into the multiplier instead of the operator.
CLAUSE_CORE = (
    "( {cash} ( latest close > 1 day ago close * 1.0995 "
    "and latest volume > 100000 ) )"
)

# Tight: adds VCP-style quality filters.
#   - volume >= 3x its own 20-day average  (real participation)
#   - price above the 21-EMA                (aligns with trailing-stop framework)
#   - close in top quartile of day's range  (held the gain, didn't fade)
#   - traded value > Rs 5 cr                (actually fillable)
#
# CAUTION: these filters work against circuit-locked stocks. When a stock is
# frozen at its upper circuit nobody can sell into it, so volume and traded
# value are often LOW, not high — the 3x-volume and Rs 5 cr clauses then screen
# out the very names that hit the circuit. Use this clause to find liquid
# breakouts you could actually trade; use CLAUSE_CORE to see every 10% mover.
CLAUSE_TIGHT = (
    "( {cash} ( latest close > 1 day ago close * 1.0995 "
    "and latest volume > 100000 "
    "and latest volume > 3 * sma( latest volume , 20 ) "
    "and latest close > latest ema( latest close , 21 ) "
    "and latest close > latest high - ( ( latest high - latest low ) * 0.25 ) "
    "and latest close * latest volume > 50000000 ) )"
)

# NOTE ON MID/SMALL-CAP RESTRICTION
# Chartink has no market-cap field in scan conditions — only OHLCV. To limit the
# scan to mid/small caps, build the scan once in the Chartink UI with "Scan on"
# set to Nifty Midsmallcap 400, then copy the generated clause here: it will
# carry a numeric group id in place of {cash}, e.g. "( {33489} ( ... ) )".
# The group id is account-visible only in the UI, so it can't be hardcoded blind.
# Until then, the traded-value clause in CLAUSE_TIGHT does the liquidity work and
# the Emerge/SME board is excluded automatically (not part of the cash segment).


# --------------------------------------------------------------------------
# Instrument filtering
# --------------------------------------------------------------------------
# Chartink's cash segment carries more than ordinary shares, and the extras
# distort a stock momentum scan:
#
#   "-RE" suffix = Rights Entitlement. A temporary instrument created during a
#   rights issue, tradeable for a few days and priced at roughly the discount to
#   the share. It swings wildly and is not the underlying stock. GENESYS-RE
#   showed up at +13.9% on 8 Sep alongside GENESYS itself at +20%.
#
#   ETFs track an index or a foreign market. A double-digit daily "gain" is
#   usually a premium/discount dislocation on thin volume, not a real move.
#   A ticker alone can't identify an ETF reliably, so this is an explicit list —
#   add to it whenever you spot one in the alert.

EXCLUDE_SUFFIXES = ("-RE",)

EXCLUDE_SYMBOLS = {
    "MONQ50",     # Motilal Oswal Nasdaq Q50 ETF
    "MASPTOP50",  # Mirae Asset S&P 500 Top 50 ETF
}


def is_tradeable_equity(symbol) -> bool:
    """False for rights entitlements and known ETFs."""
    s = str(symbol).upper().strip()
    if s in EXCLUDE_SYMBOLS:
        return False
    return not s.endswith(EXCLUDE_SUFFIXES)


# --------------------------------------------------------------------------
# Chartink fetch
# --------------------------------------------------------------------------

def fetch_chartink_scan(clause: str, retries: int = 3, timeout: int = 45) -> List[Dict]:
    """Run a Chartink scan clause and return the result rows.

    Chartink requires a CSRF token from the screener page plus the matching
    session cookie, so the GET must precede the POST on the same Session.
    """
    last_err: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            s = requests.Session()
            s.headers.update({"User-Agent": UA})

            page = s.get(SCREENER_URL, timeout=timeout)
            page.raise_for_status()

            m = re.search(
                r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page.text
            )
            if not m:
                raise RuntimeError("csrf-token not found on screener page")
            token = m.group(1)

            resp = s.post(
                PROCESS_URL,
                headers={
                    "x-csrf-token": token,
                    "x-requested-with": "XMLHttpRequest",
                    "Referer": SCREENER_URL,
                },
                data={"scan_clause": clause},
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json()

            if payload.get("error"):
                raise RuntimeError(f"Chartink rejected the clause: {payload['error']}")

            return payload.get("data", []) or []

        except Exception as e:  # noqa: BLE001 - want to retry on anything transient
            last_err = e
            log.warning("Chartink attempt %s/%s failed: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Chartink scan failed after {retries} attempts") from last_err


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def _fmt_volume(v) -> str:
    """Render volume in lakh/crore, the way it reads on Indian screens."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if v >= 1e7:
        return f"{v / 1e7:.2f}Cr"
    if v >= 1e5:
        return f"{v / 1e5:.2f}L"
    return f"{v:,.0f}"


def format_message(rows: List[Dict], label: str = "10%+ gainers") -> str:
    """Format scan rows as a monospace Telegram table."""
    from datetime import datetime, timezone, timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    stamp = datetime.now(ist).strftime("%a %d %b %Y")

    if not rows:
        return f"<b>{label} — {stamp}</b>\nNo stocks matched today."

    rows = sorted(rows, key=lambda r: float(r.get("per_chg", 0) or 0), reverse=True)

    lines = [
        f"{'SYMBOL':<12}{'%CHG':>7}{'PRICE':>10}{'VOL':>10}",
        "-" * 39,
    ]
    for r in rows:
        sym = str(r.get("nsecode", r.get("name", "?")))[:11]
        pct = float(r.get("per_chg", 0) or 0)
        close = float(r.get("close", 0) or 0)
        vol = _fmt_volume(r.get("volume", 0))
        lines.append(f"{sym:<12}{pct:>6.1f}%{close:>10,.2f}{vol:>10}")

    table = "\n".join(lines)
    return (
        f"<b>{label} — {stamp}</b>\n"
        f"{len(rows)} stock(s) matched\n"
        f"<pre>{table}</pre>"
    )


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(text: str, bot_token: str = None, chat_id: str = None) -> None:
    bot_token = bot_token or os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]

    # Telegram caps messages at 4096 chars; chunk on line boundaries if needed.
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
            timeout=30,
        )
        resp.raise_for_status()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run_daily_gainers(clause: str = CLAUSE_CORE, label: str = "10%+ gainers") -> List[Dict]:
    """Scan, format, send. Returns the rows so the caller can log or reuse them."""
    raw = fetch_chartink_scan(clause)
    rows = [r for r in raw if is_tradeable_equity(r.get("nsecode"))]

    dropped = len(raw) - len(rows)
    if dropped:
        log.info("Filtered out %s non-equity instrument(s)", dropped)

    send_telegram(format_message(rows, label=label))
    log.info("Sent %s rows to Telegram", len(rows))
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_daily_gainers()
