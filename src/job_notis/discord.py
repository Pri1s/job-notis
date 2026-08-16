"""Discord incoming-webhook delivery."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from job_notis.models import NotificationRoute, PendingNotification, Sponsorship

logger = logging.getLogger(__name__)

_MAX_EMBEDS = 10
_MAX_EMBED_TEXT = 5_400
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DiscordDeliveryError(RuntimeError):
    """Base class for webhook delivery errors."""


class PermanentDiscordError(DiscordDeliveryError):
    """An invalid or unavailable webhook that should not be retried automatically."""


class TransientDiscordError(DiscordDeliveryError):
    """A delivery failure that can safely remain in the outbox."""


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    notification_ids: tuple[int, ...]
    message_id: str | None


def _clean(value: str, limit: int) -> str:
    value = _CONTROL_CHARACTERS.sub("", value).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _sponsorship_label(value: Sponsorship) -> str:
    return {
        Sponsorship.CITIZENS_ONLY: "🇺🇸 Citizenship/US-person requirement",
        Sponsorship.NO_SPONSORSHIP: "🛂 No sponsorship",
        Sponsorship.OFFERS: "✅ Sponsorship offered",
        Sponsorship.UNKNOWN: "❔ Sponsorship unknown",
    }[value]


def _display_time(value: str | None) -> str:
    if not value:
        return "Not provided"
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).strftime("%b %-d, %Y")
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone().strftime("%b %-d, %Y at %-I:%M %p %Z")
    except ValueError:
        return _clean(value, 100)


def notification_embed(notification: PendingNotification) -> dict[str, Any]:
    """Build one compact, mention-safe job card."""

    location = ", ".join(notification.locations) or "Not specified"
    sources = ", ".join(notification.sources)
    return {
        "title": _clean(f"{notification.company} — {notification.title}", 240),
        "url": notification.apply_url,
        "color": 0x2ECC71,
        "fields": [
            {"name": "Location", "value": _clean(location, 300), "inline": True},
            {"name": "Cycle", "value": _clean(notification.season, 120), "inline": True},
            {
                "name": "Eligibility",
                "value": _sponsorship_label(notification.sponsorship),
                "inline": False,
            },
            {
                "name": "Posted / discovered",
                "value": _clean(
                    f"Posted: {_display_time(notification.posted_at)}\n"
                    f"Discovered: {_display_time(notification.first_observed_at)}",
                    240,
                ),
                "inline": True,
            },
        ],
        "footer": {"text": _clean(f"Job Notis • Sources: {sources}", 300)},
    }


def _embed_text_size(embed: dict[str, Any]) -> int:
    size = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    size += len(str(embed.get("footer", {}).get("text", "")))
    for field in embed.get("fields", []):
        size += len(str(field.get("name", ""))) + len(str(field.get("value", "")))
    return size


def notification_batches(
    notifications: Sequence[PendingNotification],
) -> Iterable[tuple[tuple[PendingNotification, ...], tuple[dict[str, Any], ...]]]:
    """Group cards within Discord's count and aggregate embed-size limits."""

    jobs: list[PendingNotification] = []
    embeds: list[dict[str, Any]] = []
    text_size = 0
    for notification in notifications:
        embed = notification_embed(notification)
        next_size = _embed_text_size(embed)
        if jobs and (len(jobs) >= _MAX_EMBEDS or text_size + next_size > _MAX_EMBED_TEXT):
            yield tuple(jobs), tuple(embeds)
            jobs, embeds, text_size = [], [], 0
        jobs.append(notification)
        embeds.append(embed)
        text_size += next_size
    if jobs:
        yield tuple(jobs), tuple(embeds)


class DiscordNotifier:
    """Synchronous webhook client suitable for a short launchd job."""

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self._webhook_url = str(httpx.URL(webhook_url).copy_merge_params({"wait": "true"}))
        self._retries = retries
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DiscordNotifier:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        delay = 1.0
        last_error = "unknown error"
        for attempt in range(self._retries + 1):
            try:
                response = self._client.post(
                    self._webhook_url,
                    json=payload,
                )
            except httpx.RequestError as exc:
                last_error = type(exc).__name__
                if attempt >= self._retries:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                return body if isinstance(body, dict) else {}

            if response.status_code in {401, 403, 404}:
                raise PermanentDiscordError(
                    f"Discord rejected the webhook with HTTP {response.status_code}."
                )

            last_error = f"HTTP {response.status_code}"
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    if retry_after is None:
                        response_body = response.json()
                        retry_after = str(
                            response_body.get("retry_after", delay)
                            if isinstance(response_body, dict)
                            else delay
                        )
                    delay = min(max(float(retry_after), 0.1), 120)
                except (TypeError, ValueError):
                    delay = min(delay * 2, 30)
            elif response.status_code >= 500:
                delay = min(delay * 2, 30)
            else:
                raise PermanentDiscordError(
                    f"Discord rejected the message with HTTP {response.status_code}."
                )

            if attempt < self._retries:
                time.sleep(delay)

        raise TransientDiscordError(f"Discord delivery failed after retries ({last_error}).")

    def send_test(self, *, route: NotificationRoute = NotificationRoute.SOFTWARE) -> str | None:
        response = self._post(
            {
                "content": (
                    "✅ **Job Notis is connected.** Future "
                    f"{'hardware' if route is NotificationRoute.HARDWARE else 'software'} "
                    "internships will appear here."
                ),
                "allowed_mentions": {"parse": []},
                "username": "Job Notis",
            }
        )
        message_id = response.get("id")
        return str(message_id) if message_id is not None else None

    def send_notifications(
        self, notifications: Sequence[PendingNotification]
    ) -> list[DeliveryReceipt]:
        return [
            self.send_notification_batch(jobs, embeds=embeds)
            for jobs, embeds in notification_batches(notifications)
        ]

    def send_notification_batch(
        self,
        notifications: Sequence[PendingNotification],
        *,
        embeds: Sequence[dict[str, Any]] | None = None,
        route: NotificationRoute = NotificationRoute.SOFTWARE,
    ) -> DeliveryReceipt:
        """Send one already-bounded batch so callers can checkpoint each receipt."""

        if not notifications:
            raise ValueError("notification batch cannot be empty")
        if len(notifications) > _MAX_EMBEDS:
            raise ValueError("notification batch cannot contain more than 10 jobs")
        rendered = (
            tuple(embeds)
            if embeds is not None
            else tuple(notification_embed(notification) for notification in notifications)
        )
        if len(rendered) != len(notifications):
            raise ValueError("each notification must have exactly one embed")
        if sum(_embed_text_size(embed) for embed in rendered) > _MAX_EMBED_TEXT:
            raise ValueError("notification batch exceeds the safe embed text limit")

        count = len(notifications)
        response = self._post(
            {
                "content": (
                    f"🚨 **{count} new "
                    f"{'hardware' if route is NotificationRoute.HARDWARE else 'SWE'} "
                    f"internship{'s' if count != 1 else ''}**"
                ),
                "embeds": list(rendered),
                "allowed_mentions": {"parse": []},
                "username": "Job Notis",
            }
        )
        message_id = response.get("id")
        receipt = DeliveryReceipt(
            notification_ids=tuple(job.id for job in notifications),
            message_id=str(message_id) if message_id is not None else None,
        )
        logger.info("Delivered %d %s internship alert(s) to Discord.", count, route.value)
        return receipt
