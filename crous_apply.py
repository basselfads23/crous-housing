#!/usr/bin/env python3
"""
CROUS Automated Housing Application Worker (Sniper)
===================================================
Automates applying for CROUS housing using Playwright and saved session credentials.

Features:
- Headless execution with pre-authenticated session.json.
- Safe --dry-run mode for testing without committing reservations.
- Direct-to-form navigation for sub-second form completion.
- Full screenshot capture at confirmation/summary step.
- Callable from CLI or imported as a module in crous_watcher.py.
"""

import os
import sys
import time
import argparse
import logging
from pathlib import Path

# Fix Windows console UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

PREFERRED_OCCUPATION_MODE = os.getenv("PREFERRED_OCCUPATION_MODE", "single").strip().lower()
STUDY_LEVEL = os.getenv("STUDY_LEVEL", "3").strip()
PURPOSE = os.getenv("PURPOSE", "studies").strip().lower()
ALREADY_ACCOMMODATED = os.getenv("ALREADY_ACCOMMODATED", "false").lower() in ("true", "1", "yes")
TAXES_IN_FRANCE = os.getenv("TAXES_IN_FRANCE", "true").lower() in ("true", "1", "yes")
AUTO_APPLY_DRY_RUN = os.getenv("AUTO_APPLY_DRY_RUN", "true").lower() in ("true", "1", "yes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("crous_apply")


def apply_for_accommodation(
    tool_id: str,
    accommodation_id: str,
    dry_run: bool = True,
    timeout_seconds: int = 25
) -> dict:
    """
    Apply for an accommodation on trouverunlogement.lescrous.fr using Playwright.
    Returns:
      {
        "success": bool,
        "error": str | None,
        "screenshot_path": str | None,
        "step": str,
        "duration_seconds": float,
        "cart_url": str,
        "payment_url": str | None
      }
    """
    start_time = time.time()
    result = {
        "success": False,
        "error": None,
        "screenshot_path": None,
        "step": "init",
        "duration_seconds": 0.0,
        "cart_url": f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/cart",
        "payment_url": None
    }

    if not SESSION_FILE.exists():
        err = "session.json does not exist. Run 'python crous_auth.py --login' first."
        logger.error(err)
        result["error"] = err
        return result

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        err = "Playwright is not installed in Python environment."
        logger.error(err)
        result["error"] = err
        return result

    logger.info(f"🚀 Starting {'DRY-RUN' if dry_run else 'LIVE'} application for accommodation ID {accommodation_id} (tool {tool_id})")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu"
            ]
        )
        context = browser.new_context(
            storage_state=str(SESSION_FILE),
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.set_default_timeout(timeout_seconds * 1000)

        try:
            # 1. Direct navigation to the request creation page
            request_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/cart/requests/create/{accommodation_id}"
            logger.info(f"Navigating to request URL: {request_url}")
            resp = page.goto(request_url, wait_until="domcontentloaded")

            # Check if redirected to login
            if "/mse/discovery/connect" in page.url or "login" in page.url:
                err = "Session expired or invalid. Redirected to login page."
                logger.error(err)
                result["error"] = err
                result["step"] = "auth_redirect"
                return result

            # If request creation page is not directly found (e.g. 404), fallback to accommodation page
            if resp and resp.status == 404:
                acc_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/accommodations/{accommodation_id}"
                logger.info(f"Request page 404. Trying accommodation page: {acc_url}")
                page.goto(acc_url, wait_until="networkidle")
                # Look for 'Ajouter à ma sélection'
                add_btn = page.locator("button:has-text('Ajouter à ma sélection'), button[title*='sélection']").first
                if add_btn.is_visible():
                    add_btn.click()
                    page.wait_for_timeout(1000)
                    page.goto(request_url, wait_until="networkidle")

            # Wait for form container to load
            result["step"] = "form_loading"
            page.wait_for_selector("form, .CartRequestPage, .fr-fieldset", timeout=10000)
            logger.info("Request form loaded successfully.")

            # 2. Fill Occupation Mode
            result["step"] = "filling_fields"
            mode_select = page.locator("select[name*='occupationMode'], select#ModalitiesForm-occupationMode, select").first
            if mode_select.is_visible():
                options = mode_select.locator("option").all_inner_texts()
                logger.info(f"Occupation mode options found: {options}")
                # Pick preferred mode
                target_value = None
                for opt in options:
                    if PREFERRED_OCCUPATION_MODE in opt.lower() or ("seul" in opt.lower() and PREFERRED_OCCUPATION_MODE == "single") or ("indiv" in opt.lower() and PREFERRED_OCCUPATION_MODE == "single"):
                        target_value = opt.strip()
                        break
                if target_value:
                    mode_select.select_option(label=target_value)
                    logger.info(f"Selected occupation mode: {target_value}")
                elif options:
                    # Select first valid option
                    mode_select.select_option(index=1 if len(options) > 1 else 0)

            # Study level if present
            study_select = page.locator("select[name*='studyLevel'], select#CartRequestForm-studyLevel").first
            if study_select.is_visible():
                study_select.select_option(value=STUDY_LEVEL)
                logger.info(f"Selected study level: {STUDY_LEVEL}")

            # Checkboxes: taxesInFrance & alreadyAccommodated
            if TAXES_IN_FRANCE:
                tax_box = page.locator("input[name*='taxesInFrance']").first
                if tax_box.is_visible() and not tax_box.is_checked():
                    tax_box.check()

            already_acc_box = page.locator("input[name*='alreadyAccommodated']").first
            if already_acc_box.is_visible():
                if ALREADY_ACCOMMODATED and not already_acc_box.is_checked():
                    already_acc_box.check()
                elif not ALREADY_ACCOMMODATED and already_acc_box.is_checked():
                    already_acc_box.uncheck()

            # 3. Advance to Review / Summary Step
            result["step"] = "submitting_form_step"
            # Look for step forward button (e.g. "Suivant", "Soumettre", arrow button)
            step_btn = page.locator("button[type='submit'], button.fr-fi-arrow-right-line, form button.fr-btn").first
            if step_btn.is_visible():
                step_btn.click()
                logger.info("Clicked form step submit button.")

            # Wait for summary step or confirmation button
            page.wait_for_timeout(1500)
            result["step"] = "summary_review"

            # Check if we have reached the summary / confirmation stage
            confirm_btn = page.locator(
                "button:has-text('Confirmer ma demande'), button:has-text('Soumettre'), button:has-text('Valider ma demande')"
            ).first

            # 4. Handle DRY-RUN vs LIVE
            screenshot_name = f"{'dry_run' if dry_run else 'applied'}_{accommodation_id}_{int(time.time())}.png"
            screenshot_file = SCREENSHOTS_DIR / screenshot_name

            if dry_run:
                page.screenshot(path=str(screenshot_file), full_page=True)
                logger.info(f"🧪 [DRY-RUN] Captured summary screenshot to: {screenshot_file}")
                result["success"] = True
                result["screenshot_path"] = str(screenshot_file)
                result["step"] = "dry_run_complete"
                result["duration_seconds"] = round(time.time() - start_time, 2)
                return result

            # LIVE SUBMISSION
            result["step"] = "final_submission"
            if confirm_btn.is_visible():
                logger.info("⚡ LIVE: Clicking final confirmation button...")
                confirm_btn.click()
                page.wait_for_load_state("networkidle", timeout=10000)
            else:
                logger.warning("Confirmation button not found directly, checking for any submit button...")
                any_submit = page.locator("button[type='submit']").first
                if any_submit.is_visible():
                    any_submit.click()
                    page.wait_for_load_state("networkidle", timeout=10000)

            # Capture confirmation screenshot
            page.wait_for_timeout(1500)
            page.screenshot(path=str(screenshot_file), full_page=True)
            result["screenshot_path"] = str(screenshot_file)

            # Check for payment button or request status
            pay_link = page.locator("a[href*='payment'], a:has-text('Payer')").first
            if pay_link.is_visible():
                result["payment_url"] = pay_link.get_attribute("href")

            result["success"] = True
            result["step"] = "live_submitted"
            result["duration_seconds"] = round(time.time() - start_time, 2)
            logger.info(f"🎉 Application submitted successfully in {result['duration_seconds']}s!")
            return result

        except Exception as err:
            logger.exception(f"Error during application execution: {err}")
            # Still attempt an error screenshot
            err_screenshot = SCREENSHOTS_DIR / f"error_{accommodation_id}_{int(time.time())}.png"
            try:
                page.screenshot(path=str(err_screenshot), full_page=True)
                result["screenshot_path"] = str(err_screenshot)
            except Exception:
                pass
            result["error"] = str(err)
            result["duration_seconds"] = round(time.time() - start_time, 2)
            return result

        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description="CROUS Automated Housing Application Worker")
    parser.add_argument("--accommodation-id", required=True, help="Target accommodation ID")
    parser.add_argument("--tool-id", default="47", help="CROUS tool ID (default: 47)")
    parser.add_argument("--dry-run", action="store_true", default=None, help="Stop before final confirmation and take screenshot")
    parser.add_argument("--live", action="store_true", help="Perform live reservation (override DRY_RUN=true)")

    args = parser.parse_args()

    # Determine dry-run mode
    if args.live:
        dry_run = False
    elif args.dry_run is not None:
        dry_run = args.dry_run
    else:
        dry_run = AUTO_APPLY_DRY_RUN

    print("\n" + "=" * 65)
    print(f"🎯 CROUS HOUSING APPLICANT ({'DRY-RUN TEST' if dry_run else 'LIVE APPLICATION'})")
    print(f"Target Accommodation: {args.accommodation_id} | Tool: {args.tool_id}")
    print("=" * 65 + "\n")

    res = apply_for_accommodation(
        tool_id=args.tool_id,
        accommodation_id=args.accommodation_id,
        dry_run=dry_run
    )

    print("\n" + "=" * 65)
    if res["success"]:
        print("✅ APPLICATION PROCESS COMPLETED SUCCESSFULLY")
        print(f"Duration: {res['duration_seconds']}s")
        if res.get("screenshot_path"):
            print(f"Screenshot: {res['screenshot_path']}")
        if res.get("payment_url"):
            print(f"Payment Link: {res['payment_url']}")
        sys.exit(0)
    else:
        print("❌ APPLICATION FAILED")
        print(f"Error: {res.get('error')}")
        print(f"Failed at step: {res.get('step')}")
        if res.get("screenshot_path"):
            print(f"Error Screenshot: {res['screenshot_path']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

