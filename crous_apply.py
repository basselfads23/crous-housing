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
import random
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

# Import session validation and auto-login
try:
    from crous_auth import is_session_valid, auto_login
except ImportError:
    is_session_valid = lambda: (False, "crous_auth not found")
    auto_login = lambda: (False, "crous_auth not found")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("crous_apply")


def _pick_mode_index(label_texts: list[str], target_mode: str) -> int | None:
    """
    Given a list of lowercased label texts (in DOM order) for a set of occupation-mode
    radio buttons or <select> options, return the index of the one matching target_mode
    ("single", "colocation", or "any").

    Returns None if nothing matches for "single"/"colocation" -- deliberately does NOT
    fall back to an arbitrary option, so the caller can fail loudly instead of silently
    submitting a request for the wrong occupation mode (this used to default to index 0,
    which for a "colocation" target with no matching label would very likely select
    "Individuel" instead -- exactly the kind of wrong-mode misfire the price-tier
    targeting in crous_watcher.py's is_target_listing() is trying to avoid).

    "any" (CLI-only manual testing mode, never produced by the automated watcher) has no
    price-tier precision to protect, so it falls back to the first option.
    """
    for i, text in enumerate(label_texts):
        if target_mode == "single" and ("seul" in text or "indiv" in text):
            return i
        if target_mode == "colocation" and "coloc" in text:
            return i
        if target_mode == "any" and ("seul" in text or "indiv" in text):
            return i
    if target_mode == "any" and label_texts:
        return 0
    return None


SCREENSHOT_MAX_AGE_DAYS = 14


def _prune_old_screenshots(max_age_days: int = SCREENSHOT_MAX_AGE_DAYS) -> None:
    """Delete screenshots older than max_age_days so the directory doesn't grow forever."""
    cutoff = time.time() - max_age_days * 86400
    try:
        for f in SCREENSHOTS_DIR.glob("*.png"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _apply_for_accommodation_core(
    tool_id: str,
    accommodation_id: str,
    target_mode: str = "single",
    dry_run: bool = True,
    timeout_seconds: int = 25
) -> dict:
    """
    Core implementation to apply for an accommodation using Playwright.
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

    # Pre-flight session validity check
    valid, msg = is_session_valid()
    if not valid:
        logger.warning(f"Session is invalid before applying: {msg}. Attempting auto-renewal...")
        try:
            from crous_auth import auto_login
            renewed, renew_msg = auto_login()
            if not renewed:
                err = f"Session expirée ({msg}) et échec du renouvellement automatique: {renew_msg}"
                logger.error(err)
                result["error"] = err
                result["step"] = "preflight_auth"
                return result
            logger.info("✅ Session successfully auto-renewed!")
        except Exception as renew_err:
            err = f"Session invalide ({msg}) et erreur lors du renouvellement: {renew_err}"
            logger.error(err)
            result["error"] = err
            result["step"] = "preflight_auth"
            return result

    if not SESSION_FILE.exists():
        err = "session.json does not exist. Run 'python crous_auth.py --auto' first."
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
        launch_kwargs = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        try:
            import proxy_manager
            pw_proxy = proxy_manager.get_sniper_proxy()
        except ImportError:
            pw_proxy = None

        if pw_proxy:
            launch_kwargs["proxy"] = pw_proxy
            result["_proxy_used"] = pw_proxy
        elif os.getenv("CROUS_PROXY"):
            launch_kwargs["proxy"] = {"server": os.getenv("CROUS_PROXY")}
            result["_proxy_used"] = os.getenv("CROUS_PROXY")

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            storage_state=str(SESSION_FILE),
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.set_default_timeout(timeout_seconds * 1000)

        # Block images, fonts, media, and analytics to prevent Apache rate-limiting and maximize speed
        page.route(
            "**/*",
            lambda r: r.abort()
            if r.request.resource_type in ("image", "media", "font") or "matomo" in r.request.url
            else r.continue_(),
        )

        try:
            # 1. First ensure any rules onboarding is passed
            rules_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/rules"
            acc_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/accommodations/{accommodation_id}"
            request_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/cart/requests/create/{accommodation_id}"

            # Check if rules page needs onboarding submit
            page.goto(rules_url, wait_until="domcontentloaded")
            sub_btn = page.locator("button[name='searchSubmit']").first
            if sub_btn.is_visible():
                logger.info("Passing rules onboarding requirement...")
                sub_btn.click()
                page.wait_for_timeout(1000)
                try:
                    context.storage_state(path=str(SESSION_FILE))
                except Exception:
                    pass

            # Random navigation delay (1.0 to 1.5s)
            time.sleep(random.uniform(1.0, 1.5))

            # 2. Check accommodation page to ensure it is added to cart selection
            logger.info(f"Navigating to accommodation page: {acc_url}")
            resp = page.goto(acc_url, wait_until="domcontentloaded")

            # Check for Rate Limit (HTTP 429)
            if resp and resp.status == 429:
                err = "Rate limited by CROUS (HTTP 429)."
                logger.error(err)
                result["error"] = err
                result["step"] = "rate_limited"
                return result

            # Check if redirected to login
            if "/mse/discovery/connect" in page.url or "login" in page.url:
                err = "Session expired or invalid. Redirected to login page."
                logger.error(err)
                result["error"] = err
                result["step"] = "auth_redirect"
                return result

            # Check if rules page appears again
            if "/rules" in page.url:
                logger.info("Encountered rules onboarding page. Bypassing...")
                pass_btn = page.locator("button[name='searchSubmit'], button:has-text('Passer à la recherche')").first
                if pass_btn.is_visible():
                    pass_btn.click()
                    time.sleep(random.uniform(1.0, 1.5))
                    resp = page.goto(acc_url, wait_until="domcontentloaded")

            # Look for 'Ajouter à ma sélection' (if already added, button shows 'Retirer de ma sélection')
            add_btn = page.locator("button:has-text('Ajouter à ma sélection')").first
            if add_btn.is_visible():
                logger.info("Clicking 'Ajouter à ma sélection'...")
                add_btn.click()
                time.sleep(random.uniform(1.0, 1.5))

            # Random navigation delay before opening request page (1.0 to 1.5s)
            time.sleep(random.uniform(1.0, 1.5))

            # 3. Navigate directly to request creation page
            logger.info(f"Navigating to request URL: {request_url}")
            resp = page.goto(request_url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            # Check for Rate Limit (HTTP 429 or 'Too Many Requests' text)
            page_text = page.content().lower()
            if (resp and resp.status == 429) or "too many requests" in page_text:
                err = "Rate limited by CROUS (HTTP 429 - Too Many Requests)."
                logger.warning(err)
                try:
                    import proxy_manager
                    proxy_manager.rotate_sniper_proxy()
                    logger.info("Rotated sniper proxy due to rate limiting.")
                except Exception:
                    pass
                result["error"] = err
                result["step"] = "rate_limited"
                try:
                    err_screenshot = SCREENSHOTS_DIR / f"error_429_{accommodation_id}_{int(time.time())}.png"
                    page.screenshot(path=str(err_screenshot), full_page=True)
                    result["screenshot_path"] = str(err_screenshot)
                except Exception:
                    pass
                return result

            # Check for HTTP 401 Unauthorized / Identification requise
            if (resp and resp.status == 401) or "identification requise" in page_text or "connexion requise" in page_text:
                err = "Session expirée ou non connectée (HTTP 401 - Identification requise)."
                logger.error(err)
                result["error"] = err
                result["step"] = "auth_expired"
                try:
                    err_screenshot = SCREENSHOTS_DIR / f"error_401_{accommodation_id}_{int(time.time())}.png"
                    page.screenshot(path=str(err_screenshot), full_page=True)
                    result["screenshot_path"] = str(err_screenshot)
                except Exception:
                    pass
                return result

            # Check for HTTP 403
            if resp and resp.status == 403:
                body_text = page.inner_text("body").lower()
                if "sélection" in body_text or "selection" in body_text or "ne pouvez pas demander" in body_text:
                    err = "Le logement n'est pas disponible à la réservation (logement complet ou non réservable)."
                    logger.warning(err)
                    result["error"] = err
                    result["step"] = "not_reservable"
                else:
                    err = "Accès refusé par CROUS (HTTP 403)."
                    logger.error(err)
                    result["error"] = err
                    result["step"] = "forbidden"
                return result

            # Check if redirected to login
            if "/mse/discovery/connect" in page.url or "login" in page.url:
                err = "Session expired or invalid. Redirected to login page."
                logger.error(err)
                result["error"] = err
                result["step"] = "auth_redirect"
                return result

            # Wait for form container to load
            result["step"] = "form_loading"
            try:
                page.wait_for_selector("form, .CartRequestPage, .fr-fieldset", timeout=8000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(500)
                logger.info("Request form loaded successfully.")
            except Exception as wait_err:
                page_text_check = page.content().lower()
                if "too many requests" in page_text_check:
                    err = "Rate limited by CROUS (HTTP 429 - Too Many Requests)."
                    result["step"] = "rate_limited"
                else:
                    err = f"Formulaire introuvable : {wait_err}"
                    result["step"] = "form_missing"
                logger.warning(err)
                result["error"] = err
                try:
                    err_screenshot = SCREENSHOTS_DIR / f"error_form_{accommodation_id}_{int(time.time())}.png"
                    page.screenshot(path=str(err_screenshot), full_page=True)
                    result["screenshot_path"] = str(err_screenshot)
                except Exception:
                    pass
                return result

            # 3. Fill Occupation Mode (support radio button and select dropdown). Whichever
            # selector is actually present is treated as authoritative; if it doesn't have
            # an option matching the requested mode, fail loudly instead of guessing --
            # silently submitting the wrong occupation mode would defeat the whole point of
            # the price-tier targeting upstream in crous_watcher.py's is_target_listing().
            result["step"] = "filling_fields"
            selected_radio = None
            mode_selected = False

            radios = page.locator("input[type='radio']").all()
            if radios:
                radio_ids = [r.get_attribute("id") for r in radios]
                label_texts = []
                for r_id in radio_ids:
                    label_el = page.locator(f"label[for='{r_id}']").first if r_id else None
                    label_texts.append(label_el.inner_text().lower() if (label_el and label_el.count() > 0) else "")

                idx = _pick_mode_index(label_texts, target_mode)
                if idx is None:
                    err = (f"No radio button matches requested mode '{target_mode}' "
                           f"(found labels: {label_texts}); refusing to guess.")
                    logger.error(err)
                    result["error"] = err
                    result["step"] = "mode_selection_failed"
                    try:
                        err_screenshot = SCREENSHOTS_DIR / f"error_mode_{accommodation_id}_{int(time.time())}.png"
                        page.screenshot(path=str(err_screenshot), full_page=True)
                        result["screenshot_path"] = str(err_screenshot)
                    except Exception:
                        pass
                    return result

                selected_radio = radios[idx]
                r_id = radio_ids[idx]
                label_el = page.locator(f"label[for='{r_id}']").first if r_id else None
                if label_el and label_el.count() > 0:
                    label_el.click()
                selected_radio.check(force=True)
                try:
                    selected_radio.dispatch_event("change")
                    selected_radio.dispatch_event("input")
                except Exception:
                    pass
                page.wait_for_timeout(500)
                logger.info(f"Selected occupation mode radio: {r_id} (checked={selected_radio.is_checked()})")
                mode_selected = True

            if not mode_selected:
                mode_select = page.locator("select[name*='occupationMode'], select#ModalitiesForm-occupationMode").first
                if mode_select.is_visible():
                    options = mode_select.locator("option").all_inner_texts()
                    logger.info(f"Occupation mode dropdown options found: {options}")
                    idx = _pick_mode_index([o.lower() for o in options], target_mode)
                    if idx is None:
                        err = (f"No dropdown option matches requested mode '{target_mode}' "
                               f"(found options: {options}); refusing to guess.")
                        logger.error(err)
                        result["error"] = err
                        result["step"] = "mode_selection_failed"
                        try:
                            err_screenshot = SCREENSHOTS_DIR / f"error_mode_{accommodation_id}_{int(time.time())}.png"
                            page.screenshot(path=str(err_screenshot), full_page=True)
                            result["screenshot_path"] = str(err_screenshot)
                        except Exception:
                            pass
                        return result
                    target_value = options[idx].strip()
                    mode_select.select_option(label=target_value)
                    logger.info(f"Selected occupation mode dropdown: {target_value}")

            # Study level if present (User requested Master, e.g. level 4 / Master)
            study_select = page.locator("select[name*='studyLevel'], select#CartRequestForm-studyLevel").first
            if study_select.is_visible():
                opts = study_select.locator("option").all_inner_texts()
                master_opt = next((o.strip() for o in opts if "master" in o.lower() or "m1" in o.lower() or "m2" in o.lower()), None)
                if master_opt:
                    study_select.select_option(label=master_opt)
                    logger.info(f"Selected study level: {master_opt}")
                else:
                    try:
                        study_select.select_option(value=STUDY_LEVEL)
                        logger.info(f"Selected study level value: {STUDY_LEVEL}")
                    except Exception as study_err:
                        logger.warning(
                            f"Could not select a study level (no 'Master' option and "
                            f"STUDY_LEVEL='{STUDY_LEVEL}' didn't match any option value: "
                            f"{study_err}). Proceeding without setting it -- this listing may "
                            f"have eligibility requirements that go unmet as a result."
                        )

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

            # Random pause before advancing to Step 2 (1.0 to 1.5s)
            time.sleep(random.uniform(1.0, 1.5))

            # Ensure radio button remains selected before advancing
            if selected_radio and not selected_radio.is_checked():
                logger.warning("Radio button became unchecked before submit; re-checking...")
                selected_radio.check(force=True)
                try:
                    selected_radio.dispatch_event("change")
                    selected_radio.dispatch_event("input")
                except Exception:
                    pass
                page.wait_for_timeout(300)

            # 4. Advance to Step 2 (Vérification et validation de votre demande)
            result["step"] = "submitting_step1"
            verify_btn = page.locator("button[type='submit']:has-text('Vérifier ma demande'), button:has-text('Vérifier ma demande'), button:has-text('Vérifier')").first
            if verify_btn.is_visible():
                logger.info("Clicking 'Vérifier ma demande' button...")
                verify_btn.click()
            else:
                step_btn = page.locator("button[type='submit'], form button.fr-btn").first
                if step_btn.is_visible():
                    logger.info("Clicking form submit button...")
                    step_btn.click()

            page.wait_for_timeout(1000)

            # Wait for Step 2 to load ("Étape 2 sur 2" / "Envoyer la demande" button)
            result["step"] = "step2_review"
            logger.info("Waiting for Step 2 (Vérification et validation de votre demande)...")
            step2_reached = False
            try:
                envoyer_btn = page.locator("button:has-text('Envoyer la demande')").first
                envoyer_btn.wait_for(state="visible", timeout=15000)
                logger.info("Successfully reached Step 2! 'Envoyer la demande' is visible.")
                step2_reached = True
            except Exception as e:
                logger.warning(f"Timeout waiting for Step 2 element: {e}")

            if not step2_reached:
                result["success"] = False
                result["step"] = "dry_run_step1_incomplete"
                result["error"] = "Le formulaire n'a pas basculé sur l'Étape 2 de validation."
                try:
                    err_screenshot = SCREENSHOTS_DIR / f"error_step1_{accommodation_id}_{int(time.time())}.png"
                    page.screenshot(path=str(err_screenshot), full_page=True)
                    result["screenshot_path"] = str(err_screenshot)
                except Exception:
                    pass
                return result

            page.wait_for_timeout(500)

            # 5. Handle DRY-RUN vs LIVE
            screenshot_name = f"{'dry_run_step2' if dry_run else 'applied'}_{accommodation_id}_{int(time.time())}.png"
            screenshot_file = SCREENSHOTS_DIR / screenshot_name

            if dry_run:
                page.screenshot(path=str(screenshot_file), full_page=True)
                logger.info(f"🧪 [DRY-RUN] Captured full Step 2 summary screenshot to: {screenshot_file}")
                result["success"] = step2_reached
                result["screenshot_path"] = str(screenshot_file)
                result["step"] = "dry_run_step2_verified" if step2_reached else "dry_run_step1_incomplete"
                result["duration_seconds"] = round(time.time() - start_time, 2)
                return result

            if not step2_reached:
                err = "Could not proceed to Step 2 before submitting."
                logger.error(err)
                result["error"] = err
                return result

            # Random pause before final live confirmation (1.0 to 1.5s)
            time.sleep(random.uniform(1.0, 1.5))

            # LIVE SUBMISSION - Click "Envoyer la demande"
            result["step"] = "final_submission"
            confirm_btn = page.locator(
                "button:has-text('Envoyer la demande'), button:has-text('Confirmer ma demande'), button:has-text('Soumettre'), button:has-text('Valider ma demande')"
            ).first

            if confirm_btn.is_visible():
                logger.info("⚡ LIVE: Clicking 'Envoyer la demande' button...")
                confirm_btn.click(force=True, no_wait_after=True)
                page.wait_for_timeout(3000)
            else:
                logger.warning("'Envoyer la demande' button not found directly, checking for any submit button...")
                any_submit = page.locator("button[type='submit']").first
                if any_submit.is_visible():
                    any_submit.click(no_wait_after=True)
                    page.wait_for_timeout(3000)

            # Capture confirmation screenshot
            page.wait_for_timeout(2000)
            try:
                doc_height = page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, 1200)")
                page.set_viewport_size({"width": 1280, "height": min(int(doc_height) + 50, 2500)})
            except Exception:
                pass

            page.screenshot(path=str(screenshot_file))
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
            try:
                if 'page' in locals() and page:
                    result["_exit_page"] = page.url
            except Exception:
                pass
            browser.close()


def apply_for_accommodation(
    tool_id: str,
    accommodation_id: str,
    target_mode: str = "single",
    dry_run: bool = True,
    timeout_seconds: int = 25
) -> dict:
    """
    Public wrapper for accommodation application.
    Executes the application and records every attempt in sniper.log,
    alerting via Telegram if unsuccessful.
    """
    target_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/cart/requests/create/{accommodation_id}"
    _prune_old_screenshots()
    result = None
    try:
        result = _apply_for_accommodation_core(
            tool_id=tool_id,
            accommodation_id=accommodation_id,
            target_mode=target_mode,
            dry_run=dry_run,
            timeout_seconds=timeout_seconds
        )
        return result
    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
            "step": "unhandled_exception",
            "duration_seconds": 0.0
        }
        return result
    finally:
        try:
            import activity_logger
            proxy_used = result.get("_proxy_used") if result else None
            exit_page = result.get("_exit_page", target_url) if result else target_url
            success = bool(result.get("success", False)) if result else False
            err_msg = result.get("error") if (result and not success) else None
            activity_logger.log_sniper_attempt(
                proxy_raw=proxy_used,
                target_url=target_url,
                exit_page=exit_page,
                success=success,
                error_message=err_msg
            )
        except Exception as log_err:
            logger.error(f"Failed to log sniper attempt: {log_err}")


def main():
    parser = argparse.ArgumentParser(description="CROUS Automated Housing Application Worker")
    parser.add_argument("--accommodation-id", required=True, help="Target accommodation ID")
    parser.add_argument("--tool-id", default="47", help="CROUS tool ID (default: 47)")
    parser.add_argument("--dry-run", action="store_true", default=None, help="Stop before final confirmation and take screenshot")
    parser.add_argument("--live", action="store_true", help="Perform live reservation (override DRY_RUN=true)")

    parser.add_argument("--mode", default="single", choices=["single", "colocation", "any"], help="Target occupation mode")

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
    print(f"Target Accommodation: {args.accommodation_id} | Tool: {args.tool_id} | Mode: {args.mode}")
    print("=" * 65 + "\n")

    res = apply_for_accommodation(
        tool_id=args.tool_id,
        accommodation_id=args.accommodation_id,
        target_mode=args.mode,
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

