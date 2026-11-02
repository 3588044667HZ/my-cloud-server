"""drive 插件共享：独立库连接。"""
import os

from config import DATA_DIR
from core.db import get_conn

DRIVE_DB = os.path.join(DATA_DIR, 'drive.db')


def get_drive_db():
    return get_conn(DRIVE_DB)
