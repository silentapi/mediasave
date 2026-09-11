"""extract_info wrapper: threadpool, 45 s timeout, cookies, error mapping (spec §3.2)."""
from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import Settings, settings
from .errors import ExtractFailed, NotFound, Private, Timeout

log = logging.getLogger("media-saver.ytdlp")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdlp")

_PRIVATE_RE = re.compile(r"private|log ?in|sign ?in|cookies|captcha|protected|not authorized|blocked|forbidden", re.I)
_NOTFOUND_RE = re.compile(r"not found|does not exist|no longer available|unavailable|removed|404|deleted|no video", re.I)


def ytdlp_version() -> str:
    try:
        import yt_dlp

        return yt_dlp.version.__version__
    except Exception:  # pragma: no cover
        return "unavailable"


def base_opts(cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or settings()
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "logger": log,
    }
    if cfg.cookies_path:
        opts["cookiefile"] = cfg.cookies_path
    return opts


def _extract_sync(url: str, opts: dict[str, Any], cookie_domain: str | None = None) -> dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        out = ydl.sanitize_info(info) if info is not None else {}
        if cookie_domain and out is not None:
            # Cookies the extractor picked up (e.g. TikTok's tt_chain_token) so the dl proxy can replay them.
            out["__cookies"] = {c.name: c.value for c in ydl.cookiejar if c.domain.lstrip(".").endswith(cookie_domain)}
    return out


def map_error(exc: Exception) -> Exception:
    msg = str(exc)
    if _PRIVATE_RE.search(msg):
        return Private()
    if _NOTFOUND_RE.search(msg):
        return NotFound()
    return ExtractFailed()


async def extract_info(url: str, extra_opts: dict[str, Any] | None = None, cfg: Settings | None = None, cookie_domain: str | None = None) -> dict[str, Any]:
    cfg = cfg or settings()
    opts = base_opts(cfg)
    if extra_opts:
        opts.update(extra_opts)
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_pool, _extract_sync, url, opts, cookie_domain), timeout=cfg.ytdlp_timeout)
    except asyncio.TimeoutError:
        raise Timeout() from None
    except Exception as e:  # yt_dlp.utils.DownloadError and friends
        log.info("yt-dlp failed for %s: %s", url, type(e).__name__)
        raise map_error(e) from e
