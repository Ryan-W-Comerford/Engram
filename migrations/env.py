import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make sure `shared` is importable when running Alembic from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    # MIGRATIONS_DATABASE_URL takes priority — use this to point Alembic at a
    # direct connection that bypasses PgBouncer / managed poolers (which don't
    # support DDL inside transactions).  Falls back to DATABASE_URL for local dev.
    return os.getenv(
        "MIGRATIONS_DATABASE_URL",
        os.getenv("DATABASE_URL", "postgresql://engram:engram_dev@localhost:5432/engram"),
    )


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
