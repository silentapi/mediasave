"""Media Saver — FastAPI app: routes, static mounting, error mapping (spec §3.3)."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, convert, dl
from .config import settings
from .errors import MediaSaverError
from .models import HealthResponse, MediaItem, ResolveRequest, ResolveResponse, ResolvedPost
from .providers import get_provider
from .ratelimit import TokenBucketLimiter, client_ip
from .urlnorm import normalize
from .ytdlp_util import ytdlp_version

log = logging.getLogger("media-saver")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_limiter: TokenBucketLimiter | None = None


def limiter() -> TokenBucketLimiter:
    global _limiter
    if _limiter is None:
        cfg = settings()
        _limiter = TokenBucketLimiter(cfg.resolve_limit, cfg.resolve_window_seconds)
    return _limiter


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = settings()
    for d in (cfg.converted_dir, cfg.tmp_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:  # pragma: no cover
            log.warning("cannot create %s: %s", d, e)
    task = asyncio.create_task(convert.prune_loop(cfg))
    log.info("media-saver %s up; yt-dlp %s; ffmpeg=%s", cfg.version, ytdlp_version(), convert.ffmpeg_available())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Media Saver", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth.router)
app.include_router(dl.router)

STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

NO_STORE = {"Cache-Control": "no-store"}


# --- error mapping ------------------------------------------------------------


@app.exception_handler(MediaSaverError)
async def _media_error(request: Request, exc: MediaSaverError) -> Response:
    headers = dict(NO_STORE)
    if exc.retry_after:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status, headers=headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    return JSONResponse({"error": "unsupported_url", "message": "Send JSON like {\"url\": \"https://…\"}."}, status_code=400)


# --- pages --------------------------------------------------------------------


def _page(name: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse((STATIC / name).read_text(encoding="utf-8"), status_code=status, headers=NO_STORE)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    if not auth.is_authenticated(request):
        return auth.login_redirect(request)
    return _page("index.html")


@app.get("/share", response_class=HTMLResponse)
async def share(request: Request) -> Response:
    # Missing/expired cookie must not dead-end: bounce through /login?next=<this url>.
    if not auth.is_authenticated(request):
        return auth.login_redirect(request)
    return _page("share.html")


@app.get("/share-debug", response_class=HTMLResponse)
async def share_debug(request: Request) -> Response:
    if not auth.is_authenticated(request):
        return auth.login_redirect(request)
    return _page("share-debug.html")


@app.get("/manifest.webmanifest")
async def manifest() -> Response:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> Response:
    # Served from the root so its scope covers the whole origin.
    return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/favicon.ico")
async def favicon() -> Response:
    return RedirectResponse("/static/icons/192.png", status_code=302)


# --- API ----------------------------------------------------------------------


@app.get("/v1/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True, ytdlp_version=ytdlp_version(), ffmpeg=convert.ffmpeg_available(), version=settings().version)


@app.get("/v1/me")
async def me(_: None = auth.AuthDep) -> dict:
    return {"ok": True}


def _summary(items: list[MediaItem]) -> str:
    kinds = [i.kind for i in items]
    n_photo = kinds.count("photo")
    n_video = kinds.count("video")
    n_gif = kinds.count("gif")
    parts: list[str] = []
    if n_gif:
        parts.append("gif" if n_gif == 1 else f"{n_gif} gifs")
        if n_video and n_video == n_gif:
            parts.append("mp4")
            n_video = 0
    if n_video:
        parts.append("video" if n_video == 1 else f"{n_video} videos")
    if n_photo:
        parts.append("image" if n_photo == 1 else f"{n_photo} images")
    return " + ".join(parts) or "nothing"


def build_response(post: ResolvedPost) -> ResolveResponse:
    cfg = settings()
    items = [
        MediaItem(kind=s.kind, filename=s.filename, bytes=s.bytes, width=s.width, height=s.height, dl=dl.dl_url(s, cfg))
        for s in post.sources
    ]
    zip_link = None
    if len(items) > 1:
        zip_link = dl.zip_url(f"{post.provider}_{post.author}_{post.post_id}.zip", post.sources, cfg)
    return ResolveResponse(
        provider=post.provider,
        post_id=post.post_id,
        author=post.author,
        text=(post.text or "")[:120],
        summary=_summary(items),
        items=items,
        zip=zip_link,
    )


@app.post("/v1/resolve", response_model=ResolveResponse, responses={400: {}, 401: {}, 403: {}, 404: {}, 422: {}, 429: {}, 502: {}, 504: {}})
async def resolve(req: ResolveRequest, request: Request, _: None = auth.AuthDep) -> Response:
    cfg = settings()
    ok, retry = limiter().check(client_ip(request))
    if not ok:
        raise MediaSaverError(code="rate_limited", retry_after=retry)
    target = await normalize(req.url)
    keep_mp4 = cfg.gif_keep_mp4_default if req.options.gif_keep_mp4 is None else req.options.gif_keep_mp4
    provider = get_provider(target.provider)
    log.info("resolve %s %s", target.provider, target.post_id)
    post = await provider.resolve(target, gif_keep_mp4=keep_mp4)
    body = build_response(post)
    return JSONResponse(body.model_dump(), headers=NO_STORE)


# --- share-debug helpers (spec §8.3–8.4) -------------------------------------

_DEBUG_FILES = {
    "1": ("mediasaver-test-1.txt", b"Media Saver test file 1\n"),
    "2": ("mediasaver-test-2.txt", b"Media Saver test file 2\n"),
    "3": ("mediasaver-test-3.txt", b"Media Saver test file 3\n"),
}


@app.get("/v1/debug/file/{n}")
async def debug_file(n: str, _: None = auth.AuthDep) -> Response:
    name, data = _DEBUG_FILES.get(n, _DEBUG_FILES["1"])
    return Response(data, media_type="text/plain", headers={"Content-Disposition": dl.content_disposition(name), "Cache-Control": "no-store"})


@app.post("/v1/debug/log")
async def debug_log(request: Request, _: None = auth.AuthDep) -> dict:
    """The phone posts what it observed so findings show up in `docker compose logs`."""
    try:
        data = json.loads((await request.body()) or b"{}")
    except ValueError:
        data = {}
    log.info("share-debug: %s", json.dumps(data)[:2000])
    return {"ok": True}
