"""Async database engine and session factory.

Uses aiosqlite driver with SQLAlchemy 2.0 async sessionmaker.
Provides `get_db()` as a FastAPI dependency.
"""

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

connect_args = {}
if "sqlite" in settings.database_url:
    connect_args["check_same_thread"] = False

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=connect_args,
)


# Enable WAL mode for SQLite
@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    if "sqlite" in settings.database_url:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
