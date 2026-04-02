import json

from gws.providers.claude_code import ClaudeCodePlannerClient


def test_claude_code_planner_client_invokes_cli_with_plan_mode(monkeypatch):
    captured = {}

    class FakeCompletedProcess:
        def __init__(self):
            self.stdout = json.dumps(
                {
                    "title": "Build player movement",
                    "goal": "Implement movement controls",
                    "repo": "repo-a",
                    "allowed_paths": ["src/**"],
                    "forbidden_paths": [],
                    "step_type": "execute",
                }
            )

    def fake_run(args, *, capture_output, text, timeout, check):
        captured["args"] = args
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["timeout"] = timeout
        captured["check"] = check
        return FakeCompletedProcess()

    monkeypatch.setattr("gws.providers.claude_code.subprocess.run", fake_run)

    client = ClaudeCodePlannerClient(command="claude", model="sonnet", effort="max", timeout=12.5)
    result = client.synthesize(
        brief="Build a platformer",
        lane="coder",
        repo_heads={"repo-a": "abc123"},
        envelope={"max_runtime": 1},
        lane_capabilities={"coder": "Write game code."},
        intent_context="Browser game. HTML/CSS/JS.",
        planner_guidance="Core loop first.",
    )

    assert result.repo == "repo-a"
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["timeout"] == 12.5
    assert captured["check"] is True
    assert captured["args"][:8] == [
        "claude",
        "-p",
        captured["args"][2],
        "--permission-mode",
        "plan",
        "--tools",
        "",
        "--output-format",
    ]
    assert captured["args"][8] == "text"
    assert "--effort" in captured["args"]
    assert "--system-prompt" in captured["args"]
    assert "--model" in captured["args"]


def test_claude_code_planner_client_reports_missing_binary(monkeypatch):
    monkeypatch.setattr("gws.providers.claude_code.shutil.which", lambda command: None)

    assert ClaudeCodePlannerClient.is_available("claude") is False
