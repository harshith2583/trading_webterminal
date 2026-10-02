import time
from decimal import Decimal
from kite_trade import KiteApp
from websocket_utils import enctoken
from db_utils import save_buy_transaction, get_trading_symbol
from datetime import datetime

kite = KiteApp(enctoken=enctoken)


def place_buy(buy_price, profit_percentage, buy_quantity):
    """
    Place a buy order, wait until it's executed, and save it to buy_transactions.
    Blocks until order is complete or timeout reached.
    """
    # Step 1: Place actual order
    exchange_name = get_trading_symbol("exchange")
    symbol = get_trading_symbol("symbol_name")

    buy_order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange_name,
        tradingsymbol=symbol,
        transaction_type=kite.TRANSACTION_TYPE_BUY,
        quantity=buy_quantity,
        order_type=kite.ORDER_TYPE_MARKET,
        product=kite.PRODUCT_NRML
    )
    print(f"✅ Buy order placed. ID: {buy_order_id}")

    # Step 2: Wait until order completes
    b_exec_price = None
    for _ in range(30):  # check up to 30 seconds
        orders = kite.orders()
        for order in orders:
            if order["order_id"] == buy_order_id:
                print(f"📢 Order Status: {order['status']}")
                if order["status"].upper() == "COMPLETE":
                    b_exec_price = Decimal(order.get("average_price", buy_price))
                    print(f"✅ Order executed at {b_exec_price}")
                    break
        if b_exec_price is not None:
            break
        time.sleep(1)

    if b_exec_price is None:
        print("⚠️ Order not completed within the check period. Aborting save.")
        return False

    # Step 3: Save executed buy to DB
    from datetime import datetime
    buy_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_buy_transaction(buy_price=b_exec_price, buy_time=buy_time, profit_percentage=profit_percentage,
                         buy_order_id=buy_order_id, buy_quantity=buy_quantity)
    '''
    if app is not None:
        app.add_log_message("SUCCESS", f"buy transaction executed Order ID: {buy_order_id} at {b_exec_price} for "
                                       f"{buy_quantity} quantity(s)")'''
    print(f"💾 Buy transaction saved for Order ID: {buy_order_id}")

    return True
