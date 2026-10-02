"""
trading_loop.py

Headless equivalent of the loop in the original main.py's enhanced_trading_logic().
Calls the exact same trade_logic.execute_trade_logic() function with the exact same
argument construction - nothing about the trading decision logic is changed here.
"""
import os
import time
import logging
import threading
from decimal import Decimal

import trade_logic
from trade_logic import execute_trade_logic, set_app_instance
from db_utils import (
    fetch_user_settings,
    store_current_price,
    fetch_open_buy_transactions,  # Dynamic drop % - added, see plan doc
    update_buy_prev_drop,         # Dynamic drop % - added, see plan doc
)
from websocket_utils import get_current_price

from bot_controller import controller

TRADE_INTERVAL_SECONDS = int(os.environ.get("TRADE_INTERVAL_SECONDS", "5"))


def _loop():
    last_trade_time = 0
    error_count = 0
    max_errors = 10

    logging.info("Web-terminal trading loop thread started")

    while True:
        try:
            # Wait until the bot is started via the API/UI
            controller.trading_event.wait()

            current_time = time.time()

            if current_time - last_trade_time >= TRADE_INTERVAL_SECONDS:
                current_price_raw = get_current_price()
                if current_price_raw is None:
                    logging.warning("Failed to fetch current price")
                    time.sleep(1)
                    continue

                current_price = Decimal(str(current_price_raw))
                store_current_price(current_price)

                if current_price > 0:
                    drop_percentage, profit_percentage, first_buy_percentage, max_purchases, quantity = \
                        fetch_user_settings()

                    drop_percentage = Decimal(str(drop_percentage))
                    profit_percentage = Decimal(str(profit_percentage))
                    first_buy_percentage = Decimal(str(first_buy_percentage))
                    max_purchases = int(max_purchases)
                    quantity = int(quantity)

                    if all([drop_percentage > 0, profit_percentage > 0,
                            first_buy_percentage > 0, quantity > 0, max_purchases > 0]):

                        execute_trade_logic(
                            current_price,
                            drop_percentage,
                            profit_percentage,
                            first_buy_percentage,
                            max_purchases,
                            quantity,
                        )

                        # --- Dynamic drop % (ROC/ATR grid spacing) - added, see
                        # plan doc. Pure bookkeeping, kept out of trade_logic.py
                        # entirely: if a buy just landed, attach the drop % that
                        # was actually used to trigger it, so the next dynamic
                        # calculation has an anchor to compound from.
                        # update_buy_prev_drop() only writes when the column is
                        # still NULL, so calling this every tick is safe and
                        # idempotent - it's a no-op on ticks where no new buy
                        # fired, or where the value was already recorded.
                        try:
                            open_txns = fetch_open_buy_transactions() or []
                            if len(open_txns) >= 2 and trade_logic.last_dynamic_drop_used is not None:
                                last_buy_id = open_txns[-1][0]
                                update_buy_prev_drop(last_buy_id, trade_logic.last_dynamic_drop_used)
                        except Exception as e:
                            logging.error(f"prev_drop_percentage write-back failed: {str(e)}")

                        last_trade_time = current_time
                        error_count = 0
                    else:
                        logging.warning("Invalid trading settings - skipping trade logic")
                else:
                    logging.warning("Invalid current price - skipping trade logic execution")

            time.sleep(1)

        except Exception as e:
            error_count += 1
            logging.error(f"Trading logic error ({error_count}/{max_errors}): {str(e)}")
            controller.add_log_message("ERROR", f"Trading loop error: {str(e)}")

            if error_count >= max_errors:
                logging.critical("Too many trading logic errors - pausing for 60 seconds")
                controller.add_log_message("ERROR", "Too many errors - pausing trading loop for 60s")
                time.sleep(60)
                error_count = 0
            else:
                time.sleep(10)


def start_trading_loop_thread():
    """Registers the controller with trade_logic.py and starts the loop in a daemon thread."""
    set_app_instance(controller)
    thread = threading.Thread(target=_loop, name="WebTradingLoop", daemon=True)
    thread.start()
    return thread

