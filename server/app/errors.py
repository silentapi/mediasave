"""Error codes shared by providers, urlnorm, and the API layer (spec §3.4)."""
from __future__ import annotations

STATUS_BY_CODE = {
    "unsupported_url": 400,
    "not_found": 404,
    "private": 403,
    "no_media": 422,
    "extract_failed": 502,
    "rate_limited": 429,
    "timeout": 504,
    "unauthorized": 401,
}

DEFAULT_MESSAGE = {
    "unsupported_url": "That link isn't a Twitter/X or TikTok post.",
    "not_found": "Post not found.",
    "private": "This post is private or requires login.",
    "no_media": "That post has no media to save.",
    "extract_failed": "Could not extract media from that post.",
    "rate_limited": "Too many requests — try again in a few minutes.",
    "timeout": "The site took too long to respond.",
    "unauthorized": "Not logged in.",
}


class MediaSaverError(Exception):
    """Any error the API maps to `{error, message}`."""

    code: str = "extract_failed"

    def __init__(self, message: str | None = None, *, code: str | None = None, retry_after: int | None = None):
        if code:
            self.code = code
        self.message = message or DEFAULT_MESSAGE.get(self.code, self.code)
        self.retry_after = retry_after
        super().__init__(self.message)

    @property
    def status(self) -> int:
        return STATUS_BY_CODE.get(self.code, 500)


class UnsupportedUrl(MediaSaverError):
    code = "unsupported_url"


class NotFound(MediaSaverError):
    code = "not_found"


class Private(MediaSaverError):
    code = "private"


class NoMedia(MediaSaverError):
    code = "no_media"


class ExtractFailed(MediaSaverError):
    code = "extract_failed"


class RateLimited(MediaSaverError):
    code = "rate_limited"


class Timeout(MediaSaverError):
    code = "timeout"
