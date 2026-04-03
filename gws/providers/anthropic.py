from __future__ import annotations

import json
import logging
import time
from typing import Optional

from gws.contracts import PlannerResult, SynthesizedPlan
from gws.providers.common import DEFAULT_CLAUDE_MODEL, build_system_prompt, parse_synthesized_plan_text

try:
    import anthropic
except ImportError:
    anthropic = None

logger = logging.getLogger(__name__)


class AnthropicPlannerClient:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, timeout: float = 60.0):
        if anthropic is None:
            raise RuntimeError("anthropic package is required to use AnthropicPlannerClient")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or DEFAULT_CLAUDE_MODEL
        self.timeout = timeout

    @staticmethod
    def _parse_response(message) -> SynthesizedPlan | PlannerResult:
        if not getattr(message, "content", None):
            raise ValueError("planner response did not contain JSON text")

        text = None
        for content_block in message.content:
            candidate = getattr(content_block, "text", None)
            if not isinstance(candidate, str) and isinstance(content_block, dict):
                candidate = content_block.get("text")
            if not isinstance(candidate, str) and hasattr(content_block, "model_dump"):
                payload = content_block.model_dump()
                if isinstance(payload, dict):
                    candidate = payload.get("text")
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
                break
        if text is None:
            raise ValueError("planner response did not contain JSON text")

        return parse_synthesized_plan_text(text)

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
    ) -> SynthesizedPlan | PlannerResult:
        system_prompt = build_system_prompt(
            lane_capabilities=lane_capabilities,
            intent_context=intent_context,
            planner_guidance=planner_guidance,
        )
        user_data = json.dumps(
            {"brief": brief, "lane": lane, "repo_heads": repo_heads, "envelope": envelope},
            indent=2,
        )
        last_exc = None
        for attempt in range(3):
            try:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=512,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_data}],
                    timeout=self.timeout,
                )
                return self._parse_response(message)
            except ValueError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    wait = 2**attempt
                    logger.warning("Planner API attempt %d failed: %s. Retrying in %ds...", attempt + 1, exc, wait)
                    time.sleep(wait)
        raise RuntimeError("Planner API failed after 3 attempts") from last_exc
