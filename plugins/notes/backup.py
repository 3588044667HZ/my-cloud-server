"""笔记冷备 txt（沿用 mock-server app/backup.py，目录按单用户 name 组织）。"""
import json
import os

from config import BACKUP_DIR
from core.auth import get_single_user


def _username():
    return get_single_user()['name']


def _backup_path(note_id):
    user_dir = os.path.join(BACKUP_DIR, _username())
    return user_dir, os.path.join(user_dir, f'{note_id}.txt')


def backup_note(note):
    """落盘冷备：{BACKUP_DIR}/{单用户名}/{note_id}.txt。"""
    user_dir, filepath = _backup_path(note['id'])
    os.makedirs(user_dir, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(note, f, ensure_ascii=False, indent=2)


def remove_backup(note_id):
    _, filepath = _backup_path(note_id)
    if os.path.exists(filepath):
        os.remove(filepath)
