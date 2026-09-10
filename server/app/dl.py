"""Signed download tokens, streaming proxy, and zip (spec §3.8).

Token payload: { u: upstream_url | None, p: local_path | None, h: headers, f: filename, m: mime }
Expiry is enforced by itsdangerous' timestamp (max_age = dl_token_ttl).
"""
from __future__ import annotations

import asyncio
import re
import struct
import time
import zlib
from typing import AsyncIterator, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import Settings, settings
from .models import MediaSource

router = APIRouter()

_DL_SALT = "media-saver-dl"
_ZIP_SALT = "media-saver-zip"
_CHUNK = 256 * 1024

EXPIRED_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Link expired</title><body style="font:16px system-ui;background:#111;color:#eee;padding:2rem;text-align:center">
<h1 style="font-size:1.3rem">Link expired</h1><p>Reshare the post to save it.</p></body>"""


class TokenError(Exception):
    pass


class TokenExpired(TokenError):
    pass


def _serializer(cfg: Settings, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cfg.secret, salt=salt)


def _pack(src: MediaSource) -> dict:
    d = {"f": src.filename, "m": src.mime}
    if src.local_path:
        d["p"] = src.local_path
    else:
        d["u"] = src.upstream_url
        if src.headers:
            d["h"] = src.headers
    return d


def _unpack(d: dict) -> MediaSource:
    return MediaSource(
        kind="video",  # kind is irrelevant for serving
        filename=d["f"],
        mime=d["m"],
        upstream_url=d.get("u"),
        local_path=d.get("p"),
        headers=d.get("h") or {},
    )


def mint_dl_token(src: MediaSource, cfg: Settings | None = None) -> str:
    cfg = cfg or settings()
    return _serializer(cfg, _DL_SALT).dumps(_pack(src))


def read_dl_token(token: str, cfg: Settings | None = None) -> MediaSource:
    cfg = cfg or settings()
    try:
        d = _serializer(cfg, _DL_SALT).loads(token, max_age=cfg.dl_token_ttl)
    except SignatureExpired as e:
        raise TokenExpired() from e
    except BadSignature as e:
        raise TokenError() from e
    return _unpack(d)


def mint_zip_token(zip_name: str, sources: list[MediaSource], cfg: Settings | None = None) -> str:
    cfg = cfg or settings()
    return _serializer(cfg, _ZIP_SALT).dumps({"z": zip_name, "i": [_pack(s) for s in sources]})


def read_zip_token(token: str, cfg: Settings | None = None) -> tuple[str, list[MediaSource]]:
    cfg = cfg or settings()
    try:
        d = _serializer(cfg, _ZIP_SALT).loads(token, max_age=cfg.dl_token_ttl)
    except SignatureExpired as e:
        raise TokenExpired() from e
    except BadSignature as e:
        raise TokenError() from e
    return d["z"], [_unpack(x) for x in d["i"]]


def dl_url(src: MediaSource, cfg: Settings | None = None) -> str:
    return f"/v1/dl/{mint_dl_token(src, cfg)}"


def zip_url(zip_name: str, sources: list[MediaSource], cfg: Settings | None = None) -> str:
    return f"/v1/zip/{mint_zip_token(zip_name, sources, cfg)}"


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def content_disposition(filename: str) -> str:
    ascii_name = _FILENAME_SAFE.sub("_", filename) or "download"
    return f'attachment; filename="{ascii_name}"'


# --- streaming proxy ---------------------------------------------------------

_PASSTHROUGH = ("content-length", "accept-ranges", "content-range", "last-modified", "etag")


def _upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))


async def _stream_upstream(src: MediaSource, range_header: Optional[str]) -> Response:
    headers = dict(src.headers)
    if range_header:
        headers["Range"] = range_header
    client = _upstream_client()
    req = client.build_request("GET", src.upstream_url or "", headers=headers)
    try:
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        return HTMLResponse("<h1>Upstream timeout</h1>", status_code=504)
    except httpx.HTTPError:
        await client.aclose()
        return HTMLResponse("<h1>Upstream error</h1>", status_code=502)
    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        return HTMLResponse(f"<h1>Upstream refused ({resp.status_code})</h1>", status_code=502)

    out_headers = {
        "Content-Disposition": content_disposition(src.filename),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    for h in _PASSTHROUGH:
        v = resp.headers.get(h)
        if v:
            out_headers[h.title() if h != "etag" else "ETag"] = v
    status = 206 if resp.status_code == 206 and range_header else 200
    if status == 200:
        out_headers.pop("Content-Range", None)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_raw(_CHUNK):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=status, media_type=src.mime, headers=out_headers)


async def serve_source(src: MediaSource, request: Request) -> Response:
    if src.local_path:
        return FileResponse(
            src.local_path,
            media_type=src.mime,
            filename=None,
            headers={"Content-Disposition": content_disposition(src.filename), "Cache-Control": "no-store"},
        )
    return await _stream_upstream(src, request.headers.get("range"))


@router.get("/v1/dl/{token}")
async def download(token: str, request: Request) -> Response:
    try:
        src = read_dl_token(token)
    except TokenExpired:
        return HTMLResponse(EXPIRED_HTML, status_code=410)
    except TokenError:
        return HTMLResponse("<h1>Bad link</h1>", status_code=404)
    return await serve_source(src, request)


# --- zip (store only, streamed) -----------------------------------------------


def _dos_datetime(ts: float) -> tuple[int, int]:
    t = time.localtime(ts)
    d = ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return d, tm


async def _source_bytes(src: MediaSource) -> AsyncIterator[bytes]:
    if src.local_path:
        loop = asyncio.get_running_loop()
        with open(src.local_path, "rb") as f:
            while True:
                chunk = await loop.run_in_executor(None, f.read, _CHUNK)
                if not chunk:
                    break
                yield chunk
        return
    async with _upstream_client() as client:
        async with client.stream("GET", src.upstream_url or "", headers=src.headers) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_raw(_CHUNK):
                yield chunk


async def zip_stream(sources: list[MediaSource]) -> AsyncIterator[bytes]:
    """Zip64-free, store-only streaming zip with data descriptors (files < 4 GB)."""
    offset = 0
    central: list[bytes] = []
    now = time.time()
    d, t = _dos_datetime(now)
    seen: set[str] = set()
    for src in sources:
        name = src.filename
        if name in seen:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem}_{len(seen)}.{ext}" if dot else f"{name}_{len(seen)}"
        seen.add(name)
        nb = name.encode("utf-8")
        flags = 0x0008 | 0x0800  # data descriptor + utf-8 names
        local = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, 0, t, d, 0, 0, 0, len(nb), 0) + nb
        local_off = offset
        yield local
        offset += len(local)
        crc = 0
        size = 0
        async for chunk in _source_bytes(src):
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
            offset += len(chunk)
            yield chunk
        desc = struct.pack("<IIII", 0x08074B50, crc & 0xFFFFFFFF, size, size)
        yield desc
        offset += len(desc)
        central.append(
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50, 20, 20, flags, 0, t, d, crc & 0xFFFFFFFF, size, size, len(nb), 0, 0, 0, 0, 0, local_off,
            )
            + nb
        )
    cd_start = offset
    cd_size = 0
    for c in central:
        yield c
        cd_size += len(c)
    yield struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), cd_size, cd_start, 0)


@router.get("/v1/zip/{token}")
async def download_zip(token: str) -> Response:
    try:
        zip_name, sources = read_zip_token(token)
    except TokenExpired:
        return HTMLResponse(EXPIRED_HTML, status_code=410)
    except TokenError:
        return HTMLResponse("<h1>Bad link</h1>", status_code=404)
    return StreamingResponse(
        zip_stream(sources),
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(zip_name), "Cache-Control": "no-store"},
    )
