"""Twitter/X provider (spec §3.6).

Primary: the public syndication endpoint (no auth), token formula copied from yt-dlp
(see DECISIONS.md §8.1). Fallback for protected tweets: yt-dlp with COOKIES_PATH.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

import httpx

from .. import convert
from ..config import settings
from ..errors import ExtractFailed, NoMedia, NotFound, Private, Timeout
from ..models import MediaSource, ResolvedPost
from ..urlnorm import BROWSER_UA, NormalizedUrl, canonicalize
from ..ytdlp_util import extract_info

log = logging.getLogger("media-saver.twitter")

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
GIF_MAX_DURATION_S = 30  # yt-dlp fallback heuristic: silent + short => gif

# --- token ----------------------------------------------------------------------


def _js_number_to_string(val: float, radix: int) -> str:
    """Port of JS Number.prototype.toString(radix); mirrors yt_dlp.jsinterp.js_number_to_string
    (yt-dlp is Unlicense) so the token still works if that helper moves."""
    import collections

    if val == 0:
        return "0"
    alphabet = b"0123456789abcdefghijklmnopqrstuvwxyz.-"
    result: collections.deque[int] = collections.deque()
    sign = val < 0
    val = abs(val)
    fraction, integer = math.modf(val)
    delta = max(math.nextafter(0.0, math.inf), math.ulp(val) / 2)
    if fraction >= delta:
        result.append(-2)  # '.'
    while fraction >= delta:
        delta *= radix
        fraction, digit = math.modf(fraction * radix)
        result.append(int(digit))
        needs_rounding = fraction > 0.5 or (fraction == 0.5 and int(digit) & 1)
        if needs_rounding and fraction + delta > 1:
            for index in reversed(range(1, len(result))):
                if result[index] + 1 < radix:
                    result[index] += 1
                    break
                result.pop()
            else:
                integer += 1
            break
    integer, digit = divmod(int(integer), radix)
    result.appendleft(digit)
    while integer > 0:
        integer, digit = divmod(integer, radix)
        result.appendleft(digit)
    if sign:
        result.appendleft(-1)
    return bytes(alphabet[d] for d in result).decode("ascii")


def syndication_token(twid: str) -> str:
    """((Number(twid) / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, '')"""
    val = (int(twid) / 1e15) * math.pi
    try:
        from yt_dlp.jsinterp import js_number_to_string  # type: ignore

        s = js_number_to_string(val, 36)
    except Exception:  # pragma: no cover
        s = _js_number_to_string(val, 36)
    return s.translate(str.maketrans(dict.fromkeys("0.")))


# --- fetching -------------------------------------------------------------------

Fetcher = Callable[[str], Awaitable[Optional[dict[str, Any]]]]


async def fetch_syndication(twid: str, client: httpx.AsyncClient | None = None) -> Optional[dict[str, Any]]:
    """Return the tweet JSON, `None` for an empty body. Raises Timeout/ExtractFailed."""
    params = {"id": twid, "token": syndication_token(twid)}
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    try:
        last_exc: Exception | None = None
        for ua in ("Googlebot", BROWSER_UA):
            try:
                r = await client.get(SYNDICATION_URL, params=params, headers={"User-Agent": ua, "Accept": "application/json"})
            except httpx.TimeoutException as e:
                raise Timeout("Twitter took too long to respond.") from e
            except httpx.HTTPError as e:
                last_exc = e
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                last_exc = ExtractFailed(f"Twitter returned HTTP {r.status_code}.")
                continue
            if not r.content.strip():
                return None
            try:
                return r.json()
            except ValueError:
                last_exc = ExtractFailed("Twitter returned a non-JSON response.")
                continue
        raise last_exc if isinstance(last_exc, ExtractFailed) else ExtractFailed("Could not reach Twitter.")
    finally:
        if own:
            await client.aclose()


# --- parsing --------------------------------------------------------------------


def _ext_from_url(url: str, default: str = "jpg") -> str:
    path = urlsplit(url).path
    if "." in path.rsplit("/", 1)[-1]:
        ext = path.rsplit(".", 1)[-1].lower()
        if ext in {"jpg", "jpeg", "png", "webp", "gif", "mp4"}:
            return "jpg" if ext == "jpeg" else ext
    return default


def _best_mp4(video_info: dict[str, Any]) -> Optional[dict[str, Any]]:
    variants = [v for v in (video_info or {}).get("variants") or [] if v.get("content_type") == "video/mp4" and v.get("url")]
    if not variants:
        return None
    return max(variants, key=lambda v: v.get("bitrate") or 0)


def _dims(m: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    oi = m.get("original_info") or {}
    w, h = oi.get("width"), oi.get("height")
    if not (w and h):
        sz = (m.get("sizes") or {}).get("large") or {}
        w, h = sz.get("w"), sz.get("h")
    return (int(w) if w else None, int(h) if h else None)


def _tombstone_text(data: dict[str, Any]) -> str:
    ts = data.get("tombstone") or {}
    return " ".join(str(x) for x in ((ts.get("text") or {}).get("text"), ts.get("text") if isinstance(ts.get("text"), str) else None) if x).lower()


class TwitterProvider:
    name = "twitter"

    def __init__(self, fetcher: Fetcher | None = None, converter=None):
        self._fetch = fetcher or fetch_syndication
        self._convert = converter or convert.convert_mp4_to_gif

    def matches(self, url: str) -> bool:
        try:
            return canonicalize(url).provider == "twitter"
        except Exception:
            return False

    async def resolve(self, target: NormalizedUrl, *, gif_keep_mp4: bool) -> ResolvedPost:
        twid = target.post_id
        data = await self._fetch(twid)
        if not data:
            return await self._fallback_or(NotFound(), target, gif_keep_mp4)
        if data.get("__typename") == "TweetTombstone" or data.get("tombstone"):
            text = _tombstone_text(data)
            if re.search(r"protected|private|limited who can view|not authorized", text):
                return await self._fallback_or(Private(), target, gif_keep_mp4)
            if re.search(r"age|sensitive|adult", text):
                return await self._fallback_or(Private("This post is age-restricted; login required."), target, gif_keep_mp4)
            return await self._fallback_or(NotFound(), target, gif_keep_mp4)
        return await self._from_syndication(data, twid, gif_keep_mp4)

    async def _from_syndication(self, data: dict[str, Any], twid: str, gif_keep_mp4: bool) -> ResolvedPost:
        user = data.get("user") or {}
        author = user.get("screen_name") or "unknown"
        text = (data.get("text") or "").replace("\n", " ").strip()
        # Only the shared tweet's own media; quoted tweet media intentionally ignored (spec §3.6).
        # include_quoted = False  # option for a later version: also walk data["quoted_tweet"]["mediaDetails"]
        media = [m for m in data.get("mediaDetails") or [] if isinstance(m, dict)]
        if not media:
            raise NoMedia()
        base = f"twitter_{author}_{twid}"
        sources: list[MediaSource] = []
        for n, m in enumerate(media, start=1):
            kind = m.get("type")
            w, h = _dims(m)
            if kind == "photo":
                url = m.get("media_url_https") or m.get("media_url")
                if not url:
                    continue
                ext = _ext_from_url(url)
                sources.append(MediaSource(kind="photo", filename=f"{base}_{n}.{ext}", mime=f"image/{'jpeg' if ext == 'jpg' else ext}", upstream_url=f"{url}?format={ext}&name=orig", width=w, height=h))
            elif kind == "video":
                v = _best_mp4(m.get("video_info") or {})
                if not v:
                    continue
                sources.append(MediaSource(kind="video", filename=f"{base}_{n}.mp4", mime="video/mp4", upstream_url=v["url"], width=w, height=h))
            elif kind == "animated_gif":
                v = _best_mp4(m.get("video_info") or {})
                if not v:
                    continue
                sources.append(MediaSource(kind="gif", filename=f"{base}_{n}.gif", mime="image/gif", upstream_url=v["url"], width=w, height=h))
                if gif_keep_mp4:
                    sources.append(MediaSource(kind="video", filename=f"{base}_{n}.mp4", mime="video/mp4", upstream_url=v["url"], width=w, height=h))
        if not sources:
            raise NoMedia()
        sources = await self._finalize_gifs(sources)
        return ResolvedPost(provider="twitter", post_id=twid, author=author, text=text, sources=sources)

    async def _finalize_gifs(self, sources: list[MediaSource]) -> list[MediaSource]:
        """Convert every gif source (mp4 upstream) to a local .gif; runs conversions concurrently."""
        idx = [i for i, s in enumerate(sources) if s.kind == "gif" and s.upstream_url and not s.local_path]
        if not idx:
            return sources
        paths = await asyncio.gather(*(self._convert(sources[i].upstream_url, sources[i].headers) for i in idx))
        out = list(sources)
        for i, path in zip(idx, paths):
            p = str(path)
            out[i] = sources[i].model_copy(update={"local_path": p, "upstream_url": None, "headers": {}, "bytes": _size(p)})
        return out

    async def _fallback_or(self, err: Exception, target: NormalizedUrl, gif_keep_mp4: bool) -> ResolvedPost:
        cfg = settings()
        if not cfg.cookies_path:
            raise err
        log.info("twitter %s: syndication gave %s; trying yt-dlp with cookies", target.post_id, type(err).__name__)
        try:
            info = await extract_info(target.canonical_url)
        except Exception as e:
            raise (e if isinstance(e, (Private, NotFound)) else err) from e
        post = self._from_ytdlp(info, target.post_id, gif_keep_mp4)
        return post.model_copy(update={"sources": await self._finalize_gifs(post.sources)})

    def _from_ytdlp(self, info: dict[str, Any], twid: str, gif_keep_mp4: bool) -> ResolvedPost:
        entries = info.get("entries") if info.get("_type") == "playlist" else [info]
        entries = [e for e in entries or [] if e]
        author = info.get("uploader_id") or (entries[0].get("uploader_id") if entries else None) or "unknown"
        text = (info.get("description") or info.get("title") or "").replace("\n", " ").strip()
        base = f"twitter_{author}_{twid}"
        sources: list[MediaSource] = []
        n = 0
        for e in entries:
            fmts = [f for f in e.get("formats") or [] if f.get("url") and f.get("protocol", "https") in ("https", "http")]
            n += 1
            if not fmts:
                url = e.get("url")
                if url and e.get("ext") in ("jpg", "png", "webp"):
                    ext = e["ext"]
                    sources.append(MediaSource(kind="photo", filename=f"{base}_{n}.{ext}", mime=f"image/{'jpeg' if ext == 'jpg' else ext}", upstream_url=url, width=e.get("width"), height=e.get("height")))
                continue
            mp4 = [f for f in fmts if f.get("ext") == "mp4" and f.get("vcodec") not in (None, "none")]
            if not mp4:
                continue
            best = max(mp4, key=lambda f: f.get("tbr") or f.get("filesize") or 0)
            silent = all(f.get("acodec") in (None, "none") for f in mp4)
            duration = e.get("duration") or 0
            is_gif = silent and 0 < duration <= GIF_MAX_DURATION_S
            hdrs = dict(best.get("http_headers") or e.get("http_headers") or {})
            if is_gif:
                sources.append(MediaSource(kind="gif", filename=f"{base}_{n}.gif", mime="image/gif", upstream_url=best["url"], headers=hdrs, width=best.get("width"), height=best.get("height")))
                if gif_keep_mp4:
                    sources.append(MediaSource(kind="video", filename=f"{base}_{n}.mp4", mime="video/mp4", upstream_url=best["url"], headers=hdrs, width=best.get("width"), height=best.get("height")))
            else:
                sources.append(MediaSource(kind="video", filename=f"{base}_{n}.mp4", mime="video/mp4", upstream_url=best["url"], headers=hdrs, bytes=best.get("filesize"), width=best.get("width"), height=best.get("height")))
        if not sources:
            raise NoMedia()
        return ResolvedPost(provider="twitter", post_id=twid, author=author, text=text, sources=sources)


def _size(path: str) -> Optional[int]:
    try:
        import os

        return os.path.getsize(path)
    except OSError:
        return None
