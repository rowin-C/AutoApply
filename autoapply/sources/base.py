"""Adapter protocol + shared listing data shape for all job sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..settings import SearchFilter, SearchProfile


@dataclass
class ListingCard:
    source: str
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    salary_raw: str = ""
    posted_ago: str | None = None
    job_id: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


class SourceAdapter(Protocol):
    source: str

    def build_search_url(
        self, keywords: str, location: str, filters: SearchFilter
    ) -> str: ...

    def capture_listings(self, browser, search: SearchProfile) -> list[ListingCard]: ...
