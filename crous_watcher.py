#!/usr/bin/env python3
"""
CROUS Housing 24/7 Watcher Daemon
=================================
High-speed, low-resource daemon that monitors the CROUS housing portal
for new accommodations in Marseille (<= 400€) and delivers instant
notifications via a dedicated Telegram bot.

Features:
- Fast direct REST API queries (no headless browser overhead).
- Automatic discovery of active CROUS tool IDs.
- Two-way Telegram bot commands (/status, /check, /test).
- Atomic state persistence (crash-safe JSON store).
- Daily heartbeat and failure alerts.
"""

import os
import sys
import time
import json
import random
import signal
import logging
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "listings_seen.json"
HISTORY_LOG_FILE = BASE_DIR / "run_history.log"
LOG_FILE = BASE_DIR / "watcher.log"

# Fix Windows console UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Load .env file explicitly from BASE_DIR
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    # Minimal fallback parser if python-dotenv is not installed
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("crous_watcher")

# Configuration from environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "45"))
TARGET_CITY = os.getenv("TARGET_CITY", "Marseille").strip().lower()
MAX_PRICE = float(os.getenv("MAX_PRICE", "400"))
COLOCATION_ONLY = os.getenv("COLOCATION_ONLY", "false").lower() in ("true", "1", "yes")
ENABLE_DAILY_HEARTBEAT = os.getenv("ENABLE_DAILY_HEARTBEAT", "true").lower() in ("true", "1", "yes")

# Smart Cadence Configuration (French Local Time)
ENABLE_SMART_CADENCE = os.getenv("ENABLE_SMART_CADENCE", "true").lower() in ("true", "1", "yes")
PEAK_CHECK_INTERVAL_SECONDS = int(os.getenv("PEAK_CHECK_INTERVAL_SECONDS", "35"))
EVENING_CHECK_INTERVAL_SECONDS = int(os.getenv("EVENING_CHECK_INTERVAL_SECONDS", "65"))
NIGHT_CHECK_INTERVAL_SECONDS = int(os.getenv("NIGHT_CHECK_INTERVAL_SECONDS", "240"))

# Automated Application (Sniper) Configuration
AUTO_APPLY_ENABLED = os.getenv("AUTO_APPLY_ENABLED", "true").lower() in ("true", "1", "yes")
AUTO_APPLY_DRY_RUN = os.getenv("AUTO_APPLY_DRY_RUN", "true").lower() in ("true", "1", "yes")

# Optional modules: crous_auth and crous_apply
try:
    from crous_auth import is_session_valid, SESSION_FILE
except ImportError:
    is_session_valid = lambda: (False, "crous_auth module not found")
    SESSION_FILE = BASE_DIR / "session.json"

try:
    from crous_apply import apply_for_accommodation
except ImportError:
    apply_for_accommodation = None


class CrousBlockedOrRateLimitedError(Exception):
    """Raised when CROUS returns 403 Forbidden or 429 Too Many Requests."""
    def __init__(self, code: int, message: str):
        super().__init__(f"CROUS HTTP {code}: {message}")
        self.code = code


# French Timezone handling for smart cadence
try:
    import zoneinfo
    PARIS_TZ = zoneinfo.ZoneInfo("Europe/Paris")
except Exception:
    from datetime import timedelta
    PARIS_TZ = timezone(timedelta(hours=2))


def get_smart_cadence() -> tuple[int, str]:
    """Calculate current polling interval and mode based on French local time."""
    if not ENABLE_SMART_CADENCE:
        return CHECK_INTERVAL_SECONDS, "Standard (Fixe)"

    now_paris = datetime.now(PARIS_TZ)
    time_float = now_paris.hour + now_paris.minute / 60.0

    # 08:00 - 18:30: Peak office hours
    if 8.0 <= time_float < 18.5:
        return PEAK_CHECK_INTERVAL_SECONDS, "⚡ Heures de pointe (Bureau)"
    # 18:30 - 23:30: Evening
    elif 18.5 <= time_float < 23.5:
        return EVENING_CHECK_INTERVAL_SECONDS, "🌆 Soirée (Modéré)"
    # 23:30 - 08:00: Night
    else:
        return NIGHT_CHECK_INTERVAL_SECONDS, "🌙 Nuit (Pause/Éco)"


# Global runtime metrics
METRICS = {
    "start_time": datetime.now(timezone.utc),
    "last_check_time": None,
    "total_checks": 0,
    "last_active_listings_count": 0,
    "last_error": None,
    "telegram_update_offset": 0,
    "current_cadence_mode": "Initialisation",
}

RUNNING = True


def handle_shutdown(signum, frame):
    global RUNNING
    logger.info(f"Received termination signal ({signum}). Gracefully shutting down...")
    RUNNING = False


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ==============================================================================
# Telegram API Helpers
# ==============================================================================

def send_telegram_message(text: str, reply_markup: dict = None) -> bool:
    """Send a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram bot token or chat ID is missing. Skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("ok", False)
    except Exception as err:
        logger.error(f"Failed to send Telegram message: {err}")
        return False


def send_telegram_photo(photo_path: str, caption: str = "") -> bool:
    """Send a photo attachment with caption to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    if not os.path.exists(photo_path):
        logger.warning(f"Photo path does not exist: {photo_path}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        import requests
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption[:1024],
                    "parse_mode": "Markdown"
                },
                files={"photo": f},
                timeout=25
            )
            return resp.ok
    except Exception as err:
        logger.error(f"Failed to send Telegram photo: {err}")
        return False


def poll_telegram_updates(on_check_callback=None):
    """Poll Telegram getUpdates to handle user commands like /status, /check, /test."""
    if not TELEGRAM_BOT_TOKEN:
        return

    offset = METRICS["telegram_update_offset"]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=0"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "crous-watcher"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                return

            for update in data.get("result", []):
                update_id = update["update_id"]
                METRICS["telegram_update_offset"] = update_id + 1

                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                # Security: only respond to the authorized user
                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                handle_telegram_command(text, on_check_callback)
    except Exception as err:
        logger.debug(f"Error checking Telegram updates: {err}")


def handle_telegram_command(cmd: str, on_check_callback=None):
    """Execute interactive Telegram commands."""
    cmd_lower = cmd.lower()
    logger.info(f"Received Telegram command: {cmd}")

    if cmd_lower in ("/start", "/help"):
        help_text = (
            "🤖 *CROUS Watcher Bot - Commandes Disponibles*\n\n"
            "• `/status` : État du watcher, cadence et métriques\n"
            "• `/check` : Lancer une vérification immédiate\n"
            "• `/session` : Vérifier la validité de l'authentification CROUS\n"
            "• `/test_apply` : Tester l'auto-candidature (Dry-Run avec capture)\n"
            "• `/test` : Envoyer une notification de test\n"
            "• `/help` : Afficher ce message d'aide\n\n"
            f"🎯 *Ville ciblée :* {TARGET_CITY.capitalize()}\n"
            f"💶 *Prix max :* {MAX_PRICE:.2f} €\n"
            f"🤖 *Auto-Apply :* {'DRY-RUN' if AUTO_APPLY_DRY_RUN else 'LIVE' if AUTO_APPLY_ENABLED else 'Désactivé'}"
        )
        send_telegram_message(help_text)

    elif cmd_lower == "/status":
        uptime = datetime.now(timezone.utc) - METRICS["start_time"]
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        last_check = METRICS["last_check_time"].strftime("%H:%M:%S UTC") if METRICS["last_check_time"] else "En cours..."
        state = load_state()
        interval, cadence_label = get_smart_cadence()
        is_logged_in, session_msg = is_session_valid()

        status_text = (
            "🟢 *CROUS Watcher Actif (24/7)*\n\n"
            f"⏱️ *Uptime :* {uptime_str}\n"
            f"🔄 *Vérifications totales :* {METRICS['total_checks']}\n"
            f"🕒 *Dernière vérification :* {last_check}\n"
            f"⏱️ *Cadence :* ~{interval}s ({cadence_label})\n"
            f"🇫🇷 *Offres actives en France :* {METRICS['last_active_listings_count']}\n"
            f"💾 *Offres déjà enregistrées :* {len(state.get('seen_ids', []))}\n"
            f"⚠️ *Échecs consécutifs :* {state.get('consecutive_failures', 0)}\n\n"
            f"🔐 *Session CROUS :* {'Connecté ✅' if is_logged_in else 'Non connecté / Expiré ❌'}\n"
            f"🤖 *Auto-Apply :* {'🧪 DRY-RUN' if AUTO_APPLY_DRY_RUN else '⚡ LIVE' if AUTO_APPLY_ENABLED else 'Désactivé'}\n"
            f"🎯 *Cible :* {TARGET_CITY.capitalize()} (≤ {MAX_PRICE} €)"
        )
        send_telegram_message(status_text)

    elif cmd_lower == "/session":
        is_logged_in, session_msg = is_session_valid()
        if is_logged_in:
            send_telegram_message(
                "🟢 *Session CROUS Active*\n\n"
                f"✅ {session_msg}\n\n"
                "Le watcher est prêt à snipé toute offre qui apparaît."
            )
        else:
            send_telegram_message(
                "🔴 *Session CROUS Invalide ou Expirée*\n\n"
                f"❌ {session_msg}\n\n"
                "Pour renouveler la session :\n"
                "1. Sur votre PC : `python crous_auth.py --login`\n"
                "2. Transférez `session.json` sur le VPS."
            )

    elif cmd_lower == "/test_apply":
        send_telegram_message("🧪 *Lancement d'un test d'auto-candidature (Dry-Run)...*\nVeuillez patienter quelques secondes.")
        sample_acc_id = "6"
        tool_id = "47"
        try:
            items = fetch_all_crous_listings(tool_id)
            if items:
                sample_acc_id = str(items[0].get("id", "6"))
        except Exception:
            pass

        if apply_for_accommodation:
            res = apply_for_accommodation(tool_id=tool_id, accommodation_id=sample_acc_id, dry_run=True)
            if res.get("success"):
                caption = (
                    f"🧪 *[TEST DRY-RUN RÉUSSI]*\n\n"
                    f"• Logement test : `#{sample_acc_id}`\n"
                    f"• Durée : *{res['duration_seconds']}s*\n"
                    f"• Étape : `{res['step']}`\n\n"
                    "Capture d'écran du formulaire ci-jointe."
                )
                if res.get("screenshot_path"):
                    send_telegram_photo(res["screenshot_path"], caption)
                else:
                    send_telegram_message(caption)
            else:
                err_caption = (
                    f"❌ *[ÉCHEC DU TEST DRY-RUN]*\n\n"
                    f"• Erreur : `{res.get('error')}`\n"
                    f"• Étape : `{res.get('step')}`"
                )
                if res.get("screenshot_path"):
                    send_telegram_photo(res["screenshot_path"], err_caption)
                else:
                    send_telegram_message(err_caption)
        else:
            send_telegram_message("❌ Le module `crous_apply` n'est pas disponible.")

    elif cmd_lower == "/check":
        send_telegram_message("🔎 *Vérification manuelle en cours...*")
        if on_check_callback:
            count, new_found = on_check_callback()
            send_telegram_message(
                f"✅ *Vérification terminée*\n\n"
                f"• Offres trouvées à Marseille : *{count}*\n"
                f"• Nouvelles alertes envoyées : *{new_found}*"
            )

    elif cmd_lower == "/test":
        send_test_notification()


def send_test_notification():
    """Send a realistic test listing notification."""
    test_text = (
        "🏠 *[TEST] Nouvelle offre CROUS Marseille !*\n\n"
        "📍 *Résidence :* Résidence Luminy (Test)\n"
        "🏷️ *Type :* T1 Studio (18 m²)\n"
        "💶 *Loyer :* 280.50 € / mois\n"
        "📬 *Adresse :* 171 Avenue de Luminy, 13009 Marseille\n\n"
        "⚡ *Ceci est une notification de test.*"
    )
    markup = {
        "inline_keyboard": [
            [{"text": "🚀 Ouvrir le portail CROUS", "url": "https://trouverunlogement.lescrous.fr"}]
        ]
    }
    success = send_telegram_message(test_text, markup)
    if success:
        logger.info("Test notification dispatched successfully.")


# ==============================================================================
# State Management (Crash-safe atomic writes)
# ==============================================================================

def load_state() -> dict:
    """Load seen listing IDs from disk."""
    if not STATE_FILE.exists():
        return {"seen_ids": [], "consecutive_failures": 0}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {"seen_ids": data, "consecutive_failures": 0}
            elif isinstance(data, dict):
                return {
                    "seen_ids": data.get("seen_ids", []),
                    "consecutive_failures": data.get("consecutive_failures", 0)
                }
    except Exception as err:
        logger.warning(f"Error reading {STATE_FILE}: {err}. Resetting state.")

    return {"seen_ids": [], "consecutive_failures": 0}


def save_state(state: dict) -> None:
    """Save state atomically using a temporary file."""
    temp_file = STATE_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        temp_file.replace(STATE_FILE)
    except Exception as err:
        logger.error(f"Failed to atomically save state to {STATE_FILE}: {err}")


def append_run_history(status: str, summary: str) -> None:
    """Append a concise entry to run_history.log (capped at last 500 lines)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"{timestamp} | [{status}] {summary}\n"

    lines = []
    if HISTORY_LOG_FILE.exists():
        try:
            with open(HISTORY_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            pass

    lines = (lines + [line])[-500:]
    try:
        with open(HISTORY_LOG_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as err:
        logger.error(f"Failed to write history log: {err}")


# ==============================================================================
# CROUS API Scraper Engine
# ==============================================================================

def discover_tool_ids() -> list[str]:
    """Dynamically discover active tool IDs from the CROUS homepage."""
    url = "https://trouverunlogement.lescrous.fr/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            import re
            found = set(re.findall(r"/tools/(\d+)", html))
            if found:
                return sorted(list(found))
    except Exception as err:
        logger.debug(f"Tool discovery fallback: {err}")

    # Default fallback
    return ["47"]


def fetch_all_crous_listings(tool_id: str) -> list[dict]:
    """
    Fetch all active listings for the given tool_id via the internal search REST API.
    Paginates automatically until all items are collected.
    """
    all_items = []
    page = 1
    total_expected = None

    url = f"https://trouverunlogement.lescrous.fr/api/fr/search/{tool_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Referer": f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/search"
    }

    while True:
        body = {"page": page}
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers
        )

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as http_err:
            if http_err.code in (403, 429):
                raise CrousBlockedOrRateLimitedError(
                    http_err.code,
                    f"Accès refusé ou rate limit (HTTP {http_err.code}) sur l'API CROUS ({url})"
                )
            raise

        results = data.get("results", {})
        items = results.get("items", [])
        total_obj = results.get("total", {})
        total_val = total_obj.get("value") if isinstance(total_obj, dict) else total_obj

        if total_expected is None and total_val is not None:
            total_expected = total_val

        if not items:
            break

        all_items.extend(items)

        # If we've collected all items, exit pagination
        if total_expected and len(all_items) >= total_expected:
            break

        # If less than 20 items returned, we are on the last page
        if len(items) < 20:
            break

        page += 1
        if page > 10:  # safety ceiling
            break

    return all_items


def is_target_listing(item: dict) -> tuple[bool, dict]:
    """
    Filter item according to TARGET_CITY, MAX_PRICE, and COLOCATION_ONLY.
    Returns (matches, parsed_info_dict).
    """
    item_id = str(item.get("id", ""))
    residence = item.get("residence", {})
    residence_name = residence.get("label") or "Résidence CROUS"
    address = residence.get("address") or ""
    entity = residence.get("entity", {}).get("name", "")
    room_label = item.get("label") or "Chambre"

    # Surface
    area_obj = item.get("area", {})
    surface = area_obj.get("min") or area_obj.get("max") or "N/A"

    # Occupation modes and rent calculation
    occupation_modes = item.get("occupationModes", [])
    rents = []
    is_colocation = False

    for mode in occupation_modes:
        m_type = mode.get("type", "").lower()
        if "sharing" in m_type or "coloc" in m_type:
            is_colocation = True
        rent_info = mode.get("rent", {})
        min_rent = rent_info.get("min") or rent_info.get("max")
        if min_rent is not None:
            # Rent in API is in cents (e.g. 28050 -> 280.50€)
            if min_rent > 1000:
                rents.append(min_rent / 100.0)
            else:
                rents.append(float(min_rent))

    if not rents:
        raw_price = item.get("price") or 0
        rents.append(raw_price / 100.0 if raw_price > 1000 else float(raw_price))

    lowest_rent = min(rents) if rents else 9999.0

    # 1. Location match: Marseille city or postal codes 13001-13016 (or 'all' for nationwide)
    if TARGET_CITY in ("all", "*", ""):
        city_match = True
    else:
        addr_clean = address.lower()
        marseille_postal_codes = [f"1300{i}" for i in range(1, 10)] + [f"1301{i}" for i in range(0, 7)] + ["13000"]
        import re
        city_match = (
            bool(re.search(r'\b' + re.escape(TARGET_CITY) + r'\b', addr_clean)) or
            any(pc in addr_clean for pc in marseille_postal_codes)
        )

    if not city_match:
        return False, {}

    # 2. Price filter
    if lowest_rent > MAX_PRICE:
        return False, {}

    # 3. Colocation filter
    if COLOCATION_ONLY and not is_colocation and "coloc" not in room_label.lower():
        return False, {}

    parsed = {
        "id": item_id,
        "residence_name": residence_name,
        "label": room_label,
        "surface": surface,
        "price": f"{lowest_rent:.2f}",
        "address": address or "Marseille",
        "is_coloc": is_colocation
    }
    return True, parsed


def check_and_notify() -> tuple[int, int]:
    """
    Core check cycle:
    1. Fetches listings from active CROUS tool(s).
    2. Identifies new matching listings in Marseille.
    3. Sends Telegram notifications with direct action button.
    4. Updates listings_seen.json.
    Returns (total_matching_in_marseille, new_alerts_sent).
    """
    state = load_state()
    seen_ids = set(str(i) for i in state.get("seen_ids", []))

    tool_ids = discover_tool_ids()
    all_raw_items = []
    errors_encountered = []

    for tid in tool_ids:
        try:
            items = fetch_all_crous_listings(tid)
            for it in items:
                it["_tool_id"] = tid
            all_raw_items.extend(items)
        except CrousBlockedOrRateLimitedError as err:
            logger.critical(f"CRITICAL API RESTRICTION: {err}")
            send_telegram_message(
                f"🚨 *ALERTE CRITIQUE : Restriction d'accès CROUS (HTTP {err.code})*\n\n"
                "Votre adresse IP semble être temporairement bloquée ou limitée par CROUS.\n"
                "Le watcher va faire une pause de sécurité de 5 minutes pour protéger votre IP."
            )
            raise
        except Exception as err:
            logger.warning(f"Error fetching tool {tid}: {err}")
            errors_encountered.append(err)

    if not all_raw_items and errors_encountered and len(errors_encountered) >= len(tool_ids):
        raise RuntimeError(f"Toutes les requêtes d'outils CROUS ont échoué : {errors_encountered[0]}")

    METRICS["last_active_listings_count"] = len(all_raw_items)
    METRICS["last_check_time"] = datetime.now(timezone.utc)
    METRICS["total_checks"] += 1

    matching_listings = []
    new_alerts_sent = 0

    for item in all_raw_items:
        matches, info = is_target_listing(item)
        if matches:
            matching_listings.append(info)
            item_id = info["id"]
            tool_id = item.get("_tool_id", "47")

            if item_id not in seen_ids:
                # NEW LISTING FOUND! Send Telegram Alert!
                logger.info(f"✨ NEW LISTING: {info['residence_name']} ({info['price']}€)")
                listing_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/accommodations/{item_id}"

                coloc_tag = " [Colocation]" if info["is_coloc"] else ""
                alert_text = (
                    "🏠 *NOUVELLE OFFRE CROUS MARSEILLE !*\n\n"
                    f"📍 *Résidence :* {info['residence_name']}\n"
                    f"🏷️ *Type :* {info['label']}{coloc_tag} ({info['surface']} m²)\n"
                    f"💶 *Loyer :* {info['price']} € / mois\n"
                    f"📬 *Adresse :* {info['address']}\n\n"
                    "⚡ *Fais vite, clique ci-dessous pour réserver immédiatement !*"
                )
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "🚀 Ouvrir l'offre & Réserver", "url": listing_url}]
                    ]
                }

                if send_telegram_message(alert_text, reply_markup):
                    seen_ids.add(item_id)
                    new_alerts_sent += 1

                # Trigger Automated Application (Sniper)
                if AUTO_APPLY_ENABLED and apply_for_accommodation:
                    logger.info(f"🤖 Triggering auto-apply for accommodation {item_id} (DRY_RUN={AUTO_APPLY_DRY_RUN})...")
                    try:
                        apply_res = apply_for_accommodation(
                            tool_id=tool_id,
                            accommodation_id=item_id,
                            dry_run=AUTO_APPLY_DRY_RUN
                        )
                        if apply_res.get("success"):
                            if AUTO_APPLY_DRY_RUN:
                                caption = (
                                    "🧪 *[DRY-RUN] Formulaire pré-rempli avec succès !*\n\n"
                                    f"📍 *Résidence :* {info['residence_name']}\n"
                                    f"⏱️ *Temps d'exécution :* {apply_res['duration_seconds']}s\n"
                                    "ℹ️ *Mode DRY-RUN actif :* le bouton final n'a pas été cliqué."
                                )
                            else:
                                caption = (
                                    "🎯 *[RÉSERVATION AUTOMATIQUE RÉUSSIE]*\n\n"
                                    f"📍 *Résidence :* {info['residence_name']}\n"
                                    f"⏱️ *Snipé en :* {apply_res['duration_seconds']}s\n\n"
                                    "🎉 Le logement a été placé dans votre panier !\n"
                                    f"🔗 [Accéder à mon panier]({apply_res['cart_url']})"
                                )
                            if apply_res.get("screenshot_path"):
                                send_telegram_photo(apply_res["screenshot_path"], caption)
                            else:
                                send_telegram_message(caption)
                        else:
                            err_msg = (
                                "⚠️ *[ÉCHEC DE L'AUTO-APPLY]*\n\n"
                                f"📍 *Résidence :* {info['residence_name']}\n"
                                f"❌ *Erreur :* `{apply_res.get('error', 'Erreur inconnue')}`\n\n"
                                f"⚡ Cliquez vite manuellement : {listing_url}"
                            )
                            if apply_res.get("screenshot_path"):
                                send_telegram_photo(apply_res["screenshot_path"], err_msg)
                            else:
                                send_telegram_message(err_msg)
                    except Exception as apply_err:
                        logger.error(f"Error executing auto-apply: {apply_err}")

    # Update state
    state["seen_ids"] = list(seen_ids)
    state["consecutive_failures"] = 0
    save_state(state)

    summary = (
        f"Check completed: {len(all_raw_items)} in France | "
        f"{len(matching_listings)} in Marseille | "
        f"{new_alerts_sent} new alerts sent"
    )
    logger.info(summary)
    if new_alerts_sent > 0:
        append_run_history("ALERT", summary)

    return len(matching_listings), new_alerts_sent


# ==============================================================================
# Main Daemon Loop
# ==============================================================================

def main_loop():
    cur_interval, cur_cadence = get_smart_cadence()
    logger.info("==================================================")
    logger.info("🚀 CROUS Watcher Daemon starting (24/7 Mode)")
    logger.info(f"Target City: {TARGET_CITY.capitalize()} | Max Price: {MAX_PRICE}€")
    logger.info(f"Smart Cadence: {'Enabled' if ENABLE_SMART_CADENCE else 'Disabled'} (~{cur_interval}s - {cur_cadence})")
    logger.info(f"Auto-Apply: {'Enabled (DRY-RUN)' if AUTO_APPLY_DRY_RUN else 'Enabled (LIVE)' if AUTO_APPLY_ENABLED else 'Disabled'}")
    logger.info(f"Telegram Notifications: {'Enabled' if TELEGRAM_BOT_TOKEN else 'Disabled'}")
    logger.info("==================================================")

    # Validate configuration
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing from .env!")
        sys.exit(1)

    state = load_state()
    last_heartbeat_day = None

    # Send startup announcement to Telegram
    is_logged_in, _ = is_session_valid()
    startup_msg = (
        "🚀 *CROUS Watcher Démarré sur votre VPS !*\n\n"
        f"🎯 *Ville :* {TARGET_CITY.capitalize()}\n"
        f"💶 *Loyer Max :* {MAX_PRICE} €\n"
        f"⏱️ *Cadence actuelle :* ~{cur_interval}s ({cur_cadence})\n"
        f"🔐 *Session CROUS :* {'Active ✅' if is_logged_in else 'Non configurée ❌'}\n"
        f"🤖 *Auto-Apply :* {'🧪 DRY-RUN' if AUTO_APPLY_DRY_RUN else '⚡ LIVE' if AUTO_APPLY_ENABLED else 'Désactivé'}\n\n"
        "Je surveille en continu 24h/24. Envoyez `/status` pour voir les métriques ou `/check` pour vérifier."
    )
    send_telegram_message(startup_msg)

    while RUNNING:
        is_blocked_error = False
        try:
            # 1. Check for incoming Telegram commands (/status, /check, /test, /session, /test_apply)
            poll_telegram_updates(on_check_callback=check_and_notify)

            # 2. Daily morning heartbeat (09:00 UTC)
            now = datetime.now(timezone.utc)
            if ENABLE_DAILY_HEARTBEAT and now.hour == 9 and last_heartbeat_day != now.date():
                last_heartbeat_day = now.date()
                send_telegram_message(
                    "☀️ *Bonjour ! CROUS Watcher est bien actif.*\n"
                    f"Surveillance 24/7 en cours pour {TARGET_CITY.capitalize()}.\n"
                    f"Offres actives en France : {METRICS['last_active_listings_count']}."
                )

            # 3. Run search check
            check_and_notify()

        except CrousBlockedOrRateLimitedError as err:
            is_blocked_error = True
            logger.exception(f"Rate limited or blocked: {err}")
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(state)
            append_run_history("BLOCKED", str(err))

        except Exception as err:
            logger.exception(f"Unexpected error in watcher cycle: {err}")
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(state)
            append_run_history("FAILURE", f"{err} | Consecutive: {state['consecutive_failures']}")

            # Alert after 5 consecutive failures
            if state["consecutive_failures"] == 5:
                send_telegram_message(
                    f"🚨 *Alerte Watcher : 5 échecs consécutifs*\n\n"
                    f"Dernière erreur : `{str(err)[:200]}`\n"
                    "Vérifiez les logs sur votre VPS (`journalctl -u crous-watcher`)."
                )

        # 4. Sleep cadence
        if is_blocked_error:
            # Backoff for 5 minutes (300s) to allow rate limit to clear
            logger.warning("Safety backoff activated: sleeping for 5 minutes...")
            sleep_time = 300.0
        else:
            cadence_seconds, cadence_label = get_smart_cadence()
            METRICS["current_cadence_mode"] = cadence_label
            jitter = random.uniform(-3.0, 5.0)
            sleep_time = max(15.0, cadence_seconds + jitter)

        # Break sleep into 1-second chunks so incoming commands or signals are handled fast
        for _ in range(int(sleep_time)):
            if not RUNNING:
                break
            poll_telegram_updates(on_check_callback=check_and_notify)
            time.sleep(1)

    logger.info("CROUS Watcher Daemon stopped cleanly.")
    send_telegram_message("🛑 *CROUS Watcher arrêté sur le VPS.*")


if __name__ == "__main__":
    main_loop()
