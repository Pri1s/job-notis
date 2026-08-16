from __future__ import annotations

from dataclasses import replace

import pytest

from job_notis.filters import (
    cycle_matches,
    geography_matches,
    matches_preferences,
    role_matches,
)
from job_notis.models import SourceItem, Sponsorship


@pytest.fixture
def item() -> SourceItem:
    return SourceItem(
        source="fixture",
        source_id="one",
        company="Example",
        title="Software Engineering Intern",
        locations=("Chicago, IL",),
        apply_url="https://example.com/jobs/one",
        season="Summer 2027",
        sponsorship=Sponsorship.UNKNOWN,
        active=True,
    )


@pytest.mark.parametrize(
    "title",
    [
        "SWE Intern",
        "Software Compiler Engineer Intern",
        "WaveLogic Software Intern",
        "Software Developer Co-op",
        "Application Development Intern",
        "Backend Engineering Intern",
        "Front-End Developer Intern",
        "Full Stack Intern",
        "iOS Software Engineer Intern",
        "Android Developer Co-op",
        "Mobile Software Engineer Intern",
        "Mobile Intern",
        "iOS Intern",
        "Android Intern",
        "Platform Engineering Intern",
        "Infrastructure Engineer Intern",
        "Infrastructure Intern",
        "Systems Developer Intern",
        "Systems Intern",
        "Site Reliability Intern",
        "SRE Co-op",
        "Embedded Systems Intern",
        "Firmware Engineering Intern",
        "DevOps Engineering Intern",
        "Linux Engineer Intern",
        "Windows Engineer Intern",
        "Tools and Compilers Research and Development Intern",
    ],
)
def test_selected_role_families_match(item: SourceItem, title: str) -> None:
    assert role_matches(replace(item, title=title))


@pytest.mark.parametrize(
    "title",
    [
        "Data Science Intern",
        "Product Management Intern",
        "Hardware Engineering Intern",
        "Electrical Engineering Intern",
        "UX Design Intern",
        "Business Systems Analyst Intern",
        "Technical Program Manager Intern",
        "Quantitative Developer Intern",
        "Trading Developer Intern",
        "Data Developer Intern",
        "Product Developer Intern",
        "Hardware Developer Intern",
    ],
)
def test_unselected_technical_roles_do_not_match(item: SourceItem, title: str) -> None:
    assert not role_matches(replace(item, title=title, category=None))


def test_software_category_can_classify_ambiguous_title(item: SourceItem) -> None:
    posting = replace(item, title="Engineering Intern", category="Software Engineering")
    assert role_matches(posting)


def test_software_category_classifies_programming_intern(item: SourceItem) -> None:
    posting = replace(item, title="Programming Intern", category="Software")
    assert role_matches(posting)


def test_category_does_not_override_explicitly_unrelated_title(
    item: SourceItem,
) -> None:
    posting = replace(item, title="Product Design Intern", category="Software Engineering")
    assert not role_matches(posting)


def test_explicit_software_phrase_keeps_specialized_developer_title(
    item: SourceItem,
) -> None:
    posting = replace(item, title="Quantitative Software Developer Intern")
    assert role_matches(posting)


@pytest.mark.parametrize(
    "season",
    [
        "Summer 2027",
        "Fall 2026",
        "Autumn 2027",
        "Winter 2026",
        "Winter 2027",
        "Spring 2026",
        "Spring 2027",
        "2026-2027",
        "Unknown",
        "",
        "Rolling",
    ],
)
def test_selected_and_unknown_cycles_match(item: SourceItem, season: str) -> None:
    assert cycle_matches(replace(item, season=season, title="Software Engineer Intern"))


@pytest.mark.parametrize(
    "season",
    [
        "Summer 2026",
        "Summer 2028",
        "Fall 2025",
        "Spring 2028",
        "2025",
        "Summer 2026 / Fall 2025",
        "Summer 2026 / Winter 2025",
    ],
)
def test_out_of_scope_cycles_do_not_match(item: SourceItem, season: str) -> None:
    assert not cycle_matches(replace(item, season=season, title="Software Engineer Intern"))


def test_cycle_in_title_is_used_when_season_is_unknown(item: SourceItem) -> None:
    posting = replace(
        item,
        season="Unknown",
        title="Software Engineer Intern - Summer 2026",
    )
    assert not cycle_matches(posting)


@pytest.mark.parametrize(
    ("locations", "remote"),
    [
        (("United States",), False),
        (("San Francisco, CA",), False),
        (("Austin, Texas",), False),
        (("New York",), False),
        (("Provo, UT",), False),
        (("Remote",), False),
        (("Remote Anywhere",), False),
        (("Worldwide Remote",), False),
        (("Remote - US",), False),
        (("Toronto, Canada", "Seattle, WA"), False),
        ((), True),
    ],
)
def test_us_and_generic_remote_locations_match(
    item: SourceItem, locations: tuple[str, ...], remote: bool
) -> None:
    assert geography_matches(replace(item, locations=locations, remote=remote))


@pytest.mark.parametrize(
    ("locations", "remote"),
    [
        (("Toronto, Canada",), False),
        (("Vancouver, BC",), True),
        (("Remote - Canada",), True),
        (("Remote", "Canada"), True),
        (("London, United Kingdom",), False),
        (("Bengaluru, India",), True),
        (("Remote in Europe",), False),
        (("Remote or London",), False),
        ((), False),
    ],
)
def test_canada_only_foreign_and_missing_locations_do_not_match(
    item: SourceItem, locations: tuple[str, ...], remote: bool
) -> None:
    assert not geography_matches(replace(item, locations=locations, remote=remote))


@pytest.mark.parametrize("sponsorship", list(Sponsorship))
def test_all_sponsorship_statuses_are_kept(item: SourceItem, sponsorship: Sponsorship) -> None:
    assert matches_preferences(replace(item, sponsorship=sponsorship))


def test_closed_or_hidden_items_are_not_notification_candidates(item: SourceItem) -> None:
    assert not matches_preferences(replace(item, active=False))
    assert not matches_preferences(replace(item, visible=False))
