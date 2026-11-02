"""photos 插件共享：独立库连接 + 行→dict 转换。"""
import os

from config import DATA_DIR
from core.db import get_conn

PHOTOS_DB = os.path.join(DATA_DIR, 'photos.db')


def get_photos_db():
    return get_conn(PHOTOS_DB)


def photo_row_to_dict(row):
    return {
        'id': row['id'],
        'mediaId': row['media_id'],
        'filename': row['filename'],
        'width': row['width'],
        'height': row['height'],
        'size': row['size'],
        'md5': row['md5'],
        'thumbFileId': row['thumb_file_id'],
        'origFileId': row['orig_file_id'],
        'state': 1 if (row['orig_file_id'] and row['orig_file_id'] != '') else 0,
        'version': row['version'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
        'deletedAt': row['deleted_at'],
    }
