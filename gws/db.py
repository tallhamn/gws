from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str, *, pool_size: int = 10, pool_timeout: int = 30, pool_pre_ping: bool = True):
    url = make_url(database_url)
    engine_kwargs = {"future": True}

    if url.get_backend_name() == "sqlite":
        if url.database in {None, "", ":memory:"}:
            engine_kwargs["poolclass"] = StaticPool
            engine_kwargs["connect_args"] = {"check_same_thread": False}
    else:
        engine_kwargs["pool_size"] = pool_size
        engine_kwargs["pool_timeout"] = pool_timeout
        engine_kwargs["pool_pre_ping"] = pool_pre_ping

    engine = create_engine(database_url, **engine_kwargs)

    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record):
            del connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def make_session_factory(database_url: str, *, pool_size: int = 10, pool_timeout: int = 30, pool_pre_ping: bool = True):
    engine = make_engine(database_url, pool_size=pool_size, pool_timeout=pool_timeout, pool_pre_ping=pool_pre_ping)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True), engine
