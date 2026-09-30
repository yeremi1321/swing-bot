"""
Swing Trade Scanner -> Discord

Once a day after the US market closes, this scans a list of liquid stocks,
finds two classic swing setups (pullback-in-uptrend and volume breakout),
posts the best ones to Discord with entry / stop / target / size, and
tracks how every past pick actually played out.

Rule-based signals for you to review. NOT financial advice.
"""
import json
import math
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

# ------------------------------------------------------------------
# Settings (override any of these with environment variables)
# ------------------------------------------------------------------
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "5000"))  # your account size in $
RISK_PCT = float(os.environ.get("RISK_PCT", "1"))             # % of account lost if a trade hits its stop
MAX_PICKS = int(os.environ.get("MAX_PICKS", "5"))             # max new picks per day
MAX_HOLD_DAYS = int(os.environ.get("MAX_HOLD_DAYS", "15"))    # trading days before a pick is closed out
DATABASE_URL = os.environ.get("DATABASE_URL", "")             # Postgres for tracking (needed on Render)
LOCAL_FILE = os.environ.get("PICKS_FILE", "picks.json")       # used when DATABASE_URL is empty
FORCE_RUN = os.environ.get("FORCE_RUN", "") == "1"            # run even if today wasn't a trading day

MIN_PRICE = 5.0
MIN_AVG_VOLUME = 1_000_000
NY = ZoneInfo("America/New_York")

# Liquid, widely traded US stocks. Edit freely, or put one ticker per
# line in tickers.txt next to this file to override the list.
DEFAULT_TICKERS = """
AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX CRM ORCL ADBE INTC QCOM MU
AMAT LRCX KLAC TXN ARM SMCI PLTR SNOW CRWD PANW NET DDOG SHOP UBER ABNB COIN
HOOD PYPL SOFI JPM BAC WFC GS MS C SCHW V MA AXP UNH LLY JNJ PFE MRK ABBV AMGN
GILD ISRG TMO CVS XOM CVX COP OXY SLB HAL CAT DE BA GE HON LMT RTX UPS FDX DAL
UAL CCL WMT COST TGT HD LOW NKE SBUX MCD DIS CMCSA T VZ KO PEP PG F GM RIVN
MARA RIOT DKNG RBLX
""".split()


def load_tickers():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickers.txt")
    if os.path.exists(path):
        with open(path) as f:
            return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]
    return DEFAULT_TICKERS


# ------------------------------------------------------------------
# Market data
# ------------------------------------------------------------------
def download(tickers):
    import yfinance as yf

    raw = yf.download(
        tickers, period="1y", interval="1d", group_by="ticker",
        auto_adjust=True, threads=True, progress=False,
    )
    out = {}
    for t in tickers:
        try:
            df = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
        except KeyError:
            continue
        if len(df):
            out[t] = df
    return out


# ------------------------------------------------------------------
# Indicators
# ------------------------------------------------------------------
def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    prev = df["Close"].shift()
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ------------------------------------------------------------------
# Setup detection
# ------------------------------------------------------------------
def find_setup(ticker, df):
    if len(df) < 70:
        return None
    c, v = df["Close"], df["Volume"]
    close = float(c.iloc[-1])
    avg_vol = v.rolling(20).mean().shift(1)
    if close < MIN_PRICE or not avg_vol.iloc[-1] >= MIN_AVG_VOLUME:
        return None

    sma50 = c.rolling(50).mean()
    r = rsi(c)
    a = float(atr(df).iloc[-1])
    hi20 = df["High"].rolling(20).max().shift(1)
    vol_ratio = float(v.iloc[-1] / avg_vol.iloc[-1])

    # Only trade stocks in an uptrend: above a rising 50-day average
    uptrend = close > sma50.iloc[-1] and sma50.iloc[-1] > sma50.iloc[-11]
    if not uptrend or a <= 0:
        return None

    if close > hi20.iloc[-1] and vol_ratio >= 1.5:
        setup, why, score = "Breakout", f"new 20-day high on {vol_ratio:.1f}x volume", vol_ratio
    elif r.iloc[-2] < 40 and r.iloc[-1] > r.iloc[-2] and close > c.iloc[-2]:
        setup, why = "Pullback", f"dip in uptrend, RSI bouncing from {r.iloc[-2]:.0f}"
        score = 1 + (40 - r.iloc[-2]) / 10
    else:
        return None

    stop = close - 2 * a                      # 2x ATR below entry
    target = close + 2 * (close - stop)       # 2:1 reward-to-risk
    risk_per_share = close - stop
    dollars = min(ACCOUNT_SIZE * RISK_PCT / 100 / risk_per_share * close, ACCOUNT_SIZE)
    return {
        "ticker": ticker, "setup": setup, "why": why, "score": round(float(score), 3),
        "entry": round(close, 2), "stop": round(stop, 2), "target": round(target, 2),
        "position_usd": round(dollars, 2), "shares": round(dollars / close, 2),
    }


# ------------------------------------------------------------------
# Tracking past picks
# ------------------------------------------------------------------
def update_open_picks(picks, data):
    """Walk each open pick forward day by day and close it at stop, target, or time limit."""
    closed_now = []
    for p in picks:
        if p["status"] != "open":
            continue
        df = data.get(p["ticker"])
        if df is None:
            continue
        risk = p["entry"] - p["stop"]
        after = df[df.index.date > date.fromisoformat(p["date"])]
        exit_price = None
        for i, (day, row) in enumerate(after.iterrows(), start=1):
            if row["Low"] <= p["stop"]:           # stop checked first = conservative
                exit_price, result = min(row["Open"], p["stop"]), "stop"
            elif row["High"] >= p["target"]:
                exit_price, result = max(row["Open"], p["target"]), "target"
            elif i >= MAX_HOLD_DAYS:
                exit_price, result = row["Close"], "time"
            if exit_price is not None:
                p.update(
                    status="closed", result=result, exit_date=day.date().isoformat(),
                    exit=round(float(exit_price), 2),
                    r=round(float((exit_price - p["entry"]) / risk), 2),
                )
                closed_now.append(p)
                break
    return closed_now


def track_record(picks):
    closed = [p for p in picks if p["status"] == "closed"]
    if not closed:
        return None
    rs = [p["r"] for p in closed]
    return {
        "n": len(closed),
        "win_rate": sum(r > 0 for r in rs) / len(rs) * 100,
        "avg_r": sum(rs) / len(rs),
        "total_r": sum(rs),
    }


# ------------------------------------------------------------------
# Storage (Postgres on Render, JSON file locally)
# ------------------------------------------------------------------
def load_picks():
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS bot_state (k TEXT PRIMARY KEY, v JSONB)")
            row = conn.execute("SELECT v FROM bot_state WHERE k = 'picks'").fetchone()
            return row[0] if row else []
    if os.path.exists(LOCAL_FILE):
        with open(LOCAL_FILE) as f:
            return json.load(f)
    return []


def save_picks(picks):
    if DATABASE_URL:
        import psycopg
        from psycopg.types.json import Jsonb
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "INSERT INTO bot_state (k, v) VALUES ('picks', %s) "
                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                (Jsonb(picks),),
            )
        return
    with open(LOCAL_FILE, "w") as f:
        json.dump(picks, f, indent=2)


# ------------------------------------------------------------------
# Discord
# ------------------------------------------------------------------
def format_message(day, new_picks, closed_now, picks, weak_market):
    lines = [f"📈 **Swing scan — {day.strftime('%a %b %d')}**"]
    if weak_market:
        lines.append("⚠️ SPY is below its 50-day average — setups fail more often in weak markets.")
    if new_picks:
        for p in new_picks:
            lines += [
                "",
                f"**{p['ticker']}** · {p['setup']} ({p['why']})",
                f"Entry ~${p['entry']:.2f} · Stop ${p['stop']:.2f} · Target ${p['target']:.2f}",
                f"Size: ~${p['position_usd']:,.0f} ({p['shares']} sh) — risks ~${ACCOUNT_SIZE * RISK_PCT / 100:,.0f}",
            ]
    else:
        lines += ["", "No setups today. Sitting out is a position too."]

    if closed_now:
        icons = {"target": "✅", "stop": "❌", "time": "⏱️"}
        closed_txt = ", ".join(f"{icons[p['result']]} {p['ticker']} {p['r']:+.1f}R" for p in closed_now)
        lines += ["", f"Closed: {closed_txt}"]

    rec = track_record(picks)
    n_open = sum(p["status"] == "open" for p in picks)
    lines.append("")
    if rec:
        lines.append(
            f"📊 Track record: {rec['n']} closed · {rec['win_rate']:.0f}% wins · "
            f"avg {rec['avg_r']:+.2f}R · total {rec['total_r']:+.1f}R · {n_open} open"
        )
    else:
        lines.append(f"📊 Track record: no closed picks yet · {n_open} open")
    lines.append("_Rule-based signals, not financial advice._")
    return "\n".join(lines)[:1990]


def post(message):
    if not WEBHOOK:
        print(message)
        return
    resp = requests.post(WEBHOOK, json={"content": message}, timeout=20)
    resp.raise_for_status()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main(downloader=download):
    picks = load_picks()
    open_tickers = {p["ticker"] for p in picks if p["status"] == "open"}
    tickers = sorted(set(load_tickers()) | open_tickers | {"SPY"})

    data = downloader(tickers)
    if "SPY" not in data:
        sys.exit("Couldn't download market data.")

    last_day = data["SPY"].index[-1].date()
    if last_day != datetime.now(NY).date() and not FORCE_RUN:
        print(f"No new trading data today (last bar {last_day}); skipping.")
        return

    closed_now = update_open_picks(picks, data)

    candidates = []
    for t, df in data.items():
        if t == "SPY" or t in open_tickers:
            continue
        try:
            s = find_setup(t, df)
        except Exception as e:  # one bad ticker shouldn't kill the run
            print(f"{t}: {e}")
            continue
        if s:
            candidates.append(s)
    new_picks = sorted(candidates, key=lambda s: s["score"], reverse=True)[:MAX_PICKS]
    for p in new_picks:
        p.update(date=last_day.isoformat(), status="open")
        picks.append(p)

    spy = data["SPY"]["Close"]
    weak_market = spy.iloc[-1] < spy.rolling(50).mean().iloc[-1]

    save_picks(picks)
    post(format_message(last_day, new_picks, closed_now, picks, weak_market))


if __name__ == "__main__":
    main()
