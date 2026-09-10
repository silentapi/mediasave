"""Pydantic models for the /v1 API (spec §3.4)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["photo", "video", "gif"]
ProviderName = Literal["twitter", "tiktok"]


class ResolveOptions(BaseModel):
    gif_keep_mp4: bool | None = None


class ResolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4000, description="Shared text or URL")
    options: ResolveOptions = Field(default_factory=ResolveOptions)


class MediaItem(BaseModel):
    kind: Kind
    filename: str
    bytes: int | None = None
    width: int | None = None
    height: int | None = None
    dl: str


class ResolveResponse(BaseModel):
    provider: ProviderName
    post_id: str
    author: str
    text: str
    summary: str
    items: list[MediaItem]
    zip: str | None = None


class ErrorBody(BaseModel):
    error: str
    message: str


# --- internal (provider -> dl) representation, never sent to the page -------


class MediaSource(BaseModel):
    """One downloadable thing as the provider sees it."""

    kind: Kind
    filename: str
    mime: str
    upstream_url: str | None = None  # remote; proxied with `headers`
    local_path: str | None = None  # already on disk (converted gif)
    headers: dict[str, str] = Field(default_factory=dict)
    bytes: int | None = None
    width: int | None = None
    height: int | None = None


class ResolvedPost(BaseModel):
    provider: ProviderName
    post_id: str
    author: str
    text: str
    sources: list[MediaSource]


class HealthResponse(BaseModel):
    ok: bool
    ytdlp_version: str
    ffmpeg: bool
    version: str
