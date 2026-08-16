"""Shared domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Sponsorship(StrEnum):
    """Normalized sponsorship status."""

    CITIZENS_ONLY = "citizens-only"
    NO_SPONSORSHIP = "no-sponsorship"
    OFFERS = "offers"
    UNKNOWN = "unknown"


class NotificationRoute(StrEnum):
    """Independent notification channels."""

    SOFTWARE = "software"
    HARDWARE = "hardware"


@dataclass(frozen=True, slots=True)
class SourceItem:
    """A posting as represented by one upstream source."""

    source: str
    source_id: str
    company: str
    title: str
    locations: tuple[str, ...]
    apply_url: str
    season: str
    sponsorship: Sponsorship
    active: bool
    visible: bool = True
    posted_at: str | None = None
    first_seen_at: str | None = None
    category: str | None = None
    remote: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    description: str | None = None
    employment_type: str | None = None
    upstream_updated_at: str | None = None

    @property
    def location_text(self) -> str:
        return ", ".join(location for location in self.locations if location).strip()


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """A validated, complete snapshot returned by one source."""

    source: str
    items: tuple[SourceItem, ...]
    etag: str | None
    last_modified: str | None
    not_modified: bool = False
    authoritative_empty: bool = False


@dataclass(frozen=True, slots=True)
class JobIdentity:
    """Cross-source identity derived from an ATS id or canonical URL."""

    global_key: str
    canonical_url: str
    provider: str | None = None
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingNotification:
    """A durable notification waiting to be delivered."""

    id: int
    global_key: str
    company: str
    title: str
    locations: tuple[str, ...]
    apply_url: str
    season: str
    sponsorship: Sponsorship
    posted_at: str | None
    first_observed_at: str
    sources: tuple[str, ...]
    attempts: int
    route: NotificationRoute = NotificationRoute.SOFTWARE
