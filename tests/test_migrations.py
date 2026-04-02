from pathlib import Path

from alembic import command
from alembic.config import Config


PRODUCTION_LIKE_PREDECESSOR_SCHEMA = """
CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL
);
INSERT INTO alembic_version (version_num) VALUES ('3d1fbc5097dc');

CREATE TABLE amendment_proposals (
    id INTEGER NOT NULL PRIMARY KEY,
    intent_id VARCHAR(128) NOT NULL,
    base_intent_version INTEGER NOT NULL,
    summary TEXT NOT NULL,
    amended_brief_text TEXT NOT NULL,
    is_breaking BOOLEAN NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    accepted_at DATETIME
);
CREATE INDEX ix_amendment_proposals_intent_id ON amendment_proposals (intent_id);

CREATE TABLE intent_versions (
    id INTEGER NOT NULL PRIMARY KEY,
    intent_id VARCHAR(128) NOT NULL,
    intent_version INTEGER NOT NULL,
    brief_text TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    planner_guidance TEXT NOT NULL DEFAULT '',
    accepted_amendments JSON NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_intent_versions_intent_id_intent_version UNIQUE (intent_id, intent_version)
);
CREATE INDEX ix_intent_versions_intent_id ON intent_versions (intent_id);

CREATE TABLE pull_requests (
    id INTEGER NOT NULL PRIMARY KEY,
    worker_id VARCHAR(128) NOT NULL,
    lane VARCHAR(64) NOT NULL,
    intent_id VARCHAR(128) NOT NULL,
    repo_access_set JSON NOT NULL,
    envelope JSON NOT NULL,
    repo_heads JSON NOT NULL,
    planning_result JSON NOT NULL,
    status VARCHAR(32) NOT NULL
);
CREATE INDEX ix_pull_requests_intent_id ON pull_requests (intent_id);
CREATE INDEX ix_pull_requests_lane ON pull_requests (lane);
CREATE INDEX ix_pull_requests_worker_id ON pull_requests (worker_id);

CREATE TABLE cases (
    id INTEGER NOT NULL PRIMARY KEY,
    intent_id VARCHAR(128) NOT NULL,
    intent_version INTEGER NOT NULL,
    title VARCHAR(255) NOT NULL,
    goal TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    workflow_status VARCHAR(32) NOT NULL,
    CONSTRAINT fk_cases_intent_versions FOREIGN KEY(intent_id, intent_version) REFERENCES intent_versions (intent_id, intent_version)
);
CREATE INDEX ix_cases_intent_id ON cases (intent_id);

CREATE TABLE steps (
    id INTEGER NOT NULL PRIMARY KEY,
    case_id INTEGER NOT NULL,
    repo VARCHAR(255) NOT NULL,
    lane VARCHAR(64) NOT NULL,
    step_type VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    allowed_paths JSON NOT NULL,
    forbidden_paths JSON NOT NULL,
    base_commit VARCHAR(64),
    target_branch VARCHAR(255),
    artifact_requirements JSON NOT NULL,
    FOREIGN KEY(case_id) REFERENCES cases (id)
);
CREATE INDEX ix_steps_lane ON steps (lane);
CREATE INDEX ix_steps_repo ON steps (repo);

CREATE TABLE leases (
    id INTEGER NOT NULL PRIMARY KEY,
    step_id INTEGER NOT NULL,
    worker_id VARCHAR(128) NOT NULL,
    lane VARCHAR(64) NOT NULL,
    issued_at DATETIME NOT NULL,
    heartbeat_deadline DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    expired_at DATETIME,
    base_commit VARCHAR(64),
    FOREIGN KEY(step_id) REFERENCES steps (id)
);
CREATE INDEX ix_leases_expires_at ON leases (expires_at);
CREATE INDEX ix_leases_heartbeat_deadline ON leases (heartbeat_deadline);
CREATE INDEX ix_leases_lane ON leases (lane);
CREATE INDEX ix_leases_step_id ON leases (step_id);
CREATE INDEX ix_leases_worker_id ON leases (worker_id);
CREATE UNIQUE INDEX uq_leases_active_step_id ON leases (step_id) WHERE expired_at IS NULL;

CREATE TABLE attempts (
    id INTEGER NOT NULL PRIMARY KEY,
    step_id INTEGER NOT NULL,
    lease_id INTEGER NOT NULL,
    worker_id VARCHAR(128) NOT NULL,
    repo VARCHAR(255) NOT NULL,
    result_status VARCHAR(32) NOT NULL,
    artifact_refs JSON NOT NULL,
    submitted_diff_ref VARCHAR(255),
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_attempts_lease_id UNIQUE (lease_id),
    FOREIGN KEY(lease_id) REFERENCES leases (id),
    FOREIGN KEY(step_id) REFERENCES steps (id)
);
CREATE INDEX ix_attempts_repo ON attempts (repo);
CREATE INDEX ix_attempts_step_id ON attempts (step_id);
CREATE INDEX ix_attempts_worker_id ON attempts (worker_id);

CREATE TABLE verdicts (
    id INTEGER NOT NULL PRIMARY KEY,
    attempt_id INTEGER NOT NULL,
    result VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(attempt_id) REFERENCES attempts (id)
);
CREATE INDEX ix_verdicts_attempt_id ON verdicts (attempt_id);
"""


def _run_sqlite_script(database_path: Path, script: str) -> None:
    import sqlite3

    conn = sqlite3.connect(database_path)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


def test_work_item_cutover_migrates_production_like_predecessor_schema(tmp_path):
    database_path = tmp_path / "gws-prod-like.db"
    _run_sqlite_script(database_path, PRODUCTION_LIKE_PREDECESSOR_SCHEMA)

    config = Config("/Users/marcus/Documents/Github/gws/alembic.ini")
    config.set_main_option("script_location", "/Users/marcus/Documents/Github/gws/alembic")

    import os

    previous_database_url = os.environ.get("GWS_DATABASE_URL")
    os.environ["GWS_DATABASE_URL"] = f"sqlite+pysqlite:///{database_path}"
    try:
        command.upgrade(config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("GWS_DATABASE_URL", None)
        else:
            os.environ["GWS_DATABASE_URL"] = previous_database_url

    import sqlite3

    conn = sqlite3.connect(database_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "outcomes" in tables
        assert "work_items" in tables
        assert "cases" not in tables
        assert "steps" not in tables

        lease_columns = [row[1] for row in conn.execute("PRAGMA table_info(leases)")]
        attempt_columns = [row[1] for row in conn.execute("PRAGMA table_info(attempts)")]
        assert "work_item_id" in lease_columns
        assert "step_id" not in lease_columns
        assert "work_item_id" in attempt_columns
        assert "step_id" not in attempt_columns
    finally:
        conn.close()
