"""Source adapter registry."""

from __future__ import annotations

from .ats_map import detect_ats, known_external
from .base import ListingCard
from .linkedin import LinkedInAdapter

ADAPTERS: dict[str, object] = {
    "linkedin": LinkedInAdapter(),
}


def get_adapter(source: str):
    adapter = ADAPTERS.get(source)
    if adapter is None:
        raise KeyError(f"No adapter for source '{source}'. Have: {sorted(ADAPTERS)}")
    return adapter


def known_sources() -> list[str]:
    return sorted(ADAPTERS)


__all__ = [
    "ADAPTERS",
    "ListingCard",
    "detect_ats",
    "get_adapter",
    "known_external",
    "known_sources",
]
