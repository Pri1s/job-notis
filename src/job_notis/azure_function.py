"""Azure Functions orchestration around the existing one-shot check."""

from __future__ import annotations

import logging
from collections.abc import Callable

from job_notis.azure_state import (
    DEFAULT_CLOUD_ROOT,
    AzureBlobStateStore,
    StateLeaseUnavailable,
)
from job_notis.config import Config, load_config, redact_secret
from job_notis.paths import AppPaths, isolated_paths
from job_notis.service import CheckSummary

logger = logging.getLogger(__name__)


def run_timer_check(
    *,
    config_loader: Callable[[AppPaths], Config] = load_config,
    store_factory: Callable[[], AzureBlobStateStore] = AzureBlobStateStore.from_environment,
) -> CheckSummary | None:
    """Run one leased cloud check and return its execution summary."""

    config_paths = isolated_paths(DEFAULT_CLOUD_ROOT)
    config = config_loader(config_paths)
    store = store_factory()
    try:
        summary, status = store.run_check(config)
    except StateLeaseUnavailable:
        logger.warning("Skipped this timer occurrence because another invocation owns state.")
        return None

    logger.info(
        "Azure check complete: %d source(s), %d new job(s), %d delivered, %d pending; "
        "%d observations persisted.",
        summary.sources_checked,
        summary.new_global_jobs,
        summary.delivered_notifications,
        summary.pending_notifications,
        status.observation_count,
    )
    if summary.source_failures:
        for source, error in summary.source_failures:
            logger.warning("Source %s failed: %s", source, redact_secret(error))
    if summary.delivery_error:
        logger.warning("Discord delivery failed: %s", redact_secret(summary.delivery_error))
    return summary
