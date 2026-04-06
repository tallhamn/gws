"""Startup canary test — verify the planner model can produce valid JSON."""

from __future__ import annotations

import logging

from .config import Settings
from .contracts import PlannerResult, SynthesizedPlan
from .planner_client import build_planner_client

logger = logging.getLogger(__name__)

_CANARY_BRIEF = "Build a test game"
_CANARY_REPO_HEADS = {"canary-repo": "0000000"}
_CANARY_ENVELOPE = {}


def run_planner_canary(settings: Settings) -> None:
    """Send a test prompt to the planner model and verify it returns valid JSON.

    Raises RuntimeError if the model cannot produce a parseable response.
    Skips silently if no planner provider is available.
    """
    try:
        client = build_planner_client(settings)
    except ValueError:
        logger.info("Planner canary skipped — no planner provider available")
        return
    logger.info(
        "Running planner canary test (provider=%s, model=%s)",
        settings.planner_provider,
        settings.planner_model,
    )

    try:
        result = client.synthesize(
            brief=_CANARY_BRIEF,
            lane="coder",
            repo_heads=_CANARY_REPO_HEADS,
            envelope=_CANARY_ENVELOPE,
            repo_trees={},
        )
    except Exception as exc:
        raise RuntimeError(
            f"Planner canary FAILED: model '{settings.planner_model}' could not produce a response. Error: {exc}"
        ) from exc

    if isinstance(result, PlannerResult):
        logger.info("Planner canary passed (returned %s)", result.value)
        return

    if isinstance(result, SynthesizedPlan):
        logger.info("Planner canary passed (returned plan: %s)", result.title[:50])
        return

    raise RuntimeError(
        f"Planner canary FAILED: model '{settings.planner_model}' returned unexpected type {type(result)}"
    )
