import pytest

from gws.db import Base, make_session_factory


@pytest.fixture()
def session():
    session_factory, engine = make_session_factory("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def worker_registry_path(tmp_path):
    path = tmp_path / "workers.yaml"
    path.write_text(
        """workers:
  - token: token-coder-1
    worker_id: coder-1
    lane: coder
    repo_access_set: ["repo-a", "repo-b"]
  - token: token-security-1
    worker_id: security-1
    lane: security-review
    repo_access_set: ["repo-a"]
""",
        encoding="utf-8",
    )
    return path
