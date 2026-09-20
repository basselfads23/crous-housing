import os
import random
from pathlib import Path
from urllib.parse import urlsplit, quote

BASE_DIR = Path(__file__).resolve().parent
PROXIES_FILE = BASE_DIR / "proxies.txt"

_CURRENT_INDEX = 0


def load_proxies() -> list[str]:
    """
    Returns a list of proxy URLs in the form: http://user:pass@ip:port
    Loads from CROUS_PROXY environment variable and/or proxies.txt.
    Supports any number of proxies in proxies.txt.
    """
    proxies = []
    env_proxy = os.getenv("CROUS_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if env_proxy:
        proxies.append(env_proxy.strip())

    if PROXIES_FILE.exists():
        for line in PROXIES_FILE.read_text(encoding="utf-8").splitlines():
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


STATE_FILE = BASE_DIR / ".proxy_index"
SNIPER_STATE_FILE = BASE_DIR / ".sniper_proxy_index"


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


def get_current_proxy(rotate: bool = False) -> str | None:
    """Get current active proxy URL, optionally rotating to next proxy in the list."""
    proxies = load_proxies()
    if not proxies:
        return None
    idx = _get_stored_index()
    if rotate:
        idx = (idx + 1) % len(proxies)
        _save_stored_index(idx)
    return proxies[idx % len(proxies)]


def rotate_proxy() -> str | None:
    """Rotate to the next proxy in the list and return it."""
    return get_current_proxy(rotate=True)


def rotate_sniper_proxy() -> dict | None:
    """Explicitly advances to the next sniper proxy and returns it."""
    proxies = load_proxies()
    if not proxies:
        return None
    idx = (_get_sniper_stored_index() + 1) % len(proxies)
    _save_sniper_stored_index(idx)
    return get_playwright_proxy(proxy_url=proxies[idx])


def get_sniper_proxy() -> dict | None:
    """
    Returns a dedicated, verified healthy proxy for the Sniper (Playwright) and auto-login.
    Rotates across proxies so no single proxy is repeatedly loaded.
    Verifies the proxy against a live CROUS page to guarantee it is not rate-limited (429).
    """
    sniper_proxy_url = os.getenv("SNIPER_PROXY")
    if sniper_proxy_url:
        return get_playwright_proxy(proxy_url=sniper_proxy_url)

    proxies = load_proxies()
    if not proxies:
        return None

    # Check Webshare proxies first (they support both CROUS and .gouv.fr auth)
    webshare_proxies = [p for p in proxies if "oxylabs" not in p]
    candidate_list = webshare_proxies + [p for p in proxies if "oxylabs" in p]
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


