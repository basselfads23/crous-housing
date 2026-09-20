#!/usr/bin/env python3
"""
Activity Logger & Error Dispatcher for CROUS Watcher & Sniper
=============================================================
Manages dedicated log files for scouter and sniper attempts:
- scouter.log: Every search attempt
- sniper.log: Every reservation/snipe attempt
Keeps logs cleanly rotated (capped at last 2000 lines).
Dispatches error messages and recent 10-line history to Telegram with
a 15-minute cooldown for repeated identical errors.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCOUTER_LOG = BASE_DIR / "scouter.log"
SNIPER_LOG = BASE_DIR / "sniper.log"
MAX_LOG_LINES = 2000

# Error cooldown cache: {error_signature: last_alert_timestamp}
_ERROR_COOLDOWN_CACHE = {}
COOLDOWN_SECONDS = 15 * 60  # 15 minutes


def _load_telegram_config() -> tuple[str, str]:
    """Retrieve Telegram credentials from environment or .env file."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip()
                            if k == "TELEGRAM_BOT_TOKEN":
                                token = v
                            elif k == "TELEGRAM_CHAT_ID":
                                chat_id = v
            except Exception:
                pass
    return token, chat_id


def sanitize_proxy(proxy_raw) -> str:
    """
    Remove user/password from proxy string, returning only host:port.
    Example: http://user:pass@1.2.3.4:8080 -> 1.2.3.4:8080
    """
    if not proxy_raw:
        return "Direct VPS"
    if isinstance(proxy_raw, dict):
        server = proxy_raw.get("server", "")
        if "@" in server:
            return server.split("@")[-1].replace("http://", "").replace("https://", "").strip("/")
        return server.replace("http://", "").replace("https://", "").strip("/")
    proxy_str = str(proxy_raw).strip()
    if "@" in proxy_str:
        return proxy_str.split("@")[-1].replace("http://", "").replace("https://", "").strip("/")
    return proxy_str.replace("http://", "").replace("https://", "").strip("/")


def _append_and_rotate_log(log_path: Path, new_line: str) -> None:
    """Append a line to log_path and keep only the latest MAX_LOG_LINES lines."""
    existing_lines = []
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                existing_lines = f.readlines()
        except Exception:
            existing_lines = []

    existing_lines.append(new_line if new_line.endswith("\n") else new_line + "\n")
    if len(existing_lines) > MAX_LOG_LINES:
        existing_lines = existing_lines[-MAX_LOG_LINES:]

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.writelines(existing_lines)
    except Exception as e:
        sys.stderr.write(f"[activity_logger] Failed to write {log_path}: {e}\n")


def get_last_lines(log_path: Path, count: int = 10) -> list[str]:
    """Return the last `count` lines from log_path."""
    if not log_path.exists():
        return []
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return [l.strip() for l in lines[-count:]]
    except Exception:
        return []


def _send_telegram_text(text: str) -> bool:
    """Send raw message to configured Telegram chat."""
    token, chat_id = _load_telegram_config()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("ok", False)
    except Exception as e:
        sys.stderr.write(f"[activity_logger] Telegram send failed: {e}\n")
        return False


def _check_cooldown(error_signature: str) -> bool:
    """
    Check if this error signature is on cooldown.
    Returns True if suppressed (within 15 minutes), False if allowed to send.
    """
    now = time.time()
    last_time = _ERROR_COOLDOWN_CACHE.get(error_signature, 0)
    if (now - last_time) < COOLDOWN_SECONDS:
        return True  # Suppress duplicate
    _ERROR_COOLDOWN_CACHE[error_signature] = now
    return False


def dispatch_error_with_history(component_name: str, error_message: str, log_path: Path) -> None:
    """
    When an error occurs:
    1. Check 15-minute cooldown for repeated identical errors.
    2. Message 1: The error message.
    3. Message 2: The last 10 log lines from the specified log file.
    """
    # Clean error signature for cooldown check
    sig = f"{component_name}:{error_message.strip()[:100]}"
    if _check_cooldown(sig):
        return

    # Message 1: The error description
    msg1 = (
        f"⚠️ *[ERREUR - {component_name.upper()}]*\n\n"
        f"• *Détail :* `{error_message}`\n\n"
        f"⏳ _Une pause de 15 minutes est appliquée pour éviter les alertes répétées pour cette même erreur._"
    )
    _send_telegram_text(msg1)

    # Message 2: The last 10 log lines
    last_10 = get_last_lines(log_path, count=10)
    if last_10:
        history_formatted = "\n".join(last_10)
        msg2 = (
            f"📋 *Derniers logs ({component_name}) :*\n"
            f"```text\n{history_formatted}\n```"
        )
    else:
        msg2 = f"📋 *Aucun historique précédent disponible dans {log_path.name}.*"

    time.sleep(0.5)  # slight pause between messages so they arrive sequentially
    _send_telegram_text(msg2)


def format_next_run(next_run_val) -> str:
    """Format next_run parameter nicely in seconds or minutes."""
    if not next_run_val:
        return "N/A"
    if isinstance(next_run_val, str):
        return next_run_val
    if isinstance(next_run_val, (int, float)):
        sec = round(float(next_run_val))
        if sec < 60:
            return f"{sec} seconds"
        mins = round(sec / 60)
        return f"{mins} minutes"
    return str(next_run_val)


def log_scouter_attempt(
    proxy_raw,
    target_url: str,
    success: bool,
    error_message: str = None,
    next_run: str = None
) -> None:
    """
    Log a scouter attempt in scouter.log and notify Telegram if failed.
    Format:
    Timestamp: [time] Proxy/IP Address: [proxy] Target Website: [url] Success:[✅ True / ❌ False] Next run: [next_run]
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    proxy_clean = sanitize_proxy(proxy_raw)
    status_str = "✅ True" if success else "❌ False"
    next_run_clean = format_next_run(next_run)
    log_line = (
        f"Timestamp: [{timestamp}] "
        f"Proxy/IP Address: [{proxy_clean}] "
        f"Target Website: [{target_url}] "
        f"Success:[{status_str}] "
        f"Next run: [{next_run_clean}]"
    )

    _append_and_rotate_log(SCOUTER_LOG, log_line)

    if not success and error_message:
        dispatch_error_with_history("Scouter", error_message, SCOUTER_LOG)


def log_sniper_attempt(
    proxy_raw,
    target_url: str,
    exit_page: str,
    success: bool,
    error_message: str = None,
    next_run: str = "On new listing"
) -> None:
    """
    Log a sniper attempt in sniper.log and notify Telegram if failed.
    Format:
    Timestamp: [time] Proxy/IP Address: [proxy] Target Website: [url] Exit Page: [exit_page] Success:[✅ True / ❌ False] Next run: [next_run]
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    proxy_clean = sanitize_proxy(proxy_raw)
    status_str = "✅ True" if success else "❌ False"
    exit_clean = exit_page or "N/A"
    next_run_clean = format_next_run(next_run)
    log_line = (
        f"Timestamp: [{timestamp}] "
        f"Proxy/IP Address: [{proxy_clean}] "
        f"Target Website: [{target_url}] "
        f"Exit Page: [{exit_clean}] "
        f"Success:[{status_str}] "
        f"Next run: [{next_run_clean}]"
    )

    _append_and_rotate_log(SNIPER_LOG, log_line)

    if not success and error_message:
        dispatch_error_with_history("Sniper", error_message, SNIPER_LOG)


def notify_general_error(error_message: str, component_name: str = "Watcher") -> None:
    """
    General error dispatcher for unexpected errors elsewhere in the bot.
    """
    sig = f"general:{component_name}:{str(error_message).strip()[:100]}"
    if _check_cooldown(sig):
        return

    msg1 = (
        f"🚨 *[ERREUR SYSTÈME - {component_name.upper()}]*\n\n"
        f"• *Détail :* `{error_message}`\n\n"
        "⏳ _Cooldown de 15 minutes actif pour cette erreur._"
    )
    _send_telegram_text(msg1)

    # Attach scouter log history as context
    last_10 = get_last_lines(SCOUTER_LOG, count=10)
    if last_10:
        history_formatted = "\n".join(last_10)
        msg2 = (
            f"📋 *Dernières requêtes du Scouter :*\n"
            f"```text\n{history_formatted}\n```"
        )
        time.sleep(0.5)
        _send_telegram_text(msg2)
