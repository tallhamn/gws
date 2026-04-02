from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import yaml


@dataclass
class PolicyVerdict:
    triggered_lanes: list[str]


class PolicyEngine:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def from_file(cls, path: str) -> "PolicyEngine":
        policy_path = Path(path)
        if not policy_path.is_absolute():
            policy_path = Path(__file__).resolve().parent.parent / policy_path

        with policy_path.open(encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    def evaluate(
        self,
        *,
        touched_paths: list[str],
        changed_hunks: list[str],
    ) -> PolicyVerdict:
        lanes: set[str] = set()

        for rule in self.data.get("path_triggers", []):
            if any(
                fnmatch(path, pattern)
                for path in touched_paths
                for pattern in rule["patterns"]
            ):
                lanes.add(rule["lane"])

        haystack = "\n".join(changed_hunks).lower()
        for rule in self.data.get("content_triggers", []):
            if any(pattern.lower() in haystack for pattern in rule["patterns"]):
                lanes.add(rule["lane"])

        return PolicyVerdict(triggered_lanes=sorted(lanes))
