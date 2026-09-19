#!/usr/bin/env python3
"""
CROUS Session Authentication Manager
====================================
Manages user authentication for trouverunlogement.lescrous.fr via MesServicesEtudiant.

Usage:
  python crous_auth.py --login   # Opens visible browser to log in and saves session.json
  python crous_auth.py --check   # Verifies whether current session.json is still valid
"""

import sys
import os
import json
import time
import argparse
import logging
import urllib.request
from pathlib import Path

# Fix Windows console UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("crous_auth")


def get_auth_cookies() -> dict[str, str]:
    """Extract cookies from session.json as a key-value dictionary."""
    if not SESSION_FILE.exists():
        return {}
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = {}
        for c in data.get("cookies", []):
            cookies[c["name"]] = c["value"]
        return cookies
    except Exception as err:
        logger.error(f"Error parsing {SESSION_FILE}: {err}")
        return {}


def is_session_valid() -> tuple[bool, str]:
    """
    Check if current session.json has an active authenticated session.
    Queries https://trouverunlogement.lescrous.fr/api/health with the saved cookies.
    """
    if not SESSION_FILE.exists():
        return False, "No session.json found. Run 'python crous_auth.py --login' first."

    cookies = get_auth_cookies()
    if not cookies:
        return False, "session.json contains no cookies."

    cookie_header = "; ".join([f"{k}={v}" for k, v in cookies.items()])
    url = "https://trouverunlogement.lescrous.fr/api/health"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": cookie_header,
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            is_logged_in = data.get("isUserLoggedIn", False)
            if is_logged_in:
                return True, "Session is ACTIVE and authenticated."
            else:
                return False, "Session has EXPIRED or user is not logged in."
    except Exception as err:
        return False, f"Failed to verify session against CROUS API: {err}"


def run_login_flow(timeout_seconds: int = 300) -> bool:
    """
    Launch a visible browser for the user to log in interactively.
    Saves session.json once login succeeds.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright is not installed! Run: pip install playwright && playwright install chromium")
        return False

    print("\n" + "=" * 65)
    print("🔐 CROUS INTERACTIVE LOGIN HELPER")
    print("=" * 65)
    print("1. A browser window will now open.")
    print("2. Enter your MesServicesEtudiant email and password.")
    print("3. Complete the Altcha security check and 2FA (if prompted).")
    print("4. Once you are successfully logged in and redirected back to")
    print("   trouverunlogement.lescrous.fr, this script will automatically")
    print(f"   save your session to: {SESSION_FILE}")
    print("=" * 65 + "\n")

    with sync_playwright() as p:
        # Launch visible browser
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        login_url = "https://trouverunlogement.lescrous.fr/mse/discovery/connect"
        logger.info(f"Navigating to: {login_url}")
        page.goto(login_url)

        logger.info("Waiting for you to log in in the browser...")
        start_time = time.time()
        authenticated = False

        while time.time() - start_time < timeout_seconds:
            time.sleep(2)
            current_url = page.url

            # Check if returned to trouverunlogement.lescrous.fr
            if "trouverunlogement.lescrous.fr" in current_url and "/mse/discovery/connect" not in current_url:
                # Let page settle
                page.wait_for_load_state("networkidle", timeout=5000)
                # Verify health endpoint
                try:
                    res = page.evaluate("() => fetch('/api/health').then(r => r.json())")
                    if res.get("isUserLoggedIn", False):
                        authenticated = True
                        break
                except Exception:
                    pass

        if authenticated:
            logger.info("✅ Login detected successfully!")
            context.storage_state(path=str(SESSION_FILE))
            logger.info(f"💾 Session saved to: {SESSION_FILE}")
            browser.close()
            print("\n" + "=" * 65)
            print("🎉 SUCCESS! Your session is saved and verified.")
            print("=" * 65)
            print(f"File created: {SESSION_FILE}")
            print("If you are deploying this to your Ubuntu VPS, simply copy this file:")
            print(f"  scp session.json user@your-vps:{BASE_DIR.name}/session.json")
            print("=" * 65 + "\n")
            return True
        else:
            logger.error("Timed out waiting for login completion.")
            browser.close()
            return False


def main():
    parser = argparse.ArgumentParser(description="CROUS Session Authentication Helper")
    parser.add_argument("--login", action="store_true", help="Open visible browser to log in and save session.json")
    parser.add_argument("--check", action="store_true", help="Check if current session.json is valid")

    args = parser.parse_args()

    if args.login:
        success = run_login_flow()
        sys.exit(0 if success else 1)
    elif args.check:
        valid, msg = is_session_valid()
        if valid:
            logger.info(f"✅ {msg}")
            sys.exit(0)
        else:
            logger.warning(f"❌ {msg}")
            sys.exit(1)
    else:
        # Default behavior: check, or show help
        valid, msg = is_session_valid()
        if valid:
            logger.info(f"✅ {msg}")
        else:
            logger.warning(f"❌ {msg}")
            print("\nTo log in, run: python crous_auth.py --login")


if __name__ == "__main__":
    main()
