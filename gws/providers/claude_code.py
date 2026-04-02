from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Optional

from gws.contracts import SynthesizedPlan
from gws.providers.common import DEFAULT_CLAUDE_MODEL, build_system_prompt, parse_synthesized_plan_text

logger = logging.getLogger(__name__)


class ClaudeCodePlannerClient:
    def __init__(
        self,
        *,
        command: str = "claude",
        model: Optional[str] = None,
        effort: str = "max",
        timeout: float = 60.0,
    ):
        self.command = command
        self.model = model or DEFAULT_CLAUDE_MODEL
        self.effort = effort
        self.timeout = timeout

    @staticmethod
    def is_available(command: str = "claude") -> bool:
        return shutil.which(command) is not None

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
    ) -> SynthesizedPlan:
        if not self.is_available(self.command):
            raise RuntimeError(f"Claude Code command not found: {self.command}")

        system_prompt = build_system_prompt(
            lane_capabilities=lane_capabilities,
            intent_context=intent_context,
            planner_guidance=planner_guidance,
        )
        user_data = json.dumps(
            {"brief": brief, "lane": lane, "repo_heads": repo_heads, "envelope": envelope},
            indent=2,
        )
        args = [
            self.command,
            "-p",
            user_data,
            "--permission-mode",
            "plan",
            "--tools",
            "",
            "--output-format",
            "text",
            "--effort",
            self.effort,
            "--system-prompt",
            system_prompt,
            "--model",
            self.model,
        ]

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Claude Code command not found: {self.command}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            if detail:
                logger.warning("Claude Code planner failed: %s", detail)
                raise RuntimeError(f"Claude Code planner failed: {detail}") from exc
            raise RuntimeError("Claude Code planner failed") from exc

        return parse_synthesized_plan_text(completed.stdout.strip())
