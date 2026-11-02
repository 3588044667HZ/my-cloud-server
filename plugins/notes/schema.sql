-- notes 插件表结构（文档参考；实际建表由 migrations/versions/0001_initial.py 执行）
-- 与 mock-server data/notes.db 原表结构一致，迁移零改动

CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#4A90D9',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    notebook_id TEXT,
    color TEXT NOT NULL DEFAULT 'blue',
    is_pinned INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS share_settings (
    user_id INTEGER PRIMARY KEY,
    logo_text TEXT NOT NULL DEFAULT '分享来自 Open Note',
    watermark TEXT NOT NULL DEFAULT '备忘录'
);

CREATE TABLE IF NOT EXISTS attachments (
    attach_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    note_id TEXT NOT NULL,
    type INTEGER NOT NULL DEFAULT 0,
    file_name TEXT NOT NULL DEFAULT '',
    file_id TEXT NOT NULL,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    md5 TEXT NOT NULL DEFAULT '',
    state INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
