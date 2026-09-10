"""TikTok provider — milestone 4. Stub until §8.2 verification is recorded."""
from __future__ import annotations

from ..errors import ExtractFailed
from ..models import ResolvedPost
from ..urlnorm import NormalizedUrl, canonicalize


class TikTokProvider:
    name = "tiktok"

    def matches(self, url: str) -> bool:
        try:
            return canonicalize(url).provider == "tiktok"
        except Exception:
            return False

    async def resolve(self, target: NormalizedUrl, *, gif_keep_mp4: bool) -> ResolvedPost:
        raise ExtractFailed("TikTok provider not implemented yet (milestone 4).")
