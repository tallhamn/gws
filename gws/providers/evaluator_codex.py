from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from gws.contracts import EvaluationResult
from gws.providers.common import build_evaluation_prompt, parse_evaluation_output

logger = logging.getLogger(__name__)


class CodexEvaluator:
    """Evaluates repo state against an intent using Codex CLI with Ollama."""

    def __init__(
        self,
        *,
        command: str = "codex",
        model: Optional[str] = None,
        timeout: float = 120.0,
        ollama_host: Optional[str] = None,
    ):
        self.command = command
        self.model = model or "qwen-coder-fast:latest"
        self.timeout = timeout
        self.ollama_host = ollama_host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

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
        prompt = build_evaluation_prompt(
            brief=brief,
            repo_trees=repo_trees,
            envelope=envelope,
            intent_context=intent_context,
        )

        fd, output_path = tempfile.mkstemp(prefix="gws-eval-", suffix=".txt")
        os.close(fd)

        openai_base_url = self.ollama_host.rstrip("/")
        if not openai_base_url.endswith("/v1"):
            openai_base_url = f"{openai_base_url}/v1"

        env = {
            **os.environ,
            "OLLAMA_HOST": self.ollama_host,
            "CODEX_OSS_BASE_URL": openai_base_url,
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        env.pop("CODEX_HOME", None)

        args = [
            self.command,
            "exec",
            "--oss",
            "--local-provider",
            "ollama",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            repo_path,
            "-o",
            output_path,
            "--model",
            self.model,
            prompt,
        ]

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=repo_path,
                env=env,
            )
        except FileNotFoundError:
            raise RuntimeError(f"Codex command not found: {self.command}")
        except subprocess.TimeoutExpired:
            logger.warning("Codex evaluator timed out after %.0fs for repo %s", self.timeout, repo)
            return EvaluationResult(
                satisfied=False,
                findings=f"Evaluation timed out after {self.timeout}s",
                files_examined=[],
            )

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            logger.warning("Codex evaluator exited %d for repo %s: %s", completed.returncode, repo, detail)
            return EvaluationResult(
                satisfied=False,
                findings=f"Evaluation failed (exit {completed.returncode}): {detail}",
                files_examined=[],
            )

        try:
            output_text = Path(output_path).read_text().strip()
        except FileNotFoundError:
            output_text = (completed.stdout or "").strip()
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass

        if not output_text:
            output_text = (completed.stdout or "").strip()

        logger.info("Codex evaluator result for repo %s (%d chars)", repo, len(output_text))
        return parse_evaluation_output(output_text)
