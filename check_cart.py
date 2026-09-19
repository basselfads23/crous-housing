#!/usr/bin/env python3
"""Diagnostic script to inspect CROUS cart and accommodation buttons."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(storage_state="session.json")
    page = context.new_page()

    # 1. Check cart content
    print("Navigating to cart...")
    page.goto("https://trouverunlogement.lescrous.fr/tools/47/cart", wait_until="networkidle")
    print("=== 1. CART PAGE ===")
    print("Cart URL:", page.url)
    print("Cart Title:", page.title())
    cards = page.locator("h1, h2, h3, .fr-card__title, a.fr-btn").all_inner_texts()
    print("Cart Elements:", [c.strip() for c in cards if c.strip()][:10])
    create_links = [a.get_attribute("href") for a in page.locator("a[href*='requests/create']").all()]
    print("Reservation Links in Cart:", create_links)

    # 2. Check accommodation 1828
    print("\nNavigating to 1828...")
    page.goto("https://trouverunlogement.lescrous.fr/tools/47/accommodations/1828", wait_until="networkidle")
    print("=== 2. ACCOMMODATION 1828 ===")
    print("URL:", page.url)
    print("Title:", page.title())
    btns = [b.inner_text().strip() for b in page.locator("button, a.fr-btn").all() if b.inner_text().strip()]
    print("Buttons on 1828:", btns)

    # 3. Check accommodation 6
    print("\nNavigating to 6...")
    page.goto("https://trouverunlogement.lescrous.fr/tools/47/accommodations/6", wait_until="networkidle")
    print("=== 3. ACCOMMODATION 6 ===")
    print("URL:", page.url)
    print("Title:", page.title())
    btns6 = [b.inner_text().strip() for b in page.locator("button, a.fr-btn").all() if b.inner_text().strip()]
    print("Buttons on 6:", btns6)

    browser.close()
