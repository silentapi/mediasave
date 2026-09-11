"""TikTok provider (spec §3.9).

Primary: fetch the post page directly with a browser User-Agent and read the
`__UNIVERSAL_DATA_FOR_REHYDRATION__` blob (works from a datacenter IP without
impersonation, see DECISIONS.md §8.2 / milestone 4). Video CDN URLs are bound to
the page session: the dl token carries `tt_chain_token`, the same User-Agent and a
tiktok.com Referer. Photo-mode posts are only rendered in the mobile ("reflow")
variant of the page. Fallback: yt-dlp (impersonation + WAF challenge solver).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from ..errors import ExtractFailed, NoMedia, NotFound, Private, Timeout
from ..models import MediaSource, ResolvedPost
from ..urlnorm import BROWSER_UA, NormalizedUrl, canonicalize
from ..ytdlp_util import extract_info

log = logging.getLogger("media-saver.tiktok")

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
MOBILE_UA = BROWSER_UA
REFERER = "https://www.tiktok.com/"
DETAIL_KEYS = ("webapp.video-detail", "webapp.reflow.video.detail")
_UNIVERSAL_RE = re.compile(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)

# statusCode values seen in yt-dlp's extractor
PRIVATE_STATUS = {10216, 10222}  # private post / private account
IP_BLOCKED_STATUS = {10204}


@dataclass
class Page:
    status: int | None  # None = no detail blob found
    item: dict[str, Any]
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = DESKTOP_UA
    challenge: bool = False


PageFetcher = Callable[[str, str], Awaitable[Page]]


def parse_page(html: str, cookies: dict[str, str], user_agent: str) -> Page:
    m = _UNIVERSAL_RE.search(html)
    if not m:
        return Page(None, {}, cookies, user_agent, challenge='id="cs"' in html or "Please wait..." in html)
    try:
        scope = json.loads(m.group(1)).get("__DEFAULT_SCOPE__") or {}
    except ValueError:
        return Page(None, {}, cookies, user_agent)
    for key in DETAIL_KEYS:
        detail = scope.get(key)
        if isinstance(detail, dict):
            item = ((detail.get("itemInfo") or {}).get("itemStruct")) or {}
            status = detail.get("statusCode")
            return Page(int(status) if status is not None else (0 if item else None), item, cookies, user_agent)
    return Page(None, {}, cookies, user_agent)


async def fetch_page(url: str, user_agent: str, client: httpx.AsyncClient | None = None) -> Page:
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    try:
        try:
            r = await client.get(url, headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"})
        except httpx.TimeoutException as e:
            raise Timeout("TikTok took too long to respond.") from e
        except httpx.HTTPError as e:
            raise ExtractFailed("Could not reach TikTok.") from e
        if str(r.url).startswith("https://www.tiktok.com/login"):
            raise Private()
        if r.status_code == 404:
            raise NotFound()
        if r.status_code >= 400:
            raise ExtractFailed(f"TikTok returned HTTP {r.status_code}.")
        cookies = {k: v for k, v in client.cookies.items()}
        return parse_page(r.text, cookies, user_agent)
    finally:
        if own:
            await client.aclose()


def _ext_from_url(url: str, default: str = "jpeg") -> str:
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if ext in {"jpeg", "jpg", "png", "webp", "heic"}:
            return ext
    return default


def _mime(ext: str) -> str:
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "heic": "image/heic"}.get(ext, "application/octet-stream")


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def choose_video(video: dict[str, Any]) -> dict[str, Any] | None:
    """Best watermark-free mp4: h264 with the highest bitrate, then other playable codecs, then playAddr."""
    cands = []
    for b in video.get("bitrateInfo") or []:
        pa = b.get("PlayAddr") or {}
        urls = [u for u in pa.get("UrlList") or [] if isinstance(u, str) and u.startswith("http")]
        if not urls:
            continue
        codec = (b.get("CodecType") or "").lower()
        if "bytevc2" in codec or "h266" in codec:
            continue  # unplayable
        pref = 2 if codec.startswith("h264") else 1
        cands.append((pref, _int(b.get("Bitrate")) or 0, {"url": urls[0], "bytes": _int(pa.get("DataSize")), "width": _int(pa.get("Width")) or _int(video.get("width")), "height": _int(pa.get("Height")) or _int(video.get("height")), "codec": codec}))
    if cands:
        cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
        return cands[0][2]
    play = video.get("playAddr")
    if isinstance(play, str) and play.startswith("http"):
        return {"url": play, "bytes": _int(video.get("size")), "width": _int(video.get("width")), "height": _int(video.get("height")), "codec": video.get("codecType")}
    return None


class TikTokProvider:
    name = "tiktok"

    def __init__(self, fetcher: PageFetcher | None = None, ytdlp=None):
        self._fetch = fetcher or fetch_page
        self._ytdlp = ytdlp or extract_info

    def matches(self, url: str) -> bool:
        try:
            return canonicalize(url).provider == "tiktok"
        except Exception:
            return False

    async def resolve(self, target: NormalizedUrl, *, gif_keep_mp4: bool) -> ResolvedPost:
        is_photo = "/photo/" in target.canonical_url
        order = (MOBILE_UA, DESKTOP_UA) if is_photo else (DESKTOP_UA, MOBILE_UA)
        page: Page | None = None
        for ua in order:
            page = await self._fetch(target.canonical_url, ua)
            if page.status is not None:
                break
        assert page is not None
        if page.status is None:
            log.info("tiktok %s: no rehydration data (challenge=%s); falling back to yt-dlp", target.post_id, page.challenge)
            return await self._via_ytdlp(target)
        if page.status in PRIVATE_STATUS:
            raise Private()
        if page.status in IP_BLOCKED_STATUS:
            raise Private("TikTok blocks this server's IP for this post; set COOKIES_PATH.")
        if page.status != 0 or not page.item:
            raise NotFound()
        return self._from_item(page.item, target.post_id, page)

    def _from_item(self, item: dict[str, Any], post_id: str, page: Page) -> ResolvedPost:
        author_info = item.get("author") if isinstance(item.get("author"), dict) else {}
        author = author_info.get("uniqueId") or (item.get("author") if isinstance(item.get("author"), str) else None) or "unknown"
        text = (item.get("desc") or "").replace("\n", " ").strip()
        base = f"tiktok_{author}_{post_id}"
        sources: list[MediaSource] = []
        images = ((item.get("imagePost") or {}).get("images")) or []
        if images:
            for n, img in enumerate(images, start=1):
                urls = ((img.get("imageURL") or {}).get("urlList")) or []
                if not urls:
                    continue
                ext = _ext_from_url(urls[0])
                sources.append(MediaSource(kind="photo", filename=f"{base}_{n}.{ext}", mime=_mime(ext), upstream_url=urls[0],
                                           headers={"Referer": REFERER, "User-Agent": page.user_agent},
                                           width=_int(img.get("imageWidth")), height=_int(img.get("imageHeight"))))
        else:
            v = item.get("video") or {}
            chosen = choose_video(v) if (v.get("duration") or v.get("bitrateInfo") or v.get("playAddr")) else None
            if chosen:
                headers = {"Referer": REFERER, "User-Agent": page.user_agent}
                if page.cookies.get("tt_chain_token"):
                    headers["Cookie"] = f"tt_chain_token={page.cookies['tt_chain_token']}"
                sources.append(MediaSource(kind="video", filename=f"{base}.mp4", mime="video/mp4", upstream_url=chosen["url"], headers=headers,
                                           bytes=chosen.get("bytes"), width=chosen.get("width"), height=chosen.get("height")))
        if not sources:
            raise NoMedia()
        return ResolvedPost(provider="tiktok", post_id=post_id, author=author, text=text, sources=sources)

    async def _via_ytdlp(self, target: NormalizedUrl) -> ResolvedPost:
        if "/photo/" in target.canonical_url:
            raise ExtractFailed("TikTok photo post could not be read (page blocked).")
        info = await self._ytdlp(target.canonical_url, cookie_domain="tiktok.com")
        return self._from_ytdlp(info, target.post_id)

    def _from_ytdlp(self, info: dict[str, Any], post_id: str) -> ResolvedPost:
        author = info.get("uploader") or info.get("uploader_id") or "unknown"
        text = (info.get("description") or info.get("title") or "").replace("\n", " ").strip()
        fmts = [f for f in info.get("formats") or [] if f.get("url") and f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
                and "watermark" not in (f.get("format_note") or "").lower() and "UNPLAYABLE" not in (f.get("format_note") or "")
                and urlsplit(f["url"]).hostname != "www.tiktok.com"]
        if not fmts:
            raise NoMedia()
        fmts.sort(key=lambda f: (2 if (f.get("vcodec") or "").startswith(("h264", "avc")) else 1, f.get("height") or 0, f.get("tbr") or 0), reverse=True)
        best = fmts[0]
        headers = {"Referer": REFERER, "User-Agent": (best.get("http_headers") or info.get("http_headers") or {}).get("User-Agent") or DESKTOP_UA}
        headers.update({k: v for k, v in (best.get("http_headers") or info.get("http_headers") or {}).items() if k.lower() in ("referer", "user-agent")})
        cookies = info.get("__cookies") or {}
        if cookies.get("tt_chain_token"):
            headers["Cookie"] = f"tt_chain_token={cookies['tt_chain_token']}"
        return ResolvedPost(provider="tiktok", post_id=post_id, author=author, text=text, sources=[
            MediaSource(kind="video", filename=f"tiktok_{author}_{post_id}.mp4", mime="video/mp4", upstream_url=best["url"], headers=headers,
                        bytes=best.get("filesize"), width=best.get("width"), height=best.get("height"))])
