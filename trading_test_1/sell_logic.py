import time
from decimal import Decimal
from kite_trade import KiteApp
from datetime import datetime
from websocket_utils import enctoken
from db_utils import log_transaction_to_all, get_trading_symbol, \
    get_sellable_price_from_db, get_buy_id, fetch_open_buy_transactions
kite = KiteApp(enctoken=enctoken)


def place_sell(buy_price, sell_price, buy_time, buy_order_id, quantity):
    sell_price = round(sell_price, 2)
    """
    Place a buy order, wait until it's executed, and save it to buy_transactions.
    Blocks until order is complete or timeout reached.
    """
    exchange_name = get_trading_symbol("exchange")
    symbol = get_trading_symbol("symbol_name")
    # Step 1: Place actual order
    sell_order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange_name,
        tradingsymbol=symbol,
        transaction_type=kite.TRANSACTION_TYPE_SELL,
        quantity=quantity,
        price=sell_price,
        order_type=kite.ORDER_TYPE_MARKET,
        product=kite.PRODUCT_NRML
    )
    print(f"✅ sell order placed. ID: {sell_order_id}")

    s_exec_price = None
    for _ in range(30):  # check up to 30 seconds
        orders = kite.orders()
        for order in orders:
            if order["order_id"] == sell_order_id:
                print(f"📢 Order Status: {order['status']}")
                if order["status"].upper() == "COMPLETE":
                    s_exec_price = Decimal(order.get("average_price", sell_price))
                    print(f"✅ Order executed at {s_exec_price} for {quantity} quantity(s)")
                    break
        if s_exec_price is not None:
            break
        time.sleep(1)

    if s_exec_price is None:
        print("⚠️ Order not completed within the check period. Aborting save.")
        return False

    buy_streak = len(fetch_open_buy_transactions())
    sell_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    buy_id = get_buy_id(buy_order_id)
    sellable_price = Decimal(str(get_sellable_price_from_db(buy_id)))
    profit_loss = (s_exec_price - buy_price) * quantity
    log_transaction_to_all(buy_price=buy_price, buy_time=buy_time, sell_price=s_exec_price, sell_time=sell_time,
                           profit_loss=profit_loss, buy_order_id=buy_order_id, sell_order_id=sell_order_id,
                           sellable_price=sellable_price, quantity=quantity, buy_streak=buy_streak)
    '''
    if app is not None:
        app.add_log_message("SUCCESS", f"sell transaction executed for Order ID: {buy_order_id} at {s_exec_price} for {quantity} quantity(s)")
    '''
    print(f"Sell Order executed for  {s_exec_price} for {quantity} quantity(s)")
    return True
