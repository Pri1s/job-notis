from __future__ import annotations

from dataclasses import replace

import pytest

from job_notis.identity import (
    canonicalize_url,
    extract_ats_identity,
    fallback_fingerprint,
    identity_for,
    observation_fingerprint,
)
from job_notis.models import SourceItem, Sponsorship


@pytest.fixture
def item() -> SourceItem:
    return SourceItem(
        source="fixture",
        source_id="one",
        company="Example, Inc.",
        title="Software Engineering Intern",
        locations=("New York, NY", "Remote"),
        apply_url="https://example.com/jobs/one",
        season="Summer 2027",
        sponsorship=Sponsorship.UNKNOWN,
        active=True,
    )


def test_canonicalize_url_removes_only_tracking_and_normalizes_structure() -> None:
    url = (
        "HTTPS://EXAMPLE.COM:443//jobs/%7Eone/?b=two&utm_source=repo"
        "&a=hello%20world&gh_jid=123#apply"
    )

    assert canonicalize_url(url) == "https://example.com/jobs/~one?a=hello%20world&b=two&gh_jid=123"


def test_canonicalize_url_repairs_scheme_relative_and_bare_urls() -> None:
    assert canonicalize_url("//EXAMPLE.com/jobs/1/") == "https://example.com/jobs/1"
    assert canonicalize_url("example.com/jobs/1") == "https://example.com/jobs/1"


def test_canonicalize_url_removes_known_referral_presentation_parameters() -> None:
    url = (
        "https://careers.sig.com/jobs/10837?lang=en-us&mode=apply"
        "&iis=LinkedIn&iisn=LinkedIn&jr_id=referral"
    )

    assert canonicalize_url(url) == "https://careers.sig.com/jobs/10837"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/12345?gh_src=abc",
            ("greenhouse", "12345"),
        ),
        (
            "https://careers.acme.com/jobs/software?gh_jid=12345&utm_campaign=x",
            ("greenhouse", "12345"),
        ),
        (
            "https://jobs.lever.co/acme/0F74C6D4-AABC-4B5B-9A2A-123456789ABC/apply",
            ("lever", "0f74c6d4-aabc-4b5b-9a2a-123456789abc"),
        ),
        (
            "https://jobs.ashbyhq.com/acme/12345678-abcd-4321-abcd-123456789abc/application",
            ("ashby", "12345678-abcd-4321-abcd-123456789abc"),
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/"
            "Software-Engineer-Intern_JR12345?source=LinkedIn",
            ("workday", "acme:jr12345"),
        ),
        (
            "https://jobs.smartrecruiters.com/Acme/744000012345678-software-intern",
            ("smartrecruiters", "acme:744000012345678"),
        ),
        (
            "https://apply.workable.com/acme/j/ABC123/",
            ("workable", "acme:abc123"),
        ),
        (
            "https://careers-acme.icims.com/jobs/1234/software-intern/job",
            ("icims", "acme:1234"),
        ),
        (
            "https://ats.rippling.com/acme/jobs/ABC123",
            ("rippling", "acme:abc123"),
        ),
        (
            "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/98765",
            ("oracle", "eeho:98765"),
        ),
        (
            "https://careers.oracle.com/jobs/#en/sites/jobsearch/job/303255",
            ("oracle", "oracle:303255"),
        ),
        (
            "https://www.amazon.jobs/en/jobs/1234567/software-development-intern",
            ("amazon-jobs", "1234567"),
        ),
        (
            "https://acme.breezy.hr/p/1234567890abcdef12345678-software-intern",
            ("breezy", "acme:1234567890abcdef12345678"),
        ),
        (
            "https://lufco.breezy.hr/p/3d3ec6639256-summer-intern-2027",
            ("breezy", "lufco:3d3ec6639256"),
        ),
        (
            "https://jobs.eightfold.ai/careers/job/123456789012345",
            ("eightfold", "123456789012345"),
        ),
        (
            "https://careers.qualcomm.com/careers/job/123456789012345",
            ("eightfold", "123456789012345"),
        ),
        (
            "https://akunacapital.com/careers/job/8018847/",
            ("greenhouse", "8018847"),
        ),
        (
            "https://careers.appian.com/jobs/8041237-software-engineering-intern",
            ("greenhouse", "8041237"),
        ),
        (
            "https://www.google.com/about/careers/applications/jobs/results/"
            "85564713261245126-software-engineering-intern",
            ("google-careers", "85564713261245126"),
        ),
    ],
)
def test_extract_ats_identity(url: str, expected: tuple[str, str]) -> None:
    assert extract_ats_identity(url) == expected


def test_extract_ats_identity_does_not_guess_for_unknown_host() -> None:
    assert extract_ats_identity("https://careers.example.com/jobs/12345") is None
    assert extract_ats_identity("https://careers.example.com/careers/job/12345") is None


def test_identity_for_deduplicates_greenhouse_url_variants(item: SourceItem) -> None:
    hosted = replace(
        item,
        apply_url="https://boards.greenhouse.io/acme/jobs/12345?gh_src=repo",
    )
    custom = replace(
        item,
        apply_url="https://careers.acme.com/opening?gh_jid=12345&utm_source=github",
    )

    assert identity_for(hosted).global_key == "ats:greenhouse:12345"
    assert identity_for(hosted).global_key == identity_for(custom).global_key


def test_identity_for_keeps_distinct_ats_requisition_ids(item: SourceItem) -> None:
    first = replace(item, apply_url="https://jobs.lever.co/acme/uuid-one")
    second = replace(item, apply_url="https://jobs.lever.co/acme/uuid-two")

    assert identity_for(first).global_key != identity_for(second).global_key


def test_non_ats_identity_uses_canonical_url(item: SourceItem) -> None:
    tracked = replace(
        item,
        apply_url="https://example.com/jobs/one?utm_source=github#apply",
    )

    identity = identity_for(tracked)

    assert identity.global_key.startswith("url:")
    assert identity.canonical_url == "https://example.com/jobs/one"
    assert identity.provider is None
    assert identity.external_id is None


def test_semantic_fingerprint_recognizes_url_rewrite_and_mutable_edits(
    item: SourceItem,
) -> None:
    rewritten = replace(
        item,
        source="other-source",
        source_id="other-id",
        apply_url="https://new.example/jobs/new-url",
        sponsorship=Sponsorship.NO_SPONSORSHIP,
        active=False,
        visible=False,
        posted_at="2027-01-01",
    )

    assert fallback_fingerprint(item) == fallback_fingerprint(rewritten)
    assert observation_fingerprint(item) == fallback_fingerprint(item)


def test_semantic_fingerprint_is_conservative(item: SourceItem) -> None:
    other_location = replace(item, locations=("Boston, MA", "Remote"))
    other_season = replace(item, season="Fall 2026")

    assert fallback_fingerprint(item) != fallback_fingerprint(other_location)
    assert fallback_fingerprint(item) != fallback_fingerprint(other_season)


def test_missing_url_uses_semantic_fingerprint(item: SourceItem) -> None:
    identity = identity_for(replace(item, apply_url=""))

    assert identity.global_key == f"fingerprint:{fallback_fingerprint(item)}"
    assert identity.canonical_url == ""
