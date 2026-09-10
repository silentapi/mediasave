"""Environment-driven settings. Plain os.environ on purpose (no extra deps)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    access_key: str
    secret: str
    public_base_url: str
    cookies_path: str | None
    gif_fps: int
    gif_max_width: int
    gif_keep_mp4_default: bool
    zip_multi_default: bool
    data_dir: Path
    resolve_limit: int
    resolve_window_seconds: int
    session_max_age: int = 365 * 24 * 3600
    dl_token_ttl: int = 15 * 60
    ytdlp_timeout: int = 45
    ffmpeg_timeout: int = 60
    mp4_download_cap: int = 50 * 1024 * 1024
    version: str = field(default="0.1.0")

    @property
    def cookie_secure(self) -> bool:
        return self.public_base_url.lower().startswith("https://")

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "static"

    @property
    def converted_dir(self) -> Path:
        return self.data_dir / "converted"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"


def load_settings() -> Settings:
    env = os.environ
    access_key = env.get("ACCESS_KEY", "")
    secret = env.get("SECRET", "")
    if not access_key or not secret:
        raise RuntimeError("ACCESS_KEY and SECRET must be set (see .env.example)")
    if access_key.startswith("change-me") or secret.startswith("change-me"):
        raise RuntimeError("ACCESS_KEY/SECRET still have the placeholder values from .env.example")
    cookies = env.get("COOKIES_PATH") or None
    return Settings(
        access_key=access_key,
        secret=secret,
        public_base_url=env.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        cookies_path=cookies,
        gif_fps=_int(env.get("GIF_FPS"), 15),
        gif_max_width=_int(env.get("GIF_MAX_WIDTH"), 480),
        gif_keep_mp4_default=_bool(env.get("GIF_KEEP_MP4_DEFAULT"), True),
        zip_multi_default=_bool(env.get("ZIP_MULTI_DEFAULT"), False),
        data_dir=Path(env.get("DATA_DIR", "/data")),
        resolve_limit=_int(env.get("RESOLVE_LIMIT"), 30),
        resolve_window_seconds=_int(env.get("RESOLVE_WINDOW_SECONDS"), 300),
    )


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Test hook: drop the cached settings so env changes are picked up."""
    global _settings
    _settings = None
