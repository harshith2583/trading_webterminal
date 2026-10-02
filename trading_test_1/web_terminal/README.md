# Silver Bot - Web Terminal

A browser-based control panel for your existing trading bot. It does **not** modify
`trade_logic.py`, `buy_logic.py`, `sell_logic.py`, `trailing_stop_loss.py`,
`websocket_utils.py`, or `db_utils.py` in any way - it imports them exactly as they
are and runs the same trading loop your `main.py` ran, just controlled from a
webpage instead of the Tkinter window.

## How it fits together

```
web_terminal/
  backend/
    app.py                 # FastAPI app: REST API + WebSocket + serves the frontend
    auth.py                 # login / JWT
    bot_controller.py       # stand-in for the Tkinter "app" object trade_logic.py expects
    trading_loop.py         # headless version of main.py's trading loop
    generate_password_hash.py
    requirements.txt
    .env.example
  frontend/
    index.html               # the web terminal UI (vanilla JS, no build step)
  nginx.conf.example
```

You run `backend/app.py` **instead of** `main.py`. It does everything `main.py` did
(connect the price WebSocket, run the trading loop) plus serves the web UI.

## 1. Install

On your cloud server, alongside your existing `Silver_2` folder:

```bash
cd web_terminal/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:
- `TRADING_BOT_DIR` — absolute path to your existing `Silver_2` folder (the one with
  `trade_logic.py`, `db_utils.py`, `main.py` in it).
- `JWT_SECRET_KEY` — any long random string (`openssl rand -hex 32` works).
- `ADMIN_USERNAME` — whatever you want to log in with.
- `ADMIN_PASSWORD_HASH` — generate it:
  ```bash
  python generate_password_hash.py
  ```
  Paste the printed line into `.env`.

## 3. Run it

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Visit `http://your-server-ip:8000` — you should see the login screen. Log in with
the username/password you set. This confirms the wiring works before you bother
with SSL/Nginx.

For it to survive reboots and crashes, run it as a systemd service instead of a
manual `uvicorn` command — happy to write that unit file next if useful.

## 4. Put it behind Nginx + SSL (recommended before exposing it publicly)

`nginx.conf.example` is a starting point. It proxies both normal HTTP requests and
the `/ws` WebSocket path to the FastAPI process on `127.0.0.1:8000`.

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo cp nginx.conf.example /etc/nginx/sites-available/silver-web-terminal
# edit server_name to your real domain
sudo ln -s /etc/nginx/sites-available/silver-web-terminal /etc/nginx/sites-enabled/
sudo certbot --nginx -d your-domain.com
sudo systemctl reload nginx
```

Now visit `https://your-domain.com` — the login page should load over SSL, and the
dashboard should update in real time after you log in.

## What each screen does

- **Start bot / Stop bot** — sets/clears the same trading-loop switch your desktop
  "Start"/"Stop" buttons controlled.
- **Manual buy / Sell all / Sell (per position)** — writes to the same
  `manual_actions` table your Tkinter GUI wrote to. `trade_logic.py` picks these up
  on its own on the next loop tick, exactly as before.
- **Settings** — reads/writes the same `user_settings` table.
- **Positions / Trade history / Activity log** — read-only views over
  `buy_transactions` / `all_transactions` and the bot's own log messages.

## Known caveats carried over from the existing bot (not introduced by this terminal)

- `websocket_utils.py` reads your Kite `enctoken` from a hardcoded file path and
  makes a live `margins()` call at import time — if that token has expired, the
  backend will log an error on startup but keep running; you'll need to refresh the
  token file and restart the process, same as with the desktop app today.
- `db_utils.py` has the database password hardcoded in the source. Since this file
  is unchanged, that's still true here — worth rotating that credential and moving
  it to an environment variable when you get a chance, independent of this project.
- Trailing-stop state is still only rebuilt from `sellable_price` in the DB on
  restart, same as before — a mid-trail restart resets `first_increment_completed`,
  as discussed earlier.

## Natural next upgrades (once this is running)

- systemd service file so it auto-restarts on crash/reboot
- Telegram alert hook fed from the same `bot_controller.add_log_message` calls
- Equity-curve chart on the dashboard using `/api/trades`
- Read-only "viewer" login separate from the control login
