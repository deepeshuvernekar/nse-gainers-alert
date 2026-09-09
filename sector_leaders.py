#!/usr/bin/env python3
"""
NSE Mid/Small Sector Leaders -> Telegram

Large caps are excluded entirely. The universe is NIFTY MIDCAP 150 +
NIFTY SMALLCAP 250, filtered for liquidity.

Sector strength is computed FROM that universe -- stocks are grouped by
their NSE industry label and the group's median move ranks the sectors.
So "IT is leading" here means mid/small IT is leading, not TCS and Infosys.

Part 1: ranked mid/small sector strength, with the top movers inside each
        leading sector, annotated with their 3-month gain.
Part 2: the strongest 3-month gainers across the whole mid/small universe,
        with the ones in today's leading sectors listed first.

Env vars required:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Optional env vars:
    TOP_SECTORS      (default 4)      leading sectors to expand
    TOP_STOCKS       (default 5)      stocks shown per sector
    MIN_GROUP        (default 4)      min liquid stocks for a sector to rank
    MIN_VOLUME       (default 100000) min shares traded today
    MIN_3M_GAIN      (default 25)     min 3-month % gain to be a "momentum" name
    TOP_MOMENTUM     (default 10)     how many momentum names to list
    SHOW_LAGGARDS    (default 1)      also list the weakest sectors
    SKIP_IF_CLOSED   (default 1)      exit quietly when the market is closed
    INDEX_STRIP      (default 0)      one context line of official sector indices
"""

import os
import sys
import time
import html
import statistics
import datetime as dt
from collections import defaultdict
from urllib.parse import quote

import requests

NSE_BASE = "https://www.nseindia.com"
ALL_INDICES = f"{NSE_BASE}/api/allIndices"
STOCK_INDICES = f"{NSE_BASE}/api/equity-stockIndices?index="
MARKET_STATUS = f"{NSE_BASE}/api/marketStatus"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

TOP_SECTORS = int(os.environ.get("TOP_SECTORS", "4"))
TOP_STOCKS = int(os.environ.get("TOP_STOCKS", "5"))
MIN_GROUP = int(os.environ.get("MIN_GROUP", "4"))
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", "100000"))
MIN_3M_GAIN = float(os.environ.get("MIN_3M_GAIN", "25"))
TOP_MOMENTUM = int(os.environ.get("TOP_MOMENTUM", "10"))
SHOW_LAGGARDS = os.environ.get("SHOW_LAGGARDS", "1") == "1"
SKIP_IF_CLOSED = os.environ.get("SKIP_IF_CLOSED", "1") == "1"
INDEX_STRIP = os.environ.get("INDEX_STRIP", "0") == "1"

# The only universe this script looks at. No large caps.
UNIVERSE_INDICES = ["NIFTY MIDCAP 150", "NIFTY SMALLCAP 250"]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{NSE_BASE}/market-data/live-market-indices",
    "Connection": "keep-alive",
}


def log(msg):
    print(f"[{dt.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def make_session():
    """NSE requires cookies from a real page visit before the API will answer."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    for url in (NSE_BASE, f"{NSE_BASE}/market-data/live-market-indices"):
        try:
            s.get(url, timeout=15)
            time.sleep(1)
        except requests.RequestException as e:
            log(f"warm-up request to {url} failed: {e}")
    return s


def get_json(session, url, attempts=4):
    delay = 2
    for i in range(1, attempts + 1):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200 and r.text.strip().startswith(("{", "[")):
                return r.json()
            log(f"attempt {i}: HTTP {r.status_code} for {url[:70]}")
        except requests.RequestException as e:
            log(f"attempt {i}: {e}")
        time.sleep(delay)
        delay *= 2
        if i == 2:
            try:
                session.get(NSE_BASE, timeout=15)
                time.sleep(1)
            except requests.RequestException:
                pass
    return None


def market_is_open(session):
    data = get_json(session, MARKET_STATUS)
    if not data:
        return True
    for row in data.get("marketState", []):
        if row.get("market") == "Capital Market":
            status = (row.get("marketStatus") or "").lower()
            log(f"capital market status: {status}")
            return "close" not in status
    return True


def fetch_constituents(session, index_name):
    data = get_json(session, STOCK_INDICES + quote(index_name))
    if not data:
        return []
    rows = []
    for row in data.get("data", []):
        sym = (row.get("symbol") or "").strip()
        if not sym or sym.upper() == index_name.upper():
            continue
        try:
            pct = float(row.get("pChange"))
            price = float(row.get("lastPrice"))
        except (TypeError, ValueError):
            continue
        try:
            vol = int(float(row.get("totalTradedVolume") or 0))
        except (TypeError, ValueError):
            vol = 0
        industry = ((row.get("meta") or {}).get("industry") or "").strip()
        rows.append({
            "symbol": sym, "pct": pct, "price": price,
            "volume": vol, "industry": industry,
        })
    return rows


def build_universe(session):
    universe = {}
    for idx in UNIVERSE_INDICES:
        rows = fetch_constituents(session, idx)
        log(f"{idx}: {len(rows)} constituents")
        for r in rows:
            universe[r["symbol"]] = r
        time.sleep(1)
    liquid = [r for r in universe.values()
              if r["volume"] >= MIN_VOLUME and r["industry"]]
    log(f"universe {len(universe)}, liquid with industry tag {len(liquid)}")
    return liquid


def three_month_returns(symbols):
    """Batch-fetch split/bonus-adjusted 3-month returns. Returns {symbol: pct}."""
    try:
        import yfinance as yf
    except ImportError:
        log("yfinance not installed -- 3-month data unavailable")
        return {}

    out = {}
    tickers = [f"{s}.NS" for s in symbols]
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            df = yf.download(
                chunk, period="3mo", interval="1d",
                auto_adjust=True, progress=False,
                threads=True, group_by="column",
            )
        except Exception as e:
            log(f"yfinance chunk {i // 100 + 1} failed: {e}")
            continue
        if df is None or df.empty:
            continue
        try:
            close = df["Close"]
        except (KeyError, TypeError):
            continue
        if hasattr(close, "columns"):
            cols = list(close.columns)
        else:
            cols = chunk[:1]
            close = close.to_frame(cols[0])
        for col in cols:
            s = close[col].dropna()
            if len(s) < 40:  # need a real 3 months of history
                continue
            first, last = float(s.iloc[0]), float(s.iloc[-1])
            if first <= 0:
                continue
            out[str(col).replace(".NS", "")] = (last / first - 1) * 100
        log(f"3M returns resolved: {len(out)}")
        time.sleep(1)
    return out


def rank_sectors(liquid):
    """Group the mid/small universe by industry and rank by median move."""
    groups = defaultdict(list)
    for r in liquid:
        groups[r["industry"]].append(r)

    ranked = []
    for industry, rows in groups.items():
        if len(rows) < MIN_GROUP:
            continue
        moves = [r["pct"] for r in rows]
        ranked.append({
            "industry": industry,
            "median": statistics.median(moves),
            "count": len(rows),
            "up": sum(1 for m in moves if m > 0),
            "stocks": sorted(rows, key=lambda x: x["pct"], reverse=True),
        })
    ranked.sort(key=lambda x: x["median"], reverse=True)
    log(f"{len(ranked)} sectors with >= {MIN_GROUP} liquid names")
    return ranked


def fetch_index_strip(session):
    data = get_json(session, ALL_INDICES)
    if not data:
        return ""
    rows = []
    for row in data.get("data", []):
        if (row.get("key") or "").upper() != "SECTORAL INDICES":
            continue
        try:
            rows.append((row.get("index", "").replace("NIFTY ", ""),
                         float(row.get("percentChange"))))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return "  |  ".join(f"{n} {p:+.1f}%" for n, p in rows[:3])


def arrow(pct):
    return "\U0001F7E2" if pct > 0 else ("\U0001F534" if pct < 0 else "\u26AA")


def fmt_3m(gain):
    if gain is None:
        return "3M n/a"
    return f"3M {gain:+.0f}%"


def build_message(ranked, returns, momentum, liquid, strip):
    now = dt.datetime.now(IST)
    green = sum(1 for r in liquid if r["pct"] > 0)
    total = len(liquid)
    pct_green = (green / total * 100) if total else 0
    breadth = "broad" if pct_green >= 60 else ("narrow" if pct_green <= 35 else "mixed")

    lines = [
        f"<b>Mid/Small Sector Leaders</b>  {now:%d %b, %H:%M} IST",
        f"<i>{green}/{total} stocks green ({pct_green:.0f}%) - {breadth} tape</i>",
        "",
    ]

    if strip:
        lines.append(f"<i>Large-cap context: {html.escape(strip)}</i>")
        lines.append("")

    for sec in ranked[:TOP_SECTORS]:
        lines.append(
            f"{arrow(sec['median'])} <b>{html.escape(sec['industry'])}</b>  "
            f"{sec['median']:+.2f}%  <i>({sec['up']}/{sec['count']} up)</i>"
        )
        for st in sec["stocks"][:TOP_STOCKS]:
            gain = returns.get(st["symbol"])
            star = "*" if gain is not None and gain >= MIN_3M_GAIN else " "
            lines.append(
                f"  {star} {html.escape(st['symbol'])}  {st['pct']:+.2f}%  "
                f"| {fmt_3m(gain)}  | {st['price']:,.1f}"
            )
        lines.append("")

    if SHOW_LAGGARDS and len(ranked) >= 3:
        tail = "  |  ".join(
            f"{s['industry']} {s['median']:+.2f}%" for s in ranked[-3:][::-1]
        )
        lines.append(f"<b>Weakest:</b> {html.escape(tail)}")
        lines.append("")

    if momentum:
        lines.append(f"<b>Strongest 3M gainers (&gt;{MIN_3M_GAIN:.0f}%)</b>")
        lines.append("<i>* = in a leading sector today</i>")
        for m in momentum:
            star = "*" if m["hot"] else "-"
            lines.append(
                f"{star} <b>{html.escape(m['symbol'])}</b>  "
                f"3M {m['gain3m']:+.0f}%  | today {m['pct']:+.2f}%"
            )
            lines.append(f"     <i>{html.escape(m['industry'][:38])}</i>")
    else:
        lines.append(f"<i>No mid/small names cleared the {MIN_3M_GAIN:.0f}% 3M filter.</i>")

    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return False

    chunks, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > 3800:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    ok = True
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            },
            timeout=25,
        )
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:300]}")
            ok = False
        time.sleep(0.5)
    if ok:
        log(f"sent to Telegram ({len(chunks)} message(s))")
    return ok


def main():
    session = make_session()

    if SKIP_IF_CLOSED and not market_is_open(session):
        log("market closed -- exiting without sending")
        return 0

    liquid = build_universe(session)
    if not liquid:
        log("no mid/small data returned")
        send_telegram(
            "<b>Mid/Small Sector Leaders</b>\nCould not fetch constituent data "
            "from NSE on this run. Check the Actions log."
        )
        return 1

    ranked = rank_sectors(liquid)
    if not ranked:
        log("no sector groups met the minimum size")
        send_telegram("<b>Mid/Small Sector Leaders</b>\nNo sector had enough "
                      "liquid names to rank. Try lowering MIN_GROUP or MIN_VOLUME.")
        return 1

    returns = three_month_returns([r["symbol"] for r in liquid])

    leading = {s["industry"] for s in ranked[:TOP_SECTORS]}
    momentum = []
    for r in liquid:
        gain = returns.get(r["symbol"])
        if gain is None or gain < MIN_3M_GAIN:
            continue
        momentum.append({**r, "gain3m": gain, "hot": r["industry"] in leading})
    momentum.sort(key=lambda x: (x["hot"], x["gain3m"]), reverse=True)
    momentum = momentum[:TOP_MOMENTUM]
    log(f"momentum picks: {len(momentum)}")

    strip = fetch_index_strip(session) if INDEX_STRIP else ""

    send_telegram(build_message(ranked, returns, momentum, liquid, strip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
