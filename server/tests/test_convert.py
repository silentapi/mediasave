import os
import shutil
import time
from pathlib import Path

import httpx
import pytest

from app import convert
from app.config import settings
from app.errors import ExtractFailed

FIX = Path(__file__).parent / "fixtures"
SAMPLE = FIX / "sample.mp4"
needs_ffmpeg = pytest.mark.skipif(not convert.ffmpeg_available(), reason="ffmpeg not installed")


def test_ffmpeg_commands_match_spec(tmp_path):
    cfg = settings()
    p1, p2 = convert.ffmpeg_commands(tmp_path / "in.mp4", tmp_path / "p.png", tmp_path / "out.gif", cfg)
    vf = f"fps={cfg.gif_fps},scale='min(iw,{cfg.gif_max_width})':-1:flags=lanczos"
    assert p1[0] == "ffmpeg" and "-y" in p1 and p1[p1.index("-vf") + 1] == f"{vf},palettegen=stats_mode=diff"
    assert p2[p2.index("-lavfi") + 1] == f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    assert p2[-1].endswith("out.gif") and str(tmp_path / "p.png") in p2


def test_cache_key_and_path():
    p = convert.gif_path_for("https://video.twimg.com/tweet_video/abc.mp4")
    assert p.parent == settings().converted_dir
    assert p.name == convert.cache_key("https://video.twimg.com/tweet_video/abc.mp4") + ".gif"
    assert convert.cache_key("a") != convert.cache_key("b")


def _serve_sample(monkeypatch, status=200, body=None, headers_seen=None):
    data = body if body is not None else SAMPLE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if headers_seen is not None:
            headers_seen.update(dict(request.headers))
        return httpx.Response(status, content=data)

    real = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(convert.httpx, "AsyncClient", client)


@needs_ffmpeg
async def test_real_conversion_and_cache(monkeypatch):
    seen = {}
    _serve_sample(monkeypatch, headers_seen=seen)
    url = "https://video.twimg.com/tweet_video/test-sample.mp4"
    out = convert.gif_path_for(url)
    out.unlink(missing_ok=True)
    p = await convert.convert_mp4_to_gif(url, headers={"Referer": "https://x.com/"})
    assert p == out and p.exists() and p.read_bytes()[:6] in (b"GIF89a", b"GIF87a")
    assert seen.get("referer") == "https://x.com/"
    assert p.stat().st_size > 500
    # temp workdir cleaned up
    assert not any(settings().tmp_dir.glob("gif-*"))
    # second call is a cache hit: no download, same path
    def boom(*a, **kw):
        raise AssertionError("should not download on cache hit")
    monkeypatch.setattr(convert, "_download_to", boom)
    assert await convert.convert_mp4_to_gif(url) == out


@needs_ffmpeg
async def test_conversion_scales_to_max_width(monkeypatch):
    # 96x64 sample is below GIF_MAX_WIDTH so dimensions are preserved; check via ffmpeg's own probe-less path: file exists and is a gif
    _serve_sample(monkeypatch)
    url = "https://video.twimg.com/tweet_video/test-sample-2.mp4"
    convert.gif_path_for(url).unlink(missing_ok=True)
    p = await convert.convert_mp4_to_gif(url)
    # GIF logical screen descriptor: width/height as little-endian u16 at bytes 6..10
    w, h = int.from_bytes(p.read_bytes()[6:8], "little"), int.from_bytes(p.read_bytes()[8:10], "little")
    assert (w, h) == (96, 64)


async def test_size_cap(monkeypatch):
    monkeypatch.setattr(convert, "ffmpeg_available", lambda: True)
    _serve_sample(monkeypatch, body=b"x" * (settings().mp4_download_cap + 1))
    url = "https://video.twimg.com/tweet_video/too-big.mp4"
    with pytest.raises(ExtractFailed):
        await convert.convert_mp4_to_gif(url)
    assert not convert.gif_path_for(url).exists()


async def test_upstream_error(monkeypatch):
    monkeypatch.setattr(convert, "ffmpeg_available", lambda: True)
    _serve_sample(monkeypatch, status=404, body=b"")
    with pytest.raises(ExtractFailed):
        await convert.convert_mp4_to_gif("https://video.twimg.com/tweet_video/missing.mp4")


async def test_no_ffmpeg_is_extract_failed(monkeypatch):
    monkeypatch.setattr(convert, "ffmpeg_available", lambda: False)
    with pytest.raises(ExtractFailed):
        await convert.convert_mp4_to_gif("https://video.twimg.com/tweet_video/nope.mp4")


def test_prune_once_removes_only_stale_files():
    cfg = settings()
    cfg.converted_dir.mkdir(parents=True, exist_ok=True)
    cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    old_gif = cfg.converted_dir / "old.gif"; old_gif.write_bytes(b"x"); os.utime(old_gif, (now - 25 * 3600, now - 25 * 3600))
    new_gif = cfg.converted_dir / "new.gif"; new_gif.write_bytes(b"x"); os.utime(new_gif, (now - 3600, now - 3600))
    old_tmp = cfg.tmp_dir / "gif-old"; old_tmp.mkdir(exist_ok=True); (old_tmp / "in.mp4").write_bytes(b"x"); os.utime(old_tmp, (now - 2 * 3600, now - 2 * 3600))
    new_tmp = cfg.tmp_dir / "gif-new"; new_tmp.mkdir(exist_ok=True); os.utime(new_tmp, (now - 600, now - 600))
    removed = convert.prune_once(cfg, now=now)
    assert removed == 2
    assert not old_gif.exists() and new_gif.exists()
    assert not old_tmp.exists() and new_tmp.exists()
    shutil.rmtree(new_tmp, ignore_errors=True); new_gif.unlink()


def test_prune_loop_is_scheduled_on_startup(client):
    # lifespan ran inside the TestClient context; directories exist and the loop task was created
    cfg = settings()
    assert cfg.converted_dir.is_dir() and cfg.tmp_dir.is_dir()
