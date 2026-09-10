"""URL extraction, shortener expansion, and provider detection (spec §3.5)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import ExtractFailed, Timeout, UnsupportedUrl

URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?)]}'\"…"

SHORTENER_HOSTS = {"t.co", "vm.tiktok.com", "vt.tiktok.com"}
TWITTER_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com", "www.twitter.com", "www.x.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com", "nitter.net"}
TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}

_TW_STATUS_RE = re.compile(r"^/(?:[A-Za-z0-9_]{1,20}|i/web|i)/status(?:es)?/(\d{1,25})(?:/|$)")
_TT_ID_RE = re.compile(r"^/(?:@[\w.\-]+/)?(?:video|photo|v)/(\d{1,25})(?:\.html)?(?:/|$)")
_TT_EMBED_RE = re.compile(r"^/embed(?:/v2)?/(\d{1,25})(?:/|$)")
_TT_SHORT_PATH_RE = re.compile(r"^/t/[A-Za-z0-9]+/?$")

MAX_HOPS = 5
EXPAND_TIMEOUT = 10.0
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Mobile Safari/537.36"
)


@dataclass(frozen=True)
class NormalizedUrl:
    provider: str  # "twitter" | "tiktok"
    post_id: str
    canonical_url: str


def extract_first_url(text: str) -> Optional[str]:
    """First http(s) URL in free text (share sheets prepend post text)."""
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0)
    # Strip trailing punctuation that share text tends to glue onto links.
    while url and url[-1] in _TRAILING_PUNCT:
        # keep a ')' if the URL has an unmatched '(' before it
        if url[-1] == ")" and url.count("(") > url.count(")") - 1:
            break
        url = url[:-1]
    return url


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_shortener(url: str) -> bool:
    host = _host(url)
    if host in SHORTENER_HOSTS:
        return True
    if host in TIKTOK_HOSTS and _TT_SHORT_PATH_RE.match(urlsplit(url).path or ""):
        return True
    return False


def canonicalize(url: str) -> NormalizedUrl:
    """Pure: map a (possibly already expanded) URL to provider + id + canonical URL."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if host in TWITTER_HOSTS or host.endswith(".twitter.com") or host.endswith(".x.com"):
        m = _TW_STATUS_RE.match(path)
        if not m:
            raise UnsupportedUrl("That Twitter/X link isn't a single post.")
        pid = m.group(1)
        return NormalizedUrl("twitter", pid, f"https://x.com/i/web/status/{pid}")
    if host in TIKTOK_HOSTS or host.endswith(".tiktok.com"):
        m = _TT_ID_RE.match(path) or _TT_EMBED_RE.match(path)
        if not m:
            if _TT_SHORT_PATH_RE.match(path) or host in SHORTENER_HOSTS:
                raise UnsupportedUrl("Short TikTok link could not be expanded.")
            raise UnsupportedUrl("That TikTok link isn't a single post.")
        pid = m.group(1)
        # Keep the @author segment when present: yt-dlp accepts both, but the
        # full form avoids a redirect and preserves photo/video distinction.
        seg = "photo" if "/photo/" in path else "video"
        author_m = re.match(r"^/(@[\w.\-]+)/", path)
        if author_m:
            return NormalizedUrl("tiktok", pid, f"https://www.tiktok.com/{author_m.group(1)}/{seg}/{pid}")
        return NormalizedUrl("tiktok", pid, f"https://www.tiktok.com/@_/{seg}/{pid}")
    raise UnsupportedUrl()


async def expand(url: str, client: httpx.AsyncClient | None = None) -> str:
    """Follow redirects for known shorteners only (max 5 hops, 10 s total)."""
    if not is_shortener(url):
        return url
    own = client is None
    if own:
        client = httpx.AsyncClient(follow_redirects=False, timeout=EXPAND_TIMEOUT, headers={"User-Agent": BROWSER_UA})
    assert client is not None
    try:
        current = url
        for _ in range(MAX_HOPS):
            try:
                r = await client.request("HEAD", current)
                if r.status_code in (403, 405) or (r.status_code < 300 and is_shortener(current)):
                    r = await client.request("GET", current, headers={"Range": "bytes=0-0"})
            except httpx.TimeoutException as e:
                raise Timeout("Link shortener took too long to respond.") from e
            except httpx.HTTPError as e:
                raise ExtractFailed("Could not expand the shared link.") from e
            loc = r.headers.get("location")
            if r.status_code in (301, 302, 303, 307, 308) and loc:
                current = str(httpx.URL(current).join(loc))
                # tiktok appends tracking params; strip query on the final canonical form
                if not is_shortener(current):
                    return _strip_query(current)
                continue
            return _strip_query(current)
        raise ExtractFailed("Too many redirects while expanding the link.")
    finally:
        if own:
            await client.aclose()


def _strip_query(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


async def normalize(text: str, client: httpx.AsyncClient | None = None) -> NormalizedUrl:
    """Full pipeline: extract → expand → canonicalize. Raises UnsupportedUrl."""
    url = extract_first_url(text)
    if not url:
        raise UnsupportedUrl("No link found in what was shared.")
    url = await expand(url, client)
    return canonicalize(url)
