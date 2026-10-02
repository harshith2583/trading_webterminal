"""
indicator_module.py

Web-terminal-owned. Computes the live ROC / ATR(14) inputs that feed the
dynamic drop % k-table lookup used by trade_logic.py's dynamic-drop helper
(position 3 onward).

Does NOT modify buy_logic.py, sell_logic.py, db_utils.py's existing
functions, kite_trade.py, or websocket_utils.py.

Design notes (see plan doc for full context):

- Uses its own dedicated, read-only KiteApp instance - separate from
  buy_logic.kite / sell_logic.kite - built from the same already-loaded
  enctoken those two use (imported from websocket_utils, not re-read from
  disk). This means indicator readings are never affected by the
  demo-mode order-simulation monkey-patches applied to those two objects:
  ATR/ROC always reflect real market conditions in both live and demo
  mode.

- ROC is ROC(27) on 5-minute candle CLOSES (27 x 5 min = 135 min, the same
  window as the previous 9 x 15 min, so the k-table ROC thresholds keep
  their meaning). It is polled from Kite's historical_data(), uses only
  fully CLOSED candles (the still-forming candle is dropped), and includes
  any overnight/session gap that falls inside the window. It is refreshed
  once per 5-minute candle close and cached in-memory between polls. It is
  available immediately after a restart - no sample warm-up - and the
  price_samples table is no longer used.

- ATR(14) is polled from Kite's historical_data() (already implemented,
  untouched, in kite_trade.py) once per new 15-min candle close, not on
  every 5s trading-loop tick. Cached in-memory between polls.
"""

import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal

from websocket_utils import enctoken
from kite_trade import KiteApp
import db_utils  # only for get_trading_symbol(), a read-only lookup

CANDLE_INTERVAL_MINUTES = 15   # ATR candle size
ATR_PERIOD = 14

# ROC: 27 x 5-min candles = 135 min (same window as 9 x 15-min)
ROC_CANDLE_MINUTES = 5
ROC_PERIOD = 27
ROC_LOOKBACK_DAYS = 4            # enough history for 28 closed candles across weekends/holidays
ROC_PUBLISH_GRACE_SECONDS = 5    # wait this long after a 5-min boundary for Kite to publish the candle
ROC_RETRY_SECONDS = 30           # min gap between attempts after a failed/empty fetch

# ---------------------------------------------------------------------------
# k-table - EDIT THIS SECTION to change bucket boundaries or add buckets.
#
# HOW THE BUCKETS WORK
# ---------------------
# ATR_BUCKET_EDGES and ROC_BUCKET_EDGES are each a plain ascending list of
# numbers (in % terms, same units get_k() already normalizes to). Each list
# defines where one bucket ends and the next begins. N edges => N+1
# buckets: the first bucket is "below edge 0", the last bucket is "at or
# above the final edge" (open-ended - this is what gives the existing
# "clamp to nearest edge bucket" behavior for values above the top edge,
# free, with no extra code needed).
#
# Example with the current 3 edges [0.25, 0.4, 0.5] -> 4 buckets:
#   bucket 0: value <  0.25
#   bucket 1: 0.25 <= value < 0.4
#   bucket 2: 0.4  <= value < 0.5
#   bucket 3: value >= 0.5   (also covers anything far above 0.5, e.g. 2.0)
#
# ATR and ROC each get their OWN edge list - they don't have to match, and
# don't have to have the same number of buckets as each other.
#
# HOW TO ADD A BUCKET (e.g. a 5th ATR bucket)
# ---------------------------------------------
# 1. Add one more ascending number to ATR_BUCKET_EDGES, e.g.:
#      ATR_BUCKET_EDGES = [0.25, 0.4, 0.5, 0.7]
#    This now defines 5 ATR buckets instead of 4.
# 2. K_TABLE must then have 5 rows instead of 4 (one row per ATR bucket).
#    Add the new row with a k-value for every ROC bucket in that row.
# 3. Do the equivalent for ROC_BUCKET_EDGES if you're adding a ROC bucket
#    instead - in that case every EXISTING row needs one more value added
#    (one new column), not a new row.
# 4. Restart the backend. If the new K_TABLE's shape doesn't match the
#    edges you defined, the app will refuse to start and tell you exactly
#    which row/column is wrong, rather than silently using bad data - see
#    the validation block below.
#
# HOW TO CHANGE WHERE A BOUNDARY SITS (no new buckets, just moving one)
# ------------------------------------------------------------------------
# Just edit the number in place, e.g. change 0.4 to 0.35. K_TABLE doesn't
# need to change at all for this - only the boundary moved, not the count.
# ---------------------------------------------------------------------------

ATR_BUCKET_EDGES = [0.25, 0.4, 0.5]
ROC_BUCKET_EDGES = [0.25, 0.4, 0.5,1]

# K_TABLE[atr_bucket_index][roc_bucket_index] -> k
# Rows = ATR buckets (must have len(ATR_BUCKET_EDGES) + 1 rows).
# Columns = ROC buckets (each row must have len(ROC_BUCKET_EDGES) + 1 values).
K_TABLE = [
    [6,   5.5, 5,   4.5, 3.5],
    [5.5, 5,   4.5, 4, 3],
    [5,   4.5, 4,   3.5, 2.5],
    [4.5, 4,   3.5, 3, 2],   # bottom-right cell confirmed = 3
]

# Fails loudly and immediately at startup if K_TABLE's shape doesn't match
# the bucket edges above - a mismatch here would otherwise silently look
# up the wrong k value, or crash mid-trade with an obscure IndexError.
_expected_rows = len(ATR_BUCKET_EDGES) + 1
_expected_cols = len(ROC_BUCKET_EDGES) + 1
if len(K_TABLE) != _expected_rows:
    raise ValueError(
        f"indicator_module: K_TABLE has {len(K_TABLE)} row(s) but "
        f"ATR_BUCKET_EDGES defines {_expected_rows} bucket(s) "
        f"({ATR_BUCKET_EDGES}). Add or remove a row from K_TABLE so the "
        f"counts match."
    )
for _row_idx, _row in enumerate(K_TABLE):
    if len(_row) != _expected_cols:
        raise ValueError(
            f"indicator_module: K_TABLE row {_row_idx} has {len(_row)} "
            f"value(s) but ROC_BUCKET_EDGES defines {_expected_cols} "
            f"bucket(s) ({ROC_BUCKET_EDGES}). Add or remove a value from "
            f"this row so the counts match."
        )

# Own dedicated read-only Kite session - never touched by demo-mode patches.
_kite_indicator = KiteApp(enctoken=enctoken)

_atr_cache = {"value": None, "fetched_at": None}
_atr_lock = threading.Lock()

_roc_cache = {"value": None, "slot": None, "last_attempt": None}
_roc_lock = threading.Lock()


# ---------------------------------------------------------------------------
# ROC(27) on 5-min candle closes - polled from Kite, cached per candle close
# ---------------------------------------------------------------------------

def sample_price_if_due():
    """Kept as a no-op so trade_logic.py's existing call still works.
    ROC no longer needs locally sampled prices (price_samples is unused)."""
    return None


def _current_roc_slot(now):
    """The 5-minute boundary whose candle should be closed AND published by
    now. Changes only once per 5 minutes, ROC_PUBLISH_GRACE_SECONDS after the
    boundary."""
    t = now - timedelta(seconds=ROC_PUBLISH_GRACE_SECONDS)
    return t.replace(minute=t.minute - t.minute % ROC_CANDLE_MINUTES, second=0, microsecond=0)


def _fetch_roc_from_kite():
    """ROC(27) of the last CLOSED 5-min candle vs the close 27 candles
    earlier, as a percentage. Session gaps inside the window are included."""
    instrument_token = db_utils.get_trading_symbol("instrument_code")
    if isinstance(instrument_token, list):
        instrument_token = instrument_token[0]

    to_date = datetime.now()
    from_date = to_date - timedelta(days=ROC_LOOKBACK_DAYS)

    candles = _kite_indicator.historical_data(
        instrument_token, from_date, to_date, "5minute"
    )
    if not candles:
        return None

    # Drop the candle that is still forming - its "close" is just the latest tick.
    last_start = candles[-1]["date"]
    if last_start + timedelta(minutes=ROC_CANDLE_MINUTES) > datetime.now(last_start.tzinfo):
        candles = candles[:-1]

    if len(candles) < ROC_PERIOD + 1:
        return None

    latest_close = Decimal(str(candles[-1]["close"]))
    ref_close = Decimal(str(candles[-1 - ROC_PERIOD]["close"]))
    if ref_close == 0:
        return None

    return ((latest_close - ref_close) / ref_close) * Decimal("100")


def get_roc():
    """Returns signed ROC(27 x 5min) as a percentage (e.g. 0.42 means
    +0.42%), or None if it can't be computed yet (callers should treat None
    as 'floor to calmest bucket', same as a positive ROC).

    Refetched once per 5-minute candle close. Failed/empty fetches are
    retried at most every ROC_RETRY_SECONDS, so the per-second websocket
    snapshot never hammers the Kite API. On an API error the last good value
    is kept."""
    with _roc_lock:
        now = datetime.now()
        slot = _current_roc_slot(now)
        due = _roc_cache["slot"] != slot
        throttled = (
            _roc_cache["last_attempt"] is not None
            and (now - _roc_cache["last_attempt"]).total_seconds() < ROC_RETRY_SECONDS
        )

        if due and not throttled:
            _roc_cache["last_attempt"] = now
            try:
                fresh = _fetch_roc_from_kite()
                _roc_cache["value"] = fresh          # may be None (not enough candles)
                if fresh is not None:
                    _roc_cache["slot"] = slot        # success - no refetch until next candle
            except Exception as e:
                logging.error(f"indicator_module: ROC fetch failed, using cached value: {e}")

        return _roc_cache["value"]


# ---------------------------------------------------------------------------
# ATR(14) - polled from Kite historical_data() once per new 15-min candle
# ---------------------------------------------------------------------------

def _fetch_atr_from_kite():
    """Pulls recent 15-min candles and computes ATR(14) (Wilder-style
    simple average of true range, sufficient for this use)."""
    instrument_token = db_utils.get_trading_symbol("instrument_code")
    if isinstance(instrument_token, list):
        instrument_token = instrument_token[0]

    to_date = datetime.now()
    from_date = to_date - timedelta(days=3)  # plenty of candles for ATR(14) on 15m

    candles = _kite_indicator.historical_data(
        instrument_token, from_date, to_date, "15minute"
    )
    if not candles or len(candles) < ATR_PERIOD + 1:
        return None

    candles = candles[-(ATR_PERIOD + 1):]
    true_ranges = []
    for i in range(1, len(candles)):
        high = Decimal(str(candles[i]["high"]))
        low = Decimal(str(candles[i]["low"]))
        prev_close = Decimal(str(candles[i - 1]["close"]))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    atr = sum(true_ranges) / Decimal(str(len(true_ranges)))
    return atr


def get_atr():
    """Returns raw ATR(14) in absolute price units (not yet normalized to
    %), using an in-memory cache refreshed at most once per 15 minutes.
    Returns None if a fresh value couldn't be fetched and no cached value
    exists yet."""
    with _atr_lock:
        now = datetime.now()
        stale = (
            _atr_cache["fetched_at"] is None
            or (now - _atr_cache["fetched_at"]) >= timedelta(minutes=CANDLE_INTERVAL_MINUTES)
        )
        if stale:
            try:
                fresh = _fetch_atr_from_kite()
                if fresh is not None:
                    _atr_cache["value"] = fresh
                    _atr_cache["fetched_at"] = now
            except Exception as e:
                logging.error(f"indicator_module: ATR fetch failed, using cached value: {e}")

        return _atr_cache["value"]


# ---------------------------------------------------------------------------
# k lookup
# ---------------------------------------------------------------------------

def _bucket_index(value, edges):
    """Generic bucket lookup - works for any ascending edge list, any
    length. Returns 0 for a value below the first edge, up to len(edges)
    for a value at or above the last edge (the open-ended top bucket)."""
    for i, edge in enumerate(edges):
        if value < Decimal(str(edge)):
            return i
    return len(edges)


def get_k(current_price):
    """Live k lookup for the dynamic drop % formula.

    current_price is required to normalize raw ATR (absolute rupees) into
    atr_pct = (ATR / current_price) * 100, matching the table's units.

    ROC sign handling ("signed with floor", confirmed): only a negative
    ROC (real downward momentum) uses its magnitude to pick the ROC
    column; positive, zero, or unavailable (None) ROC floors to the
    calmest column (index 0), regardless of magnitude.

    Returns a Decimal k, or None if ATR isn't available yet (e.g. right
    after enabling this feature, before the first successful Kite poll) -
    callers should fall back to the flat drop_percentage setting in that
    case, same as the get_last_prev_drop() None fallback.
    """
    raw_atr = get_atr()
    if raw_atr is None or current_price is None or Decimal(str(current_price)) == 0:
        return None

    current_price = Decimal(str(current_price))
    atr_pct = (raw_atr / current_price) * Decimal("100")

    roc = get_roc()
    if roc is None or roc >= 0:
        roc_magnitude = Decimal("0")  # floored - calmest column
    else:
        roc_magnitude = abs(roc)

    atr_idx = _bucket_index(atr_pct, ATR_BUCKET_EDGES)
    roc_idx = _bucket_index(roc_magnitude, ROC_BUCKET_EDGES)

    return Decimal(str(K_TABLE[atr_idx][roc_idx]))
