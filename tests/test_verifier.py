from gws.verifier import verify_attempt


def test_verifier_hard_fails_for_forbidden_path():
    verdict = verify_attempt(
        repo="repo-a",
        touched_paths=["infra/prod.tf"],
        changed_hunks=["+resource aws_kms_key main {}"],
        allowed_paths=["services/**"],
        forbidden_paths=["infra/**"],
    )

    assert verdict.result == "fail_and_replan"
    assert verdict.triggered_lanes == []
    assert verdict.reasons == ["forbidden_path"]


def test_verifier_fails_out_of_scope_changes():
    verdict = verify_attempt(
        repo="repo-a",
        touched_paths=["docs/architecture.md"],
        changed_hunks=["+new section"],
        allowed_paths=["services/**"],
        forbidden_paths=["infra/**"],
    )

    assert verdict.result == "fail_and_replan"
    assert verdict.triggered_lanes == []
    assert verdict.reasons == ["out_of_scope"]


def test_verifier_fails_closed_when_allowed_paths_are_empty():
    verdict = verify_attempt(
        repo="repo-a",
        touched_paths=["services/music/player.py"],
        changed_hunks=["+return play(queue)"],
        allowed_paths=[],
        forbidden_paths=["infra/**"],
    )

    assert verdict.result == "fail_and_replan"
    assert verdict.triggered_lanes == []
    assert verdict.reasons == ["out_of_scope"]


def test_verifier_appends_governance_when_policy_triggers_match():
    verdict = verify_attempt(
        repo="repo-a",
        touched_paths=["services/music/auth/session.py"],
        changed_hunks=["+issuer = 'https://sso.example.com'"],
        allowed_paths=["services/**"],
        forbidden_paths=["infra/**"],
    )

    assert verdict.result == "append_governance_step"
    assert verdict.triggered_lanes == ["security-review"]
    assert verdict.reasons == ["policy_trigger"]


def test_verifier_passes_clean_in_scope_changes():
    verdict = verify_attempt(
        repo="repo-a",
        touched_paths=["services/music/player.py"],
        changed_hunks=["+return play(queue)"],
        allowed_paths=["services/**"],
        forbidden_paths=["infra/**"],
    )

    assert verdict.result == "pass"
    assert verdict.triggered_lanes == []
    assert verdict.reasons == ["clean"]


def test_verifier_rejects_path_traversal_attempt():
    verdict = verify_attempt(
        repo="repo-a",
        touched_paths=["services/../infra/prod.tf"],
        changed_hunks=["+resource aws_kms_key main {}"],
        allowed_paths=["services/**"],
        forbidden_paths=["infra/**"],
    )

    assert verdict.result == "fail_and_replan"
    assert "forbidden_path" in verdict.reasons or "out_of_scope" in verdict.reasons or "invalid_path" in verdict.reasons


def test_verifier_rejects_absolute_paths():
    verdict = verify_attempt(
        repo="repo-a",
        touched_paths=["/etc/passwd"],
        changed_hunks=["+malicious"],
        allowed_paths=["services/**"],
        forbidden_paths=[],
    )

    assert verdict.result == "fail_and_replan"
    assert "invalid_path" in verdict.reasons
