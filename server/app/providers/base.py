"""Provider protocol (spec §3.2)."""
from __future__ import annotations

from typing import Protocol

from ..models import ResolvedPost
from ..urlnorm import NormalizedUrl


class Provider(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...

    async def resolve(self, target: NormalizedUrl, *, gif_keep_mp4: bool) -> ResolvedPost: ...
