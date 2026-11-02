"""分享设置（从 mock-server app/settings.py 迁入）。"""
from flask import Blueprint, g, request

from config import SHARE_DEFAULT_LOGO_TEXT, SHARE_DEFAULT_WATERMARK
from core.protocol import success
from plugins.notes.db import get_notes_db

share_settings_bp = Blueprint('share_settings', __name__)


def _get_share_settings(user_id):
    db = get_notes_db()
    row = db.execute('SELECT * FROM share_settings WHERE user_id = ?', (user_id,)).fetchone()
    if row:
        return {'logoText': row['logo_text'], 'watermark': row['watermark']}
    return {'logoText': SHARE_DEFAULT_LOGO_TEXT, 'watermark': SHARE_DEFAULT_WATERMARK}


@share_settings_bp.route('/api/settings/share', methods=['GET'])
def get_share_settings():
    return success(_get_share_settings(g.user_id))


@share_settings_bp.route('/api/settings/share', methods=['PUT'])
def update_share_settings():
    data = request.get_json(silent=True) or {}
    current = _get_share_settings(g.user_id)
    if 'logoText' in data:
        current['logoText'] = data['logoText']
    if 'watermark' in data:
        current['watermark'] = data['watermark']

    db = get_notes_db()
    db.execute(
        'INSERT OR REPLACE INTO share_settings (user_id, logo_text, watermark) VALUES (?, ?, ?)',
        (g.user_id, current['logoText'], current['watermark'])
    )
    db.commit()
    return success(current)
