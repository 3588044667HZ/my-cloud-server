"""core 库初始迁移：devices / app_config / pair_codes / file_refs / tasks。

使用 IF NOT EXISTS，保证全新与存量均可幂等执行。
"""
from alembic import op

revision = '0001_core'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id     TEXT NOT NULL UNIQUE,
            token_hash    TEXT NOT NULL,
            user_id       TEXT NOT NULL,
            name          TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            last_active   TEXT,
            revoked_at    TEXT
        )
    ''')
    op.execute('CREATE INDEX IF NOT EXISTS idx_devices_token ON devices(token_hash)')

    op.execute('''
        CREATE TABLE IF NOT EXISTS app_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')

    op.execute('''
        CREATE TABLE IF NOT EXISTS pair_codes (
            code       TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    op.execute('CREATE INDEX IF NOT EXISTS idx_pair_codes_used ON pair_codes(used)')

    op.execute('''
        CREATE TABLE IF NOT EXISTS file_refs (
            file_id    TEXT PRIMARY KEY,
            ref_count  INTEGER NOT NULL DEFAULT 0,
            size       INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')

    op.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,
            payload    TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            attempt    INTEGER NOT NULL DEFAULT 0,
            error      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    op.execute('CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)')


def downgrade():
    op.execute('DROP TABLE IF EXISTS tasks')
    op.execute('DROP TABLE IF EXISTS file_refs')
    op.execute('DROP TABLE IF EXISTS pair_codes')
    op.execute('DROP TABLE IF EXISTS app_config')
    op.execute('DROP TABLE IF EXISTS devices')
