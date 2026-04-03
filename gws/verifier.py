from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace

from .policy import PolicyEngine

logger = logging.getLogger(__name__)

_policy_cache: dict[str, PolicyEngine] = {}


def _get_policy_engine(policy_path: str) -> PolicyEngine:
    key = str(Path(policy_path).resolve()) if Path(policy_path).is_absolute() else policy_path
    if key not in _policy_cache:
        _policy_cache[key] = PolicyEngine.from_file(policy_path)
    return _policy_cache[key]


def verify_attempt(
    *,
    repo: str,
    touched_paths: list[str],
    changed_hunks: list[str],
    allowed_paths: list[str],
    forbidden_paths: list[str],
    policy_path: str = "policy.yaml",
):
    del repo

    normalized = []
    for path in touched_paths:
        clean = posixpath.normpath(path)
        if clean.startswith("/") or clean.startswith(".."):
            logger.warning("Invalid path rejected: %s", path)
            return SimpleNamespace(
                result="fail_and_replan",
                triggered_lanes=[],
                reasons=["invalid_path"],
            )
        normalized.append(clean)
    touched_paths = normalized

    if any(fnmatch(path, pattern) for path in touched_paths for pattern in forbidden_paths):
        logger.info("Forbidden path detected in touched_paths")
        return SimpleNamespace(
            result="fail_and_replan",
            triggered_lanes=[],
            reasons=["forbidden_path"],
        )

    allowed = bool(allowed_paths) and all(
        any(fnmatch(path, pattern) for pattern in allowed_paths) for path in touched_paths
    )
    if not allowed:
        return SimpleNamespace(
            result="fail_and_replan",
            triggered_lanes=[],
            reasons=["out_of_scope"],
        )

    policy_verdict = _get_policy_engine(policy_path).evaluate(
        touched_paths=touched_paths,
        changed_hunks=changed_hunks,
    )
    if policy_verdict.triggered_lanes:
        logger.info("Policy triggered lanes: %s", policy_verdict.triggered_lanes)
        return SimpleNamespace(
            result="append_governance_step",
            triggered_lanes=policy_verdict.triggered_lanes,
            reasons=["policy_trigger"],
        )

    logger.debug("Verification passed clean")
    return SimpleNamespace(
        result="pass",
        triggered_lanes=[],
        reasons=["clean"],
    )


@dataclass
class ArtifactVerdict:
    passed: bool
    results: list[dict] = field(default_factory=list)


async def verify_artifacts(
    *,
    requirements: list[str],
    gateway_url: str,
    repo: str,
    _gateway_call=None,
) -> ArtifactVerdict:
    if not requirements:
        return ArtifactVerdict(passed=True, results=[])

    results = []
    for req in requirements:
        if _gateway_call is not None:
            resp = await _gateway_call(requirement=req, repo=repo)
        else:
            import aiohttp

            async with aiohttp.ClientSession() as http_session:
                async with http_session.post(
                    f"{gateway_url}/verify",
                    json={"requirement": req, "repo": repo},
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as http_resp:
                    resp = await http_resp.json()

        passed = resp.get("exit_code", 1) == 0
        results.append({"requirement": req, "passed": passed, "output": resp.get("output", "")})

    all_passed = all(r["passed"] for r in results)
    return ArtifactVerdict(passed=all_passed, results=results)
