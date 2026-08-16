"""Preference filters for software-engineering internship postings."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from .models import NotificationRoute, SourceItem

_SPACE = re.compile(r"\s+")

_STRONG_ROLE_PATTERNS = (
    re.compile(r"\bswe\b"),
    re.compile(r"\bsoftware\s+(?:engineer(?:ing)?|developer|development)\b"),
    re.compile(
        r"(?:\bsoftware\b.*\b(?:engineer(?:ing)?|intern|co[\s-]?op)\b|"
        r"\b(?:engineer(?:ing)?|intern|co[\s-]?op)\b.*\bsoftware\b)"
    ),
    re.compile(r"\b(?:back[\s-]?end|front[\s-]?end|full[\s-]?stack)\b"),
    re.compile(r"\b(?:ios|android|mobile)\s+(?:software\s+)?(?:engineer|developer)\b"),
    re.compile(r"\b(?:ios|android|mobile|systems?)\s+(?:intern|co[\s-]?op)\b"),
    re.compile(
        r"\b(?:platform|infrastructure|systems?)\s+"
        r"(?:software\s+)?(?:engineer(?:ing)?|developer|development)\b"
    ),
    re.compile(r"\bsite\s+reliability(?:\s+engineer(?:ing)?)?\b"),
    re.compile(r"\bsre\b"),
    re.compile(r"\b(?:embedded|firmware)\b"),
    re.compile(r"\bdevops(?:\s+(?:engineer(?:ing)?|developer|development))?\b"),
    re.compile(
        r"\b(?:linux|windows|operating\s+systems?)\s+"
        r"(?:software\s+)?(?:engineer(?:ing)?|developer|development)\b"
    ),
    re.compile(
        r"\b(?:compilers?|developer\s+tools?|tooling)\b.*"
        r"\b(?:engineer(?:ing)?|developer|development|intern|co[\s-]?op)\b"
    ),
    re.compile(r"\b(?:application|web)\s+development\b"),
    re.compile(r"\b(?:platform|infrastructure)\s+(?:intern|co[\s-]?op)\b"),
)
_DEVELOPER_ROLE = re.compile(
    r"\bdeveloper(?!\s+(?:advocate|experience|marketing|relations|success)\b)"
    r"(?:\s+(?:intern|co[\s-]?op))?\b"
)
_UNRELATED_DEVELOPER_SPECIALTY = re.compile(
    r"\b(?:analytics?|data|electrical|finance|financial|hardware|mechanical|"
    r"product|quant|quantitative|trading)\b"
)

_CYCLE_WORD = re.compile(r"\b(summer|fall|autumn|winter|spring)\b")
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_CYCLE_YEAR_PAIR = re.compile(
    r"\b(?:(summer|fall|autumn|winter|spring)\s+(20\d{2})|"
    r"(20\d{2})\s+(summer|fall|autumn|winter|spring))\b"
)
_UNKNOWN_CYCLE = frozenset(
    {
        "",
        "any",
        "multiple",
        "n a",
        "na",
        "not listed",
        "not specified",
        "not stated",
        "rolling",
        "tbd",
        "unknown",
        "various",
        "year round",
    }
)

_US_COUNTRY = re.compile(r"(?:\bunited\s+states(?:\s+of\s+america)?\b|\bu\.?\s*s\.?\s*a?\.?\b)")
_US_STATE_CODES = frozenset(
    {
        "al",
        "ak",
        "az",
        "ar",
        "ca",
        "co",
        "ct",
        "de",
        "fl",
        "ga",
        "hi",
        "id",
        "il",
        "in",
        "ia",
        "ks",
        "ky",
        "la",
        "me",
        "md",
        "ma",
        "mi",
        "mn",
        "ms",
        "mo",
        "mt",
        "ne",
        "nv",
        "nh",
        "nj",
        "nm",
        "ny",
        "nc",
        "nd",
        "oh",
        "ok",
        "or",
        "pa",
        "ri",
        "sc",
        "sd",
        "tn",
        "tx",
        "ut",
        "vt",
        "va",
        "wa",
        "wv",
        "wi",
        "wy",
        "dc",
    }
)
_US_STATES = frozenset(
    {
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "florida",
        "georgia",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode island",
        "south carolina",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "washington",
        "west virginia",
        "wisconsin",
        "wyoming",
        "district of columbia",
    }
)
_US_CITY_NAMES = frozenset(
    {
        "atlanta",
        "austin",
        "boston",
        "chicago",
        "dallas",
        "denver",
        "houston",
        "los angeles",
        "miami",
        "new york",
        "philadelphia",
        "phoenix",
        "pittsburgh",
        "portland",
        "raleigh",
        "san diego",
        "san francisco",
        "san jose",
        "seattle",
        "washington dc",
    }
)
_CANADA_TERMS = frozenset(
    {
        "alberta",
        "british columbia",
        "calgary",
        "canada",
        "edmonton",
        "manitoba",
        "montreal",
        "new brunswick",
        "newfoundland",
        "nova scotia",
        "ontario",
        "ottawa",
        "quebec",
        "saskatchewan",
        "toronto",
        "vancouver",
        "waterloo",
        "winnipeg",
    }
)
_CANADA_CODES = frozenset(
    {"ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt"}
)
_US_CODE = re.compile(
    rf"(?<![A-Za-z])(?:{'|'.join(sorted(code.upper() for code in _US_STATE_CODES))})"
    r"(?![A-Za-z])"
)
_CANADA_CODE = re.compile(
    rf"(?<![A-Za-z])(?:{'|'.join(sorted(code.upper() for code in _CANADA_CODES))})"
    r"(?![A-Za-z])"
)
_GENERIC_REMOTE = re.compile(
    r"^(?:(?:fully\s+)?remote|remote\s+(?:anywhere|global|worldwide)|"
    r"(?:anywhere|global|worldwide)(?:\s+remote)?)$"
)
_REMOTE_WORD = re.compile(r"\bremote\b")

# Hardware routing is deliberately narrower than a generic engineering match.
# These terms describe implementation or silicon-design work, rather than the
# employer's industry or a generic electrical/manufacturing job family.
_HARDWARE_TITLE_EVIDENCE = re.compile(
    r"\b(?:firmware|embedded|rtos|real[\s-]?time\s+os|bsp|bootloader|device[\s-]?driver|"
    r"microcontroller|mcu|asic|fpga|rtl|uvm|verification|analog\s+ic|mixed[\s-]?signal|"
    r"vlsi|physical\s+design|dft|static\s+timing|\bsta\b|soc|system[\s-]+on[\s-]+chip|"
    r"silicon\s+validation|post[\s-]?silicon|chip\s+design|logic\s+design|"
    r"circuit\s+design)\b"
)
_HARDWARE_DESCRIPTION_EVIDENCE = re.compile(
    r"\b(?:firmware|embedded|rtos|real[\s-]?time|bsp|bootloader|device[\s-]?driver|"
    r"fpga|asic|rtl|uvm|verification|analog\s+ic|mixed[\s-]?signal|vlsi|"
    r"physical\s+design|dft|static\s+timing|soc|system[\s-]+on[\s-]+chip|"
    r"silicon\s+validation|post[\s-]?silicon|chip\s+design|logic\s+design|"
    r"circuit\s+design)\b"
)
_HARDWARE_DESIGN_CONTEXT = re.compile(
    r"\b(?:design|develop|implement|verify|layout|route|validate|debug|bring[\s-]?up|"
    r"build|program)\w*\b.{0,90}\b(?:firmware|embedded|fpga|asic|rtl|uvm|verification|"
    r"analog|mixed[\s-]?signal|vlsi|physical\s+design|dft|static\s+timing|soc|"
    r"silicon|chip|logic|circuit)\b"
)
_HARDWARE_GENERIC_TITLE = re.compile(r"\b(?:hardware|electrical|systems?)\b")
_INTERNSHIP_TITLE = re.compile(r"\b(?:intern(?:ship)?|co[\s-]?op)\b")
_INTERNSHIP_TYPE = re.compile(r"\b(?:intern(?:ship)?|co[\s-]?op)\b")


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[/_|,;:()\[\]]+", " ", value)
    value = re.sub(r"[\u2010-\u2015-]+", "-", value)
    return _SPACE.sub(" ", value).strip(" .-")


def role_matches(item: SourceItem) -> bool:
    """Return whether a posting belongs to the selected SWE role family."""

    title = _normalize(item.title)
    category = _normalize(item.category or "")
    if any(pattern.search(title) is not None for pattern in _STRONG_ROLE_PATTERNS):
        return True
    if _UNRELATED_DEVELOPER_SPECIALTY.search(title):
        return False
    if _DEVELOPER_ROLE.search(title):
        return True
    if category == "software":
        return True
    return any(pattern.search(category) is not None for pattern in _STRONG_ROLE_PATTERNS)


def hardware_role_matches(item: SourceItem) -> bool:
    """Return whether a posting is a qualifying firmware, embedded, or chip role.

    Internship evidence must come from the normalized ATS employment type or a
    word-bounded title match. A generic hardware/electrical/systems title is not
    enough on its own; it needs a concrete implementation or silicon-design
    signal in the title or description.
    """

    title = _normalize(item.title)
    category = _normalize(item.category or "")
    description = _normalize(item.description or "")
    internship = bool(_INTERNSHIP_TITLE.search(title)) or bool(
        _INTERNSHIP_TYPE.search(_normalize(item.employment_type or ""))
    )
    if not internship:
        return False
    title_evidence = _HARDWARE_TITLE_EVIDENCE.search(title) is not None
    if title_evidence:
        return True
    # Description evidence is accepted for explicitly hardware-labelled role
    # titles, but not for a chip company's generic boilerplate alone.
    design_text = f"{category} {description}"
    return (
        bool(_HARDWARE_GENERIC_TITLE.search(title) or "electrical" in title)
        and bool(_HARDWARE_DESCRIPTION_EVIDENCE.search(design_text))
        and bool(_HARDWARE_DESIGN_CONTEXT.search(design_text))
    )


def software_route_matches(item: SourceItem) -> bool:
    """Return the software route match, after hardware precedence."""

    return matches_preferences(item) and not hardware_role_matches(item)


def route_matches(item: SourceItem, route: NotificationRoute) -> bool:
    """Return whether an item belongs exclusively to one notification route."""

    if route is NotificationRoute.HARDWARE:
        return (
            item.active
            and item.visible
            and hardware_role_matches(item)
            and cycle_matches(item)
            and geography_matches(item)
        )
    return software_route_matches(item)


def route_for_item(item: SourceItem) -> NotificationRoute | None:
    """Classify an eligible item, applying hardware precedence first."""

    if route_matches(item, NotificationRoute.HARDWARE):
        return NotificationRoute.HARDWARE
    if route_matches(item, NotificationRoute.SOFTWARE):
        return NotificationRoute.SOFTWARE
    return None


# Small compatibility aliases make the route-aware API discoverable to callers
# that prefer noun-style classifier names.
hardware_matches = hardware_role_matches
software_matches = software_route_matches
matches_for_route = route_matches
classify_route = route_for_item


def _cycle_text(item: SourceItem) -> str:
    season = _normalize(item.season)
    # Some feeds put the cycle in the title while leaving the season blank or
    # "unknown". Inspecting both avoids treating an explicit old cycle as
    # unknown.
    return f"{season} {_normalize(item.title)}".strip()


def cycle_matches(item: SourceItem) -> bool:
    """Return whether a posting is in one of the requested recruiting cycles.

    Summer is limited to 2027. Fall, winter, and spring accept 2026 or 2027.
    A genuinely unstated cycle is accepted and is labeled by the caller.
    """

    normalized_season = _normalize(item.season)
    text = _cycle_text(item)
    pair_matches = tuple(_CYCLE_YEAR_PAIR.finditer(text))
    if pair_matches:
        for match in pair_matches:
            cycle = match.group(1) or match.group(4)
            year_text = match.group(2) or match.group(3)
            assert cycle is not None
            assert year_text is not None
            if _cycle_year_matches(cycle, int(year_text)):
                return True

        # A cycle not covered by any explicit pair has no stated year and is
        # intentionally accepted. Do not let a year from a different explicit
        # pair leak across it.
        pair_spans = tuple(match.span() for match in pair_matches)
        return any(
            not any(start <= match.start() and match.end() <= end for start, end in pair_spans)
            for match in _CYCLE_WORD.finditer(text)
        )

    cycles = {match.group(1) for match in _CYCLE_WORD.finditer(text)}
    years = {int(match.group(1)) for match in _YEAR.finditer(text)}

    if not cycles:
        if normalized_season in _UNKNOWN_CYCLE and not years:
            return True
        # A year on its own still communicates enough to reject stale cycles.
        return not years or bool(years & {2026, 2027})

    accepted = False
    for cycle in cycles:
        if not years:
            # The year is the missing portion of the cycle, so this follows the
            # explicit preference to include "cycle not stated" listings.
            accepted = True
        else:
            accepted |= any(_cycle_year_matches(cycle, year) for year in years)
    return accepted


def _cycle_year_matches(cycle: str, year: int) -> bool:
    normalized_cycle = "fall" if cycle == "autumn" else cycle
    if normalized_cycle == "summer":
        return year == 2027
    return year in {2026, 2027}


def _contains_phrase(value: str, phrases: Iterable[str]) -> bool:
    padded = f" {value} "
    return any(f" {phrase} " in padded for phrase in phrases)


def _has_us_marker(raw_value: str, value: str) -> bool:
    if _US_COUNTRY.search(value):
        return True
    if _US_CODE.search(raw_value):
        return True
    return _contains_phrase(value, _US_STATES | _US_CITY_NAMES)


def _has_canada_marker(raw_value: str, value: str) -> bool:
    return _CANADA_CODE.search(raw_value) is not None or _contains_phrase(value, _CANADA_TERMS)


def geography_matches(item: SourceItem) -> bool:
    """Return whether a posting is US-based or genuinely location-neutral remote."""

    locations = tuple((raw, location) for raw in item.locations if (location := _normalize(raw)))
    if any(_has_us_marker(raw, location) for raw, location in locations):
        return True

    if any(_has_canada_marker(raw, location) for raw, location in locations):
        return False

    if any(_GENERIC_REMOTE.fullmatch(location) for _, location in locations):
        return True

    if not locations:
        return item.remote

    # The adapter's explicit remote flag is useful when the feed has no textual
    # marker, but must not turn a named foreign location into generic remote.
    return item.remote and all(_REMOTE_WORD.fullmatch(location) for _, location in locations)


def matches_preferences(item: SourceItem) -> bool:
    """Return whether an open source item should be eligible for notification."""

    return (
        item.active
        and item.visible
        and role_matches(item)
        and cycle_matches(item)
        and geography_matches(item)
    )


__all__ = [
    "cycle_matches",
    "geography_matches",
    "hardware_role_matches",
    "hardware_matches",
    "matches_for_route",
    "matches_preferences",
    "role_matches",
    "classify_route",
    "route_for_item",
    "route_matches",
    "software_matches",
    "software_route_matches",
]
