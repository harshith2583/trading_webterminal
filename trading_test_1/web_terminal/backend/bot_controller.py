"""
bot_controller.py

This class is registered with trade_logic.set_app_instance(controller) and stands
in for the Tkinter GUI object that trade_logic.py, buy_logic.py etc. normally talk to.
trade_logic.py only ever calls two things on it:
    - app.add_log_message(level, message)
    - controller.stop_bot()
Neither trade_logic.py nor any other bot file is modified - this class just
implements the same interface headlessly, storing state that the web API/websocket
can read.
"""
import threading
from collections import deque
from datetime import datetime


class BotController:
    def __init__(self, log_maxlen: int = 300):
        self.trading_event = threading.Event()
        self.bot_status = "Stopped"
        self.logs = deque(maxlen=log_maxlen)
        self._lock = threading.Lock()
        self.token_expired = False
        self.token_refresh_in_progress = False

    # --- interface expected by trade_logic.py / buy_logic.py / sell_logic.py ---

    def add_log_message(self, level: str, message: str):
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Same as previous log → update time and count
            if (
                self.logs
                and self.logs[-1]["level"] == level
                and self.logs[-1]["message"] == message
            ):
                self.logs[-1]["time"] = now
                self.logs[-1]["count"] = self.logs[-1].get("count", 1) + 1
                return

            # New log
            self.logs.append({
                "level": level,
                "message": message,
                "time": now,
                "count": 1,
            })

    def stop_bot(self):
        """Called by trade_logic.py on a failed buy/sell or unexpected exception."""
        self.trading_event.clear()
        self.bot_status = "Stopped"
        self.add_log_message("INFO", "Trading bot stopped automatically (safety stop).")

    # --- extra helpers used only by the web layer, not by the bot logic ---

    def start_bot(self):
        self.trading_event.set()
        self.bot_status = "Running"
        self.add_log_message("SUCCESS", "Trading bot started")

    def manual_stop_bot(self):
        self.trading_event.clear()
        self.bot_status = "Stopped"
        self.add_log_message("INFO", "Trading bot stopped")

    def is_running(self) -> bool:
        return self.trading_event.is_set()

    def mark_token_expired(self):
        was_running = self.is_running()
        self.token_expired = True
        self.trading_event.clear()
        self.bot_status = "Stopped"
        if was_running:
            self.add_log_message("ERROR", "Kite session expired - bot paused automatically. Refresh the token to resume.")
        else:
            self.add_log_message("ERROR", "Kite session expired - refresh needed before starting the bot.")

    def mark_token_valid(self):
        if self.token_expired:
            self.add_log_message("SUCCESS", "Kite session is valid again.")
        self.token_expired = False

    def recent_logs(self, limit: int = 50):
        with self._lock:
            return list(self.logs)[-limit:]


# Single shared instance for the whole process
controller = BotController()
