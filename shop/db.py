"""Database engines and the per-request session dependency."""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    elif url.startswith("postgresql"):
        connect_args["connect_timeout"] = 3  # fail fast instead of hanging requests
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10 if not url.startswith("sqlite") else 5,
        max_overflow=10,
        connect_args=connect_args,
    )


class Database:
    """Holds the real engine and the deliberately broken one used by the bad_config fault."""

    def __init__(self, url: str, bad_url: str) -> None:
        self.engine = make_engine(url)
        self._bad_url = bad_url
        self._bad_engine: Engine | None = None

    def bad_engine(self) -> Engine:
        if self._bad_engine is None:
            self._bad_engine = make_engine(self._bad_url)
        return self._bad_engine

    def session(self, broken: bool = False) -> Iterator[Session]:
        engine = self.bad_engine() if broken else self.engine
        with Session(engine, expire_on_commit=False) as session:
            yield session
