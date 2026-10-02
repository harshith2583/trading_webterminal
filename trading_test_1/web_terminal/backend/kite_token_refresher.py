"""
kite_token_refresher.py

Adapted from the user's own working Selenium script. Behavior is unchanged -
same login flow, same cookie extraction - just refactored to:
  - accept password/TOTP as function arguments instead of interactive input()
  - never print, log, or persist the password/TOTP anywhere
  - return a result instead of driving the process directly
  - write the token to the SAME path websocket_utils.py already reads from,
    so a restart picks it up automatically
"""
import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException, NoSuchElementException

KITE_LOGIN_URL = "https://kite.zerodha.com/"
POSITIONS_URL = "https://kite.zerodha.com/positions"


def _click_submit_button(driver, retries=5):
    for attempt in range(retries):
        try:
            button = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
            driver.execute_script("arguments[0].click();", button)
            return True
        except (StaleElementReferenceException, NoSuchElementException):
            time.sleep(1)
    return False


def _find_enctoken(driver):
    for cookie in driver.get_cookies():
        if cookie.get("name", "").lower() == "enctoken" and cookie.get("value"):
            return cookie["value"]
    return None


def refresh_enctoken(username: str, password: str, totp: str, token_file_path: str, log=print):
    """
    Runs a real Kite login via headless Chrome and extracts the enctoken cookie.
    `password` and `totp` are used only in-memory for this call and are never
    written to disk or included in any log line.

    Returns (success: bool, message: str).
    """
    log("INFO", "Starting Kite token refresh (headless browser login)...")

    options = webdriver.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)

    try:
        driver.get(KITE_LOGIN_URL)
        time.sleep(3)

        driver.find_element(By.ID, "userid").send_keys(username)
        driver.find_element(By.ID, "password").send_keys(password)

        if not _click_submit_button(driver):
            return False, "Could not submit username/password form."
        time.sleep(3)

        # Same field-reuse quirk as the original working script
        driver.find_element(By.ID, "userid").send_keys(totp)

        time.sleep(1)
        if not _click_submit_button(driver):
            return False, "Could not submit TOTP form."

        time.sleep(7)

        driver.get(POSITIONS_URL)
        time.sleep(7)

        enctoken = _find_enctoken(driver)
        if not enctoken:
            log("INFO", "Enctoken not found on first attempt, retrying after refresh...")
            driver.refresh()
            time.sleep(5)
            enctoken = _find_enctoken(driver)

        if not enctoken:
            return False, "Login appeared to succeed but no enctoken cookie was found. Check credentials/TOTP."

        directory = os.path.dirname(token_file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(token_file_path, "w") as f:
            f.write(enctoken)
        try:
            os.chmod(token_file_path, 0o600)
        except Exception:
            pass

        log("SUCCESS", f"New enctoken written ({len(enctoken)} chars). Restart required to apply it.")
        return True, "Token refreshed successfully."

    except Exception as e:
        return False, f"Token refresh failed: {e}"
    finally:
        driver.quit()
