from __future__ import annotations

import subprocess


def snapshot_repo_head(path: str) -> str:
    return subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True, timeout=30).strip()


def changed_hunks(base: str, head: str, repo_path: str) -> list[str]:
    text = subprocess.check_output(["git", "-C", repo_path, "diff", "--unified=0", base, head], text=True, timeout=30)
    lines: list[str] = []
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+") or line.startswith("-"):
            lines.append(line)
    return lines
