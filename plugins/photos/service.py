"""photos 插件 service：跨插件协作入口（ARCHITECTURE.md §5.2 复制模式）。

其他插件（如 drive）经插件注册表进程内调用本模块，把本地文件导入相册：
读流 → 写 photos 独立库（内容寻址，跨插件去重）→ 幂等重试（同 md5 返回同一记录）。
"""
import hashlib
import os

from core import event_bus, file_store
from core.auth import now_iso
from core.util import generate_id
from plugins.photos.db import get_photos_db, photo_row_to_dict
from plugins.photos.thumb import image_dimensions, make_thumbnail_bytes


def import_file(local_path, filename=None):
    """把本地文件导入相册（复制语义）。返回照片 dict（含 URL 由调用方补全）。

    幂等：按文件 md5 生成 client_id（import-<md5>），重复导入返回已存在的记录。
    """
    if not os.path.isfile(local_path):
        raise FileNotFoundError('source file not found')

    with open(local_path, 'rb') as f:
        md5 = hashlib.md5()
        for chunk in iter(lambda: f.read(8192), b''):
            md5.update(chunk)
    md5_val = md5.hexdigest()
    client_id = f'import-{md5_val}'
    size = os.path.getsize(local_path)

    db = get_photos_db()
    row = db.execute('SELECT * FROM photos WHERE client_id = ?', (client_id,)).fetchone()
    if row:
        return row

    # 原图 + 压缩图（内容寻址，跨插件去重）
    orig_file_id = file_store.put_from_path(local_path, bucket='orig')
    thumb_data = make_thumbnail_bytes(local_path)
    thumb_file_id = file_store.put(thumb_data, bucket='thumb') if thumb_data else ''
    width, height = image_dimensions(local_path)

    photo_id = generate_id('p')
    now_ts = now_iso()
    db.execute(
        '''INSERT INTO photos
           (id, device_id, client_id, media_id, filename, width, height, size,
            thumb_file_id, orig_file_id, md5, version, created_at, updated_at)
           VALUES (?, '', ?, '', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)''',
        (photo_id, client_id, filename or os.path.basename(local_path),
         width, height, size, thumb_file_id, orig_file_id, md5_val, now_ts, now_ts)
    )
    db.commit()
    row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()

    event_bus.publish('photo.uploaded', {
        'id': photo_id, 'orig_file_id': orig_file_id, 'thumb_file_id': thumb_file_id,
        'size': size, 'source': 'import',
    })
    return row


def photo_dict_with_urls(row, host):
    photo = photo_row_to_dict(row)
    photo['thumbUrl'] = f'{host}/api/photos/{row["id"]}/download?size=thumb'
    photo['origUrl'] = f'{host}/api/photos/{row["id"]}/download'
    return photo
