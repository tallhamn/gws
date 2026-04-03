from __future__ import annotations

from typing import Optional, Protocol

from .config import Settings
from .contracts import PlannerResult, SynthesizedPlan
from .providers.claude_code import ClaudeCodePlannerClient


class PlannerClient(Protocol):
    def synthesize(
        self,
        *,
        brief: str,
        lane: str,
        repo_heads: dict[str, str],
        envelope: dict,
        lane_capabilities: Optional[dict[str, str]] = None,
        intent_context: Optional[str] = None,
        planner_guidance: Optional[str] = None,
    ) -> SynthesizedPlan | PlannerResult: ...


def resolve_planner_provider(settings: Settings) -> str:
    if settings.planner_provider:
        return settings.planner_provider
    if ClaudeCodePlannerClient.is_available(settings.planner_command):
        return "claude_code"
    if settings.planner_api_key:
        return "anthropic"
    raise ValueError("no planner provider available")


def build_planner_client(settings: Settings) -> PlannerClient:
    provider = resolve_planner_provider(settings)
    if provider == "claude_code":
        return ClaudeCodePlannerClient(
            command=settings.planner_command,
            model=settings.planner_model,
            effort=settings.planner_effort,
            timeout=settings.planner_timeout,
        )
    if provider == "anthropic":
        from .providers.anthropic import AnthropicPlannerClient

        return AnthropicPlannerClient(
            api_key=settings.planner_api_key, model=settings.planner_model, timeout=settings.planner_timeout
        )
    raise ValueError(f"unsupported planner provider: {provider}")
