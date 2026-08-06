#!/usr/bin/env python3
"""
CROUS Housing Portal Watcher (crous_watcher.py)

Monitors https://trouverunlogement.lescrous.fr for new student colocation listings
in Marseille priced <= 400€. Read-only monitoring with ntfy.sh notifications.
"""

import os
import sys
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Setup logging to file and stderr
LOG_FILE = Path("watcher.log")
STATE_FILE = Path("listings_seen.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("crous_watcher")


def load_state() -> dict:
    """Load seen listing IDs and consecutive failure count from STATE_FILE."""
    if not STATE_FILE.exists():
        return {"seen_ids": [], "consecutive_failures": 0}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            # Convert legacy array format to dict format
            return {"seen_ids": data, "consecutive_failures": 0}
        elif isinstance(data, dict):
            return {
                "seen_ids": data.get("seen_ids", []),
                "consecutive_failures": data.get("consecutive_failures", 0)
            }
    except Exception as err:
        logger.warning(f"Failed to read state file {STATE_FILE}: {err}. Resetting state.")

    return {"seen_ids": [], "consecutive_failures": 0}


def save_state(state: dict) -> None:
    """Save state dict to STATE_FILE."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        logger.info(f"State saved to {STATE_FILE}")
    except Exception as err:
        logger.error(f"Failed to write state file {STATE_FILE}: {err}")


def send_ntfy_notification(topic: str, title: str, message: str, tags: str = "house,euro", link: str = None) -> bool:
    """Send notification via ntfy.sh."""
    if not topic:
        logger.warning("NTFY_TOPIC is not set. Skipping notification.")
        return False

    url = f"https://ntfy.sh/{topic.strip()}"
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "ignore"),
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8"
    }
    if link:
        headers["Click"] = link
        headers["Actions"] = f"view, Voir l'offre, {link}, clear=true"

    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info(f"Notification sent successfully to ntfy.sh/{topic}")
                return True
            else:
                logger.error(f"ntfy.sh returned HTTP {resp.status}")
    except Exception as err:
        logger.error(f"Failed to send ntfy notification: {err}")

    return False


async def login_crous(page, email: str, password: str) -> None:
    """Log into CROUS account using Playwright."""
    logger.info("Navigating to CROUS login page...")
    login_url = "https://trouverunlogement.lescrous.fr/mse/discovery/connect"
    await page.goto(login_url, wait_until="networkidle")

    # Check if we are on the MesServices dispatcher page with the MSE Connect button
    mse_connect_btn = page.locator('label[for="login[app][0]"]')
    if await mse_connect_btn.count() > 0:
        logger.info("Clicking MSE Connect button on dispatcher page...")
        await mse_connect_btn.click()
        await page.wait_for_load_state("networkidle")

    # Wait for login inputs
    logger.info("Filling credentials...")
    email_input = page.locator("#login_login, input[name='login[login]']").first
    password_input = page.locator("#login_password, input[name='login[password]']").first

    await email_input.wait_for(state="visible", timeout=15000)
    await password_input.wait_for(state="visible", timeout=15000)

    await email_input.fill(email)
    await password_input.fill(password)

    # Check for Altcha captcha checkbox if present
    altcha_widget = page.locator("altcha-widget").first
    altcha_label = page.locator("altcha-label, .altcha-label, label[for*='altcha']").first
    altcha_checkbox = page.locator(".altcha-checkbox input, input[id*='login[altcha]']").first

    if await altcha_widget.count() > 0 or await altcha_label.count() > 0 or await altcha_checkbox.count() > 0:
        logger.info("Handling Altcha widget...")
        try:
            # 1. Try JS click on internal checkbox/label to ensure web component receives event
            await page.evaluate("""() => {
                const widget = document.querySelector('altcha-widget');
                if (widget) {
                    const cb = widget.querySelector('input[type="checkbox"]') || widget.shadowRoot?.querySelector('input[type="checkbox"]');
                    if (cb) { cb.click(); return; }
                    const lbl = widget.querySelector('label') || widget.shadowRoot?.querySelector('label');
                    if (lbl) { lbl.click(); return; }
                    widget.click();
                }
            }""")
        except Exception as err:
            logger.debug(f"JS Altcha click error: {err}")

        # 2. Backup Playwright click if JS click didn't trigger
        if await altcha_label.count() > 0:
            try:
                await altcha_label.click(timeout=3000)
            except Exception:
                pass

        # Wait for Altcha PoW verification computation
        logger.info("Waiting for Altcha PoW verification...")
        try:
            await page.wait_for_selector(".altcha[data-state='verified'], altcha-widget[aria-checked='true'], altcha-widget[data-state='verified']", timeout=12000)
            logger.info("Altcha widget verified successfully.")
        except Exception:
            logger.warning("Altcha verification selector wait timed out, giving 3s buffer...")
            await page.wait_for_timeout(3000)

    # Submit form
    logger.info("Submitting login form...")
    submit_btn = page.locator("button[type='submit'], input[type='submit']").first
    await submit_btn.click()
    await page.wait_for_load_state("networkidle")

    # Check for authentication errors
    current_url = page.url
    logger.info(f"Page URL after login submission: {current_url}")

    if "auth/sql/login" in current_url or "dispatcher/login" in current_url:
        # Extract all visible error texts and form feedback
        error_elements = page.locator(".alert-danger, .alert, .form-error-message, .invalid-feedback, #boxlogin .alert, .form-error")
        texts = []
        if await error_elements.count() > 0:
            for el in await error_elements.all():
                txt = (await el.text_content()).strip()
                if txt and "Parcoursup" not in txt and txt not in texts:
                    texts.append(txt)

        error_txt = " | ".join(texts)
        if not error_txt:
            # Fallback: get text of boxlogin container to see what message is displayed
            box_text = await page.locator("#boxlogin").text_content() if await page.locator("#boxlogin").count() > 0 else ""
            error_txt = box_text.strip().replace("\n", " ")[:300] if box_text else "Invalid credentials or rejected form submission."

        raise RuntimeError(f"CROUS Login failed (URL remained on login page): {error_txt}")

    logger.info("Successfully authenticated fresh CROUS session.")


async def search_listings(page) -> list[dict]:
    """
    Search housing portal for Marseille colocation listings under 400€.
    Returns list of extracted listing dicts.
    """
    listings = []
    seen_ids = set()
    intercepted_api_items = []

    # Listen for API search responses
    async def handle_response(response):
        if "api/fr/search" in response.url and response.status == 200:
            try:
                data = await response.json()
                items = data.get("results", {}).get("items", [])
                if items:
                    intercepted_api_items.extend(items)
                    logger.info(f"Intercepted {len(items)} items from search API response")
            except Exception as e:
                logger.debug(f"Could not parse API search response: {e}")

    page.on("response", handle_response)

    tool_ids = ["42", "47"]

    for tool_id in tool_ids:
        search_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/search"
        logger.info(f"Navigating to search page: {search_url}")

        try:
            # Clear any default initial page load items
            intercepted_api_items.clear()

            await page.goto(search_url, wait_until="networkidle", timeout=20000)

            # 1. Location input (Marseille)
            loc_input = page.locator("#PlaceAutocompletearia-autocomplete-1-input, #PlaceAutocomplete").first
            if await loc_input.count() > 0:
                await loc_input.fill("Marseille")
                await page.wait_for_timeout(1000)

                suggestions = page.locator(".PlaceAutocomplete__list li")
                if await suggestions.count() > 0:
                    logger.info("Selecting Marseille suggestion...")
                    await suggestions.first.click()
                    await page.wait_for_timeout(500)

            # 2. Max Price (<= 400€)
            price_input = page.locator("#SearchFormPrice").first
            if await price_input.count() > 0:
                await price_input.fill("400")

            # 3. Colocation filter
            coloc_checkbox = page.locator("#SearchOccupationMode--house_sharing").first
            if await coloc_checkbox.count() > 0:
                if not await coloc_checkbox.is_checked():
                    await coloc_checkbox.check(force=True)

            # Clear initial network items right before submitting search
            intercepted_api_items.clear()

            # 4. Execute search
            if await price_input.count() > 0:
                await price_input.press("Enter")
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(2500)

            # Extract from DOM cards (only if matching Marseille)
            cards = await page.locator(".fr-card").all()
            logger.info(f"Tool {tool_id}: found {len(cards)} accommodation cards in DOM after search")

            for card in cards:
                title_el = card.locator("h3.fr-card__title a").first
                if await title_el.count() == 0:
                    continue

                title = (await title_el.text_content()).strip()
                href = await title_el.get_attribute("href") or ""
                full_link = f"https://trouverunlogement.lescrous.fr{href}" if href.startswith("/") else href

                parts = [p for p in href.split("/") if p]
                listing_id = parts[-1] if parts else href

                price_el = card.locator("p.fr-badge").first
                price = (await price_el.text_content()).strip() if await price_el.count() > 0 else "N/A"

                desc_el = card.locator("p.fr-card__desc").first
                address = (await desc_el.text_content()).strip() if await desc_el.count() > 0 and (await desc_el.text_content()).strip() else "Adresse non spécifiée"

                details = await card.locator("p.fr-card__detail").all_text_contents()
                surface = next((d.strip() for d in details if "m²" in d), "N/A")

                # Verify listing is in Marseille
                address_check = f"{address} {title}".lower()
                if "marseille" in address_check or any(zip_code in address_check for zip_code in ["1300", "1301", "13001", "13002", "13003", "13004", "13005", "13006", "13007", "13008", "13009", "13010", "13011", "13012", "13013", "13014", "13015", "13016"]):
                    if listing_id and listing_id not in seen_ids:
                        seen_ids.add(listing_id)
                        listings.append({
                            "id": listing_id,
                            "name": title,
                            "address": address,
                            "price": price,
                            "surface": surface,
                            "link": full_link
                        })

        except Exception as err:
            logger.warning(f"Error during search on tool {tool_id}: {err}")

    # Process API items matching Marseille
    for item in intercepted_api_items:
        item_id = str(item.get("id", ""))
        title = item.get("title") or item.get("residenceName") or "CROUS Colocation"
        
        street = item.get("address") or item.get("street") or ""
        city = item.get("city") or ""
        zip_code = item.get("zipCode") or ""
        addr_parts = [p.strip() for p in [street, zip_code, city] if p and p.strip()]
        address = ", ".join(addr_parts) if addr_parts else "Adresse non spécifiée"

        check_str = f"{address} {title}".lower()

        if "marseille" in check_str or any(z in check_str for z in ["1300", "1301", "13001", "13002", "13003", "13004", "13005", "13006", "13007", "13008", "13009", "13010", "13011", "13012", "13013", "13014", "13015", "13016"]):
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                price_val = item.get("rent", {}).get("amount") or item.get("price")
                price_str = f"{price_val / 100:.2f} €" if isinstance(price_val, (int, float)) and price_val > 1000 else f"{price_val} €"
                surface_str = f"{item.get('area', {}).get('min', 'N/A')} m²"
                link = f"https://trouverunlogement.lescrous.fr/tools/42/accommodations/{item_id}"

                listings.append({
                    "id": item_id,
                    "name": title,
                    "address": address,
                    "price": price_str,
                    "surface": surface_str,
                    "link": link
                })

    return listings


async def run_watcher():
    """Main watcher routine."""
    crous_email = os.getenv("CROUS_EMAIL")
    crous_password = os.getenv("CROUS_PASSWORD")
    ntfy_topic = os.getenv("NTFY_TOPIC")

    state = load_state()

    if not crous_email or not crous_password:
        err_msg = "CROUS_EMAIL or CROUS_PASSWORD environment variable is missing."
        logger.error(err_msg)
        state["consecutive_failures"] += 1
        save_state(state)
        if state["consecutive_failures"] >= 3:
            send_ntfy_notification(
                topic=ntfy_topic,
                title="🚨 CROUS Watcher Alert: Configuration Error",
                message=f"Script failed {state['consecutive_failures']} times in a row.\n\nError: {err_msg}",
                tags="warning,rotating_light"
            )
        sys.exit(1)

    try:
        async with async_playwright() as p:
            logger.info("Launching headless Chromium browser...")
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            # 1. Fresh Authentication
            await login_crous(page, crous_email, crous_password)

            # 2. Search listings
            current_listings = await search_listings(page)

            await browser.close()

        # Compare against seen state
        seen_set = set(state.get("seen_ids", []))
        new_listings = [item for item in current_listings if item["id"] not in seen_set]

        logger.info(f"Total current listings: {len(current_listings)} | New listings: {len(new_listings)}")

        # 3. Process new listings
        for item in new_listings:
            title = f"🏠 CROUS Marseille: {item['name']}"
            body = (
                f"Logement: {item['name']}\n"
                f"Prix: {item['price']}\n"
                f"Surface: {item['surface']}\n"
                f"Adresse: {item['address']}\n"
                f"Lien: {item['link']}"
            )
            send_ntfy_notification(
                topic=ntfy_topic,
                title=title,
                message=body,
                tags="house,euro",
                link=item["link"]
            )
            state["seen_ids"].append(item["id"])

        # Reset failure counter on success
        state["consecutive_failures"] = 0
        save_state(state)

        if not new_listings:
            logger.info("No new listings found. Exiting quietly.")

    except Exception as err:
        logger.exception("An error occurred during watcher execution")
        state["consecutive_failures"] += 1
        save_state(state)

        if state["consecutive_failures"] >= 3:
            send_ntfy_notification(
                topic=ntfy_topic,
                title="🚨 CROUS Watcher Alert: Script Failure",
                message=f"The CROUS watcher script has failed {state['consecutive_failures']} runs in a row.\n\nLatest Error: {str(err)}",
                tags="warning,rotating_light"
            )
        sys.exit(1)


def main():
    import asyncio
    asyncio.run(run_watcher())


if __name__ == "__main__":
    main()
