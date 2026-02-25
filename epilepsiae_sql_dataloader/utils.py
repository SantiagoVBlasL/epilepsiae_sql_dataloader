import os
from contextlib import contextmanager
from sqlalchemy.orm import sessionmaker

DEFAULT_PGURL = "postgresql://epilepsiae:epilepsiae@localhost:5432/epilepsiae"

def _normalize_pgurl(pgurl: str) -> str:
    # SQLAlchemy acepta postgresql:// pero en algunos setups preferimos explicitar driver
    if pgurl.startswith("postgresql+"):
        return pgurl
    if pgurl.startswith("postgresql://"):
        return pgurl.replace("postgresql://", "postgresql+psycopg2://", 1)
    return pgurl  # fallback

PGURL = os.environ.get("PGURL") or os.environ.get("DATABASE_URL") or DEFAULT_PGURL
ENGINE_STR = _normalize_pgurl(PGURL)

@contextmanager
def session_scope(engine_str: str = ENGINE_STR):
    """Provide a transactional scope around a series of operations."""
    from sqlalchemy import create_engine
    engine = create_engine(engine_str)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

