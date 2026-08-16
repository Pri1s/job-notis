from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from job_notis.models import SourceSnapshot, Sponsorship
from job_notis.sources import (
    SIMPLIFY_SOURCE,
    SNDSH_SOURCE,
    VANSH_SOURCE,
    ZSHAH_SOURCE,
    FetchResult,
    SimplifyAdapter,
    SndshAdapter,
    SourceAdapter,
    SourceCursor,
    SourceValidationError,
    VanshAdapter,
    ZshahAdapter,
    fetch_all,
    fetch_source,
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value).encode()


def _vansh_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "date_updated": 1_788_000_000,
        "url": "https://jobs.example.com/apply/abc",
        "locations": ["New York, NY", "Remote"],
        "sponsorship": "Offers Sponsorship",
        "active": True,
        "company_name": "Example Corp",
        "title": "Backend Software Engineer Intern",
        "season": "Summer",
        "source": "someone-else",
        "id": "stable-upstream-uuid",
        "date_posted": 1_787_961_600,
        "company_url": "https://example.com",
        "is_visible": True,
    }
    record.update(overrides)
    return record


def _simplify_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source": "Simplify",
        "category": "Software Engineering",
        "company_name": "Example Corp",
        "id": "simplify-stable-uuid",
        "title": "Backend Software Engineer Intern",
        "active": True,
        "terms": ["Summer 2027", "Fall 2027"],
        "date_updated": 1_788_000_000,
        "date_posted": 1_787_961_600,
        "url": "https://jobs.example.com/apply/simplify",
        "locations": ["Chicago, IL", "Remote"],
        "company_url": "https://simplify.jobs/c/example",
        "is_visible": True,
        "sponsorship": "Does Not Offer Sponsorship",
        "degrees": [],
    }
    record.update(overrides)
    return record


def _zshah_job(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "greenhouse:example:123",
        "company": "Example Corp",
        "title": "Platform Engineering Intern",
        "season": "Fall 2026",
        "seasons": ["Fall 2026"],
        "season_inferred": False,
        "category": "Software",
        "location": "Chicago, IL; Remote, US",
        "url": "https://job-boards.greenhouse.io/example/jobs/123",
        "posted_at": "2026-07-20T00:00:00Z",
        "posted_at_source": "date_only",
        "first_seen_at": "2026-07-20T12:34:56Z",
        "sponsorship": "citizens-only",
        "salary": None,
        "skills": ["Python"],
        "source": "greenhouse",
        "h1b_approvals": None,
        "program": "Internship",
        "remote": True,
    }
    job.update(overrides)
    return job


def _zshah_envelope(*jobs: dict[str, Any], count: int | None = None) -> dict[str, Any]:
    return {
        "generated_at": "2026-07-20T13:00:00Z",
        "source": "https://github.com/zshah101/example",
        "h1b_window": "FY2022-2023",
        "count": len(jobs) if count is None else count,
        "jobs": list(jobs),
    }


def _readme(*rows: str, tail: str = "") -> bytes:
    body = "\n".join(
        (
            "# Summer 2027 Tech Internships",
            "",
            "## the list",
            "",
            "| Company | Role | Location | Apply | Added |",
            "| --- | --- | --- | --- | --- |",
            *rows,
            "",
            "## programs open now",
            "",
            "| org | opportunity | type | deadline |",
            "| --- | --- | --- | --- |",
            "| Ignore Me | [program](https://example.com/program) | fellowship | rolling |",
            tail,
        )
    )
    return body.encode()


def test_vansh_adapter_maps_fields_and_preserves_raw() -> None:
    raw = _vansh_record()

    snapshot = VanshAdapter().parse(_json_bytes([raw]), etag='"etag"', last_modified="yesterday")

    assert snapshot.source == VANSH_SOURCE
    assert snapshot.etag == '"etag"'
    assert snapshot.last_modified == "yesterday"
    assert not snapshot.not_modified
    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.source == VANSH_SOURCE
    assert item.source_id == "stable-upstream-uuid"
    assert item.company == "Example Corp"
    assert item.title == "Backend Software Engineer Intern"
    assert item.locations == ("New York, NY", "Remote")
    assert item.apply_url == "https://jobs.example.com/apply/abc"
    assert item.season == "Summer"
    assert item.sponsorship is Sponsorship.OFFERS
    assert item.active
    assert item.visible
    assert item.remote
    assert item.posted_at == "2026-08-29T00:00:00Z"
    assert item.raw == raw
    assert item.raw["source"] == "someone-else"


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        ("U.S. Citizenship is Required", Sponsorship.CITIZENS_ONLY),
        ("Does Not Offer Sponsorship", Sponsorship.NO_SPONSORSHIP),
        ("Offers Sponsorship", Sponsorship.OFFERS),
        ("Other", Sponsorship.UNKNOWN),
        ("new upstream value", Sponsorship.UNKNOWN),
    ],
)
def test_vansh_sponsorship_values(upstream: str, expected: Sponsorship) -> None:
    snapshot = VanshAdapter().parse(_json_bytes([_vansh_record(sponsorship=upstream)]))
    assert snapshot.items[0].sponsorship is expected


def test_vansh_adapter_keeps_closed_hidden_and_not_stated_records() -> None:
    record = _vansh_record(
        active=False,
        is_visible=False,
        season="null",
        date_posted=None,
        locations=["Austin, TX"],
    )

    item = VanshAdapter().parse(_json_bytes([record])).items[0]

    assert not item.active
    assert not item.visible
    assert item.season == "Not stated"
    assert item.posted_at is None


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not JSON",
        b"{}",
        b"[]",
        _json_bytes([_vansh_record(id="")]),
        _json_bytes([_vansh_record(locations=[])]),
        _json_bytes([_vansh_record(active="true")]),
        _json_bytes([_vansh_record(url="/relative")]),
        _json_bytes([_vansh_record(), _vansh_record()]),
    ],
)
def test_vansh_adapter_rejects_incomplete_or_ambiguous_snapshots(payload: bytes) -> None:
    with pytest.raises(SourceValidationError):
        VanshAdapter().parse(payload)


def test_simplify_adapter_maps_structured_feed_and_preserves_raw() -> None:
    raw = _simplify_record()

    snapshot = SimplifyAdapter().parse(
        _json_bytes([raw]),
        etag='"simplify"',
        last_modified="today",
    )

    assert snapshot.source == SIMPLIFY_SOURCE
    assert snapshot.etag == '"simplify"'
    assert snapshot.last_modified == "today"
    item = snapshot.items[0]
    assert item.source == SIMPLIFY_SOURCE
    assert item.source_id == "simplify-stable-uuid"
    assert item.company == "Example Corp"
    assert item.title == "Backend Software Engineer Intern"
    assert item.locations == ("Chicago, IL", "Remote")
    assert item.apply_url == "https://jobs.example.com/apply/simplify"
    assert item.season == "Summer 2027 / Fall 2027"
    assert item.sponsorship is Sponsorship.NO_SPONSORSHIP
    assert item.category == "Software Engineering"
    assert item.active
    assert item.visible
    assert item.remote
    assert item.posted_at == "2026-08-29T00:00:00Z"
    assert item.raw == raw


def test_simplify_adapter_normalizes_unknown_and_duplicate_terms() -> None:
    item = (
        SimplifyAdapter()
        .parse(
            _json_bytes([_simplify_record(terms=["N/A", "N/A"])]),
        )
        .items[0]
    )

    assert item.season == "Not stated"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not JSON",
        b"{}",
        b"[]",
        _json_bytes([_simplify_record(id="")]),
        _json_bytes([_simplify_record(locations=[])]),
        _json_bytes([_simplify_record(terms=[])]),
        _json_bytes([_simplify_record(category="")]),
        _json_bytes([_simplify_record(active="true")]),
        _json_bytes([_simplify_record(url="/relative")]),
        _json_bytes([_simplify_record(), _simplify_record()]),
    ],
)
def test_simplify_adapter_rejects_incomplete_or_ambiguous_snapshots(
    payload: bytes,
) -> None:
    with pytest.raises(SourceValidationError):
        SimplifyAdapter().parse(payload)


def test_sndsh_adapter_reads_only_primary_table_and_recognizes_markers() -> None:
    payload = _readme(
        "| Example **Labs** | Software Engineer Intern (Fall 2026 / Winter 2027) "
        "🛂 🇺🇸 | Chicago, IL / Remote (US) | "
        "[apply](https://jobs.example.com/role/1) | 2026-07-19 |",
        "| Closed Co | Firmware Co-op (Spring 2027) 🔒 | Austin, TX | "
        "[apply](https://jobs.example.com/role/2) | - |",
        "| Location TBD | Frontend Developer Intern | | "
        "[apply](https://jobs.example.com/role/3) | — |",
    )

    snapshot = SndshAdapter().parse(
        payload, etag='"markdown"', last_modified="Sat, 01 Aug 2026 00:00:00 GMT"
    )

    assert snapshot.source == SNDSH_SOURCE
    assert len(snapshot.items) == 3
    first, closed, no_location = snapshot.items
    assert first.company == "Example Labs"
    assert first.title == "Software Engineer Intern (Fall 2026 / Winter 2027)"
    assert first.locations == ("Chicago, IL", "Remote (US)")
    assert first.season == "Fall 2026 / Winter 2027"
    assert first.sponsorship is Sponsorship.CITIZENS_ONLY
    assert first.remote
    assert first.posted_at == "2026-07-19"
    assert first.raw["role"].endswith("🛂 🇺🇸")
    assert first.raw["line_number"] == 7
    assert closed.title == "Firmware Co-op (Spring 2027)"
    assert closed.season == "Spring 2027"
    assert not closed.active
    assert closed.posted_at is None
    assert no_location.locations == ()
    assert no_location.season == "Not stated"
    assert all(item.company != "Ignore Me" for item in snapshot.items)


def test_sndsh_row_id_survives_apply_url_repair() -> None:
    before = _readme(
        "| Example | Software Engineer Intern | New York, NY | "
        "[apply](https://boards.greenhouse.io/example/jobs/12345?gh_src=old) | - |"
    )
    after = _readme(
        "| Example | Software Engineer Intern | New York, NY | "
        "[apply](https://job-boards.greenhouse.io/example/jobs/12345?utm_source=new) | - |"
    )

    before_item = SndshAdapter().parse(before).items[0]
    after_item = SndshAdapter().parse(after).items[0]

    assert before_item.source_id == after_item.source_id
    assert before_item.apply_url != after_item.apply_url


def test_sndsh_keeps_identical_titles_with_distinct_requisition_ids() -> None:
    payload = _readme(
        "| Example | Software Engineer Intern | Chicago, IL | "
        "[apply](https://job-boards.greenhouse.io/example/jobs/12345) | - |\n"
        "| Example | Software Engineer Intern | New York, NY | "
        "[apply](https://job-boards.greenhouse.io/example/jobs/67890) | - |"
    )

    items = SndshAdapter().parse(payload).items

    assert len(items) == 2
    assert items[0].source_id != items[1].source_id


def test_sndsh_parser_handles_escaped_pipe_without_leaving_markdown_escape() -> None:
    payload = _readme(
        r"| Example | Software \| Platform Intern (Summer 2027) | Remote | "
        r"[apply](https://example.com/job) | 2026-07-20 |"
    )

    item = SndshAdapter().parse(payload).items[0]

    assert item.title == "Software | Platform Intern (Summer 2027)"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"# no table",
        _readme(),
        (
            b"| Company | Role | Location | Apply | Added |\n"
            b"| --- | --- | --- | --- |\n"
            b"| A | B | C | [apply](https://example.com) | - |"
        ),
        _readme("| A | B | C | not-a-link | - |"),
        _readme("| A | B | C | [apply](/relative) | - |"),
        _readme("| A | B | C | [apply](https://example.com) | yesterday |"),
        _readme("| A | B | C | [apply](https://example.com) | - | extra |"),
    ],
)
def test_sndsh_adapter_rejects_invalid_primary_table(payload: bytes) -> None:
    with pytest.raises(SourceValidationError):
        SndshAdapter().parse(payload)


def test_zshah_adapter_maps_fields_and_preserves_raw() -> None:
    raw = _zshah_job()

    snapshot = ZshahAdapter().parse(
        _json_bytes(_zshah_envelope(raw)), etag='"api"', last_modified="today"
    )

    assert snapshot.source == ZSHAH_SOURCE
    item = snapshot.items[0]
    assert item.source == ZSHAH_SOURCE
    assert item.source_id == "greenhouse:example:123"
    assert item.locations == ("Chicago, IL", "Remote, US")
    assert item.sponsorship is Sponsorship.CITIZENS_ONLY
    assert item.active
    assert item.visible
    assert item.posted_at == "2026-07-20"
    assert item.first_seen_at == "2026-07-20T12:34:56Z"
    assert item.category == "Software"
    assert item.remote
    assert item.raw == raw


def test_zshah_adapter_preserves_multiple_cycles() -> None:
    raw = _zshah_job()
    raw["season"] = "Summer 2027"
    raw["seasons"] = ["Summer 2027", "Fall 2026"]

    item = ZshahAdapter().parse(_json_bytes(_zshah_envelope(raw))).items[0]

    assert item.season == "Summer 2027 / Fall 2026"


@pytest.mark.parametrize(
    ("upstream", "posted_at_source", "expected"),
    [
        ("2026-07-28", "date_only", "2026-07-28"),
        ("2026-07-28T00:00:00Z", "date_only", "2026-07-28"),
        ("2026-07-28T12:30:00", "exact", "2026-07-28T12:30:00Z"),
    ],
)
def test_zshah_adapter_accepts_date_only_or_naive_posted_at(
    upstream: str,
    posted_at_source: str,
    expected: str,
) -> None:
    item = (
        ZshahAdapter()
        .parse(
            _json_bytes(
                _zshah_envelope(
                    _zshah_job(
                        posted_at=upstream,
                        posted_at_source=posted_at_source,
                    )
                )
            )
        )
        .items[0]
    )

    assert item.posted_at == expected


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        ("citizens-only", Sponsorship.CITIZENS_ONLY),
        ("no-sponsorship", Sponsorship.NO_SPONSORSHIP),
        ("offers", Sponsorship.OFFERS),
        ("unknown", Sponsorship.UNKNOWN),
    ],
)
def test_zshah_sponsorship_values(upstream: str, expected: Sponsorship) -> None:
    item = (
        ZshahAdapter()
        .parse(_json_bytes(_zshah_envelope(_zshah_job(sponsorship=upstream))))
        .items[0]
    )
    assert item.sponsorship is expected


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        _json_bytes({"count": 0, "jobs": []}),
        _json_bytes(_zshah_envelope(_zshah_job(), count=2)),
        _json_bytes(_zshah_envelope(_zshah_job(remote="false"))),
        _json_bytes(_zshah_envelope(_zshah_job(sponsorship="maybe"))),
        _json_bytes(_zshah_envelope(_zshah_job(first_seen_at="2026-07-20"))),
        _json_bytes(_zshah_envelope(_zshah_job(url="javascript:alert(1)"))),
        _json_bytes(_zshah_envelope(_zshah_job(), _zshah_job())),
    ],
)
def test_zshah_adapter_rejects_invalid_snapshots(payload: bytes) -> None:
    with pytest.raises(SourceValidationError):
        ZshahAdapter().parse(payload)


def test_fetch_source_sends_conditional_headers_and_handles_304() -> None:
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(304, headers={"ETag": '"new"'}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_source(
            VanshAdapter(),
            etag='"old"',
            last_modified="Sat, 01 Aug 2026 00:00:00 GMT",
            client=client,
        )

    assert result.ok
    assert result.error is None
    assert result.snapshot == SourceSnapshot(
        source=VANSH_SOURCE,
        items=(),
        etag='"new"',
        last_modified="Sat, 01 Aug 2026 00:00:00 GMT",
        not_modified=True,
    )
    assert seen_request is not None
    assert seen_request.headers["If-None-Match"] == '"old"'
    assert seen_request.headers["If-Modified-Since"] == "Sat, 01 Aug 2026 00:00:00 GMT"


def test_fetch_source_retries_transient_status_and_honors_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2.5"}, request=request)
        return httpx.Response(200, content=_json_bytes([_vansh_record()]), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_source(VanshAdapter(), client=client, sleeper=delays.append, retries=2)

    assert result.ok
    assert calls == 2
    assert delays == [2.5]


def test_fetch_source_retries_transport_errors_then_returns_sanitized_error() -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private details must not leak", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_source(VanshAdapter(), client=client, sleeper=delays.append, retries=1)

    assert not result.ok
    assert result.snapshot is None
    assert result.error == "ReadTimeout: source request failed"
    assert calls == 2
    assert delays == [0.5]


def test_fetch_source_returns_validation_failure_without_a_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_source(VanshAdapter(), client=client)

    assert not result.ok
    assert result.snapshot is None
    assert result.error is not None
    assert result.error.startswith("SourceValidationError:")


class _StaticAdapter(SourceAdapter):
    def __init__(
        self,
        source: str,
        url: str,
        parser: Callable[[bytes], SourceSnapshot],
    ) -> None:
        self.source = source
        self.url = url
        self._parser = parser

    def parse(
        self,
        payload: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SourceSnapshot:
        parsed = self._parser(payload)
        return SourceSnapshot(
            source=self.source,
            items=parsed.items,
            etag=etag,
            last_modified=last_modified,
        )


def test_fetch_all_keeps_results_independent_and_accepts_cursor_shapes() -> None:
    requests: list[httpx.Request] = []
    good = _StaticAdapter(
        "good",
        "https://sources.test/good",
        lambda _: SourceSnapshot("good", (), None, None),
    )
    bad = _StaticAdapter(
        "bad",
        "https://sources.test/bad",
        lambda _: SourceSnapshot("bad", (), None, None),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/bad":
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            content=b"valid",
            headers={"ETag": '"fresh"', "Last-Modified": "new-date"},
            request=request,
        )

    cursors: dict[str, SourceCursor | Mapping[str, str | None]] = {
        "good": SourceCursor(etag='"stale"'),
        "bad": {"etag": None, "last_modified": "old-date"},
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = fetch_all(
            cursors,
            adapters=(bad, good),
            client=client,
            retries=0,
        )

    assert set(results) == {"bad", "good"}
    assert isinstance(results["bad"], FetchResult)
    assert not results["bad"].ok
    assert results["bad"].error == "HTTP 503 from source"
    assert results["good"].ok
    assert results["good"].snapshot is not None
    assert results["good"].snapshot.etag == '"fresh"'
    assert results["good"].snapshot.last_modified == "new-date"
    bad_request = next(request for request in requests if request.url.path == "/bad")
    good_request = next(request for request in requests if request.url.path == "/good")
    assert bad_request.headers["If-Modified-Since"] == "old-date"
    assert good_request.headers["If-None-Match"] == '"stale"'
