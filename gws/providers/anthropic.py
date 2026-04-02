from __future__ import annotations

import json
from collections.abc import Mapping

try:
    import anthropic
except ImportError:  # pragma: no cover - exercised only when dependency is absent
    anthropic = None


class AnthropicPlannerClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        if anthropic is None:
            raise RuntimeError("anthropic package is required to use AnthropicPlannerClient")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or "claude-sonnet-4-20250514"

    @staticmethod
    def _parse_response(message) -> dict:
        if not getattr(message, "content", None):
            raise ValueError("planner response did not contain JSON text")

        content_block = message.content[0]
        text = getattr(content_block, "text", None)
        if not isinstance(text, str):
            raise ValueError("planner response did not contain JSON text")

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("planner response was not valid JSON") from exc

        if not isinstance(parsed, Mapping):
            raise ValueError("planner response JSON must be an object")

        return dict(parsed)

    def synthesize(self, *, brief: str, lane: str, repo_heads: dict[str, str], envelope: dict) -> dict:
        prompt = (
            "You are a planning engine for Governed Work Synthesis.\n"
            f"Brief:\n{brief}\n\n"
            f"Lane: {lane}\n"
            f"Repo heads: {repo_heads}\n"
            f"Envelope: {envelope}\n\n"
            "Return JSON with keys:\n"
            "- title\n"
            "- goal\n"
            "- repo\n"
            "- allowed_paths\n"
            "- forbidden_paths\n"
            "- step_type"
        )
        message = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return self._parse_response(message)
