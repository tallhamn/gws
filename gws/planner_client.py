from __future__ import annotations

from typing import Optional, Protocol

from .config import Settings


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
    ) -> dict:
        ...


def build_planner_client(settings: Settings) -> PlannerClient:
    provider = settings.planner_provider
    if not provider:
        raise ValueError("planner_provider is required")
    if provider == "anthropic":
        from .providers.anthropic import AnthropicPlannerClient

        return AnthropicPlannerClient(api_key=settings.planner_api_key, model=settings.planner_model, timeout=settings.planner_timeout)
    raise ValueError(f"unsupported planner provider: {provider}")
