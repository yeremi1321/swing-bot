"""Offline tests: python -m pytest -q"""
import json
from datetime import datetime, timezone

import pytest

import kalshi_bot as kb

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


def market(ticker, team, ask, result=""):
    return {
        "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "yes_sub_title": team,
        "yes_ask_dollars": f"{ask:.4f}", "yes_ask_size_fp": "500.00", "result": result,
    }


def book(home_price, away_price, home="Los Angeles Rams", away="Buffalo Bills"):
    return {"markets": [{"key": "h2h", "outcomes": [
        {"name": home, "price": home_price}, {"name": away, "price": away_price}]}]}


def game(books, start="2026-10-13T00:15:00Z"):
    return {"home_team": "Los Angeles Rams", "away_team": "Buffalo Bills",
            "commence_time": start, "bookmakers": books}


EVENT_MARKETS = [
    market("KXNFLGAME-26OCT12BUFLAR-LAR", "Los Angeles R", 0.58),
    market("KXNFLGAME-26OCT12BUFLAR-BUF", "Buffalo", 0.36),
    market("KXNFLGAME-26OCT11BALATL-BAL", "Baltimore", 0.66),
    market("KXNFLGAME-26OCT11BALATL-ATL", "Atlanta", 0.35),
]


class FakeData:
    def __init__(self, markets):
        self._markets = markets

    def markets(self, series_ticker=None, status=None, tickers=None):
        if tickers:
            wanted = tickers.split(",")
            return [m for m in self._markets if m["ticker"] in wanted]
        return [m for m in self._markets if m["ticker"].startswith(series_ticker or "")]

    def series(self, ticker):
        return {"fee_multiplier": 1}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "LEDGER_FILE", str(tmp_path / "bets.json"))
    monkeypatch.setattr(kb, "WEBHOOK", "")
    monkeypatch.setattr(kb, "TRADING_ENABLED", False)
    monkeypatch.setattr(kb, "SPORTS_ENABLED", ["nfl"])


def test_fair_probs_removes_vig():
    fair, n = kb.fair_probs(game([book(1.80, 2.10)] * 3))
    assert n == 3
    assert fair["Los Angeles Rams"] + fair["Buffalo Bills"] == pytest.approx(1)
    assert fair["Los Angeles Rams"] == pytest.approx((1 / 1.8) / (1 / 1.8 + 1 / 2.1))


def test_fair_probs_needs_enough_books():
    assert kb.fair_probs(game([book(1.8, 2.1)] * 2))[0] is None


def test_name_matching():
    assert kb.name_matches("Los Angeles R", "Los Angeles Rams")
    assert kb.name_matches("San Diego St.", "San Diego State Aztecs")
    assert not kb.name_matches("Los Angeles R", "Los Angeles Chargers")


def test_ticker_date():
    assert kb.ticker_date("KXNFLGAME-26OCT12BUFLAR").isoformat() == "2026-10-12"
    assert kb.ticker_date("KXMLBGAME-26OCT071800LADATL").isoformat() == "2026-10-07"


def test_match_game_pairs_teams_correctly():
    pair = kb.match_game(game([]), kb.kalshi_events(EVENT_MARKETS))
    assert pair["Los Angeles Rams"]["ticker"].endswith("-LAR")
    assert pair["Buffalo Bills"]["ticker"].endswith("-BUF")


def test_match_game_rejects_wrong_date():
    assert kb.match_game(game([], start="2026-10-20T00:15:00Z"), kb.kalshi_events(EVENT_MARKETS)) is None


def test_finds_underpriced_team():
    # Books: Buffalo ~41.5% fair; Kalshi asks 36c -> ~3.9c edge after fees
    g = game([book(1.68, 2.35)] * 4)
    bets = kb.find_bets("nfl", [g], kb.kalshi_events(EVENT_MARKETS), 1, NOW, set())
    assert len(bets) == 1
    b = bets[0]
    assert b["team"] == "Buffalo Bills" and b["price"] == 0.36
    assert b["edge"] >= kb.MIN_EDGE
    assert b["price"] <= b["limit"] < b["fair"]


def test_no_bet_when_fairly_priced():
    g = game([book(1.70, 2.75)] * 4)   # Buffalo fair ~38%, Kalshi 36c: under fees + margin
    assert kb.find_bets("nfl", [g], kb.kalshi_events(EVENT_MARKETS), 1, NOW, set()) == []


def test_skips_suspiciously_large_edge():
    g = game([book(3.0, 1.45)] * 4)    # Buffalo "fair" 67% vs 36c: almost surely bad data
    assert kb.find_bets("nfl", [g], kb.kalshi_events(EVENT_MARKETS), 1, NOW, set()) == []


def test_skips_games_already_started_or_far_away():
    events = kb.kalshi_events(EVENT_MARKETS)
    started = game([book(1.68, 2.35)] * 4, start="2026-10-12T17:55:00Z")
    assert kb.find_bets("nfl", [started], events, 1, NOW, set()) == []


def test_main_paper_bets_then_settles():
    odds = lambda key: [game([book(1.68, 2.35)] * 4)]
    kb.main(data=FakeData(EVENT_MARKETS), odds_fetcher=odds, now=NOW)
    ledger = json.load(open(kb.LEDGER_FILE))
    assert len(ledger) == 1 and ledger[0]["status"] == "open" and ledger[0]["mode"] == "paper"
    assert ledger[0]["cost"] <= kb.MAX_BET_USD

    # Second run: same game is not bet twice; Buffalo wins -> settles
    won = [dict(m, result="yes" if m["ticker"].endswith("BUF") else "no") for m in EVENT_MARKETS]
    kb.main(data=FakeData(won), odds_fetcher=odds, now=NOW)
    ledger = json.load(open(kb.LEDGER_FILE))
    assert len(ledger) == 1
    b = ledger[0]
    assert b["status"] == "settled" and b["result"] == "yes"
    assert b["paper_pnl"] == pytest.approx(
        b["contracts"] * (1 - 0.36) - kb.taker_fee(0.36, b["contracts"]))


def test_caps_limit_spending(monkeypatch):
    monkeypatch.setattr(kb, "MAX_DAILY_USD", 5)
    odds = lambda key: [game([book(1.68, 2.35)] * 4)]
    kb.main(data=FakeData(EVENT_MARKETS), odds_fetcher=odds, now=NOW)
    assert json.load(open(kb.LEDGER_FILE))[0]["cost"] <= 5


def test_refuses_prod_without_confirmation(monkeypatch):
    monkeypatch.setattr(kb, "TRADING_ENABLED", True)
    monkeypatch.setattr(kb, "KALSHI_ENV", "prod")
    monkeypatch.setattr(kb, "CONFIRM_REAL_MONEY", False)
    with pytest.raises(SystemExit):
        kb.make_trader()


def test_signature_headers_verify():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    import base64

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    k = kb.Kalshi("https://external-api.demo.kalshi.co/trade-api/v2", "kid", key)
    h = k._headers("GET", "/portfolio/balance")
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/v2/portfolio/balance").encode()
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
