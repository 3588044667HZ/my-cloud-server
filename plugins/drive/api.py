"""云盘文件管理 API（ARCHITECTURE.md §6）：shares/list/stat/mkdir/rename/move/copy/delete/trash/
search/thumb/download + 分片上传。

- path 一律相对 share 根，`/` 分隔，禁止绝对路径与 `..`（safe_path 沙箱）。
- 删除一律进回收站（{盘根}/回收站/<唯一名>），清空才物理删除。
- 云盘全局开关 drive_settings.enabled，未开启 → 403（10403）。
- 备份目录（云备份/photos 分桶）默认隐藏，"显示备份目录"开关兜底。
"""
import hashlib
import json
import os
import shutil
import time
import uuid

from flask import Blueprint, Response, g, request, send_file

from config import (
    DATA_DIR, DISK_FREE_THRESHOLD, DRIVE_HIDDEN_DIRS, PHOTO_CHUNK_SIZE,
    THUMB_MAX_BYTES, THUMB_MAX_EDGE, THUMB_QUALITY,
)
from core import file_store
from core.auth import now_iso
from core.protocol import CODE_DISK_FULL, error, pagination, success
from plugins.drive.db import get_drive_db
from plugins.drive.resolver import get_resolver, is_within_root, share_info

drive_bp = Blueprint('drive', __name__)

TRASH_DIR_NAME = '回收站'
THUMB_CACHE_DIR = os.path.join(DATA_DIR, 'drive_thumb_cache')
UPLOAD_TMP_DIR = os.path.join(DATA_DIR, 'drive_uploads')

# ---------- 开关 ----------

def _drive_enabled():
    db = get_drive_db()
    row = db.execute("SELECT value FROM drive_settings WHERE key = 'enabled'").fetchone()
    return row['value'] == '1' if row else False


def _require_drive(fn):
    """云盘功能开关守卫：未开启 → 403（10403）。"""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _drive_enabled():
            return error('Drive disabled', code=10403, status=403)
        return fn(*args, **kwargs)
    return wrapper


def _get_param(name):
    """从 query / JSON body 取参数。"""
    val = request.args.get(name)
    if val is not None:
        return val
    data = request.get_json(silent=True) or {}
    return data.get(name)


# ---------- 路径沙箱 ----------

def safe_path(share, path):
    """校验并返回沙箱内真实路径；越界 → ValueError，不存在 → FileNotFoundError。"""
    root = get_resolver().resolve(share)
    real_root = os.path.realpath(root)
    rel = (path or '/').replace('\\', '/').lstrip('/')
    real = os.path.realpath(os.path.join(real_root, rel))
    if not is_within_root(real, real_root):
        raise ValueError('path escapes share root')
    if not os.path.exists(real):
        raise FileNotFoundError('not found')
    return real


def _safe_path_or_error(share, path):
    """safe_path 的友好封装：返回 (real_path, error_response)。"""
    try:
        return safe_path(share, path), None
    except ValueError as e:
        return None, error(str(e), code=10403, status=403)
    except FileNotFoundError:
        return None, error('Path not found', code=10404, status=404)


def _entries(dir_path, show_hidden_dirs=False):
    """列目录；备份目录默认隐藏。"""
    result = []
    try:
        names = os.listdir(dir_path)
    except OSError:
        return result
    for name in names:
        full = os.path.join(dir_path, name)
        if not show_hidden_dirs and name in DRIVE_HIDDEN_DIRS and os.path.isdir(full):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        result.append({
            'name': name,
            'isDir': os.path.isdir(full),
            'size': st.st_size,
            'mtime': st.st_mtime,
            'ext': '' if os.path.isdir(full) else os.path.splitext(name)[1].lower().lstrip('.'),
        })
    return result


# ---------- shares ----------

@drive_bp.route('/api/drive/shares', methods=['GET'])
@_require_drive
def shares():
    res = get_resolver()
    out = []
    for s in res.list_shares():
        info = share_info(s.name)
        info['name'] = s.name
        out.append(info)
    return success(out)


# ---------- list / stat ----------

@drive_bp.route('/api/drive/list', methods=['GET'])
@_require_drive
def list_dir():
    share = _get_param('share') or ''
    path = _get_param('path') or '/'
    show_hidden = request.args.get('showHiddenDirs', '').lower() == 'true'
    real, err = _safe_path_or_error(share, path)
    if err:
        return err
    if not os.path.isdir(real):
        return error('Not a directory', code=10404, status=404)

    entries = _entries(real, show_hidden_dirs=show_hidden)

    sort_by = request.args.get('sortBy', 'name')
    if sort_by == 'time':
        entries.sort(key=lambda e: e['mtime'], reverse=True)
    elif sort_by == 'size':
        entries.sort(key=lambda e: (e['isDir'], e['size']), reverse=True)
    else:
        entries.sort(key=lambda e: (not e['isDir'], e['name'].lower()))

    page = max(1, int(request.args.get('page', 1)))
    size = min(1000, max(1, int(request.args.get('size', 200))))
    total = len(entries)
    start = (page - 1) * size
    chunk = entries[start:start + size]
    return success({'entries': chunk}, extra=pagination(page, size, total))


@drive_bp.route('/api/drive/stat', methods=['GET'])
@_require_drive
def stat():
    share = _get_param('share') or ''
    path = _get_param('path') or '/'
    real, err = _safe_path_or_error(share, path)
    if err:
        return err
    st = os.stat(real)
    return success({
        'name': os.path.basename(real) or '/',
        'isDir': os.path.isdir(real),
        'size': st.st_size,
        'mtime': st.st_mtime,
    })


# ---------- mkdir / rename / move / copy / delete ----------

@drive_bp.route('/api/drive/mkdir', methods=['POST'])
@_require_drive
def mkdir():
    share = _get_param('share') or ''
    path = _get_param('path') or ''
    if not path.strip('/'):
        return error('path is required')
    root = get_resolver().resolve(share)
    real_root = os.path.realpath(root)
    rel = path.replace('\\', '/').lstrip('/')
    real = os.path.realpath(os.path.join(real_root, rel))
    if not is_within_root(real, real_root):
        return error('path escapes share root', code=10403, status=403)
    if os.path.exists(real):
        return error('Path already exists')
    os.makedirs(real, exist_ok=True)
    return success({'path': path})


def _resolve_existing(share, path):
    real, err = _safe_path_or_error(share, path)
    return real, err


@drive_bp.route('/api/drive/rename', methods=['POST'])
@_require_drive
def rename():
    share = _get_param('share') or ''
    src = _get_param('from') or _get_param('src') or ''
    dst = _get_param('to') or ''
    real_src, err = _resolve_existing(share, src)
    if err:
        return err
    root = get_resolver().resolve(share)
    real_root = os.path.realpath(root)
    rel = dst.replace('\\', '/').lstrip('/')
    real_dst = os.path.realpath(os.path.join(real_root, rel))
    if not is_within_root(real_dst, real_root):
        return error('path escapes share root', code=10403, status=403)
    if os.path.exists(real_dst):
        return error('Destination already exists')
    if os.path.dirname(real_dst) != os.path.dirname(real_src):
        return error('Rename must stay in same directory', code=10400, status=400)
    os.rename(real_src, real_dst)
    return success(None)


def _copy_recursive(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


@drive_bp.route('/api/drive/copy', methods=['POST'])
@_require_drive
def copy():
    share = _get_param('share') or ''
    src = _get_param('src') or ''
    dst = _get_param('dst') or ''
    real_src, err = _resolve_existing(share, src)
    if err:
        return err
    root = get_resolver().resolve(share)
    real_root = os.path.realpath(root)
    rel = dst.replace('\\', '/').lstrip('/')
    real_dst = os.path.realpath(os.path.join(real_root, rel))
    if not is_within_root(real_dst, real_root):
        return error('path escapes share root', code=10403, status=403)
    if os.path.exists(real_dst):
        return error('Destination already exists')
    try:
        _copy_recursive(real_src, real_dst)
    except OSError as e:
        return error(f'Copy failed: {e}')
    return success(None)


@drive_bp.route('/api/drive/move', methods=['POST'])
@_require_drive
def move():
    share = _get_param('share') or ''
    src = _get_param('src') or ''
    dst = _get_param('dst') or ''
    real_src, err = _resolve_existing(share, src)
    if err:
        return err
    root = get_resolver().resolve(share)
    real_root = os.path.realpath(root)
    rel = dst.replace('\\', '/').lstrip('/')
    real_dst = os.path.realpath(os.path.join(real_root, rel))
    if not is_within_root(real_dst, real_root):
        return error('path escapes share root', code=10403, status=403)
    if os.path.exists(real_dst):
        return error('Destination already exists')

    # 同盘原子 rename；跨盘 = 复制 + 删除
    if os.path.splitdrive(real_src)[0].lower() == os.path.splitdrive(real_dst)[0].lower() \
            and os.path.dirname(real_src) != real_root and os.path.ismount(real_root) is False:
        try:
            os.rename(real_src, real_dst)
            return success(None)
        except OSError:
            pass  # 落到复制语义

    try:
        _copy_recursive(real_src, real_dst)
        shutil.rmtree(real_src) if os.path.isdir(real_src) else os.remove(real_src)
    except OSError as e:
        # 失败回退：删除已复制部分
        shutil.rmtree(real_dst, ignore_errors=True) if os.path.isdir(real_dst) else \
            (os.remove(real_dst) if os.path.exists(real_dst) else None)
        return error(f'Move failed: {e}')
    return success(None)


# ---------- 回收站 ----------

def _trash_root(share):
    root = get_resolver().resolve(share)
    trash = os.path.join(root, TRASH_DIR_NAME)
    os.makedirs(trash, exist_ok=True)
    return trash


@drive_bp.route('/api/drive/delete', methods=['DELETE', 'POST'])
@_require_drive
def delete():
    share = _get_param('share') or ''
    path = _get_param('path') or ''
    real, err = _resolve_existing(share, path)
    if err:
        return err
    root = get_resolver().resolve(share)
    rel = os.path.relpath(real, root).replace('\\', '/')
    if rel.startswith(TRASH_DIR_NAME):
        return error('Cannot delete from trash')

    trash = _trash_root(share)
    unique = f'{os.path.basename(real)}__{uuid.uuid4().hex[:8]}'
    trash_path = os.path.join(trash, unique)
    try:
        shutil.move(real, trash_path)
    except OSError as e:
        # Windows 上句柄可能瞬时占用（如刚下载的文件），重试一次
        import time
        time.sleep(0.1)
        try:
            shutil.move(real, trash_path)
        except OSError:
            return error(f'Delete failed: {e}', code=10500, status=500)

    db = get_drive_db()
    db.execute(
        'INSERT INTO trash_index (share, orig_path, trash_path, deleted_at) VALUES (?, ?, ?, ?)',
        (share, '/' + rel, os.path.relpath(trash_path, root).replace('\\', '/'), now_iso())
    )
    db.commit()
    return success({'trashPath': '/' + os.path.relpath(trash_path, root).replace('\\', '/')})


@drive_bp.route('/api/drive/trash', methods=['GET'])
@drive_bp.route('/api/drive/trash/list', methods=['POST', 'GET'])
@_require_drive
def trash_list():
    """回收站列表：含 deletedAt / remainDays，分页（share 可空 = 全部盘）。"""
    share = _get_param('share') or ''
    page = max(1, int(request.args.get('page', 1)))
    size = min(1000, max(1, int(request.args.get('size', 100))))
    db = get_drive_db()
    cond = 'WHERE share = ?' if share else ''
    params = (share,) if share else ()
    total = db.execute(f'SELECT COUNT(*) as cnt FROM trash_index {cond}', params).fetchone()['cnt']
    rows = db.execute(
        f'SELECT * FROM trash_index {cond} ORDER BY deleted_at DESC LIMIT ? OFFSET ?',
        params + (size, (page - 1) * size)
    ).fetchall()
    from core.protocol import pagination
    return success([_trash_item_dict(r) for r in rows], extra=pagination(page, size, total))


def _trash_item_dict(r):
    from core.trash import remain_days
    return {
        'id': r['id'], 'share': r['share'], 'origPath': r['orig_path'],
        'trashPath': r['trash_path'], 'deletedAt': r['deleted_at'],
        'remainDays': remain_days(r['deleted_at']),
    }


def _trash_physical_path(row):
    """回收站内物理路径（share 不可用返回 None）。"""
    try:
        root = get_resolver().resolve(row['share'])
    except ValueError:
        return None
    return os.path.join(root, row['trash_path'].lstrip('/'))


def _restore_one(db, item_id):
    """还原单条；返回 {ok, error?}。"""
    row = db.execute('SELECT * FROM trash_index WHERE id = ?', (item_id,)).fetchone()
    if not row:
        return {'ok': False, 'error': 'not found'}
    try:
        root = get_resolver().resolve(row['share'])
    except ValueError:
        return {'ok': False, 'error': 'share unavailable'}
    trash_path = os.path.join(root, row['trash_path'].lstrip('/'))
    orig_path = os.path.join(root, row['orig_path'].lstrip('/'))
    if not os.path.exists(trash_path):
        db.execute('DELETE FROM trash_index WHERE id = ?', (item_id,))
        db.commit()
        return {'ok': False, 'error': 'not found'}
    if os.path.exists(orig_path):
        return {'ok': False, 'error': 'target exists'}
    os.makedirs(os.path.dirname(orig_path), exist_ok=True)
    shutil.move(trash_path, orig_path)
    db.execute('DELETE FROM trash_index WHERE id = ?', (item_id,))
    db.commit()
    return {'ok': True}


@drive_bp.route('/api/drive/trash/restore', methods=['POST'])
@_require_drive
def trash_restore():
    """恢复（单 id 或 id[] 批量）。"""
    data = request.get_json(silent=True) or {}
    raw = data.get('id') if data.get('id') is not None else data.get('ids')
    ids = [raw] if not isinstance(raw, list) else raw
    ids = [i for i in ids if i is not None]
    if not ids:
        return error('id or ids is required')
    db = get_drive_db()
    if len(ids) == 1:
        result = _restore_one(db, ids[0])
        if not result['ok']:
            if result['error'] in ('not found', 'share unavailable'):
                return error('Trash item not found', code=10404, status=404)
            return error(result['error'], code=10400, status=400)
        return success(None)
    ok = failed = 0
    for i in ids:
        r = _restore_one(db, i)
        if r['ok']:
            ok += 1
        else:
            failed += 1
    return success({'restored': ok, 'failed': failed})


@drive_bp.route('/api/drive/trash/<int:item_id>', methods=['DELETE'])
@_require_drive
def trash_delete_one(item_id):
    """回收站内彻底删除（强警告后调用）：物理删除 + 移除索引。"""
    db = get_drive_db()
    row = db.execute('SELECT * FROM trash_index WHERE id = ?', (item_id,)).fetchone()
    if not row:
        return error('Trash item not found', code=10404, status=404)
    p = _trash_physical_path(row)
    if p:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    db.execute('DELETE FROM trash_index WHERE id = ?', (item_id,))
    db.commit()
    return success(None)


@drive_bp.route('/api/drive/trash/empty', methods=['DELETE', 'POST'])
@_require_drive
def trash_empty():
    share = _get_param('share') or ''
    db = get_drive_db()
    rows = db.execute('SELECT * FROM trash_index WHERE share = ?', (share,)).fetchall()
    count = 0
    for r in rows:
        p = _trash_physical_path(r)
        if p:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                elif os.path.exists(p):
                    os.remove(p)
                count += 1
            except OSError:
                pass
        db.execute('DELETE FROM trash_index WHERE id = ?', (r['id'],))
    db.commit()
    return success({'removed': count})


def sweep_expired():
    """超期自动清理：物理删除超过保留期的回收站文件。返回清除数。"""
    from core.trash import is_expired
    db = get_drive_db()
    rows = db.execute('SELECT * FROM trash_index').fetchall()
    removed = 0
    for r in rows:
        if not is_expired(r['deleted_at']):
            continue
        p = _trash_physical_path(r)
        if p:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                elif os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        db.execute('DELETE FROM trash_index WHERE id = ?', (r['id'],))
        removed += 1
    db.commit()
    return removed


# ---------- 下载 / 保存到相册 / thumb / search ----------

@drive_bp.route('/api/drive/save-to-album', methods=['POST'])
@_require_drive
def save_to_album():
    """复制模式（ARCHITECTURE §5.2）：把云盘文件导入相册「保存到云备份」。

    读 drive 流 → 调 photos service 写入相册独立库（内容寻址去重，幂等）→ 返回照片信息。
    """
    from flask import current_app

    share = _get_param('share') or ''
    path = _get_param('path') or ''
    real, err = _resolve_existing(share, path)
    if err:
        return err
    if os.path.isdir(real):
        return error('Is a directory')

    registry = current_app.extensions.get('plugin_registry')
    if not registry or not registry.plugin_enabled('photos'):
        return error('photos plugin is disabled', code=10403, status=403)

    from plugins.photos.service import import_file, photo_dict_with_urls
    try:
        row = import_file(real, filename=os.path.basename(real))
    except OSError as e:
        return error(f'Import failed: {e}', code=10500, status=500)
    return success(photo_dict_with_urls(row, request.host_url.rstrip('/')))

@drive_bp.route('/api/drive/sign', methods=['GET'])
@_require_drive
def sign_url():
    """签名临时 URL（ARCHITECTURE §6.8）：外部播放器/地址栏免鉴权下载。

    req: share, path, expires(秒, 默认 600)
    resp: { url: "/api/drive/download?...&exp=<ts>&sig=<hmac>" }
    """
    from urllib.parse import quote

    from core.signing import sign as make_sign

    share = _get_param('share') or ''
    path = _get_param('path') or ''
    try:
        ttl = max(1, min(86400, int(request.args.get('expires', 600))))
    except (TypeError, ValueError):
        ttl = 600
    real, err = _resolve_existing(share, path)
    if err:
        return err
    if os.path.isdir(real):
        return error('Is a directory')

    exp, sig = make_sign(share, path, ttl)
    url = (f'/api/drive/download?share={quote(share)}'
           f'&path={quote(path, safe="/")}&exp={exp}&sig={sig}')
    return success({'url': url, 'expiresIn': ttl, 'exp': exp})


@drive_bp.route('/api/drive/download', methods=['GET'])
@_require_drive
def download():
    share = _get_param('share') or ''
    path = _get_param('path') or ''
    # 匿名访问（无设备 token）必须携带有效签名 URL（§6.8）；带 token 直接放行
    if not hasattr(g, 'device'):
        from core.signing import verify as verify_signed
        if not verify_signed(share, path, request.args.get('exp', ''), request.args.get('sig', '')):
            return error('Unauthorized', code=10401, status=401)
    real, err = _resolve_existing(share, path)
    if err:
        return err
    # 打包下载整个文件夹（§6.5）：GET /api/drive/download?path=/folder&zip=1 → 流式 ZIP
    if request.args.get('zip', '') == '1' and os.path.isdir(real):
        from plugins.drive.stream import zip_folder
        zip_path, zip_name = zip_folder(share, real, path)
        resp = send_file(zip_path, as_attachment=True, download_name=zip_name,
                         mimetype='application/zip', conditional=False)
        resp.call_on_close(lambda p=zip_path: _safe_remove(p))
        return resp
    if os.path.isdir(real):
        return error('Is a directory')
    # send_file(conditional=True) 原生支持 HTTP Range（206）
    return send_file(real, conditional=True)


def _safe_remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


@drive_bp.route('/api/drive/thumb', methods=['GET'])
@_require_drive
def thumb():
    share = _get_param('share') or ''
    path = _get_param('path') or ''
    real, err = _resolve_existing(share, path)
    if err:
        return err
    if os.path.isdir(real):
        return error('Is a directory')
    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
    cache_key = hashlib.md5(f'{share}:{path}'.encode('utf-8')).hexdigest()
    cache_path = os.path.join(THUMB_CACHE_DIR, f'{cache_key}.webp')
    if not os.path.isfile(cache_path):
        _generate_drive_thumb(real, cache_path)
    if os.path.isfile(cache_path):
        return send_file(cache_path, mimetype='image/webp')
    return Response('', status=415, mimetype='text/plain')


def _generate_drive_thumb(src, dst):
    try:
        from PIL import Image
        with Image.open(src) as img:
            img = img.convert('RGB')
            w, h = img.size
            scale = min(1.0, THUMB_MAX_EDGE / max(w, h))
            if scale < 1.0:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            quality = THUMB_QUALITY
            img.save(dst, 'WEBP', quality=quality)
            while os.path.getsize(dst) > THUMB_MAX_BYTES and quality > 40:
                quality -= 10
                img.save(dst, 'WEBP', quality=quality)
    except Exception:
        pass


@drive_bp.route('/api/drive/search', methods=['GET'])
@_require_drive
def search():
    share = _get_param('share') or ''
    q = request.args.get('q', '').strip()
    base_dir = _get_param('dir') or '/'
    if not q:
        return error('q is required')
    root = get_resolver().resolve(share)
    real_root = os.path.realpath(root)
    base_rel = base_dir.replace('\\', '/').lstrip('/')
    base_real = os.path.realpath(os.path.join(real_root, base_rel))
    if not is_within_root(base_real, real_root):
        return error('path escapes share root', code=10403, status=403)
    if not os.path.isdir(base_real):
        return error('Not a directory', code=10404, status=404)

    limit = min(200, max(1, int(request.args.get('limit', 100))))
    results = []
    for dirpath, dirnames, filenames in os.walk(base_real):
        dirnames[:] = [d for d in dirnames if d not in DRIVE_HIDDEN_DIRS]
        for name in filenames:
            if q.lower() in name.lower():
                full = os.path.join(dirpath, name)
                st = os.stat(full)
                rel = os.path.relpath(full, real_root).replace('\\', '/')
                results.append({
                    'path': '/' + rel,
                    'name': name,
                    'size': st.st_size,
                    'mtime': st.st_mtime,
                })
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break
    return success(results)


# ---------- 分片上传（SPEC-UPLOAD-TRANSPORT.md §3） ----------

def _upload_have_blocks(chunk_dir):
    seqs = []
    if os.path.isdir(chunk_dir):
        for name in os.listdir(chunk_dir):
            if name.endswith('.part'):
                seqs.append(int(name[: -len('.part')]))
    return sorted(seqs)


def _file_md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


@drive_bp.route('/api/drive/upload/init', methods=['POST'])
@_require_drive
def upload_init():
    from config import UPLOAD_SESSION_TTL
    data = request.get_json(silent=True) or {}
    share = data.get('share') or ''
    path = data.get('path') or ''
    md5 = data.get('md5') or ''
    resume_id = (data.get('uploadId') or '').strip()
    # 预校验目标路径可写（沙箱内）
    root = get_resolver().resolve(share)
    real_root = os.path.realpath(root)
    rel = path.replace('\\', '/').lstrip('/')
    real = os.path.realpath(os.path.join(real_root, rel))
    if not is_within_root(real, real_root):
        return error('path escapes share root', code=10403, status=403)
    if os.path.isdir(real):
        return error('Target is a directory')

    # 秒传：目标文件已存在且内容一致 → 直接完成
    if md5 and os.path.isfile(real):
        if _file_md5(real) == md5:
            return success({'exists': True, 'fileId': md5, 'size': os.path.getsize(real)})

    # 断点续传：带 uploadId 且会话仍在 → 复用并返回已收块
    if resume_id:
        resume_dir = os.path.join(UPLOAD_TMP_DIR, resume_id)
        if os.path.isdir(resume_dir) and not os.path.exists(os.path.join(resume_dir, '.done')):
            return success({'exists': False, 'uploadId': resume_id,
                            'chunkSize': PHOTO_CHUNK_SIZE,
                            'haveBlocks': _upload_have_blocks(resume_dir)})

    upload_id = str(uuid.uuid4())
    os.makedirs(os.path.join(UPLOAD_TMP_DIR, upload_id), exist_ok=True)
    meta = {'share': share, 'path': path, 'md5': md5}
    with open(os.path.join(UPLOAD_TMP_DIR, upload_id, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)
    return success({'exists': False, 'uploadId': upload_id,
                    'chunkSize': PHOTO_CHUNK_SIZE, 'haveBlocks': []})


@drive_bp.route('/api/drive/upload/<upload_id>/<int:seq>', methods=['PUT'])
@_require_drive
def upload_chunk(upload_id, seq):
    chunk_dir = os.path.join(UPLOAD_TMP_DIR, upload_id)
    if not os.path.isdir(chunk_dir) or os.path.exists(os.path.join(chunk_dir, '.done')):
        return error('Upload session not found', code=10404, status=404)
    content = request.get_data()
    if not content:
        return error('Empty chunk', code=10400, status=400)

    # 块级 CRC32 校验（SPEC §2/§3.2）
    from core.util import crc32_hex
    expected_crc = request.headers.get('X-Chunk-CRC32', '').strip()
    if expected_crc and expected_crc.lower() != crc32_hex(content):
        return error('Chunk CRC32 mismatch', code=10418, status=400)

    with open(os.path.join(chunk_dir, f'{seq:06d}.part'), 'wb') as f:
        f.write(content)
    return success({'uploadId': upload_id, 'seq': seq,
                    'received': len(_upload_have_blocks(chunk_dir))})


@drive_bp.route('/api/drive/upload/<upload_id>/complete', methods=['POST'])
@_require_drive
def upload_complete(upload_id):
    chunk_dir = os.path.join(UPLOAD_TMP_DIR, upload_id)
    done_path = os.path.join(chunk_dir, '.done')
    meta_path = os.path.join(chunk_dir, 'meta.json')
    if not os.path.isdir(chunk_dir) or not os.path.isfile(meta_path):
        return error('Upload session not found', code=10404, status=404)

    # 幂等：已 complete 直接返回成功（.done 留存至 TTL 清理）
    if os.path.exists(done_path):
        with open(done_path, 'r', encoding='utf-8') as f:
            done = json.load(f)
        return success(done)

    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    share = meta['share']
    path = meta['path']

    # 合并分片 + md5 校验（stream 传输层）
    from plugins.drive.stream import atomic_write, merge_chunks, verify_md5
    tmp = os.path.join(UPLOAD_TMP_DIR, f'{upload_id}.merged')
    try:
        merge_chunks(chunk_dir, tmp)
    except ValueError as e:
        return error(str(e), code=10400, status=400)

    if not verify_md5(tmp, meta.get('md5', '')):
        os.remove(tmp)
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return error('MD5 mismatch', code=10405, status=400)

    # 磁盘满保护（SPEC §3.3）：合并前查剩余空间
    free = file_store.disk_free_bytes()
    if free is not None and free < DISK_FREE_THRESHOLD:
        os.remove(tmp)
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return error('Insufficient disk space', code=CODE_DISK_FULL, status=507)

    # 写入目标（沙箱内，原子落盘）
    root = get_resolver().resolve(share)
    try:
        real = atomic_write(root, path, tmp)
    except ValueError as e:
        return error(str(e), code=10403, status=403)
    finally:
        # 清除 .part，保留 meta + .done（幂等续查），TTL 后整体清理
        for name in os.listdir(chunk_dir):
            if name.endswith('.part'):
                try:
                    os.remove(os.path.join(chunk_dir, name))
                except OSError:
                    pass

    result = {'path': path, 'size': os.path.getsize(real), 'fileId': meta.get('md5', '')}
    with open(done_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    return success(result)


@drive_bp.route('/api/drive/upload/batch-check', methods=['POST'])
@_require_drive
def upload_batch_check():
    """批量判重（上传文件夹秒传，SPEC §3.4）：{ exists: { [md5]: path } }。"""
    data = request.get_json(silent=True) or {}
    share = data.get('share') or ''
    files = data.get('files') or []
    root = get_resolver().resolve(share)
    real_root = os.path.realpath(root)
    exists = {}
    for item in files:
        md5 = (item.get('md5') or '').strip()
        path = (item.get('path') or '').strip()
        if not md5 or not path:
            continue
        rel = path.replace('\\', '/').lstrip('/')
        real = os.path.realpath(os.path.join(real_root, rel))
        if not is_within_root(real, real_root) or not os.path.isfile(real):
            continue
        if _file_md5(real) == md5:
            exists[md5] = '/' + rel
    return success({'exists': exists})


def sweep_uploads():
    """孤儿会话 TTL 清理（SPEC §3.5）：删除超期未完成的暂存目录。"""
    from config import UPLOAD_SESSION_TTL
    now = time.time()
    removed = 0
    if not os.path.isdir(UPLOAD_TMP_DIR):
        return 0
    for name in os.listdir(UPLOAD_TMP_DIR):
        d = os.path.join(UPLOAD_TMP_DIR, name)
        if not os.path.isdir(d):
            continue
        try:
            age = now - os.path.getmtime(d)
        except OSError:
            continue
        if age > UPLOAD_SESSION_TTL:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed
