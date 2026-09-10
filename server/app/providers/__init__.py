"""Provider registry: adding a provider = one new module + one line here."""
from __future__ import annotations

from .base import Provider
from .tiktok import TikTokProvider
from .twitter import TwitterProvider

PROVIDERS: dict[str, Provider] = {
    "twitter": TwitterProvider(),
    "tiktok": TikTokProvider(),
}


def get_provider(name: str) -> Provider:
    return PROVIDERS[name]
