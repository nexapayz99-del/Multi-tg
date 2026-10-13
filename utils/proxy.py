"""utils/proxy.py — round-robin SOCKS5 proxy provider for Pyrogram."""
import random
import logging
from urllib.parse import urlparse
from config import PROXY_LIST

logger = logging.getLogger(__name__)

# Round-robin index — deterministic distribution across proxies
_rr_index = 0

# Cache parsed proxies so we don't re-parse each time
_parsed_cache = []


def _parse(raw: str):
    """Parse a socks5://host:port[?user:pass] URL into a Pyrogram proxy dict."""
    try:
        u = urlparse(raw if "://" in raw else "socks5://" + raw)
        scheme = u.scheme or "socks5"
        if scheme not in ("socks5", "socks4", "http"):
            scheme = "socks5"
        host = u.hostname
        port = u.port
        if not host or not port:
            return None
        proxy = {
            "scheme": scheme,
            "hostname": host,
            "port": int(port),
        }
        if u.username:
            proxy["username"] = u.username
        if u.password:
            proxy["password"] = u.password
        return proxy
    except Exception as e:
        logger.debug(f"proxy parse error {raw}: {e}")
        return None


# Pre-parse once at import
for raw in PROXY_LIST:
    p = _parse(raw)
    if p:
        _parsed_cache.append(p)

if _parsed_cache:
    logger.info(f"✅ Loaded {len(_parsed_cache)} proxies")
else:
    logger.warning("⚠️ No proxies configured — using direct connection")


def get_proxy(seed: str = None):
    """
    Return a proxy dict for Pyrogram Client(proxy=...).

    If `seed` (like a phone number) is provided, the SAME seed always
    maps to the SAME proxy — so a single account keeps a stable IP.

    If seed is None, rotates round-robin.
    """
    global _rr_index
    if not _parsed_cache:
        return None

    if seed is not None:
        # Deterministic hash-based mapping: same seed → same proxy
        idx = hash(seed) % len(_parsed_cache)
        return _parsed_cache[idx]

    # Round-robin for seedless calls
    proxy = _parsed_cache[_rr_index % len(_parsed_cache)]
    _rr_index += 1
    return proxy


def proxy_count():
    return len(_parsed_cache)