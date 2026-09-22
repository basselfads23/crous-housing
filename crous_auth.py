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
import hashlib
import base64
import urllib.request
from pathlib import Path
from dotenv import load_dotenv

# Fix Windows console UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
AUTH_MAX_PROXY_ATTEMPTS = 3
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("crous_auth")


def solve_altcha(challenge_data: dict) -> str:
    """
    Solve Altcha Proof-of-Work challenge in pure Python using fast C hashlib.
    Returns base64 encoded JSON payload.
    """
    algorithm = challenge_data.get("algorithm", "SHA-256")
    challenge = challenge_data["challenge"]
    salt = challenge_data["salt"]
    salt_bytes = salt.encode("utf-8")
    max_number = int(challenge_data.get("maxNumber", 1000000))
    signature = challenge_data.get("signature", "")

    solution_number = None
    for i in range(max_number + 1):
        if hashlib.sha256(salt_bytes + str(i).encode("utf-8")).hexdigest() == challenge:
            solution_number = i
            break

    if solution_number is None:
        raise ValueError(f"Could not solve Altcha PoW within maxNumber {max_number}")

    payload = {
        "algorithm": algorithm,
        "challenge": challenge,
        "number": solution_number,
        "salt": salt,
        "signature": signature
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")


def _auto_login_attempt(email, password, pw_proxy) -> tuple[bool, str, bool]:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

    logger.info(f"Initiating headless automated login for: {email}")

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            "ignore_default_args": ["--enable-automation"]
        }
        if isinstance(pw_proxy, dict):
            launch_kwargs["proxy"] = pw_proxy

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900}
        )
        page = context.new_page()

        try:
            # 1. Start discovery / login redirect
            login_url = "https://trouverunlogement.lescrous.fr/mse/discovery/connect"
            logger.info("Connecting to discovery URL...")
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

            # 2. Select MSEConnect option (Option 0)
            if "dispatcher" in page.url or page.locator("input[name='login[app]']").count() > 0:
                logger.info("Selecting MSEConnect button on dispatcher...")
                with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
                    page.evaluate('''() => {
                        const btn = document.querySelector('input[name="login[app]"][value="0"]');
                        if (btn) btn.click();
                        else document.querySelector('label.loginapp-button').click();
                    }''')
                page.wait_for_load_state("networkidle", timeout=15000)

            # 3. Wait for credentials form
            page.wait_for_selector("input#login_login, input[name='login[login]']", timeout=15000)
            logger.info("Filling login credentials...")
            page.fill("input#login_login, input[name='login[login]']", email)
            page.fill("input#login_password, input[name='login[password]']", password)

            # 4. Extract and solve Altcha PoW
            logger.info("Detecting Altcha Proof-of-Work challenge...")
            widget = page.locator("altcha-widget").first
            challenge_json_str = widget.get_attribute("challengejson")
            if not challenge_json_str:
                return False, "Failed to retrieve Altcha challenge from login page.", True

            ch_data = json.loads(challenge_json_str)
            logger.info("Solving Altcha challenge mathematically...")
            t0 = time.time()
            payload_b64 = solve_altcha(ch_data)
            duration = round(time.time() - t0, 3)
            logger.info(f"Altcha PoW solved in {duration}s!")

            # 5. Inject payload and set validity
            page.evaluate('''(b64) => {
                const w = document.querySelector('altcha-widget');
                const cb = w ? w.querySelector('input[type="checkbox"]') : null;
                if (cb) {
                    cb.checked = true;
                    cb.removeAttribute('required');
                }
                let hidden = document.querySelector('input[type="hidden"][name="login[altcha]"]');
                if (!hidden) {
                    hidden = document.createElement('input');
                    hidden.type = 'hidden';
                    hidden.name = 'login[altcha]';
                    document.querySelector('form').appendChild(hidden);
                }
                hidden.value = b64;
            }''', payload_b64)

            # 6. Submit form and wait for redirect
            logger.info("Submitting authentication form...")
            with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                page.evaluate('() => document.querySelector("form").submit()')

            page.wait_for_load_state("networkidle", timeout=15000)
            logger.info(f"Landing URL after auth: {page.url}")

            # Check for invalid credentials message
            page_content = page.content().lower()
            if "auth/sql/login" in page.url or "incorrects" in page_content or "identifiant ou mot de passe incorrect" in page_content:
                screenshot_path = BASE_DIR / "screenshots" / "login_failed.png"
                page.screenshot(path=str(screenshot_path))
                return False, "Identifiant ou mot de passe CROUS incorrect.", False

            # Check if rules page is encountered
            if "/rules" in page.url:
                logger.info("Encountered onboarding rules page. Bypassing...")
                pass_btn = page.locator("button:has-text('Passer à la recherche'), a:has-text('Passer à la recherche')").first
                if pass_btn.is_visible():
                    pass_btn.click()
                    page.wait_for_load_state("networkidle", timeout=10000)

            # 7. Save authenticated session state atomically (write to temp file, then rename)
            session_state = context.storage_state()
            tmp_session_file = SESSION_FILE.with_suffix(".json.tmp")
            with open(tmp_session_file, "w", encoding="utf-8") as f:
                json.dump(session_state, f, indent=2)
            os.replace(tmp_session_file, SESSION_FILE)
            logger.info(f"Session saved to {SESSION_FILE}")

            # 8. Verify with /api/health
            valid, msg = is_session_valid()
            if valid:
                logger.info("✅ Headless auto-login successful and verified!")
                return True, "Authentification réussie. Session active.", False
            else:
                return False, f"Login submitted but verification returned: {msg}", False

        except Exception as e:
            logger.exception(f"Error during headless auto-login: {e}")
            screenshot_path = BASE_DIR / "screenshots" / "login_error.png"
            try:
                page.screenshot(path=str(screenshot_path))
            except Exception:
                pass
            is_retryable = ("net::ERR_" in str(e)) or isinstance(e, PlaywrightTimeoutError)
            return False, f"Erreur lors de la connexion automatique: {e}", is_retryable
        finally:
            browser.close()


def auto_login(email: str = None, password: str = None) -> tuple[bool, str]:
    """
    Perform 100% headless automated login to MesServicesEtudiant using Altcha PoW solver.
    Extracts new cookies and saves session.json atomically.
    """
    # Dynamically reload .env so live updates are recognized immediately
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=True)
        except Exception:
            pass
        if not os.getenv("CROUS_EMAIL") or not os.getenv("CROUS_PASSWORD"):
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ[k.strip()] = v.strip()
            except Exception:
                pass

    email = email or os.getenv("CROUS_EMAIL", "").strip()
    password = password or os.getenv("CROUS_PASSWORD", "").strip()

    if not email or not password:
        return False, "CROUS_EMAIL ou CROUS_PASSWORD non configuré dans .env"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "Playwright n'est pas installé dans l'environnement Python."

    try:
        import proxy_manager
    except ImportError:
        proxy_manager = None

    attempts = 0
    while proxy_manager is not None and attempts < AUTH_MAX_PROXY_ATTEMPTS:
        pw_proxy = proxy_manager.get_auth_proxy()
        if pw_proxy is None:
            logger.warning("No validated auth proxy available.")
            break
        attempts += 1
        logger.info(f"Auth login attempt {attempts}/{AUTH_MAX_PROXY_ATTEMPTS} via proxy {pw_proxy['server']}")
        ok, msg, retryable = _auto_login_attempt(email, password, pw_proxy)
        if ok or not retryable:
            return ok, msg
        logger.warning(f"Auth attempt {attempts} failed with a retryable error: {msg}")

    logger.warning("Falling back to DIRECT (no proxy) connection for auth login.")
    ok, msg, _ = _auto_login_attempt(email, password, None)
    return ok, msg


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
        import proxy_manager
    except ImportError:
        proxy_manager = None

    for retry in range(3):
        try:
            if proxy_manager:
                crous_proxy = proxy_manager.get_current_proxy(rotate=(retry > 0))
            else:
                crous_proxy = os.getenv("CROUS_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")

            handlers = []
            if crous_proxy:
                handlers.append(urllib.request.ProxyHandler({"http": crous_proxy, "https": crous_proxy}))
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                is_logged_in = data.get("isUserLoggedIn", False)
                if is_logged_in:
                    return True, "Session is ACTIVE and authenticated."
                else:
                    return False, "Session has EXPIRED or user is not logged in."
        except urllib.error.HTTPError as err:
            if err.code == 429:
                return True, "Session check rate-limited (HTTP 429), assumed active."
            if retry < 2:
                continue
            return False, f"Failed to verify session against CROUS API: {err}"
        except Exception as err:
            if retry < 2:
                continue
            return False, f"Failed to verify session against CROUS API: {err}"
    return False, "Failed to verify session against CROUS API."


def run_login_flow(timeout_seconds: int = 300) -> bool:
    """
    Launch a visible browser for the user to log in interactively.
    Uses stealth anti-detection flags so Altcha and F5 BIG-IP do not hang.
    Saves session.json once login succeeds.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright is not installed! Run: pip install playwright && playwright install chromium")
        return False

    print("\n" + "=" * 65)
    print("🔐 CROUS INTERACTIVE LOGIN HELPER (STEALTH MODE)")
    print("=" * 65)
    print("1. A browser window will now open.")
    print("2. Enter your MesServicesEtudiant email and password.")
    print("3. Complete the Altcha security check and 2FA.")
    print("4. Once you are successfully logged in and redirected back to")
    print("   trouverunlogement.lescrous.fr, this script will automatically")
    print(f"   save your session to: {SESSION_FILE}")
    print("=" * 65 + "\n")

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--start-maximized"
            ],
            "ignore_default_args": ["--enable-automation"]
        }

        # Try launching real installed Chrome first for best stealth
        browser = None
        try:
            browser = p.chromium.launch(channel="chrome", **launch_kwargs)
            logger.info("Using installed Google Chrome (Bypass mode active)")
        except Exception:
            try:
                browser = p.chromium.launch(channel="msedge", **launch_kwargs)
                logger.info("Using Microsoft Edge (Bypass mode active)")
            except Exception:
                browser = p.chromium.launch(**launch_kwargs)
                logger.info("Using Chromium (Bypass mode active)")

        context = browser.new_context(
            no_viewport=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )

        # Remove webdriver flag and disguise automation
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['fr-FR', 'fr', 'en-US', 'en']
            });
        """)

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


def import_cookie_header(cookie_header: str) -> bool:
    """
    Import raw cookie string (e.g. from browser DevTools) into session.json.
    Format: 'name1=val1; name2=val2; ...'
    """
    cookie_header = cookie_header.strip().strip('"').strip("'")
    if not cookie_header:
        logger.error("Empty cookie string provided.")
        return False

    cookies_list = []
    pairs = [p.strip() for p in cookie_header.split(";") if "=" in p]
    for p in pairs:
        k, v = p.split("=", 1)
        k = k.strip()
        v = v.strip()
        cookies_list.append({
            "name": k,
            "value": v,
            "domain": ".trouverunlogement.lescrous.fr",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax"
        })

    data = {
        "cookies": cookies_list,
        "origins": []
    }

    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    logger.info(f"Imported {len(cookies_list)} cookies into {SESSION_FILE}")
    valid, msg = is_session_valid()
    if valid:
        logger.info(f"✅ Verified: {msg}")
        return True
    else:
        logger.warning(f"⚠️ Saved, but verification returned: {msg}")
        return False


def import_json_file(file_path: str) -> bool:
    """Import exported cookies JSON (e.g. from Cookie-Editor extension)."""
    p = Path(file_path)
    if not p.exists():
        logger.error(f"File does not exist: {file_path}")
        return False

    with open(p, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    # If it's already in Playwright storage_state format
    if isinstance(raw_data, dict) and "cookies" in raw_data:
        with open(SESSION_FILE, "w", encoding="utf-8") as out:
            json.dump(raw_data, out, indent=2)
    # If it's a list from Cookie-Editor extension
    elif isinstance(raw_data, list):
        cookies_list = []
        for c in raw_data:
            cookies_list.append({
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain") or ".trouverunlogement.lescrous.fr",
                "path": c.get("path") or "/",
                "expires": c.get("expirationDate") or -1,
                "httpOnly": c.get("httpOnly", False),
                "secure": c.get("secure", True),
                "sameSite": "Lax"
            })
        data = {"cookies": cookies_list, "origins": []}
        with open(SESSION_FILE, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2)
    else:
        logger.error("Unrecognized JSON format.")
        return False

    logger.info(f"Successfully converted cookies to {SESSION_FILE}")
    valid, msg = is_session_valid()
    if valid:
        logger.info(f"✅ Verified: {msg}")
        return True
    else:
        logger.warning(f"⚠️ Saved, but verification returned: {msg}")
        return False


def main():
    parser = argparse.ArgumentParser(description="CROUS Session Authentication Helper")
    parser.add_argument("--auto", action="store_true", help="Automated fully headless login using Altcha solver")
    parser.add_argument("--login", action="store_true", help="Open visible browser with stealth anti-detection (or auto if no GUI)")
    parser.add_argument("--email", type=str, help="CROUS / MesServices email address")
    parser.add_argument("--password", type=str, help="CROUS / MesServices password")
    parser.add_argument("--check", action="store_true", help="Check if current session.json is valid")
    parser.add_argument("--cookie-header", type=str, help="Import raw cookie header string from your regular browser")
    parser.add_argument("--import-json", type=str, help="Import cookies from a JSON file (e.g. Cookie-Editor export)")

    args = parser.parse_args()

    if args.auto:
        success, msg = auto_login(email=args.email, password=args.password)
        if success:
            logger.info(f"✅ {msg}")
            sys.exit(0)
        else:
            logger.error(f"❌ {msg}")
            sys.exit(1)
    elif args.login:
        if os.environ.get("DISPLAY"):
            success = run_login_flow()
            sys.exit(0 if success else 1)
        else:
            logger.info("No DISPLAY detected; falling back to automated headless login...")
            success, msg = auto_login(email=args.email, password=args.password)
            if success:
                logger.info(f"✅ {msg}")
                sys.exit(0)
            else:
                logger.error(f"❌ {msg}")
                sys.exit(1)
    elif args.cookie_header:
        success = import_cookie_header(args.cookie_header)
        sys.exit(0 if success else 1)
    elif args.import_json:
        success = import_json_file(args.import_json)
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
        valid, msg = is_session_valid()
        if valid:
            logger.info(f"✅ {msg}")
        else:
            logger.warning(f"❌ {msg}")
            print("\nOptions to authenticate:")
            print("  1. Headless auto-login: python crous_auth.py --auto [--email ...] [--password ...]")
            print("  2. Stealth browser:     python crous_auth.py --login")
            print("  3. Paste cookie header: python crous_auth.py --cookie-header \"<your_cookies>\"")
            print("  4. Import JSON:         python crous_auth.py --import-json cookies.json")


if __name__ == "__main__":
    main()
