"""
token_health.py

Periodically calls the existing, unmodified websocket_utils.get_margins() -
a real authenticated Kite API call - to detect when the daily enctoken has
expired. On failure, pauses the bot (safety-first, per user preference) and
flags it on the controller so the dashboard can show a banner. On recovery,
clears the flag.
"""
import os
import asyncio
import logging

from websocket_utils import get_margins, kite
from bot_controller import controller

TOKEN_CHECK_INTERVAL_SECONDS = int(os.environ.get("TOKEN_CHECK_INTERVAL_SECONDS", "900"))  # 15 min default


def _check_once_sync() -> bool:
    # profile() is what setup_websocket() itself relies on, so it's the most
    # reliable signal of whether the current token actually works end to end.
    # (margins() has been observed to not raise even when the token is stale,
    # so it isn't trustworthy as the sole check.)
    try:
        profile = kite.profile()
        if not profile or "user_id" not in profile:
            logging.warning("Kite token health check: profile() returned no usable data")
            return False
        return True
    except Exception as e:
        logging.warning(f"Kite token health check failed (profile): {e}")
        return False


async def token_health_loop():
    logging.info("Token health check loop started")
    while True:
        # Skip the check entirely while a manual refresh is running, to avoid
        # flapping the flag mid-refresh.
        if not controller.token_refresh_in_progress:
            ok = await asyncio.get_event_loop().run_in_executor(None, _check_once_sync)
            if ok:
                controller.mark_token_valid()
            else:
                controller.mark_token_expired()

        await asyncio.sleep(TOKEN_CHECK_INTERVAL_SECONDS)

