# devices 表 DDL（完整定义见 core/migrations/versions/0001_core.py）
#
# CREATE TABLE devices (
#   id            INTEGER PRIMARY KEY AUTOINCREMENT,
#   device_id     TEXT NOT NULL UNIQUE,   -- 客户端生成 UUID
#   token_hash    TEXT NOT NULL,          -- SHA-256(token)，不存明文
#   user_id       TEXT NOT NULL,          -- 单用户：恒为固定单用户 id
#   name          TEXT NOT NULL DEFAULT '',
#   created_at    TEXT NOT NULL,
#   last_active   TEXT,
#   revoked_at    TEXT
# );
