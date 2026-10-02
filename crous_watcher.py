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
import math
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
LISTINGS_DATA_FILE = BASE_DIR / "marseille_listings_data.jsonl"
NATIONWIDE_LISTINGS_DATA_FILE = BASE_DIR / "nationwide_listings_data.jsonl"
NATIONWIDE_SEEN_FILE = BASE_DIR / "nationwide_seen_ids.json"
POSTING_ACTIVITY_FILE = BASE_DIR / "posting_activity.json"
TELEGRAM_OFFSET_FILE = BASE_DIR / ".telegram_update_offset"


def load_telegram_update_offset() -> int:
    """
    Persisted Telegram getUpdates offset, so a service restart doesn't reset it
    to 0. Telegram redelivers any update not yet confirmed by a HIGHER offset for
    up to 24 hours -- an in-memory-only offset (the bug this fixes, found
    2026-09-25) means every restart could silently replay the entire backlog of
    unconfirmed commands and button taps since the last confirmation, each
    re-triggering a real action (e.g. a Snipe button tap firing a genuine second
    apply attempt against CROUS with nobody touching anything).
    """
    if TELEGRAM_OFFSET_FILE.exists():
        try:
            return int(TELEGRAM_OFFSET_FILE.read_text().strip())
        except Exception:
            pass
    return 0


def save_telegram_update_offset(offset: int) -> None:
    temp_file = TELEGRAM_OFFSET_FILE.with_suffix(".tmp")
    try:
        temp_file.write_text(str(offset))
        temp_file.replace(TELEGRAM_OFFSET_FILE)
    except Exception as err:
        logger.error(f"Failed to save {TELEGRAM_OFFSET_FILE}: {err}")

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
# Individual-only, price/surface tiered (colocation is never auto-applied, always
# alert-only). Anchored on real Marseille listings from 2026-09-25: a 255e/12m2 and
# a 284.82e/19m2 T1 were both judged "snipe immediately" deals. A third tier
# (300-350e, >19m2) existed briefly but was removed the same day -- too loose to
# trust unattended; that price band is alert-only now, with the manual Snipe
# button as the override for anything in it worth grabbing.
# The 250-300e tier's upper surface bound (was 19m2) was removed 2026-09-28 -- at
# the user's explicit request ("we don't want to restrict the bot...
# what if it was a 20m2 room for 270? We don't want the bot to miss this one."):
# in this price band, bigger is strictly a better find, never a riskier one, so
# only the floor matters (it guards against "cheap because it's tiny"). This is
# NOT a reinstatement of the removed third tier above -- that was about a
# different, more expensive price band (300-350e) and stays removed.
AUTO_APPLY_ENABLED = os.getenv("AUTO_APPLY_ENABLED", "true").lower() in ("true", "1", "yes")
AUTO_APPLY_DRY_RUN = os.getenv("AUTO_APPLY_DRY_RUN", "true").lower() in ("true", "1", "yes")
AUTO_APPLY_CHEAP_MAX_PRICE = float(os.getenv("AUTO_APPLY_CHEAP_MAX_PRICE", "250"))
AUTO_APPLY_MID_MAX_PRICE = float(os.getenv("AUTO_APPLY_MID_MAX_PRICE", "300"))
AUTO_APPLY_MID_MIN_SURFACE_M2 = float(os.getenv("AUTO_APPLY_MID_MIN_SURFACE_M2", "12"))


def _format_sniper_rules_text() -> str:
    """
    One shared description of the current auto-apply price/surface tiers, used in
    the /help, /status, and startup Telegram messages so they can't drift out of
    sync with each other or with the actual trigger logic in is_target_listing().
    """
    mode_tag = "🧪 DRY-RUN" if AUTO_APPLY_DRY_RUN else "⚡ LIVE"
    return (
        f"⚡ *Sniper < {AUTO_APPLY_CHEAP_MAX_PRICE:.0f} € :* Individuel, toute surface ({mode_tag})\n"
        f"🎯 *Sniper {AUTO_APPLY_CHEAP_MAX_PRICE:.0f}–{AUTO_APPLY_MID_MAX_PRICE:.0f} € :* Individuel, "
        f"≥ {AUTO_APPLY_MID_MIN_SURFACE_M2:.0f} m² ({mode_tag})\n"
        f"📢 *Alerte seule (+ bouton Snipe manuel) :* Colocation (toujours), "
        f"Individuel {AUTO_APPLY_MID_MAX_PRICE:.0f}–{MAX_PRICE:.0f} €"
    )


def _session_keeper_state() -> str:
    """'active', 'inactive', or 'unknown' -- session-keeper.service is a separate systemd unit."""
    try:
        import subprocess
        out = subprocess.run(
            ["systemctl", "is-active", "session-keeper.service"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return "active" if out == "active" else "inactive"
    except Exception:
        return "unknown"


def _count_lines(path: Path) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _count_marseille_appearances() -> int:
    """Appearance lines in the Marseille data file (it also holds "gone" lines)."""
    try:
        with open(LISTINGS_DATA_FILE, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip() and json.loads(line).get("event", "appeared") == "appeared")
    except Exception:
        return 0


def _format_bot_capabilities_text(paused: bool, keeper_state: str, has_credentials: bool) -> str:
    """
    Plain-language summary of what the bot does and does NOT do right now, built from
    the live config, for /status. Pure function (all external state passed in or read
    from module config) so it can be tested without systemd or Telegram.
    """
    viewers = len(get_viewer_chat_ids())
    nationwide_n = _count_lines(NATIONWIDE_LISTINGS_DATA_FILE)
    marseille_n = _count_marseille_appearances()

    does = []
    does_not = []

    if paused:
        does_not.append("🚫 Recherche d'offres : *EN PAUSE* (`/resume` pour reprendre)")
    else:
        does.append("✅ Recherche des offres CROUS (toute la France) via l'API publique — *sans connexion à ton compte*")
        does.append(
            f"✅ Alerte Telegram à chaque mise en ligne d'une offre à {TARGET_CITY.capitalize()} ≤ {MAX_PRICE:.0f} € "
            f"(toi + {viewers} viewer{'s' if viewers != 1 else ''}) — y compris une offre déjà vue qui revient, "
            f"une seule alerte tant qu'elle reste en ligne"
        )
        does.append(
            f"✅ Collecte de données : {nationwide_n} offres France + {marseille_n} mises en ligne "
            f"{TARGET_CITY.capitalize()} enregistrées (avec leur durée en ligne), + heures de publication suivies"
        )

    if AUTO_APPLY_ENABLED:
        mode = "🧪 DRY-RUN (capture seulement, aucune vraie demande)" if AUTO_APPLY_DRY_RUN \
            else "⚡ LIVE (envoie de VRAIES demandes de réservation)"
        does.append(f"✅ Auto-candidature : *ACTIVÉE* — {mode}\n{_format_sniper_rules_text()}")
    else:
        does_not.append("🚫 Auto-candidature : *désactivée* — aucune candidature automatique")

    if keeper_state == "active" and AUTO_APPLY_ENABLED:
        does.append("✅ Session keeper : se reconnecte à ton compte CROUS toutes les ~20 min")
    elif keeper_state == "active":
        does_not.append("🚫 Session keeper : lancé mais en veille (auto-candidature off) — aucune connexion")
    elif keeper_state == "inactive":
        does_not.append("🚫 Session keeper : arrêté — aucune connexion automatique à ton compte")
    else:
        does_not.append("❓ Session keeper : état inconnu")

    text = "📋 *Ce que le bot FAIT :*\n" + ("\n".join(does) if does else "— rien —")
    text += "\n\n🚫 *Ce que le bot NE FAIT PAS :*\n" + ("\n".join(does_not) if does_not else "— rien —")

    if has_credentials:
        snipe_effect = "capture seulement (DRY-RUN)" if AUTO_APPLY_DRY_RUN \
            else "envoie une *VRAIE demande de réservation*"
        snipe_where = "sur *chaque* alerte" if not AUTO_APPLY_ENABLED \
            else "sur les alertes non auto-snipées"
        text += (
            "\n\n⚠️ *Seulement si TU le déclenches (se connecte à ton compte) :*\n"
            f"• Bouton 🎯 Snipe ({snipe_where}) → {snipe_effect}\n"
            "• `/renew` → connexion au compte\n"
            "• `/test_apply` → connexion + test (capture seulement)"
        )
    else:
        text += "\n\n🔒 *Aucun identifiant CROUS configuré* — le bot ne peut pas se connecter à ton compte."
    return text


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
    "telegram_update_offset": load_telegram_update_offset(),
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


def send_telegram_photo(photo_path: str, caption: str = "", reply_markup: dict = None, chat_id: str = None) -> bool:
    """
    Send a photo attachment with caption to the configured Telegram chat.

    reply_markup was silently dropped before (this function never accepted it, though
    two call sites in check_and_notify() passed it anyway) -- meaning any real
    auto-snipe result with a screenshot would have raised a TypeError, crashing mid
    check_and_notify() *before* seen_ids gets saved, causing the same listing to be
    re-alerted and re-sniped on every subsequent cycle. Never triggered in production
    before AUTO_APPLY_ENABLED was flipped true (2026-09-25), so it stayed dormant.
    Fixed same day, before any real auto-snipe had a chance to hit it.
    """
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        return False
    if not os.path.exists(photo_path):
        logger.warning(f"Photo path does not exist: {photo_path}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        import requests
        data = {
            "chat_id": target,
            "caption": caption[:1024],
            "parse_mode": "Markdown"
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data=data,
                files={"photo": f},
                timeout=25
            )
            return resp.ok
    except Exception as err:
        logger.error(f"Failed to send Telegram photo: {err}")
        return False


def answer_telegram_callback(callback_query_id: str, text: str = "") -> None:
    """Acknowledge a Telegram inline-button tap so the client stops showing a spinner."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id, "text": text[:200]}
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as err:
        logger.warning(f"Failed to answer Telegram callback: {err}")


# accommodation_id -> unix timestamp of last manual-snipe trigger. Debounces a
# double-tap (or the same button tapped twice) from launching two concurrent/
# back-to-back snipe attempts on the same listing -- back-to-back attempts are a
# real, confirmed way to trigger CROUS's own rate limiting (observed live
# 2026-09-25: 2 of 5 rapid attempts got HTTP 429'd). In-memory only, resets on
# restart -- this is a short-lived debounce, not state worth persisting.
_recent_manual_snipes: dict[str, float] = {}
MANUAL_SNIPE_DEBOUNCE_SECONDS = 90


def handle_telegram_callback(callback_query: dict) -> None:
    """
    Handle a tap on the owner-only "🎯 Snipe" button attached to a listing alert
    that the automatic price/surface tiers didn't already fire on. Runs the exact
    same apply_for_accommodation() engine as the automatic sniper, respecting
    AUTO_APPLY_DRY_RUN -- this is not a separate, less-safe path.
    """
    cq_id = callback_query.get("id", "")
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    data = callback_query.get("data", "")

    # Security: the button only ever appears in the owner's own copy of the alert
    # (see check_and_notify()), so this should be unreachable for the viewer --
    # checked anyway, defensively, same spirit as the message-command check above.
    if chat_id != TELEGRAM_CHAT_ID:
        answer_telegram_callback(cq_id, "Non autorisé.")
        return

    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "snipe":
        answer_telegram_callback(cq_id, "Bouton invalide.")
        return
    _, tool_id, accommodation_id, target_mode = parts

    now = time.time()
    last = _recent_manual_snipes.get(accommodation_id, 0.0)
    if now - last < MANUAL_SNIPE_DEBOUNCE_SECONDS:
        answer_telegram_callback(cq_id, "Déjà tenté récemment pour cette offre -- patientez avant de réessayer.")
        return
    _recent_manual_snipes[accommodation_id] = now

    if not apply_for_accommodation:
        answer_telegram_callback(cq_id, "Module sniper indisponible.")
        return

    answer_telegram_callback(cq_id, "🎯 Sniper lancé...")
    listing_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/accommodations/{accommodation_id}"
    logger.info(
        f"⚡ [MANUAL SNIPE] Triggered via Telegram button for {accommodation_id} "
        f"(mode={target_mode}, DRY_RUN={AUTO_APPLY_DRY_RUN})..."
    )
    try:
        apply_res = apply_for_accommodation(
            tool_id=tool_id,
            accommodation_id=accommodation_id,
            target_mode=target_mode,
            dry_run=AUTO_APPLY_DRY_RUN
        )
    except Exception as apply_err:
        logger.error(f"Error executing manual snipe: {apply_err}")
        apply_res = {"success": False, "error": str(apply_err)}

    result_markup = {"inline_keyboard": [[{"text": "🚀 Ouvrir l'offre CROUS", "url": listing_url}]]}
    if apply_res.get("success"):
        if AUTO_APPLY_DRY_RUN:
            caption = (
                "🧪 *[DRY-RUN - SNIPE MANUEL VÉRIFIÉ]*\n\n"
                f"🆔 *Offre :* #{accommodation_id}\n"
                f"⏱️ *Temps d'exécution :* {apply_res.get('duration_seconds', '?')}s\n\n"
                "ℹ️ Étape 2 (récapitulatif) atteinte avec succès. Capture d'écran générée sans validation finale."
            )
        else:
            caption = (
                "🎯 *[RÉSERVATION SNIPÉE MANUELLEMENT !]*\n\n"
                f"🆔 *Offre :* #{accommodation_id}\n"
                f"⏱️ *Snipé en :* {apply_res.get('duration_seconds', '?')}s\n\n"
                f"🔗 [Accéder à mon panier]({apply_res.get('cart_url', listing_url)})"
            )
        if apply_res.get("screenshot_path"):
            send_telegram_photo(apply_res["screenshot_path"], caption, reply_markup=result_markup)
        else:
            send_telegram_message(caption, reply_markup=result_markup)
    else:
        err_msg = (
            "⚠️ *[SNIPE MANUEL : ÉCHEC]*\n\n"
            f"🆔 *Offre :* #{accommodation_id}\n"
            f"❌ *Raison :* `{apply_res.get('error', 'Erreur inconnue')}`\n\n"
            f"⚡ *Poursuivez manuellement :*\n{listing_url}"
        )
        if apply_res.get("screenshot_path"):
            send_telegram_photo(apply_res["screenshot_path"], err_msg, reply_markup=result_markup)
        else:
            send_telegram_message(err_msg, reply_markup=result_markup)


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
                # Persisted immediately, before handling -- not after -- so a
                # restart (even mid-handling of a long-running snipe) can never
                # replay this update again. See load_telegram_update_offset()'s
                # docstring for why an in-memory-only offset was a real bug.
                save_telegram_update_offset(update_id + 1)

                callback_query = update.get("callback_query")
                if callback_query:
                    handle_telegram_callback(callback_query)
                    continue

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


def pick_test_apply_target(items: list[dict]) -> str | None:
    """
    Picks which listing /test_apply should run its dry-run against.

    Bug fixed 2026-09-25: this used to blindly take items[0] from a full
    nationwide fetch -- an unpredictable, possibly already-unavailable listing
    that changes every call, nothing like the stable target used for manual
    CLI verification (which always tested the same known-good listing).
    Prefers the first listing CROUS itself marks "available", so this doesn't
    fail on a listing that's simply already full/expired for reasons unrelated
    to the sniper at all. Falls back to items[0] if none are marked available,
    and to None (caller keeps its own hardcoded default) if items is empty.
    """
    if not items:
        return None
    available_item = next((it for it in items if it.get("available")), None)
    chosen = available_item or items[0]
    return str(chosen.get("id", "6"))


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
            f"{_format_sniper_rules_text()}\n"
            f"🤖 *Auto-Apply :* {'Activé' if AUTO_APPLY_ENABLED else 'Désactivé'}"
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
            f"🎯 *Cible :* {TARGET_CITY.capitalize()}\n\n"
            + _format_bot_capabilities_text(
                paused=PAUSED,
                keeper_state=_session_keeper_state(),
                has_credentials=bool(os.getenv("CROUS_EMAIL") and os.getenv("CROUS_PASSWORD")),
            )
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
            picked = pick_test_apply_target(items)
            if picked is not None:
                sample_acc_id = picked
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
    success = send_telegram_message(test_text, markup)
    if success:
        logger.info("Test notification dispatched successfully.")


# ==============================================================================
# State Management (Crash-safe atomic writes)
# ==============================================================================

def load_state() -> dict:
    """
    Load watcher state from disk. Every key on disk is kept (not an allow-list), so a
    load/modify/save round trip can never silently drop a field it doesn't know about.
      seen_ids             -- every Marseille listing ID ever alerted (history only)
      consecutive_failures -- failed cycles in a row (reset by each successful cycle)
      active               -- listings currently considered online; see update_listing_visibility()
      history              -- per-ID appearance count and when it last went offline
    """
    state = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                state = {"seen_ids": data}
            elif isinstance(data, dict):
                state = data
        except Exception as err:
            logger.warning(f"Error reading {STATE_FILE}: {err}. Resetting state.")

    state.setdefault("seen_ids", [])
    state.setdefault("consecutive_failures", 0)
    state.setdefault("active", {})
    state.setdefault("history", {})
    return state


def save_state(state: dict) -> None:
    """Save state atomically using a temporary file."""
    temp_file = STATE_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        temp_file.replace(STATE_FILE)
    except Exception as err:
        logger.error(f"Failed to atomically save state to {STATE_FILE}: {err}")


def _record_cycle_failure() -> int:
    """
    Increment consecutive_failures on the CURRENT on-disk state and return the new count.

    main_loop() used to load the state once at startup and, on every failed cycle, save
    that startup snapshot back -- silently erasing every listing ID check_and_notify()
    had recorded since. Confirmed in production: #2156 was alerted 2026-09-25 07:11,
    erased by the 12:19-12:58 failure writes of a 06:47 snapshot (absent from the
    16:42 git snapshot of listings_seen.json), then alerted again 2026-09-27. The same
    stale object also made the "5 consecutive failures" alert count failures since
    startup rather than in a row, since check_and_notify()'s reset to 0 never reached it.
    """
    state = load_state()
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    save_state(state)
    return state["consecutive_failures"]


# A listing counts as gone (so its next appearance alerts again) only after it is
# missing from this many COMPLETE checks in a row. One is not enough: the search API
# has returned incomplete results without any error -- 53 -> 8 listings for one check
# on 2026-09-30 00:00 UTC, and single-check Marseille dips (3 -> 2 -> 3) on 2026-10-02
# -- and with a threshold of 1, each of those would have re-alerted every listing it
# hid. Cost: a listing taken and re-posted within ~2 checks (~1-1.5 min at peak
# cadence, ~8 min at night) doesn't get a second alert.
LISTING_GONE_AFTER_MISSES = 2


def update_listing_visibility(active: dict, present: dict, fetch_complete: bool, now_iso: str,
                              gone_after_misses: int = LISTING_GONE_AFTER_MISSES) -> tuple[list, list]:
    """
    Track which matching listings are currently online, so an alert fires once per
    APPEARANCE: when a listing comes online, never again while it stays online, and
    again if it goes offline and later comes back (a re-posting -- previously these
    were silently ignored forever, because seen_ids never forgets an ID).

    active:  {id: {"since", "last_seen", "checks", "missed", ...snapshot}} -- mutated in place
    present: {id: snapshot dict} for the matching listings seen in THIS check
    fetch_complete: False if any part of this check's fetch failed; then absences are
        not counted at all, since a missing listing may simply not have been fetched.

    Returns (appeared_ids, gone) where gone is a list of (id, final active entry).
    """
    appeared = []
    for lid, snapshot in present.items():
        entry = active.get(lid)
        if entry is None:
            active[lid] = dict(snapshot, since=now_iso, last_seen=now_iso, checks=1, missed=0)
            appeared.append(lid)
        else:
            entry.update(snapshot)
            entry["last_seen"] = now_iso
            entry["checks"] = entry.get("checks", 0) + 1
            entry["missed"] = 0

    gone = []
    if fetch_complete:
        for lid in [i for i in active if i not in present]:
            entry = active[lid]
            entry["missed"] = entry.get("missed", 0) + 1
            if entry["missed"] >= gone_after_misses:
                gone.append((lid, active.pop(lid)))
    return appeared, gone


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


def record_marseille_listing(info: dict, tool_id: str, listing_url: str,
                             appearance_no: int | None = 1, reappearance: bool = False) -> None:
    """
    Append one line of raw structured data for every Marseille listing APPEARANCE
    (first time online, or back online after going offline -- see
    update_listing_visibility()), regardless of price or whether it qualified for
    auto-apply. This is deliberately
    unfiltered observational data (price, surface, room type, mode, coordinates,
    timestamp) so the sniper's price/surface tier thresholds can eventually be tuned
    against real market data instead of guessed at. Append-only JSONL (one JSON
    object per line); never overwrites or trims previous entries. Lines written
    before 2026-10-02 have no "event" field and are all first appearances.
    appearance_no is None when the earlier count is unknown (ID seen before the
    appearance tracking existed).
    """
    entry = {
        "event": "appeared",
        "appearance_no": appearance_no,
        "reappearance": reappearance,
        "seen_at": datetime.now(timezone.utc).isoformat(),
        "id": info.get("id"),
        "tool_id": tool_id,
        "residence_name": info.get("residence_name"),
        "label": info.get("label"),
        "surface_m2": info.get("surface"),
        "min_single_rent": info.get("min_single_rent"),
        "min_coloc_rent": info.get("min_coloc_rent"),
        "is_coloc": info.get("is_coloc"),
        "should_auto_apply": info.get("should_auto_apply"),
        "address": info.get("address"),
        "lat": info.get("lat"),
        "lon": info.get("lon"),
        "url": listing_url,
    }
    try:
        with open(LISTINGS_DATA_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as err:
        logger.error(f"Failed to record listing data: {err}")


def _format_reappearance_line(appearance_no: int | None, last_seen_online_iso: str | None, now_iso: str) -> str:
    """Telegram line telling the reader this listing was already posted before."""
    if not appearance_no:
        return "🔁 *Déjà vue :* oui, avant le suivi des apparitions"
    text = f"🔁 *Déjà vue :* {appearance_no}ᵉ apparition"
    try:
        secs = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_seen_online_iso)).total_seconds()
        if secs < 3600:
            ago = f"{max(1, round(secs / 60))} min"
        elif secs < 48 * 3600:
            ago = f"{round(secs / 3600)} h"
        else:
            ago = f"{round(secs / 86400)} jours"
        text += f" (dernière fois en ligne il y a {ago})"
    except Exception:
        pass
    return text


def record_marseille_listing_gone(listing_id: str, entry: dict, detected_at_iso: str) -> None:
    """
    Append a "gone" line to the same file when a listing goes offline, closing the
    matching "appeared" line. online_seconds = last check that saw it minus the first,
    so 0 means it was online for a single check only (i.e. less than one check interval).
    """
    since, last_seen = entry.get("since"), entry.get("last_seen")
    try:
        online_seconds = round((datetime.fromisoformat(last_seen) - datetime.fromisoformat(since)).total_seconds())
    except Exception:
        online_seconds = None
    line = {
        "event": "gone",
        "id": listing_id,
        "appeared_at": since,
        "last_seen_at": last_seen,
        "gone_detected_at": detected_at_iso,
        "online_seconds": online_seconds,
        "checks_seen": entry.get("checks"),
        "residence_name": entry.get("residence_name"),
        "label": entry.get("label"),
        "price": entry.get("price"),
    }
    try:
        with open(LISTINGS_DATA_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as err:
        logger.error(f"Failed to record listing gone event: {err}")


def record_nationwide_listing_raw(item: dict, tool_id: str) -> None:
    """
    Append the FULL raw listing record, exactly as CROUS's search API returned it, for
    ONE newly-seen listing -- any city, not just Marseille, and regardless of whether it
    matches any auto-apply rule. Append-only JSONL (one JSON object per line), never
    overwrites or trims previous entries.

    Deliberately broader than record_marseille_listing() above, which keeps only a
    curated subset of fields for Marseille matches: this keeps every field CROUS sent,
    including ones the bot doesn't currently act on at all (equipments, media captions,
    occupationModes, availability, indicators...). Added 2026-09-27 after a real
    listing's private-kitchen/bathroom status turned out to hinge on exactly this kind
    of field (the "equipments" list, e.g. "Evier + plaque" = a private kitchenette) --
    and by the time that question came up, the listing had already disappeared from
    CROUS's live search results (booked), with no way to recover what it had actually
    offered. Also builds, over time, real data on whether new listings really do get
    posted at night / on weekends (see HANDOFF "Night/day request throttling").
    """
    entry = {
        "seen_at": datetime.now(timezone.utc).isoformat(),
        "tool_id": tool_id,
        "item": {k: v for k, v in item.items() if k != "_tool_id"},
    }
    try:
        with open(NATIONWIDE_LISTINGS_DATA_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as err:
        logger.error(f"Failed to record nationwide listing data: {err}")


def _detect_and_record_nationwide_new(all_raw_items: list, nationwide_seen: set) -> set:
    """
    Given this cycle's full raw item list and the previously-seen nationwide ID set,
    returns the set of genuinely new IDs (any city) and, as a side effect, records the
    full raw item for each one via record_nationwide_listing_raw(). Extracted out of
    check_and_notify() so this can be unit-tested directly, without mocking the
    network/Telegram machinery around it.
    """
    new_ids = set()
    for it in all_raw_items:
        iid = str(it.get("id", ""))
        if iid and iid not in nationwide_seen and iid not in new_ids:
            new_ids.add(iid)
            record_nationwide_listing_raw(it, it.get("_tool_id", "47"))
    return new_ids


def load_nationwide_seen_ids() -> set:
    """IDs of every listing ever seen nationwide (any city), for posting-activity tracking."""
    if not NATIONWIDE_SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(NATIONWIDE_SEEN_FILE.read_text(encoding="utf-8")))
    except Exception as err:
        logger.warning(f"Error reading {NATIONWIDE_SEEN_FILE}: {err}. Resetting.")
        return set()


def save_nationwide_seen_ids(ids: set) -> None:
    """Save atomically using a temporary file, same pattern as save_state()."""
    temp_file = NATIONWIDE_SEEN_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f)
        temp_file.replace(NATIONWIDE_SEEN_FILE)
    except Exception as err:
        logger.error(f"Failed to save {NATIONWIDE_SEEN_FILE}: {err}")


def record_posting_activity(nationwide_new_count: int, marseille_new_count: int) -> None:
    """
    Updates today's (Paris-local calendar date) first-seen/last-seen timestamp and
    count of newly-detected listings, tracked separately for nationwide (all of
    France) and Marseille. Goal: build a real, data-driven picture of when CROUS
    actually posts new listings (start/stop hours, weekday patterns), instead of
    the untested "quiet at night/on Sundays" assumption the smart-cadence feature
    currently runs on -- eventually used to widen/tune the "peak hours" cadence
    window with evidence instead of a guess.

    No-op (no write at all) when nothing new was seen this cycle, since
    check_and_notify() runs frequently and most cycles find nothing new.
    """
    if nationwide_new_count <= 0 and marseille_new_count <= 0:
        return
    now_paris = datetime.now(PARIS_TZ)
    ts_iso = now_paris.isoformat()
    day_key = now_paris.strftime("%Y-%m-%d")

    data = {}
    if POSTING_ACTIVITY_FILE.exists():
        try:
            data = json.loads(POSTING_ACTIVITY_FILE.read_text(encoding="utf-8"))
        except Exception as err:
            logger.warning(f"Error reading {POSTING_ACTIVITY_FILE}: {err}. Resetting.")
            data = {}

    day = data.setdefault(day_key, {})
    for scope, new_count in (("nationwide", nationwide_new_count), ("marseille", marseille_new_count)):
        if new_count <= 0:
            continue
        first_key, last_key, count_key = f"{scope}_first_seen", f"{scope}_last_seen", f"{scope}_count"
        if first_key not in day:
            day[first_key] = ts_iso
        day[last_key] = ts_iso
        day[count_key] = day.get(count_key, 0) + new_count

    temp_file = POSTING_ACTIVITY_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        temp_file.replace(POSTING_ACTIVITY_FILE)
    except Exception as err:
        logger.error(f"Failed to save {POSTING_ACTIVITY_FILE}: {err}")


# ==============================================================================
# CROUS API Scraper Engine
# ==============================================================================

# Cache for discover_tool_ids(): the homepage used to be re-fetched every
# single cycle with no retry and no proxy rotation, and any failure (network
# blip, rate limit, etc.) silently fell back to a hardcoded tool ID with no
# alert. Now it's checked at most once per TOOL_ID_REFRESH_INTERVAL_SEC, with
# the same proxy-retry pattern fetch_all_crous_listings() uses, and it only
# falls back to the hardcoded default (loudly, via Telegram) if there's no
# previously-known-good list to fall back to instead.
_TOOL_IDS_CACHE = {"ids": None, "checked_at": 0.0}
TOOL_ID_REFRESH_INTERVAL_SEC = 3600
TOOL_ID_MAX_PROXY_RETRIES = 3


def discover_tool_ids() -> list[str]:
    """Discover active tool IDs from the CROUS homepage, with caching and proxy retry/rotation."""
    now = time.time()
    if _TOOL_IDS_CACHE["ids"] is not None and (now - _TOOL_IDS_CACHE["checked_at"]) < TOOL_ID_REFRESH_INTERVAL_SEC:
        return _TOOL_IDS_CACHE["ids"]

    url = "https://trouverunlogement.lescrous.fr/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }

    last_error = "No /tools/<id> pattern found on homepage."
    for attempt in range(TOOL_ID_MAX_PROXY_RETRIES):
        proxy_url = None
        try:
            req = urllib.request.Request(url, headers=headers)
            opener, proxy_url = get_crous_opener_and_proxy(rotate=False)
            with opener.open(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            import re
            found = set(re.findall(r"/tools/(\d+)", html))
            if found:
                ids = sorted(list(found))
                _TOOL_IDS_CACHE["ids"] = ids
                _TOOL_IDS_CACHE["checked_at"] = now
                if activity_logger:
                    activity_logger.log_scouter_attempt(proxy_url, url, success=True, next_run="2-3 seconds")
                return ids
        except AllProxyGroupsExhaustedError:
            raise
        except Exception as err:
            last_error = err
            is_last_attempt = attempt >= TOOL_ID_MAX_PROXY_RETRIES - 1
            if activity_logger:
                activity_logger.log_scouter_attempt(
                    proxy_url,
                    url,
                    success=False,
                    error_message=f"Découverte d'outils échouée : {err}",
                    next_run=get_next_run_estimate() if is_last_attempt else "2-3 seconds"
                )
            logger.warning(f"Tool discovery attempt {attempt + 1}/{TOOL_ID_MAX_PROXY_RETRIES} failed: {err}")

        if attempt < TOOL_ID_MAX_PROXY_RETRIES - 1 and proxy_manager:
            proxy_manager.rotate_proxy(skip_exhausted=True)

    # All retries failed.
    if _TOOL_IDS_CACHE["ids"] is not None:
        logger.warning(
            f"Tool discovery failed after {TOOL_ID_MAX_PROXY_RETRIES} attempts; "
            f"reusing last known tool IDs {_TOOL_IDS_CACHE['ids']}: {last_error}"
        )
        return _TOOL_IDS_CACHE["ids"]

    logger.error(
        f"Tool discovery failed after {TOOL_ID_MAX_PROXY_RETRIES} attempts and no cached value "
        f"exists; falling back to default tool ID 47: {last_error}"
    )
    send_telegram_message(
        "⚠️ *[Alerte Scouter : Découverte d'outils échouée]*\n\n"
        f"Impossible de découvrir les outils actifs après {TOOL_ID_MAX_PROXY_RETRIES} tentatives.\n"
        f"Dernière erreur : `{str(last_error)[:200]}`\n"
        "Utilisation de l'ID par défaut (47) en secours."
    )
    return ["47"]


def discover_tool_ids_with_fetch_flag() -> tuple[list[str], bool]:
    """
    Like discover_tool_ids(), but also reports whether a real homepage fetch just
    happened (cache miss, at most once per hour) vs. a cache hit (the vast majority
    of cycles). Lets callers skip pacing delays that only make sense right after a
    real request -- e.g. check_and_notify()'s "natural delay between homepage check
    and search requests" used to fire every single cycle even when the homepage
    wasn't actually touched that cycle, once discover_tool_ids() started caching.
    """
    checked_at_before = _TOOL_IDS_CACHE.get("checked_at", 0.0)
    tool_ids = discover_tool_ids()
    did_real_fetch = _TOOL_IDS_CACHE.get("checked_at", 0.0) != checked_at_before
    return tool_ids, did_real_fetch


def _settle_proxy_health(working_proxy: str | None, failed_proxies: list) -> None:
    """
    Per-proxy health bookkeeping for the scouter's datacenter pool (see
    proxy_manager.record_proxy_strike). Called only after a request finally SUCCEEDED:
    every proxy that failed earlier in that same request is blamed (a different proxy
    just worked, so the fault is that proxy's, not an outage), and the working proxy's
    strikes are cleared. If every attempt failed this is never called -- nobody gets
    blamed for what may be a systemic outage. 402s never reach here (they're handled
    per account by mark_group_exhausted). Never raises: bookkeeping must not break polling.
    """
    if not proxy_manager:
        return
    try:
        proxy_manager.record_proxy_success(working_proxy)
        working_key = proxy_manager.proxy_key(working_proxy)
        blamed = set()
        for failed_url, reason in failed_proxies:
            failed_key = proxy_manager.proxy_key(failed_url)
            if not failed_key or failed_key == working_key or failed_key in blamed:
                continue
            blamed.add(failed_key)
            result = proxy_manager.record_proxy_strike(failed_url, reason)
            safe_reason = str(reason).replace("`", "'")[:120]
            if result["quarantined_now"]:
                limit = proxy_manager.PROXY_STRIKE_LIMIT
                logger.warning(f"Proxy {failed_key} benched for 24h after {limit} consecutive failures ({safe_reason})")
                send_telegram_message(
                    "🚫 *[Alerte Proxy - Proxy mis de côté]*\n\n"
                    f"• *Proxy :* `{failed_key}`\n"
                    f"• *Raison :* `{limit} échecs consécutifs (dernier : {safe_reason})`\n"
                    f"• *Proxys non écartés :* {result['pool_size'] - result['benched_count']}/{result['pool_size']}\n"
                    "• *Action :* Exclu du scouter pendant 24h, puis un dernier essai."
                )
            elif result["blocked_by_floor"]:
                logger.warning(f"Proxy {failed_key} reached the strike limit but was NOT benched: quarantine cap reached")
                if activity_logger:
                    activity_logger.notify_general_error(
                        "Mise de côté des proxys bloquée : la limite de 25 % du pool est atteinte, "
                        "plus aucun proxy n'est écarté. Vérifiez les proxys du scouter.",
                        component_name="Scouter"
                    )
    except Exception as err:
        logger.warning(f"Proxy health bookkeeping failed (ignored, polling continues): {err}")


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
        failed_proxies = []  # (proxy_url, reason) for each non-402 failure this request; see _settle_proxy_health
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

                is_final_attempt = attempt >= max_proxy_retries - 1
                retry_str = get_next_run_estimate() if is_final_attempt else "Retrying with next proxy..."
                failed_proxies.append((proxy_url, f"HTTP {http_err.code} ({http_err.reason})"))
                if activity_logger:
                    # Every failed attempt is still logged, but only the FINAL one pages the
                    # owner: a failure that a retry on another proxy recovers from is counted
                    # as a proxy strike instead (see _settle_proxy_health).
                    activity_logger.log_scouter_attempt(
                        proxy_url,
                        url,
                        success=False,
                        error_message=(f"HTTP {http_err.code} sur l'API CROUS ({http_err.reason})" if is_final_attempt else None),
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

                is_final_attempt = attempt >= max_proxy_retries - 1
                retry_str = get_next_run_estimate() if is_final_attempt else "Retrying with next proxy..."
                failed_proxies.append((proxy_url, str(err)))
                if activity_logger:
                    # Same rule as the HTTPError branch above: log every attempt, alert only
                    # when the request finally fails.
                    activity_logger.log_scouter_attempt(
                        proxy_url,
                        url,
                        success=False,
                        error_message=(f"Erreur réseau sur le proxy ({err})" if is_final_attempt else None),
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

        if data is not None:
            _settle_proxy_health(proxy_url, failed_proxies)

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
        # Delay between pages -- tightened 2026-09-25 from 2.0-3.0s to 1.0-1.5s after a
        # live test against the real endpoint: 9/9 clean requests at both 1.5s and 1.0s
        # spacing, but a failure appeared at 0.5s (a fast ~1.1s URLError, not a timeout --
        # possibly CROUS actively rejecting, not just proxy noise). 1.0-1.5s is the
        # tightest interval that came back fully clean; do not push below this without
        # re-testing.
        time.sleep(random.uniform(1.0, 1.5))

    return all_items


# Marseille city center, used for coordinate-based location matching.
# residence.location (lat/lon) comes straight from CROUS's own search API on
# every listing (confirmed live: 0/50 sampled items missing it) and is far
# more reliable than matching city names or postal codes in free-text
# addresses, which can false-positive on things like street names ("Route de
# Marseille" in a different town) or CEDEX-style postal codes CROUS uses for
# some residences (e.g. 13288, 13388, 13331) that fall outside the standard
# arrondissement range (13001-13016).
MARSEILLE_CENTER_LAT = 43.2965
MARSEILLE_CENTER_LON = 5.3698
MARSEILLE_RADIUS_KM = 20.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def is_target_listing(item: dict) -> tuple[bool, dict]:
    """
    Filter item according to TARGET_CITY, MAX_PRICE, and price/surface-tiered
    auto-apply rules. Individual (single) mode only -- colocation is never
    auto-applied, always alert-only:
    - rent < AUTO_APPLY_CHEAP_MAX_PRICE (250€) -> Snipe immediately, any surface.
    - AUTO_APPLY_CHEAP_MAX_PRICE <= rent <= AUTO_APPLY_MID_MAX_PRICE (250-300€) ->
      Snipe if surface is at least AUTO_APPLY_MID_MIN_SURFACE_M2 (12 m²) -- no
      upper bound (removed 2026-09-28: in this price band a bigger room is
      strictly a better find, never a riskier one; the floor alone still guards
      against "cheap because it's tiny").
    - Anything else within MAX_PRICE (400€) -> Alert only via Telegram, do NOT
      auto-apply (a manual Snipe button is offered instead). A third tier
      (300-350€, >19m²) existed briefly but was removed 2026-09-25 as too loose
      to trust unattended.
    - rent > MAX_PRICE (400€) -> Discard completely.
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

    # 1. Location match: prefer real coordinates from CROUS's own data (reliable),
    # fall back to address-text matching only if coordinates are missing.
    location = residence.get("location") or {}
    lat, lon = location.get("lat"), location.get("lon")
    if TARGET_CITY in ("all", "*", ""):
        city_match = True
    else:
        if TARGET_CITY == "marseille" and lat is not None and lon is not None:
            city_match = _haversine_km(lat, lon, MARSEILLE_CENTER_LAT, MARSEILLE_CENTER_LON) <= MARSEILLE_RADIUS_KM
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
    surface_val = surface if isinstance(surface, (int, float)) else None

    if AUTO_APPLY_ENABLED and min_single_rent is not None:
        x = min_single_rent
        if x < AUTO_APPLY_CHEAP_MAX_PRICE:
            should_auto_apply = True
        elif AUTO_APPLY_CHEAP_MAX_PRICE <= x <= AUTO_APPLY_MID_MAX_PRICE:
            # No upper surface bound here (removed 2026-09-28) -- only the floor
            # matters; a bigger room in this price band is never a worse find.
            should_auto_apply = (
                surface_val is not None
                and surface_val >= AUTO_APPLY_MID_MIN_SURFACE_M2
            )
        # Third tier (AUTO_APPLY_MID_MAX_PRICE < x <= 350e, surface > 19m2) removed
        # 2026-09-25 -- too loose to trust unattended. That band is alert-only now;
        # the manual Snipe button covers anything in it worth grabbing.

        if should_auto_apply:
            chosen_mode = "single"
            effective_rent = x

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
        "should_auto_apply": should_auto_apply,
        "min_single_rent": min_single_rent,
        "min_coloc_rent": min_coloc_rent,
        "lat": lat,
        "lon": lon,
    }
    return True, parsed


def build_alert_markups(
    listing_url: str, is_sniper_target: bool, tool_id: str, item_id: str, target_mode: str
) -> tuple[dict, dict]:
    """
    Builds the (owner_markup, viewer_markup) inline keyboards for a new-listing
    alert. Both get the "open listing" link; only the owner's copy additionally
    gets a "🎯 Snipe" button, and only when the listing wasn't already
    auto-sniped (is_sniper_target) -- the button is for listings the automatic
    price/surface tiers were too strict to catch, not a duplicate trigger for
    ones that already fired.
    """
    open_button = {"text": "⚡ Ouvrir l'offre & Postuler immédiatement", "url": listing_url}
    viewer_markup = {"inline_keyboard": [[open_button]]}
    owner_markup = {"inline_keyboard": [[open_button]]}
    if not is_sniper_target:
        owner_markup["inline_keyboard"].append([{
            "text": "🎯 Snipe",
            "callback_data": f"snipe:{tool_id}:{item_id}:{target_mode}"
        }])
    return owner_markup, viewer_markup


def check_and_notify() -> tuple[int, int]:
    """
    Core check cycle:
    1. Fetches listings from active CROUS tool(s).
    2. Identifies matching Marseille listings that just came online -- first time ever,
       or back after going offline (update_listing_visibility()). One alert per
       appearance: never repeated while a listing stays online.
    3. Triggers immediate auto-apply if the listing qualifies (is_target_listing()).
    4. Sends Telegram alert (with sniper screenshot if applied, or manual link otherwise).
    5. Updates listings_seen.json (seen_ids, active, history).
    6. Records posting-activity stats (first/last new listing seen today, nationwide
       and Marseille) to posting_activity.json, for eventually tuning smart-cadence
       "peak hours" against real data instead of an assumption.
    Returns (total_matching_in_marseille, new_alerts_sent).
    """
    state = load_state()
    seen_ids = set(str(i) for i in state.get("seen_ids", []))
    active = state["active"]
    history = state["history"]

    if proxy_manager:
        available_groups = proxy_manager.get_available_groups()
        if len(available_groups) == 1:
            logger.warning(
                f"Scouter operating with only ONE healthy proxy group remaining: {available_groups[0]}"
            )

    tool_ids, tool_ids_did_real_fetch = discover_tool_ids_with_fetch_flag()
    if tool_ids_did_real_fetch:
        # Natural delay between homepage check and search requests (random 2 to 3
        # seconds) -- only applies when discover_tool_ids() actually just hit the
        # homepage (cache miss, at most once per hour). On a cache hit (the vast
        # majority of cycles) there was no real homepage request to space out from,
        # so skip the delay entirely rather than pad every single cycle for nothing.
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

    # Nationwide (any city) new-listing detection, for posting-activity tracking --
    # separate from the Marseille-only seen_ids/alerting logic below.
    nationwide_seen = load_nationwide_seen_ids()
    nationwide_new_ids = _detect_and_record_nationwide_new(all_raw_items, nationwide_seen)
    if nationwide_new_ids:
        save_nationwide_seen_ids(nationwide_seen | nationwide_new_ids)

    matching_listings = []
    new_alerts_sent = 0

    # Which matching listings are online right now -> which just APPEARED (alert) and
    # which just went offline (record). See update_listing_visibility().
    present = {}
    for item in all_raw_items:
        matches, info = is_target_listing(item)
        if matches and info["id"] not in present:
            present[info["id"]] = {
                "residence_name": info.get("residence_name"),
                "label": info.get("label"),
                "price": info.get("price"),
            }
    now_iso = datetime.now(timezone.utc).isoformat()
    appeared, gone = update_listing_visibility(
        active, present, fetch_complete=not errors_encountered, now_iso=now_iso
    )
    for gone_id, gone_entry in gone:
        history.setdefault(gone_id, {})["last_gone_at"] = gone_entry.get("last_seen")
        record_marseille_listing_gone(gone_id, gone_entry, now_iso)
        logger.info(
            f"👋 Listing #{gone_id} ({gone_entry.get('residence_name')}) went offline "
            f"(seen in {gone_entry.get('checks')} check(s) since {gone_entry.get('since')})"
        )
    to_alert = set(appeared)

    for item in all_raw_items:
        matches, info = is_target_listing(item)
        if matches:
            matching_listings.append(info)
            item_id = info["id"]
            tool_id = item.get("_tool_id", "47")

            if item_id in to_alert:
                # LISTING JUST CAME ONLINE -- first time ever, or back after going offline.
                to_alert.discard(item_id)  # once per appearance, even if the API repeats an item
                hist = history.setdefault(item_id, {})
                count_known = "appearances" in hist
                reappearance = count_known or item_id in seen_ids
                hist["appearances"] = hist.get("appearances", 1 if item_id in seen_ids else 0) + 1
                appearance_no = hist["appearances"] if (count_known or not reappearance) else None

                mode_name = "Colocation" if info["is_coloc"] else "Individuel"
                is_sniper_target = info["should_auto_apply"] and bool(apply_for_accommodation)
                logger.info(
                    f"✨ {'LISTING BACK ONLINE' if reappearance else 'NEW LISTING'}: {info['residence_name']} "
                    f"({info['price']}€) | appearance #{appearance_no or '?'} | Sniper Target: {is_sniper_target}"
                )
                listing_url = f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/accommodations/{item_id}"
                record_marseille_listing(info, tool_id, listing_url,
                                         appearance_no=appearance_no, reappearance=reappearance)
                coloc_tag = " [Colocation]" if info["is_coloc"] else ""
                reappearance_line = (
                    _format_reappearance_line(appearance_no, hist.get("last_gone_at"), now_iso) + "\n"
                    if reappearance else ""
                )

                # 1. SEND DIRECT LINK IMMEDIATELY so the user can apply manually without delay
                logger.info(f"⚡ [IMMEDIATE ALERT] Sending listing #{item_id} link to Telegram first...")
                alert_text = (
                    ("🔁 *OFFRE CROUS DE NOUVEAU DISPONIBLE !*\n\n" if reappearance
                     else "🚨 *NOUVELLE OFFRE CROUS TROUVÉE !*\n\n")
                    + reappearance_line +
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
                owner_markup, viewer_markup = build_alert_markups(
                    listing_url, is_sniper_target, tool_id, item_id, info.get("target_mode", "single")
                )
                send_telegram_message(alert_text, reply_markup=owner_markup)
                for viewer_cid in get_viewer_chat_ids():
                    send_telegram_message(alert_text, reply_markup=viewer_markup, chat_id=viewer_cid)

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
    state["active"] = active
    state["history"] = history
    state["consecutive_failures"] = 0
    save_state(state)

    record_posting_activity(len(nationwide_new_ids), new_alerts_sent)

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

    last_heartbeat_day = None

    # Send startup announcement to Telegram
    is_logged_in, _ = is_session_valid()
    startup_msg = (
        "🚀 *CROUS Watcher Démarré sur votre VPS !*\n\n"
        f"🎯 *Ville :* {TARGET_CITY.capitalize()}\n"
        f"🤖 *Auto-Apply :* {'Activé' if AUTO_APPLY_ENABLED else 'Désactivé'}\n"
        f"{_format_sniper_rules_text()}\n"
        f"⏱️ *Cadence actuelle :* ~{cur_interval}s ({cur_cadence})\n"
        f"🔐 *Session CROUS :* {'Active ✅' if is_logged_in else 'Non configurée / Expirée ❌'}\n\n"
        "Je surveille en continu 24h/24. Envoyez `/status` pour voir les métriques ou `/check` pour vérifier."
    )
    send_telegram_message(startup_msg)

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
            failures = _record_cycle_failure()
            append_run_history("EXHAUSTED", f"{err} | Consecutive: {failures}")
            if activity_logger:
                activity_logger.notify_general_error(
                    "Tous les groupes de proxys sont épuisés (402 Payment Required). Aucun proxy sain disponible.",
                    component_name="Scouter"
                )
            if failures == 5:
                send_telegram_message(
                    f"🚨 *Alerte Watcher : 5 échecs consécutifs*\n\n"
                    f"Dernière erreur : `Tous les groupes de proxys sont épuisés (402)`\n"
                    "Vérifiez vos comptes de proxys sur votre VPS."
                )

        except CrousBlockedOrRateLimitedError as err:
            is_blocked_error = True
            logger.exception(f"Rate limited or blocked: {err}")
            failures = _record_cycle_failure()
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
            failures = _record_cycle_failure()
            append_run_history("FAILURE", f"{err} | Consecutive: {failures}")

            # Alert after 5 consecutive failures
            if failures == 5:
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
