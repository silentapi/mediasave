"""Twitter/X provider — milestone 2. Stub until §8.1 verification is recorded."""
from __future__ import annotations

from ..errors import ExtractFailed
from ..models import ResolvedPost
from ..urlnorm import NormalizedUrl, canonicalize


class TwitterProvider:
    name = "twitter"

    def matches(self, url: str) -> bool:
        try:
            return canonicalize(url).provider == "twitter"
        except Exception:
            return False

    async def resolve(self, target: NormalizedUrl, *, gif_keep_mp4: bool) -> ResolvedPost:
        raise ExtractFailed("Twitter provider not implemented yet (milestone 2).")
