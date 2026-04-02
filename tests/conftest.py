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
