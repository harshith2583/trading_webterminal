# websocket_utils.py
from kite_trade import *
from kiteconnect import KiteTicker
import time
'''
from db_utils import (
    get_trading_symbol
)'''

from db_utils import store_current_price, get_trading_symbol

current_price = 0
with open("/trading_test_1/enctoken.txt", "r") as f:
    enctoken = f.read().strip()
kite = KiteApp(enctoken=enctoken)

def get_enctoken():
    return enctoken


def on_ticks(ws, ticks):
    global current_price
    if len(ticks) > 0 and 'last_price' in ticks[0]:
        current_price = ticks[0]['last_price']

        # Update the GUI label with the current price

        print(f"Updated silver price: {current_price}")


def setup_websocket(enctoken):
    user_id = kite.profile()["user_id"]
    kws = KiteTicker(api_key="TradeViaPython", access_token=enctoken + "&user_id=" + user_id)

    kws.on_ticks = lambda ws, ticks: on_ticks(ws, ticks)

    max_retries = 5
    retry_count = 0

    while retry_count < max_retries:
        try:
            print(f"Attempting to connect (Attempt {retry_count + 1}/{max_retries})...")
            kws.connect(threaded=True)

            # Wait for connection to establish
            time.sleep(5)

            if kws.is_connected():
                print("WebSocket: Connected")
                return kws
            else:
                print("WebSocket: Connection failed. Retrying...")
                retry_count += 1
                time.sleep(2)  # Wait before retrying
        except Exception as e:
            print(f"Error during WebSocket connection: {e}")
            retry_count += 1

    raise Exception("WebSocket failed to connect after maximum retries.")


def set_ltp_mode(kws):
    tokens = get_trading_symbol("instrument_code")  # return list of ints
    if isinstance(tokens, int):
        tokens = [tokens]  # wrap single token into a list

    if kws.is_connected():
        kws.set_mode(kws.MODE_LTP, [int(tokens)])
        print(tokens)
    else:
        print("WebSocket is not connected. Cannot set mode.")


def get_current_price():
    global current_price
    return float(current_price)

def get_margins():
    return kite.margins()


