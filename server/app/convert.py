"""mp4 -> gif via two-pass ffmpeg palette, on-disk cache, hourly prune (spec §3.7)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import httpx

from .config import Settings, settings
from .errors import ExtractFailed, Timeout

log = logging.getLogger("media-saver.convert")

CONVERTED_MAX_AGE = 24 * 3600
TMP_MAX_AGE = 3600
PRUNE_INTERVAL = 3600


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def cache_key(mp4_url: str) -> str:
    return hashlib.sha1(mp4_url.encode("utf-8")).hexdigest()


def gif_path_for(mp4_url: str, cfg: Settings | None = None) -> Path:
    cfg = cfg or settings()
    return cfg.converted_dir / f"{cache_key(mp4_url)}.gif"


def _vf(cfg: Settings) -> str:
    return f"fps={cfg.gif_fps},scale='min(iw,{cfg.gif_max_width})':-1:flags=lanczos"


def ffmpeg_commands(src: Path, palette: Path, out: Path, cfg: Settings) -> tuple[list[str], list[str]]:
    vf = _vf(cfg)
    pass1 = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", f"{vf},palettegen=stats_mode=diff", str(palette)]
    pass2 = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-i", str(palette),
        "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
        str(out),
    ]
    return pass1, pass2


async def _run(cmd: list[str], timeout: float) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise Timeout("Gif conversion timed out.")
    if proc.returncode != 0:
        raise ExtractFailed(f"ffmpeg failed: {err.decode(errors='replace')[-300:]}")


async def _download_to(url: str, dest: Path, headers: dict[str, str], cap: int) -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(20.0, read=60.0)) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code >= 400:
                raise ExtractFailed(f"Could not fetch gif source ({resp.status_code}).")
            size = 0
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(256 * 1024):
                    size += len(chunk)
                    if size > cap:
                        raise ExtractFailed("Gif source is too large to convert.")
                    f.write(chunk)


async def convert_mp4_to_gif(mp4_url: str, headers: dict[str, str] | None = None, cfg: Settings | None = None) -> Path:
    """Return path of the converted gif (cached by sha1 of the mp4 URL)."""
    cfg = cfg or settings()
    out = gif_path_for(mp4_url, cfg)
    if out.exists() and time.time() - out.stat().st_mtime < CONVERTED_MAX_AGE and out.stat().st_size > 0:
        return out
    if not ffmpeg_available():
        raise ExtractFailed("ffmpeg is not installed on the server.")
    cfg.converted_dir.mkdir(parents=True, exist_ok=True)
    cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="gif-", dir=cfg.tmp_dir))
    try:
        src = work / "in.mp4"
        palette = work / "palette.png"
        tmp_out = work / "out.gif"
        await _download_to(mp4_url, src, headers or {}, cfg.mp4_download_cap)
        p1, p2 = ffmpeg_commands(src, palette, tmp_out, cfg)
        await _run(p1, cfg.ffmpeg_timeout)
        await _run(p2, cfg.ffmpeg_timeout)
        os.replace(tmp_out, out)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prune_once(cfg: Settings | None = None, now: float | None = None) -> int:
    cfg = cfg or settings()
    now = time.time() if now is None else now
    removed = 0
    if cfg.converted_dir.exists():
        for p in cfg.converted_dir.iterdir():
            if p.is_file() and now - p.stat().st_mtime > CONVERTED_MAX_AGE:
                p.unlink(missing_ok=True)
                removed += 1
    if cfg.tmp_dir.exists():
        for p in cfg.tmp_dir.iterdir():
            if now - p.stat().st_mtime > TMP_MAX_AGE:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                removed += 1
    return removed


async def prune_loop(cfg: Settings | None = None) -> None:
    while True:
        try:
            n = prune_once(cfg)
            if n:
                log.info("pruned %d stale files", n)
        except Exception:  # pragma: no cover
            log.exception("prune failed")
        await asyncio.sleep(PRUNE_INTERVAL)
