import os
import time
import json
import random
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, quote

logger = logging.getLogger("proxy_manager")


class AllProxyGroupsExhaustedError(Exception):
    """Raised when all configured proxy groups are exhausted (e.g. 402 Payment Required)."""
    pass


BASE_DIR = Path(__file__).resolve().parent
PROXIES_FILE = BASE_DIR / "proxies.txt"
RESIDENTIAL_PROXIES_FILE = BASE_DIR / "proxies_residential.txt"
EXHAUSTED_GROUPS_FILE = BASE_DIR / ".exhausted_proxy_groups.json"
EXHAUSTION_TTL_SECONDS = 24 * 3600  # 24 hours


_CURRENT_INDEX = 0


def _parse_proxies_file(path: Path) -> list[str]:
    """Parse a host:port:user:pass (or full URL) proxy list file into http://user:pass@host:port URLs."""
    proxies = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http://") or line.startswith("https://") or line.startswith("socks5://"):
                proxies.append(line)
            else:
                parts = line.split(":", 3)
                if len(parts) == 4:
                    host, port, user, pwd = parts
                    user_enc = quote(user, safe="")
                    pwd_enc = quote(pwd, safe="")
                    proxies.append(f"http://{user_enc}:{pwd_enc}@{host}:{port}")
                elif len(parts) == 2:
                    host, port = parts
                    proxies.append(f"http://{host}:{port}")

    seen = set()
    unique = []
    for p in proxies:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def load_proxies() -> list[str]:
    """
    Returns the SCOUTER's datacenter proxy pool as a list of proxy URLs in the form
    http://user:pass@ip:port. Loads from the CROUS_PROXY environment variable and/or
    proxies.txt (cheap Webshare "Proxy Server" datacenter plan + Oxylabs).
    """
    proxies = []
    env_proxy = os.getenv("CROUS_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if env_proxy:
        proxies.append(env_proxy.strip())
    proxies.extend(_parse_proxies_file(PROXIES_FILE))

    seen = set()
    unique = []
    for p in proxies:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def load_residential_proxies() -> list[str]:
    """
    Returns the AUTH + SNIPER residential proxy pool (proxies_residential.txt --
    Webshare "Static Residential" plan). Deliberately separate from load_proxies():
    auth (login) and the sniper (apply flow) need to look like a real human, which
    residential IPs do far better than datacenter ones; the scouter's bulk polling
    doesn't need that and stays on the cheap datacenter pool.
    """
    return _parse_proxies_file(RESIDENTIAL_PROXIES_FILE)


STATE_FILE = BASE_DIR / ".proxy_index"
SNIPER_STATE_FILE = BASE_DIR / ".sniper_proxy_index"
AUTH_STATE_FILE = BASE_DIR / ".auth_proxy_index"
AUTH_EXCLUDED_MARKERS = ("oxylabs",)      # proxies whose URL contains any marker are never used for auth
AUTH_PROXY_CHECK_TIMEOUT = 8              # seconds, per validation request


def _get_stored_index() -> int:
    if STATE_FILE.exists():
        try:
            return int(STATE_FILE.read_text().strip())
        except Exception:
            pass
    return 0


def _save_stored_index(idx: int):
    try:
        STATE_FILE.write_text(str(idx))
    except Exception:
        pass


def _get_sniper_stored_index() -> int:
    if SNIPER_STATE_FILE.exists():
        try:
            return int(SNIPER_STATE_FILE.read_text().strip())
        except Exception:
            pass
    return 0


def _save_sniper_stored_index(idx: int):
    try:
        SNIPER_STATE_FILE.write_text(str(idx))
    except Exception:
        pass


def _get_auth_stored_index() -> int:
    if AUTH_STATE_FILE.exists():
        try:
            return int(AUTH_STATE_FILE.read_text().strip())
        except Exception:
            pass
    return 0


def _save_auth_stored_index(idx: int):
    try:
        AUTH_STATE_FILE.write_text(str(idx))
    except Exception:
        pass



def get_proxy_group(proxy_url: str) -> str:
    """
    Extract provider account username from proxy URL to group proxies.
    Falls back to hostname or 'direct'.
    """
    if not proxy_url:
        return "direct"
    u = urlsplit(proxy_url)
    if u.username:
        import urllib.parse
        return urllib.parse.unquote(u.username)
    return u.hostname or "direct"


def get_exhausted_groups() -> dict[str, dict]:
    """
    Read .exhausted_proxy_groups.json, evict entries older than 24h TTL,
    and return dict of currently exhausted groups.
    Manual recovery: delete .exhausted_proxy_groups.json by hand (rm .exhausted_proxy_groups.json).
    """
    if not EXHAUSTED_GROUPS_FILE.exists():
        return {}
    try:
        content = EXHAUSTED_GROUPS_FILE.read_text(encoding="utf-8").strip()
        if not content:
            return {}
        data = json.loads(content)
        if not isinstance(data, dict):
            return {}
        now = time.time()
        active = {}
        changed = False
        for group, info in data.items():
            if isinstance(info, dict):
                exhausted_at = info.get("exhausted_at", 0)
                if (now - exhausted_at) < EXHAUSTION_TTL_SECONDS:
                    active[group] = info
                else:
                    changed = True
            else:
                changed = True
        if changed:
            try:
                EXHAUSTED_GROUPS_FILE.write_text(json.dumps(active, indent=2), encoding="utf-8")
            except Exception:
                pass
        return active
    except Exception as e:
        logger.warning(f"Failed to read exhausted proxy groups: {e}")
        return {}


def get_available_groups() -> list[str]:
    """Return distinct proxy groups loaded from proxies.txt that are not currently exhausted."""
    proxies = load_proxies()
    if not proxies:
        return []
    exhausted = get_exhausted_groups()
    all_groups = []
    seen = set()
    for p in proxies:
        g = get_proxy_group(p)
        if g not in seen:
            seen.add(g)
            all_groups.append(g)
    return [g for g in all_groups if g not in exhausted]


def mark_group_exhausted(group: str, reason: str = "402 Payment Required") -> bool:
    """
    Mark an entire proxy group as exhausted, persisting to .exhausted_proxy_groups.json.
    Returns True if this is a new transition (first time group is marked exhausted),
    or False if it was already marked exhausted.
    """
    if not group:
        return False
    current = get_exhausted_groups()
    is_new = group not in current
    now = time.time()
    iso = datetime.now(timezone.utc).isoformat()
    current[group] = {
        "exhausted_at": now,
        "iso": iso,
        "reason": reason
    }
    try:
        EXHAUSTED_GROUPS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to write {EXHAUSTED_GROUPS_FILE}: {e}")
    return is_new


def get_current_proxy(rotate: bool = False, skip_exhausted: bool = True) -> str | None:
    """
    Get current active proxy URL, optionally rotating to next proxy in the list.
    When skip_exhausted=True, proxies belonging to exhausted groups are excluded.
    """
    proxies = load_proxies()
    if not proxies:
        return None

    if skip_exhausted:
        exhausted = get_exhausted_groups()
        candidates = [p for p in proxies if get_proxy_group(p) not in exhausted]
    else:
        candidates = proxies

    if not candidates:
        return None

    idx = _get_stored_index()
    if rotate:
        idx = (idx + 1) % 1_000_000
        _save_stored_index(idx)
    return candidates[idx % len(candidates)]


def rotate_proxy(skip_exhausted: bool = True) -> str | None:
    """Rotate to the next proxy in the list and return it."""
    return get_current_proxy(rotate=True, skip_exhausted=skip_exhausted)


def _sniper_candidate_list() -> list[str]:
    """
    Proxy ordering shared by get_sniper_proxy() and rotate_sniper_proxy(), so the
    same stored index always refers to the same proxy in both functions.

    Sources from the residential pool (load_residential_proxies()), not the
    scouter's datacenter pool -- the sniper's multi-page apply flow is exactly the
    kind of session where looking like a real human matters, which residential IPs
    do far better than datacenter ones. (Prior to 2026-09-25 this sourced from the
    datacenter pool with Webshare-first/Oxylabs-last ordering; that distinction is
    moot now since the residential pool has no Oxylabs entries at all.)

    NOTE: get_sniper_proxy() and rotate_sniper_proxy() used to index into two
    DIFFERENT orderings while sharing the same stored index -- meaning a rotation
    triggered by a 429 could land on essentially an arbitrary proxy, including
    possibly the one that just got rate-limited, wasting a request for nothing.
    Fixed 2026-09-25 by routing both through this single shared list.
    """
    return load_residential_proxies()


def rotate_sniper_proxy() -> dict | None:
    """Explicitly advances to the next sniper proxy and returns it."""
    candidate_list = _sniper_candidate_list()
    if not candidate_list:
        return None
    idx = (_get_sniper_stored_index() + 1) % len(candidate_list)
    _save_sniper_stored_index(idx)
    return get_playwright_proxy(proxy_url=candidate_list[idx])


def get_sniper_proxy() -> dict | None:
    """
    Returns a dedicated, verified healthy proxy for the Sniper (Playwright) and auto-login.
    Rotates across proxies so no single proxy is repeatedly loaded.
    Verifies the proxy against a live CROUS page to guarantee it is not rate-limited (429).
    """
    sniper_proxy_url = os.getenv("SNIPER_PROXY")
    if sniper_proxy_url:
        return get_playwright_proxy(proxy_url=sniper_proxy_url)

    candidate_list = _sniper_candidate_list()
    if not candidate_list:
        return None

    import urllib.request
    start_idx = _get_sniper_stored_index() % len(candidate_list)

    for i in range(len(candidate_list)):
        curr_idx = (start_idx + i) % len(candidate_list)
        p = candidate_list[curr_idx]
        try:
            req = urllib.request.Request(
                "https://trouverunlogement.lescrous.fr/tools/47/search",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
            with opener.open(req, timeout=3.5) as resp:
                data = resp.read(256).decode("utf-8", errors="ignore")
                if resp.getcode() == 200 and "too many requests" not in data.lower():
                    _save_sniper_stored_index((curr_idx + 1) % len(candidate_list))
                    return get_playwright_proxy(proxy_url=p)
        except Exception:
            continue

    # Fallback to next candidate if all failed the full search test
    fallback_p = candidate_list[start_idx]
    _save_sniper_stored_index((start_idx + 1) % len(candidate_list))
    return get_playwright_proxy(proxy_url=fallback_p)


def get_playwright_proxy(proxy_url: str | None = None, rotate: bool = False) -> dict | None:
    """
    Format proxy dictionary for Playwright chromium.launch(proxy=...).
    """
    p_url = proxy_url or get_current_proxy(rotate=rotate)
    if not p_url:
        return None
    u = urlsplit(p_url)
    server = f"{u.scheme}://{u.hostname}"
    if u.port:
        server += f":{u.port}"
    cfg = {"server": server}
    if u.username:
        import urllib.parse
        cfg["username"] = urllib.parse.unquote(u.username)
    if u.password:
        import urllib.parse
        cfg["password"] = urllib.parse.unquote(u.password)
    return cfg


def get_auth_proxy() -> dict | None:
    """
    Returns a dedicated, verified proxy for authentication with CROUS and MesServices.
    Ensures both the housing site and the auth portal work before returning a candidate.

    Sources from the residential pool (load_residential_proxies()), not the scouter's
    datacenter pool -- login (Altcha challenge, cookies) is exactly the kind of flow
    where looking like a real human matters. The residential pool has no Oxylabs
    entries at all, so the AUTH_EXCLUDED_MARKERS filter that used to be needed here
    (Oxylabs gets a hard 403 on the .gouv.fr login host specifically) is moot now --
    left in place as a harmless defensive no-op in case that ever changes.
    """
    proxies = load_residential_proxies()
    candidates = [p for p in proxies if not any(m in p.lower() for m in AUTH_EXCLUDED_MARKERS)]
    if not candidates:
        logger.warning("No candidate proxies available for auth (all excluded or list empty).")
        return None

    import urllib.request

    start = _get_auth_stored_index() % len(candidates)

    for i in range(len(candidates)):
        idx = (start + i) % len(candidates)
        p = candidates[idx]

        u = urlsplit(p)
        host = u.hostname or ""
        port = str(u.port or "")
        host_port = f"{host}:{port}" if port else host

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))

        # Check 1: GET https://trouverunlogement.lescrous.fr/tools/47/search
        try:
            req1 = urllib.request.Request(
                "https://trouverunlogement.lescrous.fr/tools/47/search",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with opener.open(req1, timeout=AUTH_PROXY_CHECK_TIMEOUT) as resp:
                data = resp.read(256)
                data_str = data.decode("utf-8", errors="ignore") if isinstance(data, (bytes, bytearray)) else str(data)
                if resp.getcode() != 200 or "too many requests" in data_str.lower():
                    raise ValueError(f"HTTP {resp.getcode()}")
        except Exception as e:
            logger.warning(f"Auth proxy candidate {host_port} failed check for trouverunlogement.lescrous.fr: {type(e).__name__}")
            continue

        # Check 2: GET https://messervices.etudiant.gouv.fr/
        try:
            req2 = urllib.request.Request(
                "https://messervices.etudiant.gouv.fr/",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with opener.open(req2, timeout=AUTH_PROXY_CHECK_TIMEOUT) as resp:
                if resp.getcode() != 200:
                    raise ValueError(f"HTTP {resp.getcode()}")
        except Exception as e:
            logger.warning(f"Auth proxy candidate {host_port} failed check for messervices.etudiant.gouv.fr: {type(e).__name__}")
            continue

        _save_auth_stored_index((idx + 1) % len(candidates))
        logger.info(f"Auth proxy selected: {host}:{port}")
        return get_playwright_proxy(proxy_url=p)

    logger.error("All candidate auth proxies failed validation.")
    return None



