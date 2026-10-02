"""
app.py - Web terminal backend.

Imports the existing, UNMODIFIED trading bot modules (trade_logic, db_utils,
websocket_utils, etc.) from TRADING_BOT_DIR and exposes them over a REST + WebSocket
API, plus serves the frontend. Run this INSTEAD of the Tkinter main.py - it starts
the same trading loop, just controlled from a browser instead of a desktop window.
"""
import os
import sys
import json
import asyncio
import logging
from decimal import Decimal
from datetime import date, datetime
from typing import List

from dotenv import load_dotenv

load_dotenv()

# --- Make the existing bot's modules importable, unmodified ---
TRADING_BOT_DIR = os.environ.get("TRADING_BOT_DIR")
if not TRADING_BOT_DIR or not os.path.isdir(TRADING_BOT_DIR):
    raise RuntimeError(
        "Set TRADING_BOT_DIR in your .env to the folder containing trade_logic.py, "
        "db_utils.py, etc. (your existing Silver_2 folder)."
    )
sys.path.insert(0, TRADING_BOT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # this backend/ dir

from fastapi import FastAPI, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect, status
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

import auth
from bot_controller import controller
from trading_loop import start_trading_loop_thread
from token_health import token_health_loop
from kite_token_refresher import refresh_enctoken

# --- Unmodified bot modules ---
from db_utils import (
    fetch_open_buy_transactions,
    fetch_all_transactions,
    fetch_user_settings,
    save_user_settings,
    store_manual_action,
    get_active_positions,
    get_profit,
    get_trading_symbol,
    save_trading_symbol,
    update_sellable_price_in_db,
    fetch_buy_price,
    get_drop_mode,
    set_drop_mode,
)
from websocket_utils import get_current_price, setup_websocket, set_ltp_mode, enctoken

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Demo mode (see plan doc) - applies the persisted LIVE/DEMO state
# BEFORE the FastAPI app is created and before the trading loop thread
# starts, so a restart never leaves the running process in an ambiguous
# mode. Safe to call here: buy_logic.py/sell_logic.py/db_utils.py were
# already imported above (transitively, via trading_loop -> trade_logic).
import demo_mode_controller
demo_mode_controller.apply_startup_state()

# Read-only usage - these are the two modules' own exposed values/functions,
# used here only to surface live dynamic-drop-% state in the UI. See plan
# doc: trade_logic.last_dynamic_drop_used is documented as read-only from
# outside trade_logic.py, and indicator_module.get_k() has no side effects
# beyond its own internal cache.
import trade_logic
import indicator_module

app = FastAPI(title="Silver Trading Bot - Web Terminal")

BROADCAST_INTERVAL_SECONDS = float(os.environ.get("BROADCAST_INTERVAL_SECONDS", "1"))


# ---------------------------------------------------------------------------
# Startup: connect the price websocket + start the (paused) trading loop
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    def _connect():
        try:
            kws = setup_websocket(enctoken)
            set_ltp_mode(kws)
            logging.info("Kite price WebSocket connected")
        except Exception as e:
            logging.error(f"Failed to connect Kite price WebSocket: {e}")
            controller.add_log_message("ERROR", f"Failed to connect price feed: {e}")

    await asyncio.get_event_loop().run_in_executor(None, _connect)
    start_trading_loop_thread()  # bot stays STOPPED until /api/bot/start is called
    asyncio.create_task(token_health_loop())
    controller.add_log_message("INFO", "Web terminal backend started")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post("/api/login")
async def login(response: Response, form: OAuth2PasswordRequestForm = Depends()):
    if not auth.authenticate(form.username, form.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    token = auth.create_access_token(form.username)
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        httponly=True,
        secure=auth.SESSION_COOKIE_SECURE,
        samesite="lax",
        # No max_age/expires: this makes it a session cookie, so the browser
        # deletes it when the browser itself is fully closed (not just a tab),
        # rather than surviving on disk until JWT_EXPIRE_MINUTES passes.
        path="/",
    )
    # Also returned in the body so curl/scripts can use a Bearer header if preferred.
    return {"access_token": token, "token_type": "bearer"}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(user: str = Depends(auth.get_current_user)):
    """Lets the frontend silently check 'am I still logged in?' on page load."""
    return {"username": user}


class PasswordVerify(BaseModel):
    password: str


@app.post("/api/verify-password")
async def verify_password(payload: PasswordVerify, user: str = Depends(auth.get_current_user)):
    """Used to unlock action buttons in the frontend's locked/view-only mode.
    Requires an already-valid login session AND the password again - the page
    always loads locked, and unlocking never persists across a refresh."""
    if not auth.verify_admin_password(payload.password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Status / positions / trade history
# ---------------------------------------------------------------------------
def _decimal_safe(value):
    """Converts DB types that plain json.dumps() can't handle (Decimal, datetime,
    date) into JSON-safe equivalents. FastAPI's normal REST responses handle these
    automatically, but websocket.send_json() uses plain json.dumps() under the
    hood and does not - so every value going out over /ws needs to pass through
    this first."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _serialize_row(row):
    return [_decimal_safe(v) for v in row]


def _safe_drop_mode():
    try:
        return get_drop_mode()
    except Exception:
        logging.exception("Failed to read drop mode")
        return None


def _dynamic_drop_stats(current_price):
    """Live values for the Settings-tab 'Next drop % (dynamic)' / 'k factor
    in use' cards. Both are read-only, informational: trade_logic's own
    subsequent-buy branch is the only place that actually acts on them.
    Returns (next_dynamic_drop_percentage, k_factor_in_use), either of
    which may be None (e.g. before the first tick, or before ATR/ROC have
    enough data yet)."""
    try:
        raw_drop = trade_logic.last_dynamic_drop_used
        next_dynamic_drop_percentage = _decimal_safe(raw_drop * 100) if raw_drop is not None else None
    except Exception:
        logging.exception("Failed to read trade_logic.last_dynamic_drop_used")
        next_dynamic_drop_percentage = None

    try:
        raw_k = indicator_module.get_k(current_price) if current_price else None
        k_factor_in_use = _decimal_safe(raw_k) if raw_k is not None else None
    except Exception:
        logging.exception("Failed to compute indicator_module.get_k()")
        k_factor_in_use = None

    return next_dynamic_drop_percentage, k_factor_in_use


@app.get("/api/status")
async def get_status(user: str = Depends(auth.get_current_user)):
    try:
        price = get_current_price()
    except Exception:
        price = None
    try:
        symbol = get_trading_symbol("symbol_name")
    except Exception:
        symbol = None
    next_dynamic_drop_percentage, k_factor_in_use = _dynamic_drop_stats(price)
    return {
        "bot_status": controller.bot_status,
        "bot_running": controller.is_running(),
        "current_price": price,
        "symbol": symbol,
        "active_position_qty": _decimal_safe(get_active_positions()),
        "profit_today": _decimal_safe(get_profit("today")),
        "profit_all": _decimal_safe(get_profit("all")),
        "token_expired": controller.token_expired,
        "demo_mode_active": demo_mode_controller.is_active(),
        "next_dynamic_drop_percentage": next_dynamic_drop_percentage,
        "k_factor_in_use": k_factor_in_use,
        "drop_mode": _safe_drop_mode(),
    }


def _positions_with_unrealized(current_price):
    rows = fetch_open_buy_transactions() or []
    columns = ["buy_id", "buy_price", "buy_time", "sellable_price", "expected_profit_loss", "buy_quantity"]
    positions = [dict(zip(columns, _serialize_row(r))) for r in rows]
    for p in positions:
        if current_price:
            p["unrealized_pnl"] = round((float(current_price) - p["buy_price"]) * p["buy_quantity"], 2)
        else:
            p["unrealized_pnl"] = None
    return positions


@app.get("/api/positions")
async def get_positions(user: str = Depends(auth.get_current_user)):
    try:
        price = get_current_price()
    except Exception:
        price = None
    return _positions_with_unrealized(price)


@app.get("/api/trades")
async def get_trades(limit: int = 100, user: str = Depends(auth.get_current_user)):
    rows = fetch_all_transactions() or []
    columns = ["id", "buy_price", "buy_time", "sell_price", "sell_time", "profit_loss", "quantity"]
    trades = [dict(zip(columns, _serialize_row(r))) for r in rows]
    return trades[-limit:][::-1]


@app.get("/api/logs")
async def get_logs(limit: int = 100, user: str = Depends(auth.get_current_user)):
    return controller.recent_logs(limit)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class Settings(BaseModel):
    drop_percentage: float
    profit_percentage: float
    first_buy_percentage: float
    max_purchases: int
    quantity: int


@app.get("/api/settings")
async def read_settings(user: str = Depends(auth.get_current_user)):
    drop_pct, profit_pct, first_buy_pct, max_purchases, quantity = fetch_user_settings()
    return {
        "drop_percentage": _decimal_safe(drop_pct),
        "profit_percentage": _decimal_safe(profit_pct),
        "first_buy_percentage": _decimal_safe(first_buy_pct),
        "max_purchases": max_purchases,
        "quantity": quantity,
    }


@app.post("/api/settings")
async def write_settings(settings: Settings, user: str = Depends(auth.get_current_user)):
    if not (0 < settings.drop_percentage < 100 and 0 < settings.profit_percentage < 100
            and 0 < settings.first_buy_percentage < 100):
        raise HTTPException(status_code=400, detail="Percentage values must be between 0 and 100")
    if settings.max_purchases <= 0 or settings.quantity <= 0:
        raise HTTPException(status_code=400, detail="max_purchases and quantity must be greater than 0")

    save_user_settings(
        settings.drop_percentage,
        settings.profit_percentage,
        settings.first_buy_percentage,
        settings.max_purchases,
        settings.quantity,
    )
    controller.add_log_message("SUCCESS", "Trading settings updated")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Bot control + manual actions (writes the same manual_actions table the
# original Tkinter controller used - trade_logic.py picks these up itself)
# ---------------------------------------------------------------------------
@app.post("/api/bot/start")
async def start_bot(user: str = Depends(auth.get_current_user)):
    if controller.token_expired:
        raise HTTPException(status_code=400, detail="Kite session expired - refresh the token before starting the bot")
    controller.start_bot()
    return {"bot_status": controller.bot_status}


@app.post("/api/bot/stop")
async def stop_bot(user: str = Depends(auth.get_current_user)):
    controller.manual_stop_bot()
    return {"bot_status": controller.bot_status}


@app.post("/api/manual/buy")
async def manual_buy(user: str = Depends(auth.get_current_user)):
    price = get_current_price()
    if not price or price <= 0:
        raise HTTPException(status_code=400, detail="Cannot get current price")
    settings = fetch_user_settings()
    if not settings:
        raise HTTPException(status_code=400, detail="Save trading settings first")
    store_manual_action("BUY")
    controller.add_log_message("INFO", f"Manual buy requested at {price}")
    return {"ok": True, "price": price}


@app.post("/api/manual/sell_all")
async def manual_sell_all(user: str = Depends(auth.get_current_user)):
    positions = fetch_open_buy_transactions() or []
    if not positions:
        raise HTTPException(status_code=400, detail="No open positions to sell")
    store_manual_action("SELL_ALL")
    controller.add_log_message("INFO", f"Manual sell-all requested for {len(positions)} position(s)")
    return {"ok": True, "count": len(positions)}


@app.post("/api/manual/sell_one/{buy_id}")
async def manual_sell_one(buy_id: int, user: str = Depends(auth.get_current_user)):
    positions = fetch_open_buy_transactions() or []
    if not any(p[0] == buy_id for p in positions):
        raise HTTPException(status_code=404, detail="Position not found")
    store_manual_action(f"SELL_ONE:{buy_id}")
    controller.add_log_message("INFO", f"Manual sell requested for position #{buy_id}")
    return {"ok": True, "buy_id": buy_id}


# ---------------------------------------------------------------------------
# Edit target sell price for an open position
#
# Uses db_utils.update_sellable_price_in_db() directly - the same function
# trade_logic.py's own trailing-stop logic uses. trade_logic.py re-reads the
# sellable price from the DB on every loop tick and syncs its in-memory
# trailing-stop object to match, so an edit here is picked up automatically
# within one loop interval - no restart needed, and it can't be silently
# overwritten by stale in-memory state.
# ---------------------------------------------------------------------------
class TargetPriceUpdate(BaseModel):
    sellable_price: float


@app.put("/api/positions/{buy_id}/target")
async def update_target_price(buy_id: int, payload: TargetPriceUpdate, user: str = Depends(auth.get_current_user)):
    positions = fetch_open_buy_transactions() or []
    match = next((p for p in positions if p[0] == buy_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Position not found")
    if payload.sellable_price <= 0:
        raise HTTPException(status_code=400, detail="Target price must be greater than 0")

    buy_price = match[1]
    buy_quantity = match[5]

    update_sellable_price_in_db(buy_id, Decimal(str(payload.sellable_price)), buy_price, buy_quantity)
    controller.add_log_message(
        "SUCCESS", f"Target sell price for #{buy_id} manually set to {payload.sellable_price}"
    )
    return {"ok": True, "buy_id": buy_id, "sellable_price": payload.sellable_price}


# ---------------------------------------------------------------------------
# Next buy price (what the bot has calculated as its next buy trigger)
# ---------------------------------------------------------------------------
@app.get("/api/next-buy-price")
async def next_buy_price(user: str = Depends(auth.get_current_user)):
    price = fetch_buy_price()
    return {"next_buy_price": _decimal_safe(price) if price is not None else None}


# ---------------------------------------------------------------------------
# Kite session / token refresh
#
# Password + TOTP are accepted only in the request body of this one call,
# used in-memory to drive the Selenium login, and never written to disk,
# a database, or a log line. After a successful refresh the process restarts
# itself (via systemd if installed, otherwise it exits and must be restarted
# manually) because buy_logic.py/sell_logic.py/websocket_utils.py each build
# their own KiteApp instance from the token at import time - a running
# process cannot pick up a new token without reloading those modules.
# ---------------------------------------------------------------------------
KITE_USERNAME = os.environ.get("KITE_USERNAME", "")
KITE_TOKEN_FILE_PATH = os.environ.get(
    "KITE_TOKEN_FILE_PATH",
    "/home/ec2-user/Trade_algo_aws_v2/enctoken.txt",  # must match the path websocket_utils.py reads
)


class KiteRefreshRequest(BaseModel):
    password: str
    totp: str


@app.get("/api/kite/status")
async def kite_status(user: str = Depends(auth.get_current_user)):
    return {
        "token_expired": controller.token_expired,
        "refresh_in_progress": controller.token_refresh_in_progress,
    }


async def _restart_process_soon():
    await asyncio.sleep(1.5)  # let the HTTP response reach the browser first
    try:
        import subprocess
        subprocess.run(["systemctl", "restart", "silver-web-terminal"], check=True)
    except Exception:
        controller.add_log_message(
            "ERROR",
            "Could not auto-restart via systemd. Restart the backend manually "
            "(uvicorn app:app ...) to apply the new token.",
        )
        os._exit(1)  # if under systemd with Restart=on-failure this still recovers it


@app.post("/api/kite/refresh")
async def kite_refresh(payload: KiteRefreshRequest, user: str = Depends(auth.get_current_user)):
    if controller.token_refresh_in_progress:
        raise HTTPException(status_code=409, detail="A token refresh is already in progress")
    if not KITE_USERNAME:
        raise HTTPException(status_code=500, detail="KITE_USERNAME is not set in .env")

    controller.token_refresh_in_progress = True
    controller.add_log_message("INFO", "Kite token refresh requested from web terminal")
    try:
        success, message = await asyncio.get_event_loop().run_in_executor(
            None,
            refresh_enctoken,
            KITE_USERNAME,
            payload.password,
            payload.totp,
            KITE_TOKEN_FILE_PATH,
            controller.add_log_message,
        )
    finally:
        controller.token_refresh_in_progress = False

    if not success:
        controller.add_log_message("ERROR", f"Kite token refresh failed: {message}")
        raise HTTPException(status_code=400, detail=message)

    controller.mark_token_valid()
    asyncio.create_task(_restart_process_soon())
    return {"ok": True, "message": "Token refreshed. Backend is restarting to apply it - reconnect in a few seconds."}


# ---------------------------------------------------------------------------
# Trading instrument / symbol
#
# Order placement (buy_logic.py / sell_logic.py) reads the symbol fresh from
# the DB on every trade, so that part takes effect immediately. The price
# WebSocket subscription (set_ltp_mode) only runs once at startup though, so
# changing the instrument without a restart would leave prices streaming for
# the OLD instrument while new orders go out for the NEW one - a real
# mismatch, not just a display gap. So this also restarts the backend, same
# as the token refresh. Blocked entirely while there are open positions,
# since buy_transactions rows don't carry a symbol - switching underneath
# them would leave the bot managing stale positions against the wrong price.
# ---------------------------------------------------------------------------
class InstrumentUpdate(BaseModel):
    exchange: str
    symbol_name: str
    instrument_code: int


@app.get("/api/instrument")
async def get_instrument(user: str = Depends(auth.get_current_user)):
    exchange, symbol_name, instrument_code = get_trading_symbol()
    return {"exchange": exchange, "symbol_name": symbol_name, "instrument_code": instrument_code}


@app.put("/api/instrument")
async def update_instrument(payload: InstrumentUpdate, user: str = Depends(auth.get_current_user)):
    if controller.is_running():
        raise HTTPException(status_code=400, detail="Stop the bot before changing the instrument")

    positions = fetch_open_buy_transactions() or []
    if positions:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot change instrument with {len(positions)} open position(s). Close them first.",
        )
    if not payload.exchange or not payload.symbol_name or not payload.instrument_code:
        raise HTTPException(status_code=400, detail="Exchange, symbol name, and instrument code are all required")

    save_trading_symbol(payload.exchange, payload.symbol_name, payload.instrument_code)
    controller.add_log_message(
        "SUCCESS",
        f"Trading instrument changed to {payload.exchange}:{payload.symbol_name} "
        f"(token {payload.instrument_code}). Restarting to reconnect the price feed.",
    )
    asyncio.create_task(_restart_process_soon())
    return {"ok": True, "message": "Instrument updated. Backend is restarting to apply it - reconnect in a few seconds."}


# ---------------------------------------------------------------------------
# Demo mode (see plan doc / demo_mode_controller.py)
#
# Kept as a restart-triggering setting, same as the token/instrument
# changes above, for the same reason: the mechanism needs to be applied
# consistently from a clean process start rather than hot-swapped
# mid-request. The new mode is persisted to disk BEFORE the restart is
# issued, so apply_startup_state() picks it up correctly on the way back
# up regardless of exactly when the restart lands.
# ---------------------------------------------------------------------------
class DemoModeUpdate(BaseModel):
    active: bool


@app.get("/api/demo-mode")
async def get_demo_mode(user: str = Depends(auth.get_current_user)):
    return {"active": demo_mode_controller.is_active()}


@app.put("/api/demo-mode")
async def update_demo_mode(payload: DemoModeUpdate, user: str = Depends(auth.get_current_user)):
    if controller.is_running():
        raise HTTPException(status_code=400, detail="Stop the bot before switching demo/live mode")

    if payload.active == demo_mode_controller.is_active():
        return {"ok": True, "message": f"Already in {'DEMO' if payload.active else 'LIVE'} mode."}

    demo_mode_controller.set_active(payload.active)
    controller.add_log_message(
        "SUCCESS" if payload.active else "INFO",
        f"Switched to {'DEMO' if payload.active else 'LIVE'} mode. Restarting to apply it cleanly.",
    )
    asyncio.create_task(_restart_process_soon())
    return {
        "ok": True,
        "message": f"Switched to {'DEMO' if payload.active else 'LIVE'} mode. "
                   f"Backend is restarting to apply it - reconnect in a few seconds.",
    }


# ---------------------------------------------------------------------------
# Static / dynamic drop mode. "static" = next subsequent buy uses the flat
# drop_percentage; trade_logic.py flips it back to "dynamic" once that buy fills.
# ---------------------------------------------------------------------------
class DropModeUpdate(BaseModel):
    mode: str


@app.get("/api/drop-mode")
async def read_drop_mode(user: str = Depends(auth.get_current_user)):
    return {"mode": get_drop_mode()}


@app.put("/api/drop-mode")
async def write_drop_mode(payload: DropModeUpdate, user: str = Depends(auth.get_current_user)):
    if payload.mode not in ("static", "dynamic"):
        raise HTTPException(status_code=400, detail="mode must be 'static' or 'dynamic'")
    if payload.mode == "static" and get_active_positions() < 2:
        raise HTTPException(status_code=400,
                            detail="Static mode only applies from the 3rd buy on (need at least 2 open positions)")
    set_drop_mode(payload.mode)
    controller.add_log_message(
        "INFO", "Static drop armed - next buy uses the flat drop %" if payload.mode == "static"
        else "Static drop cancelled - back to dynamic drop %")
    return {"ok": True, "mode": payload.mode}


# ---------------------------------------------------------------------------
# WebSocket: pushes live status/positions/logs to connected browsers
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, payload: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


async def _build_snapshot():
    try:
        price = get_current_price()
    except Exception:
        price = None

    try:
        rows = fetch_open_buy_transactions() or []
        columns = ["buy_id", "buy_price", "buy_time", "sellable_price", "expected_profit_loss", "buy_quantity"]
        positions = [dict(zip(columns, _serialize_row(r))) for r in rows]
    except Exception:
        logging.exception("Snapshot: fetch_open_buy_transactions failed")
        positions = []

    try:
        active_qty = _decimal_safe(get_active_positions())
    except Exception:
        logging.exception("Snapshot: get_active_positions failed")
        active_qty = None

    try:
        profit_today = _decimal_safe(get_profit("today"))
    except Exception:
        logging.exception("Snapshot: get_profit failed")
        profit_today = None

    try:
        next_buy = _decimal_safe(fetch_buy_price())
    except Exception:
        logging.exception("Snapshot: fetch_buy_price failed")
        next_buy = None

    next_dynamic_drop_percentage, k_factor_in_use = _dynamic_drop_stats(price)

    return {
        "type": "snapshot",
        "bot_status": controller.bot_status,
        "bot_running": controller.is_running(),
        "current_price": price,
        "active_position_qty": active_qty,
        "profit_today": profit_today,
        "positions": positions,
        "logs": controller.recent_logs(20),
        "token_expired": controller.token_expired,
        "next_buy_price": next_buy,
        "demo_mode_active": demo_mode_controller.is_active(),
        "next_dynamic_drop_percentage": next_dynamic_drop_percentage,
        "k_factor_in_use": k_factor_in_use,
        "drop_mode": _safe_drop_mode(),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    token = ws.query_params.get("token") or ws.cookies.get(auth.COOKIE_NAME)
    try:
        auth.get_user_from_ws_token(token) if token else (_ for _ in ()).throw(Exception("no token"))
    except Exception:
        await ws.close(code=4401)
        return

    await manager.connect(ws)
    try:
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)
            snapshot = await _build_snapshot()
            await ws.send_json(snapshot)
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        logging.exception("WebSocket loop crashed - closing this connection")
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Frontend (static files) - mounted last so it doesn't shadow /api or /ws
# ---------------------------------------------------------------------------
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
