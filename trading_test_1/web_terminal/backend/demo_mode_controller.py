"""
demo_mode_controller.py

Toggles the web terminal between LIVE and DEMO trading, entirely through
runtime patches applied from outside - buy_logic.py, sell_logic.py, and
db_utils.py are never edited. See the plan doc for full reasoning; summary:

  1. DB redirect - db_utils.get_db_connection is reassigned (the module
     attribute, not the file) to a version pointing at the silver2_demo_db
     schema instead of production. Every existing db_utils function calls
     get_db_connection() fresh each time it needs a connection, so this
     transparently redirects every read/write with zero source edits.

  2. Order simulation - buy_logic.kite and sell_logic.kite are
     already-constructed KiteApp instances (built at those modules' own
     import time). Their place_order()/orders() methods are overridden at
     the instance level (not the class) to simulate an instant "COMPLETE"
     fill instead of calling the real broker. Everything else on those
     instances (VARIETY_REGULAR, margins(), etc.) is untouched. Order IDs
     are synthetic (DEMO-<timestamp>-<counter>) so nothing in the demo
     tables is ever mistaken for a real broker ID.

  3. State persists to a small local JSON file, not the database (a
     DB-stored flag can't resolve which DB to check before you've decided
     which DB to use) - read on startup BEFORE these patches are applied,
     so the mode survives any restart, not just an intentional toggle.

indicator_module.py's price_samples table is intentionally NOT redirected
here - it's shared market history, identical in both modes (see the
comment in that module).

margins()/profile() calls elsewhere (websocket_utils.py's own kite
instance, token_health.py) are untouched and continue to reflect the real
account - they're read-only telemetry, not something demo mode needs to
fake, and leaving them alone means the Kite-session-health banner keeps
working normally in demo mode too.
"""

import os
import json
import time
import logging
import threading

from mysql.connector import pooling

import db_utils
import buy_logic
import sell_logic
from websocket_utils import get_current_price

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_mode_state.json")

_original_get_db_connection = db_utils.get_db_connection
_demo_pool = None
_demo_pool_lock = threading.Lock()

_order_counter = 0
_order_counter_lock = threading.Lock()

_active = False


def is_active():
    return _active


# ---------------------------------------------------------------------------
# DB redirect
# ---------------------------------------------------------------------------

def _demo_dbconfig():
    cfg = dict(db_utils.dbconfig)
    cfg["database"] = "trading_demo_db"
    return cfg


def _get_demo_pool():
    global _demo_pool
    with _demo_pool_lock:
        if _demo_pool is None:
            _demo_pool = pooling.MySQLConnectionPool(
                pool_name="silver_demo_pool", pool_size=5, **_demo_dbconfig()
            )
        return _demo_pool


def _demo_get_db_connection():
    try:
        return _get_demo_pool().get_connection()
    except Exception as e:
        print(f"\u26a0\ufe0f Demo DB connection pool error: {e}")
        raise


# ---------------------------------------------------------------------------
# Order simulation
# ---------------------------------------------------------------------------

def _next_demo_order_id():
    global _order_counter
    with _order_counter_lock:
        _order_counter += 1
        return f"DEMO-{int(time.time())}-{_order_counter}"


def _install_order_patch(kite_instance, label):
    """Overrides place_order()/orders() on a single, already-constructed
    KiteApp instance. Instance-scoped state (via closure), so buy_logic's
    and sell_logic's separate kite objects never cross-talk."""

    state = {"order_id": None, "fill_price": None}

    def fake_place_order(**kwargs):
        order_id = _next_demo_order_id()
        state["order_id"] = order_id
        # sell_logic.py passes an explicit target price (its computed
        # sell target); buy_logic.py is a pure market order with no price
        # kwarg, so fall back to the live tick price - simulating "filled
        # at current market price", same as a real market order would be.
        if kwargs.get("price") is not None:
            fill_price = kwargs["price"]
        else:
            try:
                fill_price = get_current_price()
            except Exception:
                fill_price = 0
        state["fill_price"] = fill_price
        logging.info(
            f"[DEMO MODE] Simulated {kwargs.get('transaction_type')} order via "
            f"{label}.kite: {order_id}, qty={kwargs.get('quantity')}, fill~{fill_price}"
        )
        return order_id

    def fake_orders():
        if state["order_id"] is None:
            return []
        return [{
            "order_id": state["order_id"],
            "status": "COMPLETE",
            "average_price": state["fill_price"],
        }]

    kite_instance.place_order = fake_place_order
    kite_instance.orders = fake_orders


def _uninstall_order_patch(kite_instance):
    """Removes the instance-level override, so attribute lookup falls
    back to the real KiteApp.place_order/orders methods again."""
    for attr in ("place_order", "orders"):
        if attr in kite_instance.__dict__:
            del kite_instance.__dict__[attr]


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

def enable():
    global _active
    db_utils.get_db_connection = _demo_get_db_connection
    _install_order_patch(buy_logic.kite, "buy_logic")
    _install_order_patch(sell_logic.kite, "sell_logic")
    _active = True
    logging.warning("DEMO MODE is now ACTIVE - orders are simulated, DB writes go to silver2_demo_db")


def disable():
    global _active
    db_utils.get_db_connection = _original_get_db_connection
    _uninstall_order_patch(buy_logic.kite)
    _uninstall_order_patch(sell_logic.kite)
    _active = False
    logging.warning("DEMO MODE is now OFF - back to LIVE trading against the real account/schema")


# ---------------------------------------------------------------------------
# Persisted state (survives restarts, not just intentional toggles)
# ---------------------------------------------------------------------------

def _read_state_file():
    try:
        with open(STATE_FILE, "r") as f:
            return bool(json.load(f).get("active", False))
    except FileNotFoundError:
        return False
    except Exception as e:
        logging.error(f"demo_mode_controller: could not read state file, defaulting to LIVE: {e}")
        return False


def _write_state_file(active):
    with open(STATE_FILE, "w") as f:
        json.dump({"active": bool(active)}, f)


def apply_startup_state():
    """Call once, early in app.py - after buy_logic/sell_logic/db_utils
    are already imported, before the trading loop thread starts - to
    restore whichever mode was last set. Reads from disk, so this holds
    across ANY restart: an intentional toggle, a crash, a server reboot,
    or a manual `systemctl restart` - never silently defaults to live."""
    active = _read_state_file()
    if active:
        enable()
    logging.info(f"Started in {'DEMO' if active else 'LIVE'} mode (loaded from {STATE_FILE})")
    return active


def set_active(active):
    """Called from the API endpoint. Writes the new state to disk BEFORE
    the caller triggers a restart, so the next startup picks it up
    correctly regardless of exactly when the restart actually happens."""
    _write_state_file(active)
    if active:
        enable()
    else:
        disable()

