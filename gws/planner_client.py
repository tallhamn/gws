from __future__ import annotations

from typing import Protocol

from .config import Settings


class PlannerClient(Protocol):
    def synthesize(self, *, brief: str, lane: str, repo_heads: dict[str, str], envelope: dict) -> dict:
        ...


def build_planner_client(settings: Settings) -> PlannerClient:
    provider = settings.planner_provider
    if not provider:
        raise ValueError("planner_provider is required")
    if provider == "anthropic":
        from .providers.anthropic import AnthropicPlannerClient

        return AnthropicPlannerClient(api_key=settings.planner_api_key, model=settings.planner_model, timeout=settings.planner_timeout)
    raise ValueError(f"unsupported planner provider: {provider}")
