from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
import os
from urllib.parse import urlparse

from app.config import get_settings
from app.models import Base

settings = get_settings()

def _is_valid_port(value: str) -> bool:
    """Check if value is a valid port number (not a URL)."""
    if not value or "://" in value or len(value) > 5:
        return False
    return value.isdigit() and 1 <= int(value) <= 65535


def get_async_database_url() -> str:
    """Convert standard postgres URL to asyncpg URL."""
    # Check if PGHOST or PGPORT was accidentally set to full URL (Railway copy-paste)
    pghost = os.environ.get("PGHOST") or os.environ.get("PGHOST_PUBLIC")
    pgport = os.environ.get("PGPORT") or os.environ.get("PGPORT_PUBLIC")
    if pghost and "://" in pghost and urlparse(pghost).port:
        url = pghost  # PGHOST contains full URL
    elif pgport and "://" in pgport and urlparse(pgport).port:
        url = pgport  # PGPORT contains full URL
    elif pghost and _is_valid_port(pgport or ""):
        # Prefer PGHOST+PGPORT for local dev (public URL) over DATABASE_URL (often private)
        user = os.environ.get("PGUSER", "postgres")
        password = os.environ.get("PGPASSWORD")
        database = os.environ.get("PGDATABASE", "railway")
        if password and "://" not in pghost:
            url = f"postgresql://{user}:{password}@{pghost}:{pgport}/{database}"
        else:
            url = (
                os.environ.get("DATABASE_PUBLIC_URL")
                or os.environ.get("DATABASE_URL")
                or settings.database_url
            )
    else:
        url = (
            os.environ.get("DATABASE_PUBLIC_URL")
            or os.environ.get("DATABASE_URL")
            or settings.database_url
        )
    # Build from components if URL is malformed (e.g. Railway URL with empty host/port)
    parsed = urlparse(url) if url else None
    if not url or not url.strip() or (parsed and parsed.scheme and not parsed.port):
        host = pghost if pghost and "://" not in pghost else None
        port = pgport if _is_valid_port(pgport or "") else None
        user = os.environ.get("PGUSER", "postgres")
        password = os.environ.get("PGPASSWORD")
        database = os.environ.get("PGDATABASE", "railway")
        if host and port and password:
            url = f"postgresql://{user}:{password}@{host}:{port}/{database}"
        elif not url or not url.strip():
            raise ValueError(
                "DATABASE_URL is empty. Set it in .config/secrets.env. "
                "For Railway: copy DATABASE_PUBLIC_URL, or PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE from the Variables tab."
            )
        else:
            raise ValueError(
                "DATABASE_URL is missing host/port. For Railway: copy PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE "
                "from the pgvector service Variables tab into .config/secrets.env"
            )
    # Railway provides postgresql:// but asyncpg needs postgresql+asyncpg://
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url

engine = create_async_engine(
    get_async_database_url(),
    echo=False,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db():
    """Initialize database and create tables."""
    async with engine.begin() as conn:
        # Enable pgvector extension
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # Create all tables
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """Dependency for getting database sessions."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
