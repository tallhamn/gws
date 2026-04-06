from __future__ import annotations

import logging
import os
from typing import Optional, Protocol

from .config import Settings
from .contracts import EvaluationResult

logger = logging.getLogger(__name__)


class RepoEvaluator(Protocol):
    def evaluate(
        self,
        *,
        brief: str,
        repo: str,
        repo_path: str,
        repo_trees: list[str],
        envelope: dict,
        intent_context: Optional[str] = None,
    ) -> EvaluationResult: ...


def resolve_repo_path(source_repos_root: str, repo: str) -> str | None:
    """Map repo identifier to filesystem path. Returns None if the directory doesn't exist."""
    candidate = os.path.join(source_repos_root, repo)
    if os.path.isdir(candidate):
        return candidate
    return None


def build_evaluator(settings: Settings) -> RepoEvaluator | None:
    """Build an evaluator from settings. Returns None when source_repos_root is not configured."""
    if not settings.source_repos_root:
        return None

    if not os.path.isdir(settings.source_repos_root):
        raise RuntimeError(
            f"GWS_SOURCE_REPOS_ROOT is set to '{settings.source_repos_root}' but the directory does not exist. "
            "GWS cannot evaluate intent satisfaction without repo access."
        )

    provider = settings.evaluator_provider
    model = settings.evaluator_model or settings.planner_model

    if provider == "codex":
        from .providers.evaluator_codex import CodexEvaluator

        return CodexEvaluator(
            command=settings.evaluator_command,
            model=model,
            timeout=settings.evaluator_timeout,
        )

    if provider == "claude_code":
        from .providers.evaluator_claude_code import ClaudeCodeEvaluator

        return ClaudeCodeEvaluator(
            command=settings.planner_command,
            model=model,
            timeout=settings.evaluator_timeout,
        )

    raise ValueError(f"unsupported evaluator provider: {provider}")
