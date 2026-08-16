"""Validated source adapters and resilient conditional HTTP fetching."""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from job_notis.identity import canonicalize_url, extract_ats_identity
from job_notis.models import SourceItem, SourceSnapshot, Sponsorship

VANSH_SOURCE = "vanshb03"
SNDSH_SOURCE = "sndsh404"
ZSHAH_SOURCE = "zshah101"
SIMPLIFY_SOURCE = "simplifyjobs"

VANSH_URL = (
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/"
    "dev/.github/scripts/listings.json"
)
SNDSH_URL = "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md"
ZSHAH_URL = (
    "https://zshah101.github.io/"
    "Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/api/jobs.json"
)
SIMPLIFY_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
    "dev/.github/scripts/listings.json"
)

# Public first-party boards. Each entry is a separate source on purpose: a
# broken board or a valid empty board must never affect another employer.
GREENHOUSE_BOARDS = {
    "anduril": "andurilindustries",
    "astranis": "astranis",
    "figure-ai": "figureai",
    "neuralink": "neuralink",
    "redwood-materials": "redwoodmaterials",
    "spacex": "spacex",
    "tenstorrent-university": "tenstorrent",
    "axon": "axon",
}
ASHBY_BOARDS = {
    "base-power": "basepowercompany",
    "etched": "etched",
    "skydio": "skydio",
    "windborne-systems": "windbornesystems",
    "rivian-volkswagen": "rivian",
    "atomic-semi": "atomicsemi",
    "blackstar": "blackstartechnologies",
}
LEVER_BOARDS = {
    "cirrus-logic-eu": "cirruslogic",
    "fluxergy": "fluxergy",
    "shield-ai": "shieldai",
    "hermeus": "hermeus",
    "layup": "layup",
}
SMARTRECRUITERS_BOARDS = {
    "bosch": "bosch-group",
    "western-digital": "westerndigital",
    "sandisk": "sandisk",
}
EMPLOYER_BOARD_REGISTRY = {
    "greenhouse": GREENHOUSE_BOARDS,
    "ashby": ASHBY_BOARDS,
    "lever": LEVER_BOARDS,
    "smartrecruiters": SMARTRECRUITERS_BOARDS,
}

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
DEFAULT_RETRIES = 2
_MAX_RETRY_DELAY_SECONDS = 30.0
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_USER_AGENT = "job-notis/0.1 (+https://github.com/)"
_HTTP_URL_SCHEMES = frozenset({"http", "https"})
_SEASON_PATTERN = re.compile(
    r"(?i)\b(?:(summer|fall|autumn|winter|spring)\s*(20\d{2})"
    r"|(20\d{2})\s*(summer|fall|autumn|winter|spring))\b"
)
_MARKDOWN_LINK_PATTERN = re.compile(r"^\[[^\]]+]\((https?://.+)\)$")
_INLINE_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)]\([^)]+\)")
_HTML_BREAK_PATTERN = re.compile(r"(?i)<br\s*/?>")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_SPACE_PATTERN = re.compile(r"\s+")


class SourceValidationError(ValueError):
    """Raised when an upstream response is not a complete, trustworthy snapshot."""


@dataclass(frozen=True, slots=True)
class SourceCursor:
    """Conditional request validators saved after a successful source fetch."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The independent outcome of fetching one source."""

    source: str
    snapshot: SourceSnapshot | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the source returned a valid snapshot or a 304 response."""

        return self.snapshot is not None


class SourceAdapter(ABC):
    """Convert one upstream representation into the shared source model."""

    source: str
    url: str
    hardware_capable: bool = True

    @abstractmethod
    def parse(
        self,
        payload: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SourceSnapshot:
        """Validate and parse a complete response body."""


class ATSBoardAdapter(SourceAdapter):
    """Provider-neutral adapter for one public employer ATS board."""

    hardware_capable = True
    page_size = 100

    def __init__(self, provider: str, board: str, token: str) -> None:
        self.provider = provider
        self.board = board
        self.token = token
        self.source = f"{provider}:{board}"
        if provider == "greenhouse":
            self.url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        elif provider == "ashby":
            self.url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
        elif provider == "lever":
            self.url = f"https://api.lever.co/v0/postings/{token}?mode=json&limit={self.page_size}"
        elif provider == "smartrecruiters":
            self.url = (
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
                f"?limit={self.page_size}&offset=0"
            )
        else:  # pragma: no cover - registry is intentionally closed.
            raise ValueError(f"unsupported ATS provider: {provider}")

    def parse(
        self,
        payload: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SourceSnapshot:
        records = self._records(payload)
        return self._snapshot(records, etag=etag, last_modified=last_modified)

    def parse_pages(
        self,
        payloads: Iterable[bytes],
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SourceSnapshot:
        records: list[Mapping[str, Any]] = []
        for payload in payloads:
            records.extend(self._records(payload))
        return self._snapshot(records, etag=etag, last_modified=last_modified)

    def next_page_url(self, page: int) -> str | None:
        if self.provider == "lever":
            return (
                f"https://api.lever.co/v0/postings/{self.token}?mode=json&limit={self.page_size}"
                f"&skip={page * self.page_size}"
            )
        if self.provider == "smartrecruiters":
            return (
                f"https://api.smartrecruiters.com/v1/companies/{self.token}/postings"
                f"?limit={self.page_size}&offset={page * self.page_size}"
            )
        return None

    def has_more(self, payload: bytes, count: int) -> bool:
        if self.provider == "smartrecruiters":
            value = _json_payload(payload, expected_description="a JSON object")
            if isinstance(value, dict):
                total = value.get("totalFound", value.get("totalHits"))
                if isinstance(total, int):
                    return count < total
        return self.provider in {"lever", "smartrecruiters"} and count >= self.page_size

    def _records(self, payload: bytes) -> list[Mapping[str, Any]]:
        value = _json_payload(payload, expected_description="a JSON object or array")
        records: Any
        if isinstance(value, list):
            records = value
        elif isinstance(value, dict):
            records_value = value.get("jobs")
            if records_value is None:
                records_value = value.get("content", value.get("postings"))
            records = records_value
        else:
            records = None
        if not isinstance(records, list):
            raise SourceValidationError(f"{self.source} response has no job array")
        return [
            _object(record, f"{self.source} item {index}") for index, record in enumerate(records)
        ]

    def _snapshot(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> SourceSnapshot:
        items: list[SourceItem] = []
        seen_ids: set[str] = set()
        for index, record in enumerate(records):
            item = self._item(record, index)
            _ensure_unique(item.source_id, seen_ids, self.source)
            items.append(item)
        return SourceSnapshot(
            source=self.source,
            items=tuple(items),
            etag=etag,
            last_modified=last_modified,
            authoritative_empty=True,
        )

    def _item(self, record: Mapping[str, Any], index: int) -> SourceItem:
        context = f"{self.source} item {index}"
        if self.provider == "greenhouse":
            source_id = _required_identifier(record, "id", context)
            title = _required_text(record, "title", context)
            apply_url = _valid_url(
                _required_text(record, "absolute_url", context), f"{context}.absolute_url"
            )
            location = record.get("location")
            locations = tuple(
                _text(location.get("name"))
                for location in (location,)
                if isinstance(location, dict) and _text(location.get("name"))
            )
            offices = record.get("offices")
            if isinstance(offices, list):
                locations += tuple(
                    _text(office.get("location") or office.get("name"))
                    for office in offices
                    if isinstance(office, dict)
                    and _text(office.get("location") or office.get("name"))
                )
            description = _html_text(record.get("content"))
            category = _names(record.get("departments"))
            posted_at = _optional_timestamp(record.get("first_published"), context)
            updated = _optional_timestamp(record.get("updated_at"), context)
            employment = _employment(record)
        elif self.provider == "ashby":
            source_id = _required_identifier(record, "jobId", context)
            title = _required_text(record, "title", context)
            apply_url = _valid_url(
                _required_text(record, "applyUrl", context), f"{context}.applyUrl"
            )
            locations = _string_list(record.get("location"), record.get("locations"))
            description = _html_text(record.get("descriptionHtml") or record.get("description"))
            category = _text(record.get("team")) or _text(record.get("department")) or None
            posted_at = _optional_timestamp(record.get("publishedAt"), context)
            updated = _optional_timestamp(record.get("updatedAt"), context)
            employment = (
                _text(record.get("employmentType")) or _text(record.get("employment_type")) or None
            )
        elif self.provider == "lever":
            source_id = _required_identifier(record, "id", context)
            title = _required_text(record, "text", context)
            apply_url = _valid_url(
                _required_text(record, "hostedUrl", context), f"{context}.hostedUrl"
            )
            locations = _string_list(
                record.get("categories", {}).get("location")
                if isinstance(record.get("categories"), dict)
                else None,
                record.get("workplaceType"),
            )
            description = _html_text(record.get("descriptionPlain") or record.get("description"))
            category_value = record.get("categories")
            category = (
                _text(category_value.get("department"))
                if isinstance(category_value, dict)
                else None
            )
            posted_at = _optional_epoch_or_timestamp(record.get("createdAt"), context)
            updated = _optional_epoch_or_timestamp(record.get("updatedAt"), context)
            employment = (
                _text(category_value.get("commitment"))
                if isinstance(category_value, dict)
                else None
            )
        else:
            source_id = _required_identifier(record, "id", context)
            title = _required_text(record, "name", context)
            job_ad = record.get("jobAd") if isinstance(record.get("jobAd"), dict) else {}
            apply_url = _valid_url(
                _required_text(job_ad or record, "applyUrl", context), f"{context}.applyUrl"
            )
            location = record.get("location")
            locations = _string_list(
                location.get("city") if isinstance(location, dict) else location,
                record.get("customField"),
            )
            description = _html_text(
                job_ad.get("sections") if isinstance(job_ad, dict) else None
            ) or _html_text(record.get("description"))
            category = _text(record.get("function"))
            posted_at = _optional_timestamp(record.get("releasedDate"), context)
            updated = _optional_timestamp(record.get("lastUpdatedDate"), context)
            employment = (
                _text(record.get("typeOfEmployment"))
                or _text(record.get("experienceLevel"))
                or None
            )
        locations = tuple(dict.fromkeys(location for location in locations if location)) or (
            "United States",
        )
        text = f"{title} {description or ''}".casefold()
        sponsorship = _ats_sponsorship(text)
        return SourceItem(
            source=self.source,
            source_id=source_id,
            company=self.board.replace("-", " ").title(),
            title=title,
            locations=locations,
            apply_url=apply_url,
            season=_seasons_from_title(title),
            sponsorship=sponsorship,
            active=True,
            posted_at=posted_at,
            category=category,
            remote=any("remote" in location.casefold() for location in locations),
            description=description,
            employment_type=employment,
            upstream_updated_at=updated,
            raw=dict(record),
        )


class GreenhouseAdapter(ATSBoardAdapter):
    """Greenhouse board adapter with a convenient provider-specific constructor."""

    def __init__(self, board: str, token: str | None = None) -> None:
        super().__init__("greenhouse", board, token or board)


class AshbyAdapter(ATSBoardAdapter):
    """Ashby public job-board adapter."""

    def __init__(self, board: str, token: str | None = None) -> None:
        super().__init__("ashby", board, token or board)


class LeverAdapter(ATSBoardAdapter):
    """Lever postings adapter."""

    def __init__(self, board: str, token: str | None = None) -> None:
        super().__init__("lever", board, token or board)


class SmartRecruitersAdapter(ATSBoardAdapter):
    """SmartRecruiters postings adapter."""

    def __init__(self, board: str, token: str | None = None) -> None:
        super().__init__("smartrecruiters", board, token or board)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _required_identifier(record: Mapping[str, Any], field: str, context: str) -> str:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise SourceValidationError(f"{context}.{field} must be a non-empty identifier")
    return str(value).strip()


def _html_text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = " ".join(_html_text(part) or "" for part in value.values())
    if isinstance(value, list):
        value = " ".join(_html_text(part) or "" for part in value)
    if not isinstance(value, str) or not value.strip():
        return None
    return _SPACE_PATTERN.sub(" ", html.unescape(_HTML_TAG_PATTERN.sub(" ", value))).strip()


def _names(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    names = [_text(item.get("name")) for item in value if isinstance(item, dict)]
    return ", ".join(name for name in names if name) or None


def _string_list(primary: Any, secondary: Any = None) -> tuple[str, ...]:
    values: list[str] = []
    for value in (primary, secondary):
        if isinstance(value, str) and value.strip():
            values.extend(part.strip() for part in re.split(r"[;,]", value) if part.strip())
        elif isinstance(value, list):
            values.extend(_text(part) for part in value)
        elif isinstance(value, dict):
            values.append(_text(value.get("name") or value.get("city") or value.get("location")))
    return tuple(value for value in values if value)


def _employment(record: Mapping[str, Any]) -> str | None:
    for key in ("employment_type", "employmentType", "type", "commitment"):
        value = _text(record.get(key))
        if value:
            return value
    metadata = record.get("metadata")
    if isinstance(metadata, list):
        for entry in metadata:
            if isinstance(entry, dict) and "employment" in _text(entry.get("name")).casefold():
                return _text(entry.get("value")) or None
    return None


def _optional_timestamp(value: Any, context: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise SourceValidationError(f"{context} timestamp must be a string")
    return _posted_timestamp(value, f"{context}.timestamp")


def _optional_epoch_or_timestamp(value: Any, context: str) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_timestamp(value, context=f"{context}.timestamp")
    return _optional_timestamp(value, context)


def _ats_sponsorship(text: str) -> Sponsorship:
    if re.search(
        r"\b(?:us|u\.s\.|united states)\s+(?:citizen|person)s?\s+only\b|\bcitizenship\s+required\b",
        text,
    ):
        return Sponsorship.CITIZENS_ONLY
    if re.search(
        r"\b(?:does not|cannot|will not)\s+(?:offer|sponsor)|"
        r"\bno\s+sponsorship\b|\bwithout\s+sponsorship\b",
        text,
    ):
        return Sponsorship.NO_SPONSORSHIP
    if re.search(r"\bsponsorship\s+(?:available|provided|offered)\b|\bwill\s+sponsor\b", text):
        return Sponsorship.OFFERS
    return Sponsorship.UNKNOWN


def _ats_adapters() -> tuple[ATSBoardAdapter, ...]:
    return tuple(
        ATSBoardAdapter(provider, board, token)
        for provider, boards in (
            ("greenhouse", GREENHOUSE_BOARDS),
            ("ashby", ASHBY_BOARDS),
            ("lever", LEVER_BOARDS),
            ("smartrecruiters", SMARTRECRUITERS_BOARDS),
        )
        for board, token in boards.items()
    )


class VanshAdapter(SourceAdapter):
    """Adapter for the structured vanshb03 listings feed."""

    source = VANSH_SOURCE
    url = VANSH_URL

    def parse(
        self,
        payload: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SourceSnapshot:
        records = _json_payload(payload, expected_description="a JSON array")
        if not isinstance(records, list) or not records:
            raise SourceValidationError("vanshb03 response must be a non-empty JSON array")

        items: list[SourceItem] = []
        seen_ids: set[str] = set()
        for index, raw_record in enumerate(records):
            record = _object(raw_record, f"vanshb03 item {index}")
            source_id = _required_text(record, "id", f"vanshb03 item {index}")
            _ensure_unique(source_id, seen_ids, self.source)
            company = _required_text(record, "company_name", f"vanshb03 item {index}")
            title = _required_text(record, "title", f"vanshb03 item {index}")
            apply_url = _valid_url(
                _required_text(record, "url", f"vanshb03 item {index}"),
                f"vanshb03 item {index}.url",
            )
            locations_value = record.get("locations")
            if not isinstance(locations_value, list) or not locations_value:
                raise SourceValidationError(
                    f"vanshb03 item {index}.locations must be a non-empty array"
                )
            locations = tuple(
                _nonempty_string(value, f"vanshb03 item {index}.locations")
                for value in locations_value
            )
            active = _required_bool(record, "active", f"vanshb03 item {index}")
            visible = _required_bool(record, "is_visible", f"vanshb03 item {index}")
            season_value = record.get("season")
            if season_value is None or (
                isinstance(season_value, str)
                and season_value.strip().casefold() in {"", "null", "none", "n/a"}
            ):
                season = "Not stated"
            else:
                season = _nonempty_string(season_value, f"vanshb03 item {index}.season")
            sponsorship = _listings_sponsorship(record.get("sponsorship"))
            posted_at = _epoch_timestamp(
                record.get("date_posted"),
                context=f"vanshb03 item {index}.date_posted",
            )

            items.append(
                SourceItem(
                    source=self.source,
                    source_id=source_id,
                    company=company,
                    title=title,
                    locations=locations,
                    apply_url=apply_url,
                    season=season,
                    sponsorship=sponsorship,
                    active=active,
                    visible=visible,
                    posted_at=posted_at,
                    remote=any("remote" in location.casefold() for location in locations),
                    raw=dict(record),
                )
            )

        return SourceSnapshot(
            source=self.source,
            items=tuple(items),
            etag=etag,
            last_modified=last_modified,
        )


class SimplifyAdapter(SourceAdapter):
    """Adapter for the structured SimplifyJobs listings feed."""

    source = SIMPLIFY_SOURCE
    url = SIMPLIFY_URL

    def parse(
        self,
        payload: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SourceSnapshot:
        records = _json_payload(payload, expected_description="a JSON array")
        if not isinstance(records, list) or not records:
            raise SourceValidationError("simplifyjobs response must be a non-empty JSON array")

        items: list[SourceItem] = []
        seen_ids: set[str] = set()
        for index, raw_record in enumerate(records):
            context = f"simplifyjobs item {index}"
            record = _object(raw_record, context)
            source_id = _required_text(record, "id", context)
            _ensure_unique(source_id, seen_ids, self.source)
            company = _required_text(record, "company_name", context)
            title = _required_text(record, "title", context)
            apply_url = _valid_url(
                _required_text(record, "url", context),
                f"{context}.url",
            )
            locations_value = record.get("locations")
            if not isinstance(locations_value, list) or not locations_value:
                raise SourceValidationError(f"{context}.locations must be a non-empty array")
            locations = tuple(
                _nonempty_string(value, f"{context}.locations") for value in locations_value
            )
            terms_value = record.get("terms")
            if not isinstance(terms_value, list) or not terms_value:
                raise SourceValidationError(f"{context}.terms must be a non-empty array")
            terms = tuple(_nonempty_string(value, f"{context}.terms") for value in terms_value)
            stated_terms = tuple(
                dict.fromkeys(
                    term
                    for term in terms
                    if term.casefold() not in {"n/a", "na", "not stated", "unknown"}
                )
            )
            active = _required_bool(record, "active", context)
            visible = _required_bool(record, "is_visible", context)
            category = _required_text(record, "category", context)
            sponsorship = _listings_sponsorship(record.get("sponsorship"))
            posted_at = _epoch_timestamp(
                record.get("date_posted"),
                context=f"{context}.date_posted",
            )

            items.append(
                SourceItem(
                    source=self.source,
                    source_id=source_id,
                    company=company,
                    title=title,
                    locations=locations,
                    apply_url=apply_url,
                    season=" / ".join(stated_terms) if stated_terms else "Not stated",
                    sponsorship=sponsorship,
                    active=active,
                    visible=visible,
                    posted_at=posted_at,
                    category=category,
                    remote=any("remote" in location.casefold() for location in locations),
                    raw=dict(record),
                )
            )

        return SourceSnapshot(
            source=self.source,
            items=tuple(items),
            etag=etag,
            last_modified=last_modified,
        )


class SndshAdapter(SourceAdapter):
    """Adapter for only the primary five-column table in the sndsh404 README."""

    source = SNDSH_SOURCE
    url = SNDSH_URL

    def parse(
        self,
        payload: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SourceSnapshot:
        text = _utf8_payload(payload, source=self.source)
        lines = text.splitlines()
        header_index = _find_primary_table_header(lines)
        if header_index + 1 >= len(lines):
            raise SourceValidationError("sndsh404 primary table is missing its separator row")

        separator = _split_markdown_row(lines[header_index + 1])
        if len(separator) != 5 or not all(
            re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in separator
        ):
            raise SourceValidationError("sndsh404 primary table has an invalid separator row")

        items: list[SourceItem] = []
        seen_ids: set[str] = set()
        for line_index in range(header_index + 2, len(lines)):
            line = lines[line_index]
            if not line.lstrip().startswith("|"):
                break
            cells = _split_markdown_row(line)
            if len(cells) != 5:
                raise SourceValidationError(
                    f"sndsh404 primary table row {line_index + 1} must have five columns"
                )
            raw_company, raw_title, raw_location, raw_apply, raw_added = cells
            company = _plain_cell(raw_company)
            title = _plain_cell(raw_title)
            location_text = _plain_cell(raw_location)
            if not company or not title:
                raise SourceValidationError(
                    f"sndsh404 primary table row {line_index + 1} has an empty company or role"
                )
            apply_url = _markdown_apply_url(raw_apply, line_number=line_index + 1)
            active = "🔒" not in line
            sponsorship = _sndsh_sponsorship(f"{raw_company} {raw_title}")
            clean_company = _remove_status_markers(company)
            clean_title = _remove_status_markers(title)
            if not clean_company or not clean_title:
                raise SourceValidationError(
                    f"sndsh404 primary table row {line_index + 1} has only status markers"
                )
            locations = _locations_from_markdown(location_text)
            posted_at = _markdown_date(raw_added, line_number=line_index + 1)
            source_id = _sndsh_source_id(apply_url)
            _ensure_unique(source_id, seen_ids, self.source)

            items.append(
                SourceItem(
                    source=self.source,
                    source_id=source_id,
                    company=clean_company,
                    title=clean_title,
                    locations=locations,
                    apply_url=apply_url,
                    season=_seasons_from_title(clean_title),
                    sponsorship=sponsorship,
                    active=active,
                    posted_at=posted_at,
                    remote="remote" in location_text.casefold()
                    or "remote" in clean_title.casefold(),
                    raw={
                        "company": raw_company,
                        "role": raw_title,
                        "location": raw_location,
                        "apply": raw_apply,
                        "added": raw_added,
                        "line": line,
                        "line_number": line_index + 1,
                    },
                )
            )

        if not items:
            raise SourceValidationError("sndsh404 primary table must contain at least one job row")
        return SourceSnapshot(
            source=self.source,
            items=tuple(items),
            etag=etag,
            last_modified=last_modified,
        )


class ZshahAdapter(SourceAdapter):
    """Adapter for the website's structured jobs API."""

    source = ZSHAH_SOURCE
    url = ZSHAH_URL

    def parse(
        self,
        payload: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SourceSnapshot:
        envelope_value = _json_payload(payload, expected_description="a JSON object")
        envelope = _object(envelope_value, "zshah101 response")
        records = envelope.get("jobs")
        if not isinstance(records, list) or not records:
            raise SourceValidationError("zshah101 response.jobs must be a non-empty array")
        count = envelope.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count != len(records):
            raise SourceValidationError("zshah101 response.count must equal the jobs array length")

        items: list[SourceItem] = []
        seen_ids: set[str] = set()
        for index, raw_record in enumerate(records):
            record = _object(raw_record, f"zshah101 item {index}")
            context = f"zshah101 item {index}"
            source_id = _required_text(record, "id", context)
            _ensure_unique(source_id, seen_ids, self.source)
            company = _required_text(record, "company", context)
            title = _required_text(record, "title", context)
            location = _required_text(record, "location", context)
            apply_url = _valid_url(_required_text(record, "url", context), f"{context}.url")
            season = _zshah_seasons(record, context=context)
            category = _required_text(record, "category", context)
            first_seen_at = _iso_timestamp(
                _required_text(record, "first_seen_at", context),
                f"{context}.first_seen_at",
            )
            posted_value = record.get("posted_at")
            posted_at = (
                None
                if posted_value is None
                else _posted_timestamp(
                    _nonempty_string(posted_value, f"{context}.posted_at"),
                    f"{context}.posted_at",
                )
            )
            if posted_at is not None and record.get("posted_at_source") == "date_only":
                # The API represents date-only values as midnight UTC. Preserve
                # their calendar semantics so Discord does not shift them to
                # the previous day in western time zones.
                posted_at = posted_at[:10]
            remote = _required_bool(record, "remote", context)
            sponsorship = _zshah_sponsorship(record.get("sponsorship"), context=context)

            items.append(
                SourceItem(
                    source=self.source,
                    source_id=source_id,
                    company=company,
                    title=title,
                    locations=tuple(part.strip() for part in location.split(";") if part.strip()),
                    apply_url=apply_url,
                    season=season,
                    sponsorship=sponsorship,
                    active=True,
                    posted_at=posted_at,
                    first_seen_at=first_seen_at,
                    category=category,
                    remote=remote,
                    raw=dict(record),
                )
            )

        return SourceSnapshot(
            source=self.source,
            items=tuple(items),
            etag=etag,
            last_modified=last_modified,
        )


VANSH_ADAPTER = VanshAdapter()
SNDSH_ADAPTER = SndshAdapter()
ZSHAH_ADAPTER = ZshahAdapter()
SIMPLIFY_ADAPTER = SimplifyAdapter()
ATS_ADAPTERS: tuple[ATSBoardAdapter, ...] = _ats_adapters()
DEFAULT_ADAPTERS: tuple[SourceAdapter, ...] = (
    VANSH_ADAPTER,
    SNDSH_ADAPTER,
    ZSHAH_ADAPTER,
    SIMPLIFY_ADAPTER,
    *ATS_ADAPTERS,
)

type CursorLike = SourceCursor | SourceSnapshot | Mapping[str, str | None]


def fetch_source(
    adapter: SourceAdapter,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    client: httpx.Client | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    sleeper: Any = time.sleep,
) -> FetchResult:
    """Fetch one source without allowing its failure to escape into the polling loop."""

    if retries < 0:
        raise ValueError("retries cannot be negative")
    if client is not None:
        return _fetch_with_client(
            adapter,
            client=client,
            etag=etag,
            last_modified=last_modified,
            timeout=timeout,
            retries=retries,
            sleeper=sleeper,
        )
    with httpx.Client(follow_redirects=True) as owned_client:
        return _fetch_with_client(
            adapter,
            client=owned_client,
            etag=etag,
            last_modified=last_modified,
            timeout=timeout,
            retries=retries,
            sleeper=sleeper,
        )


def fetch_all(
    cursors: Mapping[str, CursorLike] | None = None,
    *,
    adapters: Iterable[SourceAdapter] = DEFAULT_ADAPTERS,
    client: httpx.Client | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    sleeper: Any = time.sleep,
) -> dict[str, FetchResult]:
    """Fetch every adapter and retain an independent success or error per source."""

    adapter_tuple = tuple(adapters)
    cursor_map = cursors or {}

    def run(active_client: httpx.Client) -> dict[str, FetchResult]:
        host_limits: dict[str, threading.BoundedSemaphore] = {}
        host_lock = threading.Lock()

        def fetch_one(adapter: SourceAdapter) -> FetchResult:
            host = urlsplit(adapter.url).netloc.casefold()
            with host_lock:
                limiter = host_limits.setdefault(host, threading.BoundedSemaphore(2))
            etag, last_modified = _cursor_values(cursor_map.get(adapter.source))
            with limiter:
                try:
                    return _fetch_with_client(
                        adapter,
                        client=active_client,
                        etag=etag,
                        last_modified=last_modified,
                        timeout=timeout,
                        retries=retries,
                        sleeper=sleeper,
                    )
                except Exception as exc:  # One board must never fail the poll.
                    return FetchResult(
                        source=adapter.source,
                        error=f"{type(exc).__name__}: source adapter failed",
                    )

        with ThreadPoolExecutor(max_workers=min(6, max(1, len(adapter_tuple)))) as executor:
            futures = {
                adapter.source: executor.submit(fetch_one, adapter) for adapter in adapter_tuple
            }
            return {
                source: futures[source].result()
                for source in (adapter.source for adapter in adapter_tuple)
            }

    if client is not None:
        return run(client)
    with httpx.Client(follow_redirects=True) as owned_client:
        return run(owned_client)


def _fetch_with_client(
    adapter: SourceAdapter,
    *,
    client: httpx.Client,
    etag: str | None,
    last_modified: str | None,
    timeout: httpx.Timeout | float,
    retries: int,
    sleeper: Any,
) -> FetchResult:
    if isinstance(adapter, ATSBoardAdapter):
        return _fetch_ats_with_client(
            adapter,
            client=client,
            etag=etag,
            last_modified=last_modified,
            timeout=timeout,
            retries=retries,
            sleeper=sleeper,
        )
    headers = {"Accept": "*/*", "User-Agent": _USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    for attempt in range(retries + 1):
        try:
            response = client.get(adapter.url, headers=headers, timeout=timeout)
            response_etag = response.headers.get("ETag") or etag
            response_last_modified = response.headers.get("Last-Modified") or last_modified
            if response.status_code == httpx.codes.NOT_MODIFIED:
                return FetchResult(
                    source=adapter.source,
                    snapshot=SourceSnapshot(
                        source=adapter.source,
                        items=(),
                        etag=response_etag,
                        last_modified=response_last_modified,
                        not_modified=True,
                    ),
                )
            if response.status_code in _TRANSIENT_STATUSES and attempt < retries:
                sleeper(_retry_delay(response, attempt))
                continue
            response.raise_for_status()
            snapshot = adapter.parse(
                response.content,
                etag=response_etag,
                last_modified=response_last_modified,
            )
            return FetchResult(source=adapter.source, snapshot=snapshot)
        except httpx.TransportError as exc:
            if attempt < retries:
                sleeper(min(0.5 * (2**attempt), _MAX_RETRY_DELAY_SECONDS))
                continue
            return FetchResult(
                source=adapter.source,
                error=f"{type(exc).__name__}: source request failed",
            )
        except (httpx.HTTPStatusError, SourceValidationError, UnicodeDecodeError) as exc:
            return FetchResult(source=adapter.source, error=_safe_error(exc))
    return FetchResult(source=adapter.source, error="source request failed after retries")


def _fetch_ats_with_client(
    adapter: ATSBoardAdapter,
    *,
    client: httpx.Client,
    etag: str | None,
    last_modified: str | None,
    timeout: httpx.Timeout | float,
    retries: int,
    sleeper: Any,
) -> FetchResult:
    """Fetch a complete ATS board, including all provider pages."""

    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    payloads: list[bytes] = []
    page = 0
    next_url: str | None = adapter.url
    response_etag = etag
    response_last_modified = last_modified
    while next_url is not None:
        if page >= 100:
            return FetchResult(
                source=adapter.source, error="source pagination exceeded safety limit"
            )
        request_url = next_url
        assert request_url is not None
        for attempt in range(retries + 1):
            try:
                response = client.get(
                    request_url,
                    headers=headers
                    if page == 0
                    else {"Accept": "application/json", "User-Agent": _USER_AGENT},
                    timeout=timeout,
                )
                response_etag = response.headers.get("ETag") or response_etag
                response_last_modified = (
                    response.headers.get("Last-Modified") or response_last_modified
                )
                if response.status_code == httpx.codes.NOT_MODIFIED:
                    if page == 0:
                        return FetchResult(
                            source=adapter.source,
                            snapshot=SourceSnapshot(
                                source=adapter.source,
                                items=(),
                                etag=response_etag,
                                last_modified=response_last_modified,
                                not_modified=True,
                            ),
                        )
                    return FetchResult(source=adapter.source, error="ATS pagination returned 304")
                if response.status_code in _TRANSIENT_STATUSES and attempt < retries:
                    sleeper(_retry_delay(response, attempt))
                    continue
                response.raise_for_status()
                payload = response.content
                records = adapter._records(payload)
                payloads.append(payload)
                next_url = (
                    adapter.next_page_url(page + 1)
                    if adapter.has_more(payload, len(records))
                    else None
                )
                page += 1
                break
            except httpx.TransportError as exc:
                if attempt < retries:
                    sleeper(min(0.5 * (2**attempt), _MAX_RETRY_DELAY_SECONDS))
                    continue
                return FetchResult(
                    source=adapter.source, error=f"{type(exc).__name__}: source request failed"
                )
            except (httpx.HTTPStatusError, SourceValidationError, UnicodeDecodeError) as exc:
                return FetchResult(source=adapter.source, error=_safe_error(exc))
        else:
            return FetchResult(source=adapter.source, error="source request failed after retries")
    try:
        snapshot = adapter.parse_pages(
            payloads,
            etag=response_etag,
            last_modified=response_last_modified,
        )
    except (SourceValidationError, UnicodeDecodeError) as exc:
        return FetchResult(source=adapter.source, error=_safe_error(exc))
    return FetchResult(source=adapter.source, snapshot=snapshot)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                seconds = (retry_at - datetime.now(UTC)).total_seconds()
                return min(max(seconds, 0.0), _MAX_RETRY_DELAY_SECONDS)
            except (TypeError, ValueError, OverflowError):
                pass
    return min(0.5 * (2.0**attempt), _MAX_RETRY_DELAY_SECONDS)


def _cursor_values(cursor: CursorLike | None) -> tuple[str | None, str | None]:
    if cursor is None:
        return None, None
    if isinstance(cursor, Mapping):
        return cursor.get("etag"), cursor.get("last_modified")
    return cursor.etag, cursor.last_modified


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from source"
    return f"{type(exc).__name__}: {exc}"


def _json_payload(payload: bytes, *, expected_description: str) -> Any:
    if not payload or not payload.strip():
        raise SourceValidationError(f"source response must contain {expected_description}")
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SourceValidationError(
            f"source response must contain valid {expected_description}"
        ) from exc


def _utf8_payload(payload: bytes, *, source: str) -> str:
    if not payload or not payload.strip():
        raise SourceValidationError(f"{source} response cannot be empty")
    return payload.decode("utf-8")


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceValidationError(f"{context} must be an object")
    return value


def _required_text(record: Mapping[str, Any], field: str, context: str) -> str:
    if field not in record:
        raise SourceValidationError(f"{context}.{field} is required")
    return _nonempty_string(record[field], f"{context}.{field}")


def _nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceValidationError(f"{context} must be a non-empty string")
    return value.strip()


def _required_bool(record: Mapping[str, Any], field: str, context: str) -> bool:
    value = record.get(field)
    if not isinstance(value, bool):
        raise SourceValidationError(f"{context}.{field} must be a boolean")
    return value


def _valid_url(value: str, context: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in _HTTP_URL_SCHEMES or not parsed.netloc:
        raise SourceValidationError(f"{context} must be an absolute HTTP(S) URL")
    return value


def _ensure_unique(source_id: str, seen_ids: set[str], source: str) -> None:
    if source_id in seen_ids:
        raise SourceValidationError(f"{source} response contains duplicate id {source_id!r}")
    seen_ids.add(source_id)


def _epoch_timestamp(value: Any, *, context: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceValidationError(f"{context} must be an epoch")
    try:
        return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError) as exc:
        raise SourceValidationError(f"{context} is outside the supported range") from exc


def _iso_timestamp(value: str, context: str) -> str:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SourceValidationError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SourceValidationError(f"{context} must include a timezone")
    return value


def _posted_timestamp(value: str, context: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise SourceValidationError(f"{context} must be an ISO-8601 date or timestamp") from exc
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SourceValidationError(f"{context} must be an ISO-8601 date or timestamp") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    return value


def _listings_sponsorship(value: Any) -> Sponsorship:
    if not isinstance(value, str):
        return Sponsorship.UNKNOWN
    normalized = value.strip().casefold()
    if normalized == "u.s. citizenship is required":
        return Sponsorship.CITIZENS_ONLY
    if normalized == "does not offer sponsorship":
        return Sponsorship.NO_SPONSORSHIP
    if normalized == "offers sponsorship":
        return Sponsorship.OFFERS
    return Sponsorship.UNKNOWN


def _zshah_sponsorship(value: Any, *, context: str) -> Sponsorship:
    if not isinstance(value, str):
        raise SourceValidationError(f"{context}.sponsorship must be a string")
    try:
        return Sponsorship(value.strip().casefold())
    except ValueError as exc:
        raise SourceValidationError(f"{context}.sponsorship has an unsupported value") from exc


def _find_primary_table_header(lines: list[str]) -> int:
    expected = ("company", "role", "location", "apply", "added")
    matches = [
        index
        for index, line in enumerate(lines)
        if line.lstrip().startswith("|")
        and tuple(cell.strip().casefold() for cell in _split_markdown_row(line)) == expected
    ]
    if len(matches) != 1:
        raise SourceValidationError(
            "sndsh404 response must contain exactly one primary five-column job table"
        )
    return matches[0]


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    content = stripped[1:]
    if content.endswith("|") and not content.endswith(r"\|"):
        content = content[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in content:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _plain_cell(value: str) -> str:
    result = _HTML_BREAK_PATTERN.sub(" / ", html.unescape(value))
    result = _HTML_TAG_PATTERN.sub("", result)
    result = _INLINE_MARKDOWN_LINK_PATTERN.sub(r"\1", result)
    for marker in ("**", "__", "~~", "`"):
        result = result.replace(marker, "")
    return _SPACE_PATTERN.sub(" ", result).strip()


def _remove_status_markers(value: str) -> str:
    without_markers = value.replace("🔒", "").replace("🛂", "").replace("🇺🇸", "")
    return _SPACE_PATTERN.sub(" ", without_markers).strip()


def _markdown_apply_url(value: str, *, line_number: int) -> str:
    match = _MARKDOWN_LINK_PATTERN.fullmatch(value.strip())
    if match is None:
        raise SourceValidationError(
            f"sndsh404 primary table row {line_number} has an invalid apply link"
        )
    return _valid_url(match.group(1), f"sndsh404 primary table row {line_number}.apply")


def _markdown_date(value: str, *, line_number: int) -> str | None:
    normalized = value.strip()
    if normalized in {"", "-", "—"}:
        return None
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise SourceValidationError(
            f"sndsh404 primary table row {line_number}.added must be YYYY-MM-DD or '-'"
        ) from exc


def _locations_from_markdown(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in re.split(r"\s+(?:/|;)\s+", value) if part.strip())


def _sndsh_sponsorship(value: str) -> Sponsorship:
    if "🇺🇸" in value:
        return Sponsorship.CITIZENS_ONLY
    if "🛂" in value:
        return Sponsorship.NO_SPONSORSHIP
    return Sponsorship.UNKNOWN


def _seasons_from_title(title: str) -> str:
    seasons: list[str] = []
    for match in _SEASON_PATTERN.finditer(title):
        season = match.group(1) or match.group(4)
        year = match.group(2) or match.group(3)
        assert season is not None
        assert year is not None
        normalized_season = "Fall" if season.casefold() == "autumn" else season.title()
        value = f"{normalized_season} {year}"
        if value not in seasons:
            seasons.append(value)
    return " / ".join(seasons) if seasons else "Not stated"


def _sndsh_source_id(apply_url: str) -> str:
    # Prefer a real ATS requisition id so URL repairs stay stable without conflating two
    # legitimate postings that happen to have the same company and title.
    ats_identity = extract_ats_identity(apply_url)
    if ats_identity is not None:
        provider, external_id = ats_identity
        stable_text = f"ats:{provider}:{external_id}"
    else:
        stable_text = f"url:{canonicalize_url(apply_url)}"
    digest = hashlib.sha256(stable_text.encode()).hexdigest()
    return f"row:{digest}"


def _zshah_seasons(record: Mapping[str, Any], *, context: str) -> str:
    primary = _required_text(record, "season", context)
    raw_seasons = record.get("seasons")
    if raw_seasons is None:
        return primary
    if not isinstance(raw_seasons, list) or not raw_seasons:
        raise SourceValidationError(f"{context}.seasons must be null or a non-empty array")
    seasons = [_nonempty_string(value, f"{context}.seasons") for value in raw_seasons]
    if primary not in seasons:
        seasons.insert(0, primary)
    return " / ".join(dict.fromkeys(seasons))
