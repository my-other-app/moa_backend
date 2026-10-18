import logging
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from app.config import settings
from app.db.base import AbstractSQLModel
from app.db.migration_session import Guard
from app.db.registry import *

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = AbstractSQLModel.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.

config.set_main_option("sqlalchemy.url", settings.DATABASE_URL_SYNC)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode, guarded for a live database.

    Production migrates while the API serves from the same database, so:

    - Each migration runs in its own transaction (transaction_per_migration).
      It used to be one transaction for every pending migration, so a lock
      taken by an early ALTER TABLE on a hot table was held until the last
      migration committed -- or until a later migration's autocommit block
      committed it -- and every request touching that table waited the whole
      time. Now a migration's locks go when it commits, and a failure leaves
      the ones before it applied and stamped.
    - The session has a lock_timeout, a statement_timeout and an
      idle_in_transaction_session_timeout (app/db/migration_session.py says
      why each; the settings are APP_MIGRATION_* in app/config.py). An index
      built CONCURRENTLY is exempt from the statement limit: it blocks no one,
      and killing it half way leaves an INVALID index.
    - A migration whose lock is not granted in time is rolled back and tried
      again on a fresh connection, a few times with a growing pause, then the
      run fails loudly. The rollback releases everything it held, so the
      requests that queued behind the waiting ALTER go through while we pause.
    """
    guard = Guard.from_settings(settings)
    logging.getLogger("alembic.env").info("Migration guard: %s", guard.describe())

    def attempt() -> None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
            connect_args=guard.connect_args(),
        )
        guard.install(connectable)
        try:
            with connectable.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    transaction_per_migration=True,
                    on_version_apply=guard.on_version_apply,
                )

                with context.begin_transaction():
                    context.run_migrations()
        finally:
            connectable.dispose()

    guard.run(attempt)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
