from gws.policy import PolicyEngine


def test_path_trigger_appends_security_review():
    engine = PolicyEngine.from_file("policy.yaml")

    verdict = engine.evaluate(
        touched_paths=["auth/session.py"],
        changed_hunks=[
            "-allow_all = True",
            "+allow_all = False",
        ],
    )

    assert verdict.triggered_lanes == ["security-review"]


def test_content_trigger_appends_security_review():
    engine = PolicyEngine.from_file("policy.yaml")

    verdict = engine.evaluate(
        touched_paths=["services/music/player.py"],
        changed_hunks=["+issuer = 'https://sso.example.com'"],
    )

    assert verdict.triggered_lanes == ["security-review"]


def test_policy_engine_loads_default_policy_independent_of_cwd(monkeypatch, tmp_path):
    (tmp_path / "policy.yaml").write_text("path_triggers: []\ncontent_triggers: []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    engine = PolicyEngine.from_file("policy.yaml")
    verdict = engine.evaluate(
        touched_paths=["auth/session.py"],
        changed_hunks=[],
    )

    assert verdict.triggered_lanes == ["security-review"]


def test_lane_capabilities_parsed():
    engine = PolicyEngine(
        {
            "lanes": {
                "coder": {
                    "lease_ttl_seconds": 900,
                    "capabilities": "Write and modify game code.",
                },
                "artist": {
                    "lease_ttl_seconds": 900,
                    "capabilities": "Create visual assets.",
                },
            },
        }
    )
    caps = engine.lane_capabilities()
    assert caps == {
        "coder": "Write and modify game code.",
        "artist": "Create visual assets.",
    }


def test_lane_capabilities_empty_when_missing():
    engine = PolicyEngine({"lanes": {"coder": {"lease_ttl_seconds": 900}}})
    caps = engine.lane_capabilities()
    assert caps == {"coder": ""}
