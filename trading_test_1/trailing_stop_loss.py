# trailing_stop_loss.py

from decimal import Decimal


class TrailingStopLoss:
    def __init__(self, initial_sellable_price, profit_percentage, buy_id, buy_price):
        """
        Initializes the trailing stop loss for a transaction.
        :param initial_sellable_price: The initial sellable price based on the profit percentage.
        :param profit_percentage: The profit percentage for trailing the stop loss.
        :param buy_id: The ID of the buy transaction.
        :param buy_price: The original price of the asset at which the buy was executed.
        """
        self.sellable_price = Decimal(initial_sellable_price)
        self.profit_percentage = Decimal(profit_percentage)
        self.buy_id = buy_id
        self.buy_price = Decimal(buy_price)
        self.first_increment_completed = False
        # This flag will ensure no sell is triggered until price surpasses sellable price
        self.triggered = False

    def update_trailing_stop_loss(self, current_price):
        """
        Updates the sellable price based on the current price movement and profit percentage.
        The trailing logic only activates after the cur price has crossed the initial sellable price.
        :param current_price: The current market price.
        """
        current_price = Decimal(str(current_price))

        # First increment logic: Check if the current price has crossed the initial sellable price
        if not self.first_increment_completed and current_price > self.sellable_price:
            self.first_increment_completed = True  # Mark that the first increment has been crossed
            print(f"First increment completed for buy_id {self.buy_id} at price {current_price}")

        # Trailing logic: Only update sellable price if current price is higher than the existing sellable price
        # and the first increment has been completed.
        if self.first_increment_completed and current_price > self.sellable_price:
            # Ensure Decimal division
            profit_fraction = self.profit_percentage / Decimal("4")
            new_sellable_price = current_price * (Decimal("1") - profit_fraction)
            if new_sellable_price > self.sellable_price:
                self.sellable_price = new_sellable_price
                print(f"Updated trailing sellable price for buy_id {self.buy_id} to {self.sellable_price}")

    def check_trigger_sell(self, current_price):
        """
        Checks if the current price has fallen below the trailing stop loss price.
        Only allows sell after the first increment is completed.
        :param current_price: The current market price.
        :return: True if a sell should be triggered, False otherwise.
        """
        current_price = Decimal(str(current_price))

        # Sell should only trigger if the first increment has been crossed
        if self.first_increment_completed and current_price < self.sellable_price:
            self.triggered = True
            return True
        return False
