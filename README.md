# Kalshi Sports Bot → Discord

Three times a day, this bot compares Kalshi's **"who wins the game"** prices (NFL, college football, NBA, WNBA, MLB, NHL) with moneylines from a dozen or so US sportsbooks.

1. For each game, it removes each sportsbook's built-in margin and takes the median across books. The result is a **fair win probability**.
2. A team's Kalshi YES contract pays $1 if that team wins. If it costs less than the fair probability, minus Kalshi's fee and a safety margin (`MIN_EDGE`, 3¢ by default), the bot buys it.
3. It posts each bet to Discord. Every signal is also **paper-tracked at the real Kalshi price**. When the game settles, the bot records the P&L, and each post shows the running record: win rate, P&L and ROI.

By default it trades on **Kalshi's demo exchange, which uses fake money**. Real-money trading needs two extra switches; see the bottom of this page.

> **The honest part:** Sportsbook lines are usually efficient, and Kalshi sports markets mostly follow them. Real 3¢+ gaps are uncommon and short-lived, and a run three times a day will catch few of them. Many "edges" are just stale odds or a line that moved on news. Treat the paper record as the experiment. Expect it to need **100+ settled bets** before the win rate and ROI mean anything. Not financial advice.

## Setup (≈15 minutes)

All secrets go in the repo under **Settings → Secrets and variables → Actions**.

1. **Odds API key (free):** sign up at <https://the-odds-api.com>. The free tier includes 500 credits a month. Add it as the secret `ODDS_API_KEY`.
2. **Discord webhook:** in your Discord server, go to Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Add it as the secret `DISCORD_WEBHOOK_URL`. Leave it out and the bot prints to the Actions log instead.
3. **Test in paper mode:** go to Actions → **Kalshi scan** → **Run workflow**. A post appears in Discord. No Kalshi account is needed yet.
4. **Demo trading (fake money):**
   - Create an account at <https://demo.kalshi.co>. It is separate from your real Kalshi account.
   - Go to Account → API Keys → Create key. Save the downloaded private key file and copy the Key ID.
   - Add the secret `KALSHI_API_KEY_ID` with the Key ID.
   - Add the secret `KALSHI_PRIVATE_KEY` with the **whole** private key file, including the `-----BEGIN…` and `-----END…` lines.
   - Under the **Variables** tab, add `TRADING_ENABLED` = `1`.

After that, it runs automatically at 11am, 5pm and 7:30pm ET. To stop trading at any time, delete `TRADING_ENABLED` or set it to `0`. This is the kill switch. The bot then goes back to paper-only mode.

## How orders work
- An order is an **immediate-or-cancel buy at a limit price**. The limit is the highest price that still clears `MIN_EDGE`. An order never rests on the book or chases the price.
- The bot places at most one bet per game and never adds to a game you already hold on Kalshi.
- Fills, fill prices and fees are saved in `bets.json`, which the workflow commits back to the repo.
- Market data always comes from real Kalshi prices. The demo exchange's order book is thin and not realistic, so many demo orders simply won't fill. The paper record is the number to judge the strategy by.

## Safety limits (Actions → Variables)
| Variable | Default | Meaning |
| - | - | - |
| `TRADING_ENABLED` | off | `1` places orders; anything else = paper only |
| `MAX_BET_USD` | 10 | max cost of one bet |
| `MAX_DAILY_USD` | 50 | max new money per day |
| `MAX_OPEN_USD` | 100 | max money tied up in unsettled bets |
| `MIN_EDGE` | 0.03 | required edge after fees, in $ per contract |
| `SPORTS` | `nfl,ncaaf,nba,wnba,mlb,nhl` | which leagues to scan |

These settings can only be changed in `kalshi_bot.py`: `MAX_EDGE` (0.15; bigger edges are skipped because they're almost always a data error), `MIN_PRICE`/`MAX_PRICE` (0.15–0.85), `MIN_BOOKS` (3), `LOOKAHEAD_HOURS` (24) and `MAX_BETS_PER_RUN` (5).

**Odds API credits:** each run costs 1 credit for each sport that has a game in the next day. Sports with no games coming up are skipped for free. If you get close to 500 a month, cut down `SPORTS` or remove a cron line.

## Real money
Don't switch until the paper record has 100+ settled bets with positive ROI. When you do switch:
- Create API keys on your real Kalshi account and replace the two Kalshi secrets.
- Set the variables `KALSHI_ENV` = `prod` and `CONFIRM_REAL_MONEY` = `yes`.
- Keep the caps small.

## Run locally
```
pip install -r requirements.txt pytest
python -m pytest -q
ODDS_API_KEY=... ALWAYS_POST=1 python kalshi_bot.py        # paper mode, prints instead of posting
```
