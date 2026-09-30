# Swing Trade Scanner → Discord

Every weekday after the close, this bot scans ~100 liquid US stocks for two setups and posts the best ones to Discord:

- **Breakout:** the stock is in an uptrend and closes at a new 20-day high on at least 1.5× normal volume.
- **Pullback:** the stock is in an uptrend, dips until RSI falls below 40, then starts bouncing back.

Each pick includes an entry price, a stop (2× ATR below entry), a target (2:1 reward-to-risk), and a position size. The size is set so that hitting the stop loses about `RISK_PCT`% of your account.

The bot also tracks every pick until it hits its stop or target, or until 15 trading days pass. It saves the results to `picks.json` in this repo and shows its win rate in each post.

*Rule-based signals, not financial advice. Paper-trade it for 30–50 closed picks before using real money.*

The bot runs free on GitHub Actions, so it needs no server and no Render plan.

## Setup
1. **Discord webhook:** in your Discord server, go to Server Settings → Integrations → Webhooks → New Webhook. Pick a channel, then click **Copy Webhook URL**.
2. **Push this folder** to a new GitHub repo. Keep the `.github/workflows` folder.
3. **Add the webhook as a secret:** in the repo, go to Settings → Secrets and variables → Actions → **New repository secret**. Name it `DISCORD_WEBHOOK_URL` and paste the URL.
4. *(Optional)* On the same page, open the **Variables** tab and add `ACCOUNT_SIZE`, your account size in dollars. The default is 5000.
5. **Test it:** go to the Actions tab → Swing scan → **Run workflow**. A post should appear in Discord within about a minute.

After that, it runs by itself every weekday at 21:30 UTC. GitHub sometimes starts scheduled jobs a few minutes late.

## Settings
Set these in `swing_bot.py` or as environment variables in the workflow: `RISK_PCT` (default 1), `MAX_PICKS` (default 5), `MAX_HOLD_DAYS` (default 15).
To change which stocks it scans, add a `tickers.txt` file with one ticker per line.

## Run locally
```
pip install -r requirements.txt
DISCORD_WEBHOOK_URL="your-url" FORCE_RUN=1 python swing_bot.py
```
