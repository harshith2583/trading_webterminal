# db_utils.py
import mysql.connector
from decimal import Decimal
from datetime import datetime
from mysql.connector import pooling


dbconfig = {
    "host": "65.20.85.250",
    "user": "admin",
    "password": "Admin2508@",
    "database": "trading_db"
}
connection_pool = pooling.MySQLConnectionPool(pool_name="silver_pool",
                                              pool_size=10,
                                              **dbconfig)

def get_db_connection():
    """Get a pooled database connection."""
    try:
        return connection_pool.get_connection()
    except Exception as e:
        print(f"⚠️ Database connection pool error: {e}")
        raise


def initialize_db():
    """Create tables if they don't exist."""
    conn = get_db_connection()
    # Define table creation statements if needed

    conn.commit()
    conn.close()
    print("Initialized tables in SQL database.")


def store_first_buy_price(first_buy_price):
    """Store the first buy price in the table (if not exists)."""
    conn = get_db_connection()
    cursor = conn.cursor()
        
    cursor.execute("""
    UPDATE buy_prices SET buy_price = %s
    """, (first_buy_price,))

    conn.commit()
    conn.close()


def update_buy_price(next_buy_price):
    """Keep exactly one buy_price value in the table."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM buy_prices")
    cursor.execute(
        "INSERT INTO buy_prices (buy_price) VALUES (%s)",
        (float(next_buy_price),)
    )

    conn.commit()
    conn.close()

def fetch_buy_price():
    """Fetch the latest buy price from the database."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT buy_price FROM buy_prices LIMIT 1")
    row = cursor.fetchone()

    conn.close()

    return row[0] if row else None


def save_buy_transaction(buy_price, buy_time, profit_percentage,buy_order_id, buy_quantity):
    """Save a new buy transaction with sellable price and expected profit/loss."""
    # Convert buy_price and profit_percentage to Decimal for consistency
    buy_price_decimal = Decimal(str(buy_price))  # Convert float to Decimal
    profit_percentage_decimal = Decimal(profit_percentage)  # Ensure profit_percentage is Decimal

    # Calculate sellable price and expected profit/loss
    sellable_price = buy_price_decimal * (Decimal(1) + profit_percentage_decimal)
    expected_profit_loss = (sellable_price - buy_price_decimal) * buy_quantity

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(''' 
        INSERT INTO buy_transactions (buy_price, buy_time, sellable_price, expected_profit_loss,buy_order_id, buy_quantity) 
        VALUES (%s, %s, %s, %s, %s, %s) 
    ''', (buy_price_decimal, buy_time, sellable_price, expected_profit_loss,buy_order_id, buy_quantity))
    conn.commit()
    conn.close()

def log_transaction_to_all(buy_price, buy_time, sell_price, sell_time, profit_loss, buy_order_id, sell_order_id, sellable_price, quantity,
                                buy_streak):
    """Log all transaction details into the all_transactions table."""
    trading_symbol = get_trading_symbol('symbol_name')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
    INSERT INTO all_transactions (buy_price, buy_time, sell_price, sell_time, profit_loss ,buy_order_id, sell_order_id, sellable_price, quantity
    ,trading_symbol,buy_streak)
    VALUES (%s, %s, %s, %s, %s,%s, %s, %s, %s, %s, %s)
    ''', (buy_price, buy_time, sell_price, sell_time, profit_loss,buy_order_id, sell_order_id, sellable_price, quantity,
          trading_symbol, buy_streak))
    conn.commit()
    conn.close()


def fetch_all_transactions():
    """Fetch all transactions from the all_transactions table."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
    SELECT id, buy_price, buy_time, sell_price, sell_time, profit_loss, quantity FROM all_transactions
    ''')
    all_transactions = c.fetchall()
    conn.close()
    return all_transactions


def fetch_open_buy_transactions():
    """Fetch all open buy transactions from the database."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT buy_id, buy_price, buy_time, sellable_price, expected_profit_loss ,buy_quantity 
    FROM buy_transactions WHERE status = %s''', ('open',))
    transactions = c.fetchall()
    conn.close()
    return transactions


def fetch_buy_order_id(buy_id):
    """Fetch all open buy transactions from the database."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT buy_order_id FROM buy_transactions WHERE buy_id = %s''', (buy_id,))
    result = c.fetchone()  # fetchone because buy_id should be unique
    conn.close()
    if result:
        return result[0]
    else:
        return None


def update_buy_transaction(buy_id):
    """Update a buy transaction to mark it as closed."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
    UPDATE buy_transactions
    SET status = 'closed' 
    WHERE buy_id = %s
    ''', (buy_id,))
    conn.commit()
    conn.close()


def delete_buy_transaction(buy_id):
    """Delete a buy transaction from the database after it's sold."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
    DELETE FROM buy_transactions
    WHERE buy_id = %s
    ''', (buy_id,))
    conn.commit()
    conn.close()


def update_sellable_price_in_db(buy_id, new_sellable_price, buy_price, buy_quantity):
    """Update the sellable price in the buy transactions table based on buy_id."""
    conn = get_db_connection()  # Make sure to get a valid DB connection
    cursor = conn.cursor()
    buy_price_decimal = Decimal(str(buy_price))
    expected_profit_loss = (new_sellable_price - buy_price_decimal) * buy_quantity

    update_query = """
    UPDATE buy_transactions
    SET sellable_price = %s, expected_profit_loss = %s
    WHERE buy_id = %s
    """
    # Execute the query with correct parameters
    cursor.execute(update_query, (new_sellable_price, expected_profit_loss, buy_id))

    conn.commit()
    conn.close()


def get_sellable_price_from_db(buy_id: int) -> Decimal:
    """Retrieve the sellable price for the given buy transaction from the database.

    Parameters:
    - buy_id: ID of the buy transaction.

    Returns:
    - Decimal: Sellable price as a Decimal, or None if not found.
    """
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT sellable_price FROM buy_transactions WHERE buy_id = %s", (buy_id,))
        result = c.fetchone()
    finally:
        conn.close()

    if result is None:
        return Decimal('0.0')
    return Decimal(result[0])


def save_user_settings(drop_percentage, profit_percentage, first_buy_percentage, max_purchases, quantity):
    """Save user settings to the database, replacing any existing settings."""
    conn = get_db_connection()
    c = conn.cursor()

    # Clear any existing settings (you may want to customize this logic if you want multiple users, etc.)
    c.execute("DELETE FROM user_settings")

    # Insert new settings
    c.execute('''
    INSERT INTO user_settings (drop_percentage, profit_percentage, first_buy_percentage, max_purchases,quantity)
    VALUES (%s, %s, %s, %s, %s)
    ''', (drop_percentage, profit_percentage, first_buy_percentage, max_purchases, quantity))

    conn.commit()
    conn.close()


def fetch_user_settings():
    """Fetch the user settings from the database."""
    conn = get_db_connection()
    c = conn.cursor()

    # Select the most recent settings
    c.execute('''
    SELECT drop_percentage, profit_percentage, first_buy_percentage, max_purchases, quantity
    FROM user_settings
    ORDER BY timestamp DESC LIMIT 1
    ''')

    settings = c.fetchone()
    conn.close()

    if settings:
        return settings
    else:
        return 0, 0, 0, 0, 1


def save_trading_symbol(exchange, symbol_name, instrument_code):
    """Save trading symbol details to the database, replacing any existing record."""
    conn = get_db_connection()
    c = conn.cursor()

    # Clear any existing symbol entry
    c.execute("DELETE FROM trading_symbol")

    # Insert new record
    c.execute('''
        INSERT INTO trading_symbol (exchange, symbol_name, instrument_code)
        VALUES (%s, %s, %s)
    ''', (exchange, symbol_name, instrument_code))

    conn.commit()
    conn.close()


def get_trading_symbol(field=None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT exchange, symbol_name, instrument_code FROM trading_symbol")
    result = c.fetchone()
    conn.close()

    if not result:
        # Empty defaults if nothing in DB
        result = ("", "", "")

    if field is None:
        # Return all three values as tuple
        return result
    elif field == "exchange":
        return result[0]
    elif field == "symbol_name":
        return result[1]
    elif field == "instrument_code":
        return result[2]
    else:
        raise ValueError(f"Invalid field name: {field}")


def get_active_positions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT SUM(buy_quantity) FROM buy_transactions WHERE status = %s", ('open',))
    result = c.fetchone()
    conn.close()
    return result[0] if result[0] is not None else 0


def get_profit(date_mode="today"):
    """
    Get total profit for today or all time.
    date_mode: "today" or "all"
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    today_str = datetime.now().strftime("%Y-%m-%d")

    if date_mode.lower() == "today":
        query = """
            SELECT SUM(profit_loss)
            FROM all_transactions
            WHERE DATE(sell_time) = %s
        """
        cursor.execute(query, (today_str,))
    elif date_mode.lower() == "all":
        query = """
            SELECT SUM(profit_loss)
            FROM all_transactions
        """
        cursor.execute(query)
    else:
        conn.close()
        raise ValueError("Invalid date_mode. Use 'today' or 'all'.")

    result = cursor.fetchone()[0] or 0
    conn.close()
    return round(result, 2)


def store_current_price(current_price):
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("DELETE FROM current_price")

    c.execute('''
            INSERT INTO current_price (current_price)
            VALUES (%s)
            ''', (current_price,))

    conn.commit()
    conn.close()


def store_manual_action(action_type):
    """Store a manual action in MySQL (only one row at a time)."""
    conn = get_db_connection()
    c = conn.cursor()

    # Optional: clear previous action to keep only one row
    c.execute("DELETE FROM manual_actions")

    # Insert new action
    c.execute(
        "INSERT INTO manual_actions (action_type) VALUES (%s)",
        (action_type,)
    )

    conn.commit()
    conn.close()


def fetch_manual_action():
    """Fetch the latest manual action (if any)."""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT action_type FROM manual_actions LIMIT 1")
    row = c.fetchone()

    conn.close()
    return row[0] if row else None


def clear_manual_action():
    """Delete any stored manual action."""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("DELETE FROM manual_actions")

    conn.commit()
    conn.close()

def get_buy_id(buy_order_id):
    conn = get_db_connection()
    c = conn.cursor()

    try:
        # Use a tuple for parameterized query
        c.execute("SELECT buy_id FROM buy_transactions WHERE buy_order_id = %s", (buy_order_id,))
        result = c.fetchone()  # Fetch a single result (since buy_order_id should be unique)
        return result[0] if result else None

    except Exception as e:
        print(f"Error fetching sellable_price for buy_order_id {buy_order_id}: {e}")
        return None

    finally:
        c.close()
        conn.close()


# ---------------------------------------------------------------------------
# Dynamic drop % (ROC/ATR grid spacing) - added, see plan doc.
# These two functions are new and standalone; no existing function above
# this line has been modified. save_buy_transaction() and
# fetch_open_buy_transactions() are untouched, so nothing already calling
# them is affected by this addition.
# ---------------------------------------------------------------------------

def update_buy_prev_drop(buy_id, prev_drop_percentage):
    """Write-back: record the drop % actually used to trigger this buy.

    Called by trading_loop.py shortly after a new buy lands (buy_logic.py's
    save_buy_transaction() call has no room for this value at insert time,
    so it's attached via a follow-up UPDATE once the row's buy_id is known).

    Deliberately only writes when the column is still NULL, so calling this
    repeatedly (e.g. once per trading-loop tick) is safe and idempotent -
    it will never overwrite a value that was already recorded.
    """
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
    UPDATE buy_transactions
    SET prev_drop_percentage = %s
    WHERE buy_id = %s AND prev_drop_percentage IS NULL
    """, (str(prev_drop_percentage), buy_id))
    conn.commit()
    conn.close()


def get_last_prev_drop():
    """Fetch prev_drop_percentage off the most recently opened position.

    This is the anchor ('prev_drop') that trade_logic.py's dynamic drop %
    helper compounds from for position 3 onward. Returns None if there are
    no open positions, or if the most recent one hasn't been backfilled
    yet (e.g. the tick right after it was inserted, before trading_loop.py's
    write-back has run) - callers should fall back to the flat
    drop_percentage setting in that case.
    """
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
    SELECT prev_drop_percentage FROM buy_transactions
    ORDER BY buy_id DESC LIMIT 1
    """)
    result = c.fetchone()
    conn.close()
    if result and result[0] is not None:
        return result[0]
    return None
    
    
def get_drop_mode():
    """'static' or 'dynamic'. Defaults to 'dynamic' if the table is empty."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT mode FROM drop_mode LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else "dynamic"


def set_drop_mode(mode):
    if mode not in ("static", "dynamic"):
        raise ValueError("mode must be 'static' or 'dynamic'")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM drop_mode")
    c.execute("INSERT INTO drop_mode (mode) VALUES (%s)", (mode,))
    conn.commit()
    conn.close()

