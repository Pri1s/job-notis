from __future__ import annotations

import json
from dataclasses import replace

import pytest

from job_notis.filters import hardware_role_matches, route_for_item, route_matches
from job_notis.models import NotificationRoute, SourceItem, Sponsorship
from job_notis.sources import ATSBoardAdapter, SourceValidationError


@pytest.fixture
def item() -> SourceItem:
    return SourceItem(
        source="fixture",
        source_id="one",
        company="Example",
        title="FPGA Design Intern",
        locations=("Austin, TX",),
        apply_url="https://example.com/jobs/one",
        season="Summer 2027",
        sponsorship=Sponsorship.UNKNOWN,
        active=True,
    )


@pytest.mark.parametrize(
    "title",
    [
        "Firmware Intern",
        "Embedded Systems Co-op",
        "RTOS Engineer Intern",
        "ASIC Design Intern",
        "FPGA Intern",
        "RTL Verification Intern",
        "Analog IC Design Intern",
        "VLSI Physical Design Intern",
        "DFT Intern",
        "SoC Validation Intern",
        "Silicon Validation Co-op",
    ],
)
def test_hardware_families_route_exclusively(item: SourceItem, title: str) -> None:
    posting = replace(item, title=title)
    assert hardware_role_matches(posting)
    assert route_for_item(posting) is NotificationRoute.HARDWARE
    assert not route_matches(posting, NotificationRoute.SOFTWARE)


@pytest.mark.parametrize(
    "title",
    [
        "Hardware Engineering Intern",
        "Electrical Engineering Intern",
        "Manufacturing Engineering Intern",
        "Systems Intern",
        "Software Engineer Intern",
    ],
)
def test_generic_or_software_roles_do_not_route_hardware(item: SourceItem, title: str) -> None:
    assert not hardware_role_matches(replace(item, title=title))


def test_hardware_requires_internship_evidence(item: SourceItem) -> None:
    assert not hardware_role_matches(replace(item, title="FPGA Design Engineer"))
    assert hardware_role_matches(
        replace(
            item,
            title="Hardware Engineer",
            employment_type="Internship",
            description="Design FPGA boards",
        )
    )


def test_greenhouse_does_not_use_updated_at_as_publication_date() -> None:
    adapter = ATSBoardAdapter("greenhouse", "example", "example")
    payload = json.dumps(
        {
            "jobs": [
                {
                    "id": 123,
                    "title": "Firmware Intern",
                    "absolute_url": "https://boards.greenhouse.io/example/jobs/123",
                    "location": {"name": "Austin, TX"},
                    "updated_at": "2026-08-04T00:00:00Z",
                }
            ]
        }
    ).encode()

    snapshot = adapter.parse(payload)

    assert snapshot.items[0].posted_at is None
    assert snapshot.items[0].upstream_updated_at == "2026-08-04T00:00:00Z"


def test_ats_empty_board_is_authoritative() -> None:
    adapter = ATSBoardAdapter("ashby", "example", "example")
    snapshot = adapter.parse(b'{"jobs": []}')
    assert snapshot.items == ()
    assert snapshot.authoritative_empty is True


def test_ats_duplicate_ids_are_rejected() -> None:
    adapter = ATSBoardAdapter("lever", "example", "example")
    payload = json.dumps(
        [
            {"id": "same", "text": "Firmware Intern", "hostedUrl": "https://example.com/a"},
            {"id": "same", "text": "FPGA Intern", "hostedUrl": "https://example.com/b"},
        ]
    ).encode()
    with pytest.raises(SourceValidationError):
        adapter.parse(payload)
