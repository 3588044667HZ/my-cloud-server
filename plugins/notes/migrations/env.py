"""alembic 环境：目标库 URL 由 core/db.run_migrations 注入配置（sqlalchemy.url）。"""
from alembic import context
from sqlalchemy import create_engine

config = context.config
target_metadata = None


def run_migrations_offline():
    context.configure(url=config.get_main_option('sqlalchemy.url'), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    url = config.get_main_option('sqlalchemy.url')
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
