# trade_logic.py

from datetime import datetime
from decimal import Decimal
from db_utils import (
    fetch_open_buy_transactions,
    delete_buy_transaction,
    log_transaction_to_all,
    save_buy_transaction,
    get_sellable_price_from_db,  # Fetch sellable price from DB
    update_sellable_price_in_db,  # Update sellable price in DB
    store_first_buy_price,
    update_buy_price,
    fetch_buy_order_id,
    get_active_positions,
    fetch_manual_action,
    clear_manual_action,
    get_last_prev_drop,
    get_drop_mode,   # static/dynamic drop toggle - added
    set_drop_mode)  # Dynamic drop % (ROC/ATR grid spacing) - added, see plan doc
from trailing_stop_loss import TrailingStopLoss
from buy_logic import place_buy
from websocket_utils import get_margins
from sell_logic import place_sell
from indicator_module import get_k, sample_price_if_due  # Dynamic drop % - added, see plan doc

'''
from db_utils import (
    fetch_open_buy_transactions,
    delete_buy_transaction,
    save_buy_transaction,
    log_transaction_to_all,
    get_sellable_price_from_db,  # Fetch sellable price from DB
    update_sellable_price_in_db,  # Update sellable price in DB
    store_first_buy_price,
    update_buy_price,
    fetch_buy_order_id,
    get_active_positions,
    fetch_manual_action,
    clear_manual_action)'''

# Initialize global variables
first_buy_price = None
last_first_buy_percentage = None
trailing_stop_loss = {}  # Dictionary to hold trailing stop loss per buy_id

app = None  # placeholder for GUI instance

# --- Dynamic drop % (ROC/ATR grid spacing) - added, see plan doc ---------
# Exposes the effective drop % used for the *next* buy target on the most
# recent tick, so trading_loop.py can write it back onto the newly-created
# buy_transactions row once a buy actually fires (see
# db_utils.update_buy_prev_drop()). Read-only from outside this module.
last_dynamic_drop_used = None
# ---------------------------------------------------------------------------


def set_app_instance(gui_app):
    global app
    app = gui_app


def _compute_next_drop_percentage(drop_percentage, current_price, active_position):
    """Dynamic drop % (ROC/ATR grid spacing) - added, see plan doc.

    Positions 1-2 are unaffected: returns the flat drop_percentage setting
    unchanged, exactly as before this feature existed.

    Position 3 onward (active_position >= 2, i.e. two positions already
    open, about to compute the target for the next one):
        next_drop = prev_drop * (1 + 1/k)
    where prev_drop is the drop % that actually fired the previous
    position (read fresh from the DB every tick - see
    db_utils.get_last_prev_drop()) and k is looked up live from current
    ROC(9)/ATR(14) (see indicator_module.get_k()). Both are recomputed on
    every tick until the position actually fires, so the target keeps
    breathing with live volatility right up to the moment it's hit.

    Falls back to the flat drop_percentage setting if either input isn't
    available yet (e.g. right after enabling this feature, before enough
    price samples/candles have accumulated) - degrades gracefully rather
    than blocking the bot.
    """
    global last_dynamic_drop_used

    drop_percentage = Decimal(str(drop_percentage))

    if active_position < 2:
        last_dynamic_drop_used = drop_percentage
        return drop_percentage

    # Static mode armed: use the flat setting. last_dynamic_drop_used = flat
    # value, so trading_loop writes it as prev_drop_percentage and the next
    # dynamic drop compounds from it.
    if get_drop_mode() == "static":
        last_dynamic_drop_used = drop_percentage
        return drop_percentage

    prev_drop = get_last_prev_drop()
    k = get_k(current_price)

    if prev_drop is None or k is None:
        last_dynamic_drop_used = drop_percentage
        return drop_percentage

    prev_drop = Decimal(str(prev_drop))
    next_drop = prev_drop * (Decimal("1") + Decimal("1") / k)
    last_dynamic_drop_used = next_drop
    return next_drop


def execute_trade_logic(current_price, drop_percentage, profit_percentage, first_buy_percentage,
                        max_purchases, quantity):
    global first_buy_price, trailing_stop_loss
    active_position = get_active_positions()
    current_price = Decimal(str(current_price))
    print(f"Current price fetched: {current_price}")

    if current_price <= 0:
        print("Invalid current price. Skipping trade logic execution.")
        return

    # Dynamic drop % (ROC/ATR grid spacing) - added, see plan doc.
    # Cheap no-op unless 15 minutes have passed since the last sample.
    try:
        sample_price_if_due()
    except Exception as e:
        print(f"indicator_module: price sampling failed (non-fatal): {e}")

    fetched_transactions = fetch_open_buy_transactions() or []

    # Sell logic
    for transaction in fetched_transactions:
        buy_id, buy_price, buy_time = transaction[:3]
        buy_quantity = transaction[5]

        # --- Always get current sellable price from DB ---
        sellable_price = Decimal(str(get_sellable_price_from_db(buy_id)))

        # --- Initialize or refresh TrailingStopLoss ---
        if buy_id not in trailing_stop_loss:
            trailing_stop_loss[buy_id] = TrailingStopLoss(sellable_price, profit_percentage, buy_id, buy_price)
        else:
            # 🔑 Sync in-memory object with DB (manual changes respected)
            trailing_stop_loss[buy_id].sellable_price = sellable_price

        # --- Update trailing stop loss ---
        old_sellable_price = sellable_price
        trailing_stop_loss[buy_id].update_trailing_stop_loss(current_price)
        new_sellable_price = trailing_stop_loss[buy_id].sellable_price

        # --- ✅ Only update DB if trailing actually changed the price ---
        if new_sellable_price != old_sellable_price:
            update_sellable_price_in_db(buy_id, new_sellable_price, buy_price, buy_quantity)

        # --- Check trigger condition for sell ---
        if trailing_stop_loss[buy_id].check_trigger_sell(current_price):
            print(f"Sell triggered by trailing stop loss at price {current_price} for buy_id {buy_id}.")
            execute_sell_logic(buy_id, buy_price, buy_time, current_price, buy_quantity, controller=app)

    # After loop, log open transactions for debugging
    open_buy_transactions = fetch_open_buy_transactions() or []
    print(f"Open buy transactions: {open_buy_transactions}")

    # First Buy Logic
    if len(open_buy_transactions) == 0 and active_position == 0:
        if first_buy_price is None or last_first_buy_percentage != first_buy_percentage:
            recalc_first_buy_price(current_price, first_buy_percentage)

    # Execute first buy if conditions are met
    if first_buy_price is not None and current_price <= first_buy_price:
        # Determine quantity to buy (cannot exceed max_purchases)
        remaining_qty = max_purchases - active_position
        buy_qty = min(quantity, remaining_qty)

        if buy_qty > 0:
            execute_first_buy_logic(current_price, profit_percentage, buy_qty, controller=app)
            first_buy_price = None  # Reset to avoid multiple buys
        else:
            print("Maximum quantity reached. No further buys will be executed.")
            if app is not None:
                app.add_log_message("INFO", "Maximum purchase quantity reached. No further buys will be executed")

    # Subsequent Buys Logic
    elif len(open_buy_transactions) > 0 and active_position < max_purchases:
        last_buy_price = open_buy_transactions[-1][1]
        # Auto-disarm static mode if positions fell below 2
        static_on = get_drop_mode() == "static"
        if static_on and active_position < 2:
            set_drop_mode("dynamic")
            static_on = False

        # Dynamic drop % (ROC/ATR grid spacing) - added, see plan doc.
        # Positions 1-2 unaffected (returns drop_percentage unchanged).
        effective_drop_percentage = _compute_next_drop_percentage(
            drop_percentage, current_price, active_position
        )
        next_buy_price = last_buy_price * (Decimal("1") - effective_drop_percentage)
        next_buy_price = round(next_buy_price, 2)
        update_buy_price(next_buy_price)
        print(f"Next Buy price: {next_buy_price} (drop % used: {effective_drop_percentage * 100:.4f}%)")

        if current_price <= next_buy_price:
            # Calculate remaining quantity allowed
            remaining_qty = max_purchases - active_position
            buy_qty = min(quantity, remaining_qty)

            if buy_qty > 0:
                filled = execute_buy_logic(current_price, profit_percentage, buy_qty, controller=app)
                if filled and static_on:
                    set_drop_mode("dynamic")
                    if app is not None:
                        app.add_log_message("INFO", "Static buy filled - switched back to dynamic drop %")
            else:
                print("Maximum quantity reached. No further buys will be executed.")
                if app is not None:
                    app.add_log_message("INFO", "Maximum purchase quantity reached. No further buys will be executed")

    action = fetch_manual_action()

    if action == "BUY":
        execute_buy_logic(current_price, profit_percentage, quantity, controller=app)
        clear_manual_action()

    elif action == "SELL_ALL":
        for pos in fetch_open_buy_transactions() or []:
            buy_id, buy_price, buy_time = pos[:3]
            qty = pos[5]
            execute_sell_logic(buy_id, buy_price, buy_time, current_price, qty, controller=app)
        clear_manual_action()

    elif action and action.startswith("SELL_ONE:"):
        buy_id = int(action.split(":")[1])
        for pos in fetch_open_buy_transactions() or []:
            if pos[0] == buy_id:
                buy_price, buy_time, qty = pos[1], pos[2], pos[5]
                execute_sell_logic(buy_id, buy_price, buy_time, current_price, qty, controller=app)
                break
        clear_manual_action()
    if active_position >= max_purchases:

        if app is not None:
            app.add_log_message("INFO", "Maximum purchase quantity reached. No further buys will be executed")

def recalc_first_buy_price(current_price, first_buy_percentage, next_buy_price=None):
    """Recalculate and store the first buy price based on current market and settings,
       with dynamic reset if price moves too far from reference."""
    
    global first_buy_price, last_first_buy_percentage

    current_price = Decimal(str(current_price))
    first_buy_percentage = Decimal(str(first_buy_percentage))

    # --- New condition: distance-based recalculation ---
    if next_buy_price is not None:
        next_buy_price = Decimal(str(next_buy_price))

        distance = abs(current_price - next_buy_price)
        threshold = next_buy_price * first_buy_percentage * Decimal("8")

        if distance >= threshold:
            print(f"[RESET] Price moved far from reference. Recalculating first buy price.")

            first_buy_price = current_price * (Decimal("1") - first_buy_percentage)
            first_buy_price = round(first_buy_price, 2)

            last_first_buy_percentage = first_buy_percentage
            store_first_buy_price(first_buy_price)

            print(f"[RESET] New first buy price: {first_buy_price}")
            return first_buy_price

    # --- Default behavior ---
    first_buy_price = current_price * (Decimal("1") - first_buy_percentage)
    first_buy_price = round(first_buy_price, 2)

    last_first_buy_percentage = first_buy_percentage
    store_first_buy_price(first_buy_price)

    print(f"[REFRESH] First buy price recalculated: {first_buy_price}")
    return first_buy_price

def execute_buy_logic(current_price, profit_percentage, buy_quantity, controller=app):
    try:
        # Ensure Decimal consistency
        current_price = Decimal(str(current_price))

        buy_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # --- Execute Buy ---
        result = place_buy(current_price, profit_percentage, buy_quantity)

        # --- Handle Success ---
        if result:
            print(f"? Buy executed at {current_price}")

            if app is not None:
                app.add_log_message(
                    "SUCCESS",
                    f"Buy executed successfully at {current_price}"
                )

            return True

        # --- Handle Failure ---
        else:
            print("?? Buy failed. No position created.")

            if app is not None:
                app.add_log_message(
                    "ERROR",
                    "Buy FAILED. No position created."
                )

            # Stop bot for safety
            if controller is not None:
                controller.stop_bot()

            return False

    except Exception as e:
        error_msg = f"Exception in execute_buy_logic: {str(e)}"
        print(error_msg)

        if app is not None:
            app.add_log_message("ERROR", error_msg)

        # Stop bot on unexpected exception
        if controller is not None:
            controller.stop_bot()

        return False


def execute_first_buy_logic(current_price, profit_percentage, buy_quantity, controller=None):
    try:
        buy_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # --- Execute Buy ---
        result = place_buy(current_price, profit_percentage, buy_quantity)

        # --- Handle Success ---
        if result:
            print(f"? First buy executed at price {current_price}")

            if app is not None:
                app.add_log_message(
                    "SUCCESS",
                    f"First buy executed at {current_price:.2f}"
                )

            return True

        # --- Handle Failure ---
        else:
            print("?? First buy FAILED.")

            if app is not None:
                app.add_log_message(
                    "ERROR",
                    "First buy FAILED. No position created."
                )

            # Stop bot for safety
            if controller is not None:
                print("? Stopping bot due to buy failure...")
                controller.stop_bot()

            return False

    except Exception as e:
        error_msg = f"Exception in execute_first_buy_logic: {str(e)}"
        print(error_msg)

        if app is not None:
            app.add_log_message("ERROR", error_msg)

        # Stop bot on unexpected exception
        if controller is not None:
            print("? Stopping bot due to exception...")
            controller.stop_bot()

        return False


def execute_sell_logic(buy_id, buy_price, buy_time, current_price, quantity, controller=None):
    try:
        buy_order_id = fetch_buy_order_id(buy_id)

        buy_price = Decimal(str(buy_price))
        current_price = Decimal(str(current_price))
        sell_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        result = place_sell(buy_price, current_price, buy_time, buy_order_id, quantity)

        if result:
            if app is not None:
                app.add_log_message(
                    "SUCCESS",
                    f"Sell executed successfully for Buy ID {buy_id}"
                )

            delete_buy_transaction(buy_id)

        else:
            if app is not None:
                app.add_log_message(
                    "ERROR",
                    f"Sell FAILED for Buy ID {buy_id}. Position still open."
                )

            print("?? Critical: Sell failed. Stopping bot...")

            if controller is None:
                print("? CRITICAL: Controller not passed. Cannot stop bot.")
            else:
                print("? Stopping bot...")
                controller.stop_bot()

    except Exception as e:
        error_msg = f"Exception in execute_sell_logic: {str(e)}"

        if app is not None:
            app.add_log_message("ERROR", error_msg)

        print(error_msg)

        if controller is None:
            print("? CRITICAL: Controller not passed. Cannot stop bot.")
        else:
            print("? Stopping bot due to exception...")
            controller.stop_bot()
