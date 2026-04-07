from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Optional

from gws.contracts import EvaluationResult
from gws.providers.common import build_evaluation_prompt, parse_evaluation_output

logger = logging.getLogger(__name__)


class ClaudeCodeEvaluator:
    """Evaluates repo state against an intent using Claude Code CLI with tool access."""

    def __init__(
        self,
        *,
        command: str = "claude",
        model: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.command = command
        self.model = model or "claude-sonnet-4-20250514"
        self.timeout = timeout

    @staticmethod
    def is_available(command: str = "claude") -> bool:
        return shutil.which(command) is not None

    def evaluate(
        self,
        *,
        brief: str,
        repo: str,
        repo_path: str,
        repo_trees: list[str],
        envelope: dict,
        intent_context: Optional[str] = None,
    ) -> EvaluationResult:
        if not self.is_available(self.command):
            raise RuntimeError(f"Claude Code command not found: {self.command}")

        prompt = build_evaluation_prompt(
            brief=brief,
            repo_trees=repo_trees,
            envelope=envelope,
            intent_context=intent_context,
        )

        args = [
            self.command,
            "-p",
            prompt,
            "--permission-mode",
            "plan",
            "--output-format",
            "text",
            "--model",
            self.model,
        ]

        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=repo_path,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                import os
                import signal

                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
                logger.warning("Claude Code evaluator timed out after %.0fs for repo %s", self.timeout, repo)
                return EvaluationResult(
                    satisfied=False,
                    findings=f"Evaluation timed out after {self.timeout}s",
                    files_examined=[],
                )
            if proc.returncode != 0:
                detail = (stderr or stdout or "").strip()
                logger.warning("Claude Code evaluator failed for repo %s: %s", repo, detail)
                return EvaluationResult(
                    satisfied=False,
                    findings=f"Evaluation failed: {detail}",
                    files_examined=[],
                )
        except FileNotFoundError:
            raise RuntimeError(f"Claude Code command not found: {self.command}")

        output_text = stdout.strip()
        logger.info("Claude Code evaluator result for repo %s (%d chars)", repo, len(output_text))
        return parse_evaluation_output(output_text)
