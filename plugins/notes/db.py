"""notes 插件共享：独立库连接 + 行→dict 转换（沿用 mock-server app/database.py 逻辑）。"""
import os

from config import DATA_DIR
from core.db import get_conn

NOTES_DB = os.path.join(DATA_DIR, 'notes.db')


def get_notes_db():
    return get_conn(NOTES_DB)


def note_row_to_dict(row):
    return {
        'id': row['id'],
        'title': row['title'],
        'content': row['content'],
        'notebookId': row['notebook_id'],
        'color': row['color'],
        'isPinned': bool(row['is_pinned']),
        'version': row['version'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
        'deletedAt': row['deleted_at'],
    }


def notebook_row_to_dict(row):
    return {
        'id': row['id'],
        'name': row['name'],
        'color': row['color'],
        'createdAt': row['created_at'],
    }


def attachment_row_to_dict(row):
    return {
        'attachId': row['attach_id'],
        'noteId': row['note_id'],
        'type': row['type'],
        'fileName': row['file_name'],
        'fileId': row['file_id'],
        'width': row['width'],
        'height': row['height'],
        'md5': row['md5'],
        'url': '',
        'state': row['state'],
        'createdAt': row['created_at'],
    }
