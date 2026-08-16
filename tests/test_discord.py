from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from job_notis.discord import (
    DiscordNotifier,
    PermanentDiscordError,
    notification_batches,
    notification_embed,
)
from job_notis.models import PendingNotification, Sponsorship


def pending(identifier: int, *, title: str = "Software Engineer Intern") -> PendingNotification:
    return PendingNotification(
        id=identifier,
        global_key=f"greenhouse:{identifier}",
        company="Example Co",
        title=title,
        locations=("Chicago, IL",),
        apply_url=f"https://job-boards.greenhouse.io/example/jobs/{identifier}",
        season="Summer 2027",
        sponsorship=Sponsorship.UNKNOWN,
        posted_at=None,
        first_observed_at=datetime.now(UTC).isoformat(),
        sources=("vansh", "zshah"),
        attempts=0,
    )


def test_notification_embed_is_direct_and_mention_safe() -> None:
    embed = notification_embed(pending(1, title="@everyone Software Engineer Intern"))
    assert embed["url"].endswith("/1")
    assert "@everyone" in embed["title"]
    assert embed["fields"][2]["value"].startswith("❔")


def test_date_only_posting_does_not_shift_to_previous_local_day() -> None:
    notification = pending(1)
    notification = PendingNotification(
        id=notification.id,
        global_key=notification.global_key,
        company=notification.company,
        title=notification.title,
        locations=notification.locations,
        apply_url=notification.apply_url,
        season=notification.season,
        sponsorship=notification.sponsorship,
        posted_at="2026-07-28",
        first_observed_at=notification.first_observed_at,
        sources=notification.sources,
        attempts=notification.attempts,
    )

    embed = notification_embed(notification)

    assert embed["fields"][3]["value"].startswith("Posted: Jul 28, 2026\nDiscovered: ")


def test_batches_never_exceed_ten_cards() -> None:
    batches = list(notification_batches([pending(index) for index in range(23)]))
    assert sum(len(jobs) for jobs, _ in batches) == 23
    assert all(len(jobs) <= 10 for jobs, _ in batches)


def test_send_notifications_waits_for_confirmation_and_disables_mentions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "discord-message"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = DiscordNotifier(
        "https://discord.com/api/webhooks/1/token?thread_id=42",
        client=client,
    )

    receipts = notifier.send_notifications([pending(1), pending(2)])

    assert receipts[0].message_id == "discord-message"
    assert requests[0].url.params["wait"] == "true"
    assert requests[0].url.params["thread_id"] == "42"
    body = json.loads(requests[0].content)
    assert body["allowed_mentions"] == {"parse": []}
    assert len(body["embeds"]) == 2


def test_invalid_webhook_is_permanent() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(404, json={"message": "Unknown"}))
    )
    notifier = DiscordNotifier("https://discord.com/api/webhooks/1/token", client=client)
    with pytest.raises(PermanentDiscordError, match="404"):
        notifier.send_test()


def test_rate_limit_respects_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"}, json={"retry_after": 0.25})
        return httpx.Response(200, json={"id": "ok"})

    monkeypatch.setattr("job_notis.discord.time.sleep", sleeps.append)
    notifier = DiscordNotifier(
        "https://discord.com/api/webhooks/1/token",
        retries=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert notifier.send_test() == "ok"
    assert sleeps == [0.25]


def test_rate_limit_with_non_object_json_uses_safe_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json=[])
        return httpx.Response(200, json={"id": "ok"})

    monkeypatch.setattr("job_notis.discord.time.sleep", sleeps.append)
    notifier = DiscordNotifier(
        "https://discord.com/api/webhooks/1/token",
        retries=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert notifier.send_test() == "ok"
    assert sleeps == [1.0]


def test_server_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"id": "recovered"})

    monkeypatch.setattr("job_notis.discord.time.sleep", sleeps.append)
    notifier = DiscordNotifier(
        "https://discord.com/api/webhooks/1/token",
        retries=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert notifier.send_test() == "recovered"
    assert attempts == 2
    assert sleeps == [2.0]
