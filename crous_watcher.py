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
AUTO_APPLY_ANY_MAX_PRICE = float(os.getenv("AUTO_APPLY_ANY_MAX_PRICE", "300"))
AUTO_APPLY_SINGLE_MAX_PRICE = float(os.getenv("AUTO_APPLY_SINGLE_MAX_PRICE", "350"))
# Proxy manager integration
try:
    import proxy_manager
    from proxy_manager import AllProxyGroupsExhaustedError
except ImportError:
    proxy_manager = None
    class AllProxyGroupsExhaustedError(Exception):
        """Raised when all configured proxy groups are exhausted (e.g. 402 Payment Required)."""
        pass

try:
    import activity_logger
except ImportError:
    activity_logger = None


def is_payment_required_error(err: Exception) -> bool:
    """Check if exception is an HTTP 402 Payment Required or tunnel CONNECT 402."""
    if isinstance(err, urllib.error.HTTPError) and err.code == 402:
        return True
    err_str = str(err).lower()
    if "402" in err_str and ("payment required" in err_str or "tunnel" in err_str):
        return True
    if "payment required" in err_str:
        return True
    return False


def get_crous_opener_and_proxy(rotate: bool = False):
    handlers = []
    proxy_url = proxy_manager.get_current_proxy(rotate=rotate) if proxy_manager else (os.getenv("CROUS_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"))
    if proxy_manager and proxy_manager.load_proxies() and not proxy_url:
        raise AllProxyGroupsExhaustedError("All proxy groups are exhausted (no available proxies)")
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        }))
    return urllib.request.build_opener(*handlers), proxy_url

def get_crous_opener(rotate: bool = False):
    opener, _ = get_crous_opener_and_proxy(rotate=rotate)
    return opener


# Optional modules: crous_auth and crous_apply
try:
    from crous_auth import is_session_valid, auto_login, SESSION_FILE
except ImportError:
    is_session_valid = lambda: (False, "crous_auth module not found")
    auto_login = lambda: (False, "crous_auth module not found")
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


def get_next_run_estimate() -> str:
    """Returns human-friendly estimate for the next check cycle."""
    try:
        cadence_seconds, _ = get_smart_cadence()
        if cadence_seconds < 60:
            return f"{int(cadence_seconds)} seconds"
        mins = round(cadence_seconds / 60)
        return f"{mins} minutes"
    except Exception:
        return "N/A"


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
PAUSED = False


def handle_shutdown(signum, frame):
    global RUNNING
    logger.info(f"Received termination signal ({signum}). Gracefully shutting down...")
    RUNNING = False


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ==============================================================================
# Telegram API Helpers
# ==============================================================================

def get_viewer_chat_ids() -> list[str]:
    """Read TELEGRAM_VIEWER_CHAT_IDS from env: comma-separated chat IDs that receive broadcast-only alerts."""
    raw = os.getenv("TELEGRAM_VIEWER_CHAT_IDS", "").strip()
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


def send_telegram_message(text: str, reply_markup: dict = None, chat_id: str = None) -> bool:
    """Send a message to the configured Telegram chat."""
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        logger.warning("Telegram bot token or chat ID is missing. Skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target,
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


def broadcast_telegram_message(text: str, reply_markup: dict = None) -> bool:
    """Send to the admin chat plus every configured viewer chat ID. Returns True if at least one send succeeded."""
    recipients = []
    seen = set()
    for cid in [TELEGRAM_CHAT_ID] + get_viewer_chat_ids():
        if cid and cid not in seen:
            seen.add(cid)
            recipients.append(cid)
    results = [send_telegram_message(text, reply_markup=reply_markup, chat_id=cid) for cid in recipients]
    return any(results)


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
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                return

            for update in data.get("result", []):
                update_id = update["update_id"]
                METRICS["telegram_update_offset"] = update_id + 1

                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                if text.lower() == "/whoami":
                    send_telegram_message(f"Your Telegram chat ID is: `{chat_id}`", chat_id=chat_id)
                    continue

                # Security: only respond to the authorized user
                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                handle_telegram_command(text, on_check_callback)
    except Exception as err:
        logger.debug(f"Error checking Telegram updates: {err}")


def handle_telegram_command(cmd: str, on_check_callback=None):
    """Execute interactive Telegram commands."""
    global PAUSED, RUNNING
    cmd_lower = cmd.lower()
    logger.info(f"Received Telegram command: {cmd}")

    if cmd_lower in ("/start", "/help"):
        help_text = (
            "🤖 *CROUS Watcher Bot - Commandes Disponibles*\n\n"
            "• `/status` : État du watcher, cadence et métriques\n"
            "• `/pause` : Mettre en pause la surveillance automatique\n"
            "• `/resume` : Reprendre la surveillance automatique\n"
            "• `/stop` : Arrêter complètement le bot sur le VPS\n"
            "• `/check` : Lancer une vérification immédiate\n"
            "• `/session` : Vérifier la validité de l'authentification CROUS\n"
            "• `/renew` : Renouvellement automatique de la session CROUS\n"
            "• `/test_apply` : Tester l'auto-candidature (Dry-Run avec capture)\n"
            "• `/test` : Envoyer une notification de test\n"
            "• `/help` : Afficher ce message d'aide\n\n"
            f"🎯 *Ville ciblée :* {TARGET_CITY.capitalize()}\n"
            f"⚡ *Sniper immédiat :* < {AUTO_APPLY_ANY_MAX_PRICE:.0f} € (Individuel ou Colocation)\n"
            f"🎯 *Sniper ciblé :* {AUTO_APPLY_ANY_MAX_PRICE:.0f} € – {AUTO_APPLY_SINGLE_MAX_PRICE:.0f} € (Individuel uniquement)\n"
            f"📢 *Alerte Telegram seule :* Jusqu'à {MAX_PRICE:.0f} € (Réservation manuelle)\n"
            f"🤖 *Mode Sniper :* {'🧪 DRY-RUN' if AUTO_APPLY_DRY_RUN else '⚡ LIVE' if AUTO_APPLY_ENABLED else 'Désactivé'}"
        )
        send_telegram_message(help_text)

    elif cmd_lower == "/pause":
        PAUSED = True
        send_telegram_message(
            "⏸️ *CROUS Watcher mis en pause*\n\n"
            "Toutes les recherches et candidatures automatiques sont suspendues.\n"
            "Tapez `/resume` pour réactiver la surveillance à tout moment."
        )

    elif cmd_lower == "/resume":
        PAUSED = False
        send_telegram_message(
            "▶️ *CROUS Watcher réactivé*\n\n"
            "La surveillance 24/7 a repris normalement !"
        )

    elif cmd_lower in ("/stop", "/shutdown"):
        send_telegram_message("🛑 *Arrêt du CROUS Watcher...*\nLe processus s'éteint.")
        RUNNING = False

    elif cmd_lower == "/status":
        uptime = datetime.now(timezone.utc) - METRICS["start_time"]
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        last_check = METRICS["last_check_time"].strftime("%H:%M:%S UTC") if METRICS["last_check_time"] else "En cours..."
        state = load_state()
        interval, cadence_label = get_smart_cadence()
        is_logged_in, session_msg = is_session_valid()

        total_proxies = len(proxy_manager.load_proxies()) if proxy_manager else 0
        current_proxy = proxy_manager.get_current_proxy() if proxy_manager else None
        if not current_proxy:
            proxy_display = "Aucun (Tous les groupes sont épuisés ⚠️)" if total_proxies > 0 else "Aucun"
        else:
            proxy_display = current_proxy.split("@")[-1] if "@" in current_proxy else current_proxy

        header_icon = "⏸️" if PAUSED else "🟢"
        header_text = "CROUS Watcher en PAUSE (Tapez /resume)" if PAUSED else "CROUS Watcher Actif (24/7)"

        status_text = (
            f"{header_icon} *{header_text}*\n\n"
            f"⏱️ *Uptime :* {uptime_str}\n"
            f"🔄 *Vérifications totales :* {METRICS['total_checks']}\n"
            f"🕒 *Dernière vérification :* {last_check}\n"
            f"⏱️ *Cadence :* ~{interval}s ({cadence_label})\n"
            f"🌐 *Proxies :* {total_proxies} actifs (Actuel : `{proxy_display}`)\n"
            f"🇫🇷 *Offres actives en France :* {METRICS['last_active_listings_count']}\n"
            f"💾 *Offres déjà enregistrées :* {len(state.get('seen_ids', []))}\n"
            f"⚠️ *Échecs consécutifs :* {state.get('consecutive_failures', 0)}\n\n"
            f"🔐 *Session CROUS :* {'Connecté ✅' if is_logged_in else 'Non connecté / Expiré ❌'}\n"
            f"⚡ *Sniper < {AUTO_APPLY_ANY_MAX_PRICE:.0f} € :* Tout mode ({'🧪 DRY-RUN' if AUTO_APPLY_DRY_RUN else '⚡ LIVE'})\n"
            f"🎯 *Sniper {AUTO_APPLY_ANY_MAX_PRICE:.0f}–{AUTO_APPLY_SINGLE_MAX_PRICE:.0f} € :* Individuel seul ({'🧪 DRY-RUN' if AUTO_APPLY_DRY_RUN else '⚡ LIVE'})\n"
            f"📢 *Alerte seule :* {AUTO_APPLY_SINGLE_MAX_PRICE:.0f} € – {MAX_PRICE:.0f} € (et Coloc 300–400€)\n"
            f"🎯 *Cible :* {TARGET_CITY.capitalize()}"
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
                "Tapez `/renew` pour tenter une reconnexion automatique avec vos identifiants."
            )

    elif cmd_lower == "/renew":
        send_telegram_message("🔄 *Tentative de reconnexion automatique en cours...*\nVeuillez patienter 15 à 25 secondes.")
        try:
            renewed, renew_msg = auto_login()
            if renewed:
                send_telegram_message("🟢 *Reconnexion réussie !*\n\nLa session CROUS est active et valide ✅.")
            else:
                if any(x in str(renew_msg).lower() for x in ("111", "connection refused", "err_connection_refused")):
                    send_telegram_message(
                        "🔴 *Pare-feu CROUS temporairement fermé (Connection Refused)*\n\n"
                        "L'adresse IP de ce VPS a atteint le seuil de limitation de requêtes du CROUS.\n\n"
                        "⏳ *Action requise :* Laissez reposer 15 à 30 minutes sans envoyer de requêtes pour que le pare-feu débloque automatiquement l'accès.\n"
                        "💡 *Alternative :* Vous pouvez également renseigner `CROUS_PROXY` dans `.env` si vous disposez d'un proxy."
                    )
                else:
                    send_telegram_message(
                        "🔴 *Échec de la reconnexion automatique*\n\n"
                        f"Raison : `{renew_msg}`\n\n"
                        "Assurez-vous que `CROUS_EMAIL` et `CROUS_PASSWORD` sont renseignés dans le fichier `.env`."
                    )
        except Exception as renew_err:
            send_telegram_message(f"❌ *Erreur système lors du renouvellement :* `{renew_err}`")

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
                err_str = str(res.get("error"))
                if any(x in err_str.lower() for x in ("111", "connection refused", "err_connection_refused")):
                    err_caption = (
                        "❌ *[TEST DRY-RUN SUSPENDU - PARE-FEU CROUS]*\n\n"
                        "L'accès à `trouverunlogement.lescrous.fr` a été temporairement refusé par le site.\n\n"
                        "⏳ Le bot utilise automatiquement les autres proxys du pool."
                    )
                elif "429" in err_str or res.get("step") == "rate_limited":
                    err_caption = (
                        "❌ *[TEST DRY-RUN - PAUSE SUR CE PROXY (429)]*\n\n"
                        "Ce proxy précis a temporairement reçu trop de requêtes.\n\n"
                        "🔄 *Action automatique :* Le bot fait tourner la liste et utilisera un proxy différent au prochain essai."
                    )
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
    success = broadcast_telegram_message(test_text, markup)
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
    """Dynamically discover active tool IDs from the CROUS homepage using proxy."""
    url = "https://trouverunlogement.lescrous.fr/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }
    proxy_url = None
    try:
        req = urllib.request.Request(url, headers=headers)
        opener, proxy_url = get_crous_opener_and_proxy(rotate=False)
        with opener.open(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            if activity_logger:
                activity_logger.log_scouter_attempt(proxy_url, url, success=True, next_run="2-3 seconds")
            import re
            found = set(re.findall(r"/tools/(\d+)", html))
            if found:
                return sorted(list(found))
    except AllProxyGroupsExhaustedError:
        raise
    except Exception as err:
        if activity_logger:
            activity_logger.log_scouter_attempt(
                proxy_url,
                url,
                success=False,
                error_message=f"Découverte d'outils échouée : {err}",
                next_run="2-3 seconds"
            )
        logger.debug(f"Tool discovery fallback: {err}")

    # Default fallback
    return ["47"]


def fetch_all_crous_listings(tool_id: str) -> list[dict]:
    """
    Fetch all active listings for the given tool_id via the internal search REST API.
    Paginates automatically until all items are collected.
    Uses proxy rotation and automatic retry if an individual proxy fails.
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

    max_proxy_retries = 3
    while True:
        body = {"page": page}
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers
        )

        data = None
        last_error = None
        for attempt in range(max_proxy_retries):
            opener, proxy_url = get_crous_opener_and_proxy(rotate=False)
            try:
                with opener.open(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    break
            except urllib.error.HTTPError as http_err:
                last_error = http_err
                if is_payment_required_error(http_err):
                    group = proxy_manager.get_proxy_group(proxy_url) if proxy_manager else "unknown"
                    is_new = proxy_manager.mark_group_exhausted(group, reason=f"HTTP 402 ({http_err.reason})") if proxy_manager else False
                    if is_new:
                        send_telegram_message(
                            f"⚠️ *[Alerte Proxy - Fournisseur Épuisé]*\n\n"
                            f"• *Groupe :* `{group}`\n"
                            f"• *Raison :* `HTTP {http_err.code} ({http_err.reason})`\n"
                            f"• *Action :* Basculement automatique sur les fournisseurs restants."
                        )
                    remaining = proxy_manager.get_available_groups() if proxy_manager else []
                    if len(remaining) == 1:
                        logger.warning(f"Scouter operating with only ONE healthy proxy group remaining: {remaining[0]}")
                    if not remaining:
                        if activity_logger:
                            activity_logger.log_scouter_attempt(
                                proxy_url,
                                url,
                                success=False,
                                error_message="Tous les groupes de proxys sont épuisés (402 Payment Required)",
                                next_run=get_next_run_estimate()
                            )
                        raise AllProxyGroupsExhaustedError("All proxy groups are exhausted (402 Payment Required)")

                    retry_str = "Retrying with next proxy..." if (attempt < max_proxy_retries - 1) else get_next_run_estimate()
                    if activity_logger:
                        activity_logger.log_scouter_attempt(
                            proxy_url,
                            url,
                            success=False,
                            error_message=None,
                            next_run=retry_str
                        )
                    if attempt < max_proxy_retries - 1 and proxy_manager:
                        logger.warning(f"Proxy group '{group}' exhausted (402). Rotating to remaining groups...")
                        proxy_manager.rotate_proxy(skip_exhausted=True)
                        continue
                    else:
                        raise

                retry_str = "Retrying with next proxy..." if (attempt < max_proxy_retries - 1) else get_next_run_estimate()
                if activity_logger:
                    activity_logger.log_scouter_attempt(
                        proxy_url,
                        url,
                        success=False,
                        error_message=f"HTTP {http_err.code} sur l'API CROUS ({http_err.reason})",
                        next_run=retry_str
                    )
                if http_err.code in (403, 429):
                    if attempt < max_proxy_retries - 1 and proxy_manager:
                        logger.warning(f"HTTP {http_err.code} on current proxy. Rotating to next proxy...")
                        proxy_manager.rotate_proxy(skip_exhausted=True)
                        continue
                    raise CrousBlockedOrRateLimitedError(
                        http_err.code,
                        f"Accès refusé ou rate limit (HTTP {http_err.code}) sur l'API CROUS ({url})"
                    )
                if attempt < max_proxy_retries - 1 and proxy_manager:
                    logger.warning(f"HTTP {http_err.code} on proxy. Rotating proxy and retrying...")
                    proxy_manager.rotate_proxy(skip_exhausted=True)
            except Exception as err:
                last_error = err
                if is_payment_required_error(err):
                    group = proxy_manager.get_proxy_group(proxy_url) if proxy_manager else "unknown"
                    is_new = proxy_manager.mark_group_exhausted(group, reason="402 Payment Required") if proxy_manager else False
                    if is_new:
                        send_telegram_message(
                            f"⚠️ *[Alerte Proxy - Fournisseur Épuisé]*\n\n"
                            f"• *Groupe :* `{group}`\n"
                            f"• *Raison :* `HTTP 402 Payment Required`\n"
                            f"• *Action :* Basculement automatique sur les fournisseurs restants."
                        )
                    remaining = proxy_manager.get_available_groups() if proxy_manager else []
                    if len(remaining) == 1:
                        logger.warning(f"Scouter operating with only ONE healthy proxy group remaining: {remaining[0]}")
                    if not remaining:
                        if activity_logger:
                            activity_logger.log_scouter_attempt(
                                proxy_url,
                                url,
                                success=False,
                                error_message="Tous les groupes de proxys sont épuisés (402 Payment Required)",
                                next_run=get_next_run_estimate()
                            )
                        raise AllProxyGroupsExhaustedError("All proxy groups are exhausted (402 Payment Required)")

                    retry_str = "Retrying with next proxy..." if (attempt < max_proxy_retries - 1) else get_next_run_estimate()
                    if activity_logger:
                        activity_logger.log_scouter_attempt(
                            proxy_url,
                            url,
                            success=False,
                            error_message=None,
                            next_run=retry_str
                        )
                    if attempt < max_proxy_retries - 1 and proxy_manager:
                        logger.warning(f"Proxy group '{group}' exhausted (402). Rotating to remaining groups...")
                        proxy_manager.rotate_proxy(skip_exhausted=True)
                        continue
                    else:
                        raise

                retry_str = "Retrying with next proxy..." if (attempt < max_proxy_retries - 1) else get_next_run_estimate()
                if activity_logger:
                    activity_logger.log_scouter_attempt(
                        proxy_url,
                        url,
                        success=False,
                        error_message=f"Erreur réseau sur le proxy ({err})",
                        next_run=retry_str
                    )
                if attempt < max_proxy_retries - 1 and proxy_manager:
                    logger.warning(f"Network issue on proxy: {err}. Rotating to next proxy...")
                    proxy_manager.rotate_proxy(skip_exhausted=True)
                else:
                    err_str = str(err)
                    if "111" in err_str or "connection refused" in err_str.lower():
                        raise CrousBlockedOrRateLimitedError(code=111, message="Connection refused by CROUS firewall") from err
                    raise

        if data is None:
            if last_error:
                raise last_error
            break

        results = data.get("results", {})
        items = results.get("items", [])
        total_obj = results.get("total", {})
        total_val = total_obj.get("value") if isinstance(total_obj, dict) else total_obj

        if total_expected is None and total_val is not None:
            total_expected = total_val

        all_items.extend(items)

        # Check if more pages exist
        is_last_page = False
        if not items or len(items) < 20 or (total_expected and len(all_items) >= total_expected) or page >= 10:
            is_last_page = True

        next_run_str = get_next_run_estimate() if is_last_page else "2-3 seconds"
        if activity_logger:
            activity_logger.log_scouter_attempt(proxy_url, url, success=True, next_run=next_run_str)

        if is_last_page:
            break

        page += 1
        # Human-like delay between pages (random 2 to 3 seconds)
        time.sleep(random.uniform(2.0, 3.0))

    return all_items


def is_target_listing(item: dict) -> tuple[bool, dict]:
    """
    Filter item according to TARGET_CITY, MAX_PRICE, and dual-tier auto-apply rules:
    - Tier 1: rent < AUTO_APPLY_ANY_MAX_PRICE (300€) -> Snipe immediately, any mode (single or colocation)!
    - Tier 2: 300€ <= rent <= AUTO_APPLY_SINGLE_MAX_PRICE (350€) -> Snipe ONLY IF individual (single) mode is available!
    - Tier 3: 350€ < rent <= MAX_PRICE (400€) (or colocation in 300-400€) -> Alert only via Telegram, do NOT auto-apply.
    - Tier 4: rent > MAX_PRICE (400€) -> Discard completely.
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
    single_rents = []
    coloc_rents = []
    all_rents = []
    is_colocation = False

    for mode in occupation_modes:
        m_type = mode.get("type", "").lower()
        rent_info = mode.get("rent", {})
        min_rent = rent_info.get("min") or rent_info.get("max")
        if min_rent is not None:
            rent_val = (min_rent / 100.0) if min_rent > 1000 else float(min_rent)
            all_rents.append(rent_val)
            if "alone" in m_type or "single" in m_type or "indiv" in m_type:
                single_rents.append(rent_val)
            elif "sharing" in m_type or "coloc" in m_type:
                coloc_rents.append(rent_val)
                is_colocation = True

    if not all_rents:
        raw_price = item.get("price") or 0
        rent_val = raw_price / 100.0 if raw_price > 1000 else float(raw_price)
        all_rents.append(rent_val)
        if "coloc" in room_label.lower():
            coloc_rents.append(rent_val)
            is_colocation = True
        else:
            single_rents.append(rent_val)

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

    # 2. Dual-tier auto-apply & pricing evaluation
    min_single_rent = min(single_rents) if single_rents else None
    min_coloc_rent = min(coloc_rents) if coloc_rents else None
    overall_min_rent = min(all_rents) if all_rents else 0.0

    # If minimum rent is above MAX_PRICE (400€), ignore completely
    if overall_min_rent > MAX_PRICE:
        return False, {}

    # 3. Colocation only filter if configured
    if COLOCATION_ONLY and not is_colocation and "coloc" not in room_label.lower():
        return False, {}

    should_auto_apply = False
    chosen_mode = "single"
    effective_rent = overall_min_rent

    if AUTO_APPLY_ENABLED:
        # Check single mode under 350€ or 300€
        if min_single_rent is not None and min_single_rent <= AUTO_APPLY_SINGLE_MAX_PRICE:
            should_auto_apply = True
            chosen_mode = "single"
            effective_rent = min_single_rent
        # Check colocation mode under 300€
        elif min_coloc_rent is not None and min_coloc_rent < AUTO_APPLY_ANY_MAX_PRICE:
            should_auto_apply = True
            chosen_mode = "colocation"
            effective_rent = min_coloc_rent

    if not should_auto_apply:
        # For manual alerts: prioritize single rent if <= MAX_PRICE, else coloc / overall min
        if min_single_rent is not None and min_single_rent <= MAX_PRICE:
            effective_rent = min_single_rent
            chosen_mode = "single"
        elif min_coloc_rent is not None and min_coloc_rent <= MAX_PRICE:
            effective_rent = min_coloc_rent
            chosen_mode = "colocation"
        else:
            effective_rent = overall_min_rent
            chosen_mode = "single"

    parsed = {
        "id": item_id,
        "residence_name": residence_name,
        "label": room_label,
        "surface": surface,
        "price": f"{effective_rent:.2f}",
        "raw_price": effective_rent,
        "address": address or "Marseille",
        "is_coloc": (chosen_mode == "colocation"),
        "target_mode": chosen_mode,
        "should_auto_apply": should_auto_apply
    }
    return True, parsed


def check_and_notify() -> tuple[int, int]:
    """
    Core check cycle:
    1. Fetches listings from active CROUS tool(s).
    2. Identifies new matching listings in Marseille.
    3. Triggers immediate auto-apply if rent <= AUTO_APPLY_MAX_PRICE (330€).
    4. Sends Telegram alert (with sniper screenshot if applied, or manual link if 330-400€).
    5. Updates listings_seen.json.
    Returns (total_matching_in_marseille, new_alerts_sent).
    """
    state = load_state()
    seen_ids = set(str(i) for i in state.get("seen_ids", []))

    if proxy_manager:
        available_groups = proxy_manager.get_available_groups()
        if len(available_groups) == 1:
            logger.warning(
                f"Scouter operating with only ONE healthy proxy group remaining: {available_groups[0]}"
            )

    tool_ids = discover_tool_ids()
    # Natural delay between homepage check and search requests (random 2 to 3 seconds)
    time.sleep(random.uniform(2.0, 3.0))

    all_raw_items = []
    errors_encountered = []

    for idx, tid in enumerate(tool_ids):
        if idx > 0:
            time.sleep(random.uniform(2.0, 3.0))
        try:
            items = fetch_all_crous_listings(tid)
            for it in items:
                it["_tool_id"] = tid
            all_raw_items.extend(items)
        except AllProxyGroupsExhaustedError:
            raise
        except CrousBlockedOrRateLimitedError as err:
            logger.critical(f"CRITICAL API RESTRICTION: {err}")
            send_telegram_message(
                f"🚨 *ALERTE CRITIQUE : Restriction d'accès CROUS (HTTP {err.code})*\n\n"
                "Votre adresse IP semble être temporairement bloquée ou limitée par CROUS.\n"
                "Le watcher va faire une pause de sécurité de 10 minutes pour protéger votre IP."
            )
            raise
        except Exception as err:
            err_str = str(err)
            if "111" in err_str or "connection refused" in err_str.lower():
                logger.critical(f"Connection refused by CROUS firewall on tool {tid}: {err}")
                raise CrousBlockedOrRateLimitedError(code=111, message="Connection refused by CROUS firewall") from err
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
                # NEW LISTING FOUND!
                mode_name = "Colocation" if info["is_coloc"] else "Individuel"
                is_sniper_target = info["should_auto_apply"] and bool(apply_for_accommodation)
                logger.info(f"✨ NEW LISTING: {info['residence_name']} ({info['price']}€) | Sniper Target: {is_sniper_target}")
                listing_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/accommodations/{item_id}"
                coloc_tag = " [Colocation]" if info["is_coloc"] else ""

                # 1. SEND DIRECT LINK IMMEDIATELY so the user can apply manually without delay
                logger.info(f"⚡ [IMMEDIATE ALERT] Sending listing #{item_id} link to Telegram first...")
                alert_text = (
                    "🚨 *NOUVELLE OFFRE CROUS TROUVÉE !*\n\n"
                    f"📍 *Résidence :* {info['residence_name']}\n"
                    f"🏷️ *Type :* {info['label']}{coloc_tag} ({info['surface']} m²)\n"
                    f"👤 *Mode :* {mode_name}\n"
                    f"💶 *Loyer :* {info['price']} € / mois\n"
                    f"📬 *Adresse :* {info['address']}\n\n"
                    "⚡ *POSTULEZ IMMÉDIATEMENT :*\n"
                    f"{listing_url}\n\n"
                    + ("🤖 *Le sniper tente également l'auto-candidature en parallèle...*" if is_sniper_target
                       else "ℹ️ *Alerte manuelle : réservation requise via le lien ci-dessus.*")
                )
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "⚡ Ouvrir l'offre & Postuler immédiatement", "url": listing_url}]
                    ]
                }
                broadcast_telegram_message(alert_text, reply_markup=reply_markup)

                # 2. AFTER SENDING, ATTEMPT THE SNIPE (if listing qualifies for auto-apply)
                if is_sniper_target:
                    target_mode = info.get("target_mode", "single")
                    logger.info(f"⚡ [SNIPER AUTO-APPLY] Attempting auto-apply for {item_id} (mode={target_mode}, DRY_RUN={AUTO_APPLY_DRY_RUN})...")
                    try:
                        apply_res = apply_for_accommodation(
                            tool_id=tool_id,
                            accommodation_id=item_id,
                            target_mode=target_mode,
                            dry_run=AUTO_APPLY_DRY_RUN
                        )
                    except Exception as apply_err:
                        logger.error(f"Error executing auto-apply: {apply_err}")
                        apply_res = {"success": False, "error": str(apply_err)}

                    if apply_res.get("success"):
                        if AUTO_APPLY_DRY_RUN:
                            caption = (
                                "🧪 *[DRY-RUN - SNIPER ÉTAPE 2 VÉRIFIÉE]*\n\n"
                                f"📍 *Résidence :* {info['residence_name']}\n"
                                f"🏷️ *Type :* {info['label']}{coloc_tag} ({info['surface']} m²)\n"
                                f"👤 *Mode choisi :* {mode_name}\n"
                                f"💶 *Loyer :* {info['price']} € / mois\n"
                                f"⏱️ *Temps d'exécution :* {apply_res['duration_seconds']}s\n"
                                f"📬 *Adresse :* {info['address']}\n\n"
                                "ℹ️ *Mode DRY-RUN :* Étape 2 (récapitulatif) atteinte avec succès ! Capture d'écran générée sans validation finale."
                            )
                        else:
                            caption = (
                                "🎯 *[RÉSERVATION SNIPÉE AVEC SUCCÈS !]*\n\n"
                                f"📍 *Résidence :* {info['residence_name']}\n"
                                f"🏷️ *Type :* {info['label']}{coloc_tag} ({info['surface']} m²)\n"
                                f"👤 *Mode :* {mode_name}\n"
                                f"💶 *Loyer :* {info['price']} € / mois\n"
                                f"⏱️ *Snipé en :* {apply_res['duration_seconds']}s\n\n"
                                "🎉 Demande envoyée au CROUS avec succès !\n"
                                f"🔗 [Accéder à mon panier]({apply_res.get('cart_url', listing_url)})"
                            )
                        result_markup = {
                            "inline_keyboard": [
                                [{"text": "🚀 Ouvrir l'offre CROUS", "url": listing_url}]
                            ]
                        }
                        if apply_res.get("screenshot_path"):
                            send_telegram_photo(apply_res["screenshot_path"], caption, reply_markup=result_markup)
                        else:
                            send_telegram_message(caption, reply_markup=result_markup)
                    else:
                        err_msg = (
                            "⚠️ *[RÉSULTAT DU SNIPER : ÉCHEC]*\n\n"
                            f"📍 *Résidence :* {info['residence_name']}\n"
                            f"🏷️ *Type :* {info['label']}{coloc_tag} ({info['surface']} m²)\n"
                            f"💶 *Loyer :* {info['price']} € / mois\n"
                            f"❌ *Raison :* `{apply_res.get('error', 'Erreur inconnue')}`\n\n"
                            "⚡ *Poursuivez manuellement via le lien déjà envoyé :*\n"
                            f"{listing_url}"
                        )
                        result_markup = {
                            "inline_keyboard": [
                                [{"text": "⚡ Réserver manuellement", "url": listing_url}]
                            ]
                        }
                        if apply_res.get("screenshot_path"):
                            send_telegram_photo(apply_res["screenshot_path"], err_msg, reply_markup=result_markup)
                        else:
                            send_telegram_message(err_msg, reply_markup=result_markup)

                seen_ids.add(item_id)
                new_alerts_sent += 1

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

def wait_and_poll(total_seconds, poll_fn, poll_interval=3.0, clock=time.monotonic, sleep=time.sleep, keep_running=None):
    if total_seconds <= 0:
        return {
            "elapsed": 0.0,
            "polls": 0,
            "poll_time": 0.0,
            "slowest_poll": 0.0,
        }

    if keep_running is None:
        keep_running = lambda: RUNNING

    start = clock()
    deadline = start + total_seconds
    polls = 0
    poll_time = 0.0
    slowest_poll = 0.0
    next_poll = 0.0

    def _do_poll():
        nonlocal polls, poll_time, slowest_poll, next_poll
        t0 = clock()
        try:
            poll_fn()
        except Exception as err:
            logger.debug(f"wait_and_poll poll error: {err}")
        t1 = clock()
        duration = t1 - t0
        polls += 1
        poll_time += duration
        if duration > slowest_poll:
            slowest_poll = duration
        next_poll = t1 + poll_interval

    if not keep_running():
        return {
            "elapsed": float(clock() - start),
            "polls": 0,
            "poll_time": 0.0,
            "slowest_poll": 0.0,
        }

    _do_poll()

    while keep_running() and clock() < deadline:
        now = clock()
        if now >= next_poll:
            _do_poll()
            if not keep_running() or clock() >= deadline:
                break
            now = clock()

        time_to_deadline = deadline - now
        time_to_poll = next_poll - now
        sleep_secs = min(0.5, time_to_deadline, time_to_poll)
        if sleep_secs < 0.05:
            sleep_secs = 0.05
        sleep(sleep_secs)

    return {
        "elapsed": float(clock() - start),
        "polls": polls,
        "poll_time": float(poll_time),
        "slowest_poll": float(slowest_poll),
    }


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
        f"⚡ *Sniper < {AUTO_APPLY_ANY_MAX_PRICE:.0f} € :* Tout mode ({'🧪 DRY-RUN' if AUTO_APPLY_DRY_RUN else '⚡ LIVE'})\n"
        f"🎯 *Sniper {AUTO_APPLY_ANY_MAX_PRICE:.0f}–{AUTO_APPLY_SINGLE_MAX_PRICE:.0f} € :* Individuel seul ({'🧪 DRY-RUN' if AUTO_APPLY_DRY_RUN else '⚡ LIVE'})\n"
        f"📢 *Alerte Telegram seule :* Jusqu'à {MAX_PRICE:.0f} €\n"
        f"⏱️ *Cadence actuelle :* ~{cur_interval}s ({cur_cadence})\n"
        f"🔐 *Session CROUS :* {'Active ✅' if is_logged_in else 'Non configurée / Expirée ❌'}\n\n"
        "Je surveille en continu 24h/24. Envoyez `/status` pour voir les métriques ou `/check` pour vérifier."
    )
    broadcast_telegram_message(startup_msg)

    while RUNNING:
        is_blocked_error = False

        # 1. Check for incoming Telegram commands (/status, /check, /test, /session, /renew, /test_apply, /pause, /resume)
        poll_telegram_updates(on_check_callback=check_and_notify)

        if PAUSED:
            time.sleep(1)
            continue

        work_start = time.monotonic()

        try:
            # 2. Daily morning heartbeat (09:00 UTC)
            now = datetime.now(timezone.utc)
            if ENABLE_DAILY_HEARTBEAT and now.hour == 9 and last_heartbeat_day != now.date():
                last_heartbeat_day = now.date()
                send_telegram_message(
                    "☀️ *Bonjour ! CROUS Watcher est bien actif.*\n"
                    f"Surveillance 24/7 en cours pour {TARGET_CITY.capitalize()}.\n"
                    f"Offres actives en France : {METRICS['last_active_listings_count']}."
                )

            # 3. Run search check (rotate to next proxy on every cycle)
            if proxy_manager:
                proxy_manager.rotate_proxy(skip_exhausted=True)
            check_and_notify()

        except AllProxyGroupsExhaustedError as err:
            logger.critical(f"ALL PROXY GROUPS EXHAUSTED: {err}")
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(state)
            append_run_history("EXHAUSTED", f"{err} | Consecutive: {state['consecutive_failures']}")
            if activity_logger:
                activity_logger.notify_general_error(
                    "Tous les groupes de proxys sont épuisés (402 Payment Required). Aucun proxy sain disponible.",
                    component_name="Scouter"
                )
            if state["consecutive_failures"] == 5:
                send_telegram_message(
                    f"🚨 *Alerte Watcher : 5 échecs consécutifs*\n\n"
                    f"Dernière erreur : `Tous les groupes de proxys sont épuisés (402)`\n"
                    "Vérifiez vos comptes de proxys sur votre VPS."
                )

        except CrousBlockedOrRateLimitedError as err:
            is_blocked_error = True
            logger.exception(f"Rate limited or blocked: {err}")
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(state)
            append_run_history("BLOCKED", str(err))
            if activity_logger:
                activity_logger.notify_general_error(
                    f"Accès refusé ou limite atteinte (HTTP {err.code}) sur l'API CROUS",
                    component_name="Scouter"
                )

        except Exception as err:
            err_str = str(err)
            if "111" in err_str or "connection refused" in err_str.lower():
                is_blocked_error = True
                logger.critical(f"Connection refused by CROUS firewall: {err}")
                if activity_logger:
                    activity_logger.notify_general_error(
                        "Connexion refusée par le pare-feu CROUS (Erreur 111)",
                        component_name="Scouter"
                    )
            else:
                logger.exception(f"Unexpected error in watcher cycle: {err}")
                if activity_logger:
                    activity_logger.notify_general_error(
                        f"Erreur inattendue dans le cycle : {err}",
                        component_name="Watcher"
                    )
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

        # 5. Sleep cadence
        if is_blocked_error:
            # Backoff for 10 minutes (600s) to allow firewall block to clear
            logger.warning("Safety backoff activated: sleeping for 10 minutes (600s)...")
            sleep_time = 600.0
        else:
            cadence_seconds, cadence_label = get_smart_cadence()
            METRICS["current_cadence_mode"] = cadence_label
            jitter = random.uniform(-3.0, 5.0)
            sleep_time = max(15.0, cadence_seconds + jitter)

        work = time.monotonic() - work_start
        wait_res = wait_and_poll(sleep_time, lambda: poll_telegram_updates(on_check_callback=check_and_notify))
        logger.info(
            "Cycle timing: work=%.1fs planned_wait=%.1fs actual_wait=%.1fs polls=%d poll_time=%.1fs slowest_poll=%.1fs total=%.1fs",
            work,
            sleep_time,
            wait_res["elapsed"],
            wait_res["polls"],
            wait_res["poll_time"],
            wait_res["slowest_poll"],
            work + wait_res["elapsed"],
        )

    logger.info("CROUS Watcher Daemon stopped cleanly.")
    send_telegram_message("🛑 *CROUS Watcher arrêté sur le VPS.*")


if __name__ == "__main__":
    main_loop()
