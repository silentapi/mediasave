"""Access-key login, signed session cookie, and the auth dependency (spec §3.3, §5).

Browser paths use the `session` cookie; scripts send `X-Api-Key`.
The key is never logged.
"""
from __future__ import annotations

import secrets
from typing import Optional
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import Settings, settings
from .errors import MediaSaverError

COOKIE_NAME = "session"
router = APIRouter()


def _signer(cfg: Settings) -> TimestampSigner:
    return TimestampSigner(cfg.secret, salt="media-saver-session")


def make_session_cookie(cfg: Settings) -> str:
    return _signer(cfg).sign(b"ok").decode("ascii")


def session_cookie_valid(cfg: Settings, value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        _signer(cfg).unsign(value.encode("ascii"), max_age=cfg.session_max_age)
        return True
    except (BadSignature, SignatureExpired, UnicodeEncodeError):
        return False


def api_key_valid(cfg: Settings, value: Optional[str]) -> bool:
    if not value:
        return False
    return secrets.compare_digest(value.encode("utf-8"), cfg.access_key.encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    cfg = settings()
    return session_cookie_valid(cfg, request.cookies.get(COOKIE_NAME)) or api_key_valid(
        cfg, request.headers.get("x-api-key")
    )


class Unauthorized(MediaSaverError):
    code = "unauthorized"


def require_auth(request: Request) -> None:
    """Dependency for /v1/* routes: 401 JSON when neither cookie nor header is valid."""
    if not is_authenticated(request):
        raise Unauthorized()


def safe_next(value: Optional[str]) -> str:
    """Only allow same-site relative paths for the post-login redirect (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def login_redirect(request: Request) -> RedirectResponse:
    """302 to /login?next=<full current path+query> — used by page routes."""
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(url=f"/login?next={quote(target, safe='')}", status_code=302)


def set_session_cookie(response: Response, cfg: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_session_cookie(cfg),
        max_age=cfg.session_max_age,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    cfg = settings()
    nxt = safe_next(request.query_params.get("next"))
    if session_cookie_valid(cfg, request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(url=nxt, status_code=302)
    html = (cfg.static_dir / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.post("/login")
async def login_submit(request: Request) -> Response:
    cfg = settings()
    body = (await request.body()).decode("utf-8", errors="replace")
    ctype = request.headers.get("content-type", "")
    key = ""
    nxt = None
    if ctype.startswith("application/json"):
        import json

        try:
            data = json.loads(body or "{}")
        except ValueError:
            data = {}
        key = str(data.get("key", ""))
        nxt = data.get("next")
    else:
        form = parse_qs(body, keep_blank_values=True)
        key = (form.get("key") or [""])[0]
        nxt = (form.get("next") or [None])[0]
    nxt = safe_next(nxt or request.query_params.get("next"))
    if not api_key_valid(cfg, key.strip()):
        payload = {"error": "unauthorized", "message": "Wrong access key."}
        if ctype.startswith("application/json"):
            return JSONResponse(payload, status_code=401)
        html = (cfg.static_dir / "login.html").read_text(encoding="utf-8")
        html = html.replace("<!--ERROR-->", '<p class="err" role="alert">Wrong access key.</p>')
        return HTMLResponse(html, status_code=401, headers={"Cache-Control": "no-store"})
    if ctype.startswith("application/json"):
        resp: Response = JSONResponse({"ok": True, "next": nxt})
    else:
        resp = RedirectResponse(url=nxt, status_code=303)
    set_session_cookie(resp, cfg)
    return resp


@router.post("/logout")
async def logout() -> Response:
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


AuthDep = Depends(require_auth)
