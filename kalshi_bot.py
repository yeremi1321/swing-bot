"""
Kalshi Sports Bot -> Discord

A few times a day this compares Kalshi "who wins the game" prices with
sportsbook moneylines (via The Odds API). It strips the bookmakers' margin
out of the odds to get a fair win probability. When a team's Kalshi YES
contract is cheaper than that fair price by more than fees plus a safety
margin, it buys (on Kalshi's demo exchange by default) and posts to Discord.

Every signal is also paper-tracked at the real Kalshi price it saw and
settled once the game is decided, so you get an honest track record.

Rule-based signals, not financial advice. Prediction markets are risky.
"""
import base64
import json
import math
import os
import statistics
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

# ------------------------------------------------------------------
# Settings (override any of these with environment variables)
# ------------------------------------------------------------------
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_REGIONS = os.environ.get("ODDS_REGIONS", "us")           # each region costs 1 credit per sport
SPORTS_ENABLED = os.environ.get("SPORTS", "nfl,ncaaf,nba,wnba,mlb,nhl").split(",")

KALSHI_ENV = os.environ.get("KALSHI_ENV", "demo")              # "demo" (fake money) or "prod"
KALSHI_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY = os.environ.get("KALSHI_PRIVATE_KEY", "")  # PEM text (GitHub secret)
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
TRADING_ENABLED = os.environ.get("TRADING_ENABLED", "") == "1"  # kill switch: off = paper only
CONFIRM_REAL_MONEY = os.environ.get("CONFIRM_REAL_MONEY", "") == "yes"

MIN_EDGE = float(os.environ.get("MIN_EDGE", "0.03"))           # fair prob - price - fee, per $1 contract
MAX_EDGE = float(os.environ.get("MAX_EDGE", "0.15"))           # bigger "edges" are usually bad data
MIN_PRICE = float(os.environ.get("MIN_PRICE", "0.15"))         # skip long shots...
MAX_PRICE = float(os.environ.get("MAX_PRICE", "0.85"))         # ...and heavy favorites
MIN_BOOKS = int(os.environ.get("MIN_BOOKS", "3"))              # sportsbooks needed for a consensus
LOOKAHEAD_HOURS = float(os.environ.get("LOOKAHEAD_HOURS", "24"))
MIN_MINUTES_TO_START = float(os.environ.get("MIN_MINUTES_TO_START", "15"))

MAX_BET_USD = float(os.environ.get("MAX_BET_USD", "10"))       # max cost of one bet
MAX_DAILY_USD = float(os.environ.get("MAX_DAILY_USD", "50"))   # max new money per UTC day
MAX_OPEN_USD = float(os.environ.get("MAX_OPEN_USD", "100"))    # max money in unsettled bets
MAX_BETS_PER_RUN = int(os.environ.get("MAX_BETS_PER_RUN", "5"))
FEE_RATE = float(os.environ.get("FEE_RATE", "0.07"))           # Kalshi taker fee: rate * P * (1-P)

LEDGER_FILE = os.environ.get("LEDGER_FILE", "bets.json")

# Market data always comes from production, so signals and the paper record
# use real prices. Orders go to KALSHI_ENV.
KALSHI_DATA_URL = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_TRADE_URLS = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "prod": "https://external-api.kalshi.com/trade-api/v2",
}
ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
NY = ZoneInfo("America/New_York")

# name -> (Kalshi series, The Odds API sport key). Two-outcome sports only.
SPORTS = {
    "nfl": ("KXNFLGAME", "americanfootball_nfl"),
    "ncaaf": ("KXNCAAFGAME", "americanfootball_ncaaf"),
    "nba": ("KXNBAGAME", "basketball_nba"),
    "wnba": ("KXWNBAGAME", "basketball_wnba"),
    "mlb": ("KXMLBGAME", "baseball_mlb"),
    "nhl": ("KXNHLGAME", "icehockey_nhl"),
}


# ------------------------------------------------------------------
# Kalshi API
# ------------------------------------------------------------------
class Kalshi:
    def __init__(self, base_url, key_id="", private_key=None):
        self.base_url = base_url
        self.key_id = key_id
        self.private_key = private_key

    def _headers(self, method, path):
        if not self.private_key:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        ts = str(int(time.time() * 1000))
        # Sign timestamp + method + full path (with /trade-api/v2, without query string)
        msg = (ts + method + urlparse(self.base_url + path).path).encode()
        if isinstance(self.private_key, Ed25519PrivateKey):
            sig = self.private_key.sign(msg)
        else:
            sig = self.private_key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    def request(self, method, path, params=None, body=None):
        for attempt in range(4):
            resp = requests.request(
                method, self.base_url + path, params=params, json=body,
                headers=self._headers(method, path), timeout=20,
            )
            if resp.status_code != 429:
                break
            time.sleep(2 ** attempt)
        if resp.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def markets(self, **params):
        out, cursor = [], None
        while True:
            q = dict(params, limit=1000)
            if cursor:
                q["cursor"] = cursor
            d = self.request("GET", "/markets", params=q)
            out += d.get("markets", [])
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                return out

    def series(self, ticker):
        return self.request("GET", f"/series/{ticker}")["series"]

    def balance(self):
        return self.request("GET", "/portfolio/balance")

    def event_position(self, event_ticker):
        d = self.request("GET", "/portfolio/positions", params={"event_ticker": event_ticker})
        return sum(abs(float(p.get("position_fp") or 0)) for p in d.get("market_positions", []))

    def buy_yes(self, ticker, count, limit_price):
        """Immediate-or-cancel buy of YES contracts at limit_price or better."""
        return self.request("POST", "/portfolio/events/orders", body={
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": "bid",                       # bid = buy YES
            "count": f"{count:.2f}",
            "price": f"{limit_price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        })


def load_private_key():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    pem = KALSHI_PRIVATE_KEY
    if not pem and KALSHI_PRIVATE_KEY_PATH:
        with open(KALSHI_PRIVATE_KEY_PATH) as f:
            pem = f.read()
    if not pem:
        return None
    return load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)


# ------------------------------------------------------------------
# Odds & fair probabilities
# ------------------------------------------------------------------
def fetch_odds(sport_key):
    resp = requests.get(ODDS_URL.format(sport=sport_key), params={
        "apiKey": ODDS_API_KEY, "regions": ODDS_REGIONS, "markets": "h2h", "oddsFormat": "decimal",
    }, timeout=20)
    resp.raise_for_status()
    print(f"Odds API credits left: {resp.headers.get('x-requests-remaining', '?')}")
    return resp.json()


def fair_probs(game):
    """Consensus no-vig win probability per team, or None if too few books."""
    per_team = {game["home_team"]: [], game["away_team"]: []}
    for book in game.get("bookmakers", []):
        h2h = next((m for m in book.get("markets", []) if m["key"] == "h2h"), None)
        if not h2h or len(h2h["outcomes"]) != 2:
            continue                                   # skip books with a draw line etc.
        implied = {o["name"]: 1 / o["price"] for o in h2h["outcomes"] if o["price"] > 1}
        if set(implied) != set(per_team):
            continue
        total = sum(implied.values())                  # > 1 because of the bookmaker's margin
        for team, p in implied.items():
            per_team[team].append(p / total)
    n_books = len(per_team[game["home_team"]])
    if n_books < MIN_BOOKS:
        return None, n_books
    med = {t: statistics.median(ps) for t, ps in per_team.items()}
    total = sum(med.values())
    return {t: p / total for t, p in med.items()}, n_books


def taker_fee(price, contracts=1):
    """Kalshi taker fee in dollars, rounded up to the cent."""
    return math.ceil(FEE_RATE * contracts * price * (1 - price) * 100 - 1e-9) / 100


# ------------------------------------------------------------------
# Matching Kalshi events to sportsbook games
# ------------------------------------------------------------------
def norm(name):
    name = name.lower().replace("st.", "state")
    return " ".join("".join(ch if ch.isalnum() else " " for ch in name).split())


def name_matches(kalshi_name, full_name):
    # Kalshi uses short names ("Los Angeles R", "Buffalo"); books use full ones.
    k, f = norm(kalshi_name), norm(full_name)
    return bool(k) and f.startswith(k)


def ticker_date(event_ticker):
    """KXNFLGAME-26OCT12BUFLAR -> date(2026, 10, 12)."""
    try:
        return datetime.strptime(event_ticker.split("-")[1][:7].title(), "%y%b%d").date()
    except (IndexError, ValueError):
        return None


def kalshi_events(markets):
    """Group open markets into two-team events: {event_ticker: [market, market]}."""
    events = {}
    for m in markets:
        events.setdefault(m["event_ticker"], []).append(m)
    return {e: ms for e, ms in events.items() if len(ms) == 2}


def match_game(game, events):
    """Find the Kalshi event for a sportsbook game -> {team full name: market}, or None."""
    start = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00")).astimezone(NY).date()
    home, away = game["home_team"], game["away_team"]
    found = []
    for e, (m1, m2) in events.items():
        d = ticker_date(e)
        if d is None or abs((d - start).days) > 1:
            continue
        a, b = m1["yes_sub_title"], m2["yes_sub_title"]
        straight = name_matches(a, home) and name_matches(b, away)
        crossed = name_matches(a, away) and name_matches(b, home)
        if straight != crossed:                        # exactly one way to pair them up
            found.append({home: m1, away: m2} if straight else {home: m2, away: m1})
    return found[0] if len(found) == 1 else None


# ------------------------------------------------------------------
# Finding bets
# ------------------------------------------------------------------
def find_bets(sport, games, events, fee_multiplier, now, skip_events):
    bets = []
    for game in games:
        start = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
        minutes_out = (start - now).total_seconds() / 60
        if not MIN_MINUTES_TO_START <= minutes_out <= LOOKAHEAD_HOURS * 60:
            continue
        pair = match_game(game, events)
        if not pair or next(iter(pair.values()))["event_ticker"] in skip_events:
            continue
        fair, n_books = fair_probs(game)
        if not fair:
            continue
        best = None
        for team, m in pair.items():
            ask = float(m.get("yes_ask_dollars") or 0)
            if not MIN_PRICE <= ask <= MAX_PRICE:
                continue
            fee = FEE_RATE * fee_multiplier * ask * (1 - ask)
            edge = fair[team] - ask - fee
            if edge > MAX_EDGE:
                print(f"Skipping suspicious edge {edge:.2f} on {m['ticker']} (check the match)")
                continue
            if edge >= MIN_EDGE and (best is None or edge > best["edge"]):
                opponent = next(t for t in pair if t != team)
                best = {
                    "sport": sport, "event_ticker": m["event_ticker"], "ticker": m["ticker"],
                    "team": team, "opponent": opponent, "start": game["commence_time"],
                    "fair": round(fair[team], 4), "price": ask, "edge": round(edge, 4),
                    "books": n_books, "ask_size": float(m.get("yes_ask_size_fp") or 0),
                    # highest price that still clears MIN_EDGE, whole cents
                    "limit": math.floor((fair[team] - fee - MIN_EDGE) * 100 + 1e-9) / 100,
                }
        if best:
            bets.append(best)
    return bets


# ------------------------------------------------------------------
# Ledger (bets.json, committed back to the repo by the workflow)
# ------------------------------------------------------------------
def load_ledger():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return []


def save_ledger(ledger):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def settle(ledger, data):
    """Close out bets whose markets have a result."""
    open_bets = [b for b in ledger if b["status"] == "open"]
    if not open_bets:
        return []
    tickers = sorted({b["ticker"] for b in open_bets})
    markets = {}
    for i in range(0, len(tickers), 100):
        for m in data.markets(tickers=",".join(tickers[i:i + 100])):
            markets[m["ticker"]] = m
    settled = []
    for b in open_bets:
        result = (markets.get(b["ticker"]) or {}).get("result")
        if result not in ("yes", "no"):
            continue
        payout = 1.0 if result == "yes" else 0.0
        n = b["contracts"]
        b["paper_pnl"] = round(n * (payout - b["price"]) - taker_fee(b["price"], n), 2)
        if b.get("filled"):
            b["pnl"] = round(b["filled"] * (payout - b["fill_price"]) - b.get("fee_paid", 0), 2)
        b.update(status="settled", result=result)
        settled.append(b)
    return settled


def exposure(ledger, today):
    open_usd = sum(b["cost"] for b in ledger if b["status"] == "open")
    today_usd = sum(b["cost"] for b in ledger if b["placed_at"][:10] == today.isoformat())
    return open_usd, today_usd


def track_record(ledger):
    done = [b for b in ledger if b["status"] == "settled"]
    if not done:
        return None
    staked = sum(b["contracts"] * b["price"] for b in done)
    pnl = sum(b["paper_pnl"] for b in done)
    return {
        "n": len(done),
        "win_rate": sum(b["result"] == "yes" for b in done) / len(done) * 100,
        "pnl": pnl,
        "roi": pnl / staked * 100 if staked else 0,
        "avg_edge": sum(b["edge"] for b in done) / len(done) * 100,
    }


# ------------------------------------------------------------------
# Discord
# ------------------------------------------------------------------
def format_message(now, placed, settled, ledger, mode, notes):
    lines = [f"🎯 **Kalshi sports scan — {now.astimezone(NY).strftime('%a %b %d %I:%M %p ET')}** · {mode}"]
    lines += [f"⚠️ {n}" for n in notes]
    if placed:
        for b in placed:
            start = datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(NY)
            fill = ""
            if mode != "paper":
                fill = (f" · filled {b['filled']:g} @ ${b['fill_price']:.2f}" if b.get("filled")
                        else " · not filled")
            lines += [
                "",
                f"**{b['team']}** over {b['opponent']} ({b['sport'].upper()}, {start.strftime('%a %I:%M %p ET')})",
                f"Kalshi ${b['price']:.2f} vs fair {b['fair'] * 100:.0f}% ({b['books']} books) · "
                f"edge {b['edge'] * 100:+.1f}¢ · {b['contracts']} contracts, max ${b['limit']:.2f} "
                f"(≤ ${b['cost']:.2f}){fill}",
            ]
    else:
        lines += ["", "No edges today. Sitting out is a position too."]

    if settled:
        txt = ", ".join(f"{'✅' if b['result'] == 'yes' else '❌'} {b['team']} {b['paper_pnl']:+.2f}" for b in settled)
        lines += ["", f"Settled: {txt}"]

    rec = track_record(ledger)
    n_open = sum(b["status"] == "open" for b in ledger)
    lines.append("")
    if rec:
        lines.append(
            f"📊 Paper record: {rec['n']} settled · {rec['win_rate']:.0f}% won · "
            f"P&L ${rec['pnl']:+.2f} · ROI {rec['roi']:+.1f}% · avg edge {rec['avg_edge']:.1f}¢ · {n_open} open"
        )
    else:
        lines.append(f"📊 Paper record: nothing settled yet · {n_open} open")
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
def make_trader():
    """Return (Kalshi client or None, mode label, notes)."""
    if not TRADING_ENABLED:
        return None, "paper", []
    if KALSHI_ENV not in KALSHI_TRADE_URLS:
        sys.exit(f"KALSHI_ENV must be 'demo' or 'prod', not {KALSHI_ENV!r}")
    if KALSHI_ENV == "prod" and not CONFIRM_REAL_MONEY:
        sys.exit("Refusing real-money trading: set CONFIRM_REAL_MONEY=yes as well as KALSHI_ENV=prod.")
    key = load_private_key()
    if not (key and KALSHI_KEY_ID):
        return None, "paper", ["TRADING_ENABLED is on but Kalshi API keys are missing — paper only."]
    return Kalshi(KALSHI_TRADE_URLS[KALSHI_ENV], KALSHI_KEY_ID, key), KALSHI_ENV, []


def main(data=None, odds_fetcher=None, now=None):
    if not ODDS_API_KEY and odds_fetcher is None:
        sys.exit("Set ODDS_API_KEY (free key at https://the-odds-api.com).")
    data = data or Kalshi(KALSHI_DATA_URL)
    odds_fetcher = odds_fetcher or fetch_odds
    now = now or datetime.now(timezone.utc)
    trader, mode, notes = make_trader()

    ledger = load_ledger()
    settled = settle(ledger, data)
    seen_events = {b["event_ticker"] for b in ledger}

    candidates = []
    for sport in SPORTS_ENABLED:
        sport = sport.strip().lower()
        if sport not in SPORTS:
            continue
        series, odds_key = SPORTS[sport]
        try:
            events = kalshi_events(data.markets(series_ticker=series, status="open"))
            first, last = now.astimezone(NY).date(), (now + timedelta(hours=LOOKAHEAD_HOURS)).astimezone(NY).date()
            if not any(d and first <= d <= last for d in map(ticker_date, events)):
                continue                               # no games soon: don't spend odds credits
            fee_mult = float(data.series(series).get("fee_multiplier") or 1)
            candidates += find_bets(sport, odds_fetcher(odds_key), events, fee_mult, now, seen_events)
        except Exception as e:                         # one bad sport shouldn't kill the run
            print(f"{sport}: {e}")
            notes.append(f"{sport.upper()} scan failed: {str(e)[:120]}")

    open_usd, today_usd = exposure(ledger, now.date())
    placed = []
    for b in sorted(candidates, key=lambda b: b["edge"], reverse=True):
        if len(placed) >= MAX_BETS_PER_RUN:
            break
        budget = min(MAX_BET_USD, MAX_DAILY_USD - today_usd, MAX_OPEN_USD - open_usd)
        price = max(b["limit"], b["price"])            # worst case we could pay
        contracts = int(budget // price)
        if contracts < 1:
            notes.append("Daily/open-money cap reached — skipped remaining signals.")
            break
        b.update(contracts=contracts, cost=round(contracts * price, 2),
                 placed_at=now.isoformat(timespec="seconds"), status="open", mode=mode)

        if trader:
            try:
                if trader.event_position(b["event_ticker"]):
                    continue                           # already hold this game on Kalshi
                r = trader.buy_yes(b["ticker"], contracts, b["limit"])
                filled = float(r.get("fill_count") or 0)
                b.update(order_id=r.get("order_id"), filled=filled)
                if filled:
                    b.update(fill_price=float(r.get("average_fill_price") or b["limit"]),
                             fee_paid=round(float(r.get("average_fee_paid") or 0) * filled, 2))
            except Exception as e:
                print(f"Order failed for {b['ticker']}: {e}")
                notes.append(f"Order failed for {b['team']}: {str(e)[:120]}")
                b.update(filled=0, order_error=str(e)[:300])

        open_usd += b["cost"]
        today_usd += b["cost"]
        ledger.append(b)
        placed.append(b)

    save_ledger(ledger)
    if placed or settled or notes or os.environ.get("ALWAYS_POST") == "1":
        post(format_message(now, placed, settled, ledger, mode, notes))
    else:
        print("Nothing new; not posting.")


if __name__ == "__main__":
    main()
