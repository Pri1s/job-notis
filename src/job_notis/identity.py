"""Stable identities for jobs observed through multiple upstream sources."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import SplitResult, parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from .models import JobIdentity, SourceItem

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_SPACE = re.compile(r"\s+")
_MARKUP = re.compile(r"<[^>]+>")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)

# These parameters identify a campaign or referrer, not the posting. Parameters
# that can carry a job id (for example ``gh_jid`` and ``jobId``) are deliberately
# absent.
_TRACKING_PARAMETERS = frozenset(
    {
        "_ga",
        "_gl",
        "campaign",
        "campaignid",
        "fbclid",
        "gclid",
        "gh_src",
        "iis",
        "iisn",
        "jr_id",
        "lang",
        "lever-origin",
        "lever-source",
        "mc_cid",
        "mc_eid",
        "mode",
        "ref",
        "referrer",
        "refid",
        "source",
        "sourceid",
        "trackingid",
        "trk",
    }
)
_TRACKING_PREFIXES = ("utm_",)

_GREENHOUSE_HOST = re.compile(r"(?:^|\.)greenhouse\.io$")
_LEVER_HOST = re.compile(r"(?:^|\.)lever\.co$")
_ASHBY_HOST = re.compile(r"(?:^|\.)ashbyhq\.com$")
_WORKDAY_HOST = re.compile(r"(?:^|\.)myworkday(?:jobs|site)\.com$")
_SMARTRECRUITERS_HOST = re.compile(r"(?:^|\.)smartrecruiters\.com$")
_WORKABLE_HOST = re.compile(r"(?:^|\.)workable\.com$")
_ICIMS_HOST = re.compile(r"(?:^|\.)icims\.com$")
_RIPPLING_HOST = re.compile(r"(?:^|\.)rippling\.com$")
_ORACLE_HOST = re.compile(r"(?:^|\.)oraclecloud\.com$|^careers\.oracle\.com$")
_AMAZON_JOBS_HOST = re.compile(r"(?:^|\.)amazon\.jobs$")
_BREEZY_HOST = re.compile(r"(?:^|\.)breezy\.hr$")
_EIGHTFOLD_HOST = re.compile(r"(?:^|\.)eightfold\.ai$")
_GOOGLE_HOST = re.compile(r"(?:^|\.)google\.com$")
_GREENHOUSE_CUSTOM_JOB_HOSTS = frozenset(
    {
        "akunacapital.com",
        "www.akunacapital.com",
        "careers.appian.com",
    }
)
_EIGHTFOLD_CUSTOM_HOSTS = frozenset({"careers.qualcomm.com"})


def _url_for_parsing(url: str) -> str:
    """Return a minimally repaired URL suitable for :func:`urlsplit`."""

    cleaned = html.unescape(url).strip().strip("<>")
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    if cleaned and "://" not in cleaned and not cleaned.startswith(("/", "#", "?")):
        return f"https://{cleaned}"
    return cleaned


def _normalize_percent_escape(match: re.Match[str]) -> str:
    value = int(match.group(1), 16)
    character = chr(value)
    if character in _UNRESERVED:
        return character
    return f"%{value:02X}"


def _normalized_host(parts: SplitResult) -> str:
    """Build a normalized network location from parsed URL parts."""

    hostname = parts.hostname
    if not hostname:
        return ""
    try:
        host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        host = hostname.lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    scheme = parts.scheme.lower()
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return host


def _is_tracking_parameter(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _TRACKING_PARAMETERS or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize_url(url: str) -> str:
    """Return a deterministic application URL with tracking data removed.

    The transformation is intentionally conservative: path case is retained,
    unknown query parameters are retained, and only well-known tracking
    parameters are discarded. Fragments and default ports are removed.
    """

    candidate = _url_for_parsing(url)
    if not candidate:
        return ""

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return candidate

    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"} or not parts.hostname:
        return candidate

    host = _normalized_host(parts)
    path = _PERCENT_ESCAPE.sub(_normalize_percent_escape, parts.path or "/")
    path = re.sub(r"/{2,}", "/", path)
    if path != "/":
        path = path.rstrip("/")

    try:
        query_items = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking_parameter(name)
        ]
    except ValueError:
        query_items = []
    query_items.sort(key=lambda item: (item[0].casefold(), item[0], item[1]))
    query = urlencode(query_items, doseq=True, quote_via=quote)

    return urlunsplit((scheme, host, path, query, ""))


def _query_values(url: str) -> dict[str, list[str]]:
    try:
        query = parse_qsl(urlsplit(_url_for_parsing(url)).query, keep_blank_values=False)
    except ValueError:
        return {}
    result: dict[str, list[str]] = {}
    for name, value in query:
        result.setdefault(name.casefold(), []).append(value.strip())
    return result


def _safe_external_id(value: str) -> str | None:
    candidate = unquote(value).strip().strip("/")
    candidate = re.sub(r"\.(?:html?|aspx?)$", "", candidate, flags=re.IGNORECASE)
    if (
        not candidate
        or len(candidate) > 240
        or not any(character.isalnum() for character in candidate)
        or any(character.isspace() for character in candidate)
    ):
        return None
    return candidate.casefold()


def _path_segments(url: str) -> tuple[str, ...]:
    try:
        path = urlsplit(_url_for_parsing(url)).path
    except ValueError:
        return ()
    return tuple(unquote(segment) for segment in path.split("/") if segment)


def _first_query_id(query: dict[str, list[str]], *names: str) -> str | None:
    for name in names:
        for value in query.get(name.casefold(), []):
            if normalized := _safe_external_id(value):
                return normalized
    return None


def _greenhouse_id(host: str, segments: tuple[str, ...], query: dict[str, list[str]]) -> str | None:
    # gh_jid appears on both Greenhouse-hosted pages and employers' custom
    # career sites, and is a stronger signal than the host name.
    if identifier := _first_query_id(query, "gh_jid"):
        return identifier
    greenhouse_host = _GREENHOUSE_HOST.search(host) is not None
    if not greenhouse_host and host not in _GREENHOUSE_CUSTOM_JOB_HOSTS:
        return None
    if identifier := _first_query_id(query, "token"):
        return identifier
    for index, segment in enumerate(segments[:-1]):
        if segment.casefold() in {"job", "jobs"}:
            candidate = segments[index + 1]
            if not greenhouse_host:
                numeric = re.match(r"(\d{6,})(?:-|$)", candidate)
                if numeric is None:
                    continue
                candidate = numeric.group(1)
            return _safe_external_id(candidate)
    return None


def _lever_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _LEVER_HOST.search(host) or len(segments) < 2:
        return None
    candidate_index = -1
    if segments[-1].casefold() in {"apply", "application"}:
        candidate_index = -2
    return _safe_external_id(segments[candidate_index])


def _ashby_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _ASHBY_HOST.search(host) or len(segments) < 2:
        return None
    candidate_index = -1
    if segments[-1].casefold() in {"apply", "application"}:
        candidate_index = -2
    return _safe_external_id(segments[candidate_index])


def _workday_id(host: str, segments: tuple[str, ...], query: dict[str, list[str]]) -> str | None:
    if not _WORKDAY_HOST.search(host):
        return None
    tenant = host.split(".", maxsplit=1)[0].casefold()
    identifier = _first_query_id(query, "jobId", "requisitionId")
    if identifier is None and segments:
        final_segment = segments[-1]
        suffix_match = re.search(
            r"(?:^|_)((?:[a-z]{1,5}[-_]?)?\d[\w.-]*)$",
            final_segment,
            flags=re.IGNORECASE,
        )
        if suffix_match:
            identifier = _safe_external_id(suffix_match.group(1))
    if identifier is None:
        return None
    return f"{tenant}:{identifier}"


def _smartrecruiters_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _SMARTRECRUITERS_HOST.search(host) or len(segments) < 2:
        return None
    company = _safe_external_id(segments[-2])
    slug = segments[-1]
    numeric = re.match(r"(\d{6,})(?:-|$)", slug)
    uuid = re.match(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:-|$)",
        slug,
        flags=re.IGNORECASE,
    )
    identifier = _safe_external_id(numeric.group(1) if numeric else uuid.group(1) if uuid else slug)
    if company is None or identifier is None:
        return None
    return f"{company}:{identifier}"


def _workable_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _WORKABLE_HOST.search(host):
        return None
    lowered = [segment.casefold() for segment in segments]
    try:
        marker = lowered.index("j")
    except ValueError:
        return None
    if marker == 0 or marker + 1 >= len(segments):
        return None
    company = _safe_external_id(segments[marker - 1])
    identifier = _safe_external_id(segments[marker + 1])
    if company is None or identifier is None:
        return None
    return f"{company}:{identifier}"


def _icims_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _ICIMS_HOST.search(host):
        return None
    lowered = [segment.casefold() for segment in segments]
    try:
        marker = lowered.index("jobs")
    except ValueError:
        return None
    if marker + 1 >= len(segments):
        return None
    identifier = _safe_external_id(segments[marker + 1])
    if identifier is None:
        return None
    tenant = host.split(".", maxsplit=1)[0].removeprefix("careers-").casefold()
    return f"{tenant}:{identifier}"


def _rippling_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _RIPPLING_HOST.search(host):
        return None
    lowered = [segment.casefold() for segment in segments]
    marker = next(
        (index for index, segment in enumerate(lowered) if segment in {"job", "jobs"}),
        None,
    )
    if marker is None or marker == 0 or marker + 1 >= len(segments):
        return None
    tenant = _safe_external_id(segments[marker - 1])
    identifier = _safe_external_id(segments[marker + 1])
    if tenant is None or identifier is None:
        return None
    return f"{tenant}:{identifier}"


def _oracle_id(host: str, segments: tuple[str, ...], fragment: str) -> str | None:
    if not _ORACLE_HOST.search(host):
        return None
    route_segments = segments + tuple(
        unquote(segment) for segment in fragment.split("/") if segment
    )
    lowered = [segment.casefold() for segment in route_segments]
    job_markers = [index for index, segment in enumerate(lowered[:-1]) if segment == "job"]
    if not job_markers:
        return None
    identifier = _safe_external_id(route_segments[job_markers[-1] + 1])
    if identifier is None:
        return None
    tenant = "oracle" if host == "careers.oracle.com" else host.split(".", maxsplit=1)[0]
    return f"{tenant.casefold()}:{identifier}"


def _amazon_jobs_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _AMAZON_JOBS_HOST.search(host):
        return None
    lowered = [segment.casefold() for segment in segments]
    for index, segment in enumerate(lowered[:-1]):
        if segment == "jobs":
            return _safe_external_id(segments[index + 1])
    return None


def _breezy_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _BREEZY_HOST.search(host):
        return None
    lowered = [segment.casefold() for segment in segments]
    try:
        marker = lowered.index("p")
    except ValueError:
        return None
    if marker + 1 >= len(segments):
        return None
    candidate = segments[marker + 1]
    uuid_match = re.match(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:-|$)",
        candidate,
        flags=re.IGNORECASE,
    )
    hex_match = re.match(r"([0-9a-f]{12,})(?:-|$)", candidate, flags=re.IGNORECASE)
    identifier = _safe_external_id(
        uuid_match.group(1) if uuid_match else hex_match.group(1) if hex_match else candidate
    )
    if identifier is None:
        return None
    tenant = host.split(".", maxsplit=1)[0].casefold()
    return f"{tenant}:{identifier}"


def _eightfold_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _EIGHTFOLD_HOST.search(host) and host not in _EIGHTFOLD_CUSTOM_HOSTS:
        return None
    lowered = [segment.casefold() for segment in segments]
    marker = next(
        (
            index
            for index, segment in enumerate(lowered[:-1])
            if segment == "job" and index > 0 and lowered[index - 1] == "careers"
        ),
        None,
    )
    if marker is None:
        marker = next(
            (index for index, segment in enumerate(lowered[:-1]) if segment == "job"),
            None,
        )
    if marker is None:
        return None
    return _safe_external_id(segments[marker + 1])


def _google_careers_id(host: str, segments: tuple[str, ...]) -> str | None:
    if not _GOOGLE_HOST.search(host):
        return None
    lowered = [segment.casefold() for segment in segments]
    try:
        marker = lowered.index("results")
    except ValueError:
        return None
    if marker + 1 >= len(segments):
        return None
    numeric = re.match(r"(\d{8,})(?:-|$)", segments[marker + 1])
    return _safe_external_id(numeric.group(1)) if numeric else None


def extract_ats_identity(url: str) -> tuple[str, str] | None:
    """Extract ``(provider, external_id)`` from a recognized ATS URL.

    IDs are normalized for comparison. Workday, SmartRecruiters, Workable, and
    iCIMS IDs are tenant-qualified because those systems may reuse requisition
    numbers between customers.
    """

    candidate = _url_for_parsing(url)
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    host = (parts.hostname or "").casefold().rstrip(".")
    if not host:
        return None
    segments = _path_segments(candidate)
    query = _query_values(candidate)

    greenhouse = _greenhouse_id(host, segments, query)
    if greenhouse is not None:
        return ("greenhouse", greenhouse)

    providers = (
        ("lever", _lever_id(host, segments)),
        ("ashby", _ashby_id(host, segments)),
        ("workday", _workday_id(host, segments, query)),
        ("smartrecruiters", _smartrecruiters_id(host, segments)),
        ("workable", _workable_id(host, segments)),
        ("icims", _icims_id(host, segments)),
        ("rippling", _rippling_id(host, segments)),
        ("oracle", _oracle_id(host, segments, parts.fragment)),
        ("amazon-jobs", _amazon_jobs_id(host, segments)),
        ("breezy", _breezy_id(host, segments)),
        ("eightfold", _eightfold_id(host, segments)),
        ("google-careers", _google_careers_id(host, segments)),
    )
    return next(
        ((provider, identifier) for provider, identifier in providers if identifier is not None),
        None,
    )


def _normalized_text(value: str) -> str:
    value = html.unescape(_MARKUP.sub(" ", value))
    value = unicodedata.normalize("NFKC", value).casefold()
    value = _NON_WORD.sub(" ", value)
    return _SPACE.sub(" ", value).strip()


def _normalized_locations(locations: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({_normalized_text(location) for location in locations if location.strip()}))


def fallback_fingerprint(item: SourceItem) -> str:
    """Return a conservative semantic fingerprint for URL-rewrite recovery.

    The URL, source metadata, timestamps, and mutable status fields are omitted.
    A match therefore requires the same normalized company, title, location set,
    and season. Callers should use this only as a fallback after ATS and URL
    aliases have failed.
    """

    payload = {
        "company": _normalized_text(item.company),
        "locations": _normalized_locations(item.locations),
        "season": _normalized_text(item.season),
        "title": _normalized_text(item.title),
    }
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def observation_fingerprint(item: SourceItem) -> str:
    """Compatibility name for the semantic fallback fingerprint."""

    return fallback_fingerprint(item)


def identity_for(item: SourceItem) -> JobIdentity:
    """Build the strongest available cross-source identity for ``item``."""

    canonical_url = canonicalize_url(item.apply_url)
    ats_identity = extract_ats_identity(item.apply_url)
    if ats_identity is not None:
        provider, external_id = ats_identity
        return JobIdentity(
            global_key=f"ats:{provider}:{external_id}",
            canonical_url=canonical_url,
            provider=provider,
            external_id=external_id,
        )

    if canonical_url:
        digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        return JobIdentity(global_key=f"url:{digest}", canonical_url=canonical_url)

    fingerprint = fallback_fingerprint(item)
    return JobIdentity(global_key=f"fingerprint:{fingerprint}", canonical_url="")


__all__ = [
    "canonicalize_url",
    "extract_ats_identity",
    "fallback_fingerprint",
    "identity_for",
    "observation_fingerprint",
]
