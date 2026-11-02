"""ARCHITECTURE.md 落地功能测试：
If-Match 乐观锁 / 删除 GC（引用计数+后台回收）/ 跨插件复制（云盘→相册）/ 事件订阅 / SQLite 一致性备份。
"""
import importlib.util
import io
import os
import sqlite3

import pytest
from PIL import Image

from core import file_store


_counter = [0]


def _jpg():
    """每次生成不同内容的图片，避免跨测试内容寻址引用共享。"""
    _counter[0] += 1
    n = _counter[0]
    buf = io.BytesIO()
    Image.new('RGB', (100 + n % 50, 80), (n % 255, 40, 200)).save(buf, 'JPEG')
    return buf.getvalue()


def _ref_count(fid):
    from config import CORE_DB
    from core.db import get_conn
    db = get_conn(CORE_DB)
    row = db.execute('SELECT ref_count FROM file_refs WHERE file_id = ?', (fid,)).fetchone()
    return row['ref_count'] if row else 0


def _upload_photo(client, auth_headers, client_id):
    r = client.post('/api/photos/register', json={'clientId': client_id, 'md5': ''},
                    headers=auth_headers)
    pid = r.json['data']['id']
    r = client.put(f'/api/photos/{pid}/file', data=_jpg(), headers=auth_headers)
    assert r.status_code == 200, r.json
    return pid, r.json['data']


class TestPhotosIfMatch:
    """§4.1 If-Match 乐观锁（照抄 notes 模块）。"""

    def test_wrong_if_match_409(self, client, auth_headers):
        pid, data = _upload_photo(client, auth_headers, 'im-wrong')
        r = client.put(f'/api/photos/{pid}', json={'md5': 'x'},
                       headers={**auth_headers, 'If-Match': '2020-01-01T00:00:00Z'})
        assert r.status_code == 409
        assert r.json['code'] == 409

    def test_correct_if_match_ok(self, client, auth_headers):
        pid, data = _upload_photo(client, auth_headers, 'im-ok')
        updated_at = data['updatedAt']
        r = client.put(f'/api/photos/{pid}', json={'md5': 'abc'},
                       headers={**auth_headers, 'If-Match': updated_at})
        assert r.status_code == 200
        assert r.json['data']['md5'] == 'abc'


class TestPhotoGc:
    """回收站语义：软删保留文件（可恢复），彻底删除才释放引用并 GC。"""

    def test_soft_delete_keeps_files_until_permanent(self, client, auth_headers):
        pid, data = _upload_photo(client, auth_headers, 'gc-1')
        orig = data['origFileId']
        assert _ref_count(orig) >= 1
        p = file_store.path_for(orig, 'orig')
        assert os.path.isfile(p)

        # 软删 → 进回收站，文件/引用仍在（可恢复）
        client.delete(f'/api/photos/{pid}', headers=auth_headers)
        assert _ref_count(orig) >= 1
        assert os.path.isfile(p)

        # 回收站内彻底删除 → 引用归零，GC 物理回收
        r = client.delete(f'/api/photos/trash/{pid}', headers=auth_headers)
        assert r.status_code == 200, r.json
        assert _ref_count(orig) == 0
        removed = file_store.gc()
        assert not os.path.isfile(p)
        assert removed >= 1

    def test_dedup_shared_ref(self, client, auth_headers):
        """同内容两张照片共享引用；彻底删一张不误删另一张的文件。"""
        jpg = _jpg()  # 同一份内容，注册到两个 client_id

        def _up(client_id):
            r = client.post('/api/photos/register', json={'clientId': client_id, 'md5': ''},
                            headers=auth_headers)
            pid = r.json['data']['id']
            r = client.put(f'/api/photos/{pid}/file', data=jpg, headers=auth_headers)
            return pid, r.json['data']

        pid1, data1 = _up('gc-dedup-1')
        pid2, data2 = _up('gc-dedup-2')
        assert data1['origFileId'] == data2['origFileId']  # 内容寻址去重
        orig = data1['origFileId']
        assert _ref_count(orig) == 2

        # 彻底删除 pid1（先软删进回收站）→ 引用 -1，文件仍在（pid2 还引用）
        client.delete(f'/api/photos/{pid1}', headers=auth_headers)
        client.delete(f'/api/photos/trash/{pid1}', headers=auth_headers)
        assert _ref_count(orig) == 1
        assert os.path.isfile(file_store.path_for(orig, 'orig'))
        file_store.gc()
        assert os.path.isfile(file_store.path_for(orig, 'orig'))

        # 彻底删除 pid2 → 引用归零，GC 回收
        client.delete(f'/api/photos/{pid2}', headers=auth_headers)
        client.delete(f'/api/photos/trash/{pid2}', headers=auth_headers)
        assert _ref_count(orig) == 0
        file_store.gc()
        assert not os.path.isfile(file_store.path_for(orig, 'orig'))

    def test_gc_trigger_endpoint(self, client, auth_headers):
        r = client.post('/api/photos/gc', headers=auth_headers)
        assert r.status_code == 200
        assert 'taskId' in r.json['data']


class TestCloudTrash:
    """SPEC-CLOUD-RECYCLE-RECOVERY §1：photos/drive/notes 统一回收站。"""

    def _upload_photo(self, client, auth_headers, cid):
        r = client.post('/api/photos/register', json={'clientId': cid, 'md5': ''},
                        headers=auth_headers)
        pid = r.json['data']['id']
        client.put(f'/api/photos/{pid}/file', data=_jpg(), headers=auth_headers)
        return pid

    def test_photos_trash_flow(self, client, auth_headers):
        pid = self._upload_photo(client, auth_headers, 'ct-1')
        client.delete(f'/api/photos/{pid}', headers=auth_headers)

        # 回收站列表含 remainDays
        r = client.get('/api/photos/trash', headers=auth_headers)
        assert r.status_code == 200
        items = {p['id']: p for p in r.json['data']}
        assert pid in items
        assert 'remainDays' in items[pid]
        assert items[pid]['remainDays'] is not None

        # 恢复 → 回列表
        r = client.post('/api/photos/trash/restore', json={'id': pid}, headers=auth_headers)
        assert r.status_code == 200
        r = client.get('/api/photos/list', headers=auth_headers)
        assert any(p['id'] == pid for p in r.json['data'])

        # 再删 → 清空
        client.delete(f'/api/photos/{pid}', headers=auth_headers)
        r = client.delete('/api/photos/trash/empty', headers=auth_headers)
        assert r.status_code == 200
        assert r.json['data']['removed'] >= 1

    def test_photos_trash_restore_batch(self, client, auth_headers):
        pid1 = self._upload_photo(client, auth_headers, 'ct-b1')
        pid2 = self._upload_photo(client, auth_headers, 'ct-b2')
        client.delete(f'/api/photos/{pid1}', headers=auth_headers)
        client.delete(f'/api/photos/{pid2}', headers=auth_headers)
        r = client.post('/api/photos/trash/restore', json={'ids': [pid1, pid2]},
                        headers=auth_headers)
        assert r.status_code == 200
        assert r.json['data']['restored'] == 2

    def test_photos_trash_permanent_not_found(self, client, auth_headers):
        r = client.delete('/api/photos/trash/nope', headers=auth_headers)
        assert r.status_code == 404

    def test_drive_trash_remain_days_and_delete(self, client, admin_headers, auth_headers,
                                                tmp_path, monkeypatch):
        import config
        share = tmp_path / 'share'
        share.mkdir(parents=True, exist_ok=True)
        (share / 'a.txt').write_text('data')
        monkeypatch.setattr(config, 'DRIVE_SHARES', {'CT': str(share)})
        client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)

        client.delete('/api/drive/delete', query_string={'share': 'CT', 'path': '/a.txt'},
                      headers=auth_headers)
        r = client.get('/api/drive/trash?share=CT', headers=auth_headers)
        assert r.status_code == 200
        items = r.json['data']
        assert len(items) == 1
        assert 'remainDays' in items[0]
        item_id = items[0]['id']

        # 单条彻底删除
        r = client.delete(f'/api/drive/trash/{item_id}', headers=auth_headers)
        assert r.status_code == 200
        r = client.get('/api/drive/trash?share=CT', headers=auth_headers)
        assert r.json['data'] == []

    def test_drive_trash_restore_batch(self, client, admin_headers, auth_headers,
                                       tmp_path, monkeypatch):
        import config
        share = tmp_path / 'share2'
        share.mkdir(parents=True, exist_ok=True)
        (share / 'a.txt').write_text('1')
        (share / 'b.txt').write_text('2')
        monkeypatch.setattr(config, 'DRIVE_SHARES', {'CT2': str(share)})
        client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)
        client.delete('/api/drive/delete', query_string={'share': 'CT2', 'path': '/a.txt'},
                      headers=auth_headers)
        client.delete('/api/drive/delete', query_string={'share': 'CT2', 'path': '/b.txt'},
                      headers=auth_headers)
        r = client.get('/api/drive/trash?share=CT2', headers=auth_headers)
        ids = [x['id'] for x in r.json['data']]
        r = client.post('/api/drive/trash/restore', json={'ids': ids}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json['data']['restored'] == 2
        assert os.path.isfile(os.path.join(str(share), 'a.txt'))
        assert os.path.isfile(os.path.join(str(share), 'b.txt'))

    def test_notes_trash_remain_days(self, client, auth_headers):
        r = client.post('/api/notes', json={'title': 'trash note', 'content': 'x'},
                        headers=auth_headers)
        nid = r.json['data']['id']
        client.delete(f'/api/notes/{nid}', headers=auth_headers)
        r = client.get('/api/notes/trash', headers=auth_headers)
        items = {n['id']: n for n in r.json['data']}
        assert nid in items
        assert 'remainDays' in items[nid]


class TestCrossPluginCopy:
    """§5.2 复制模式：云盘 → 相册「保存到云备份」（读流→写独立库→幂等）。"""

    def _setup_share(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        import config
        share = tmp_path / 'share'
        share.mkdir(parents=True, exist_ok=True)
        (share / 'photo.jpg').write_bytes(_jpg())
        # 用 monkeypatch 设置/恢复，避免污染其它 drive 测试的共享配置
        monkeypatch.setattr(config, 'DRIVE_SHARES', {'CP': str(share)})
        client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)

    def test_save_to_album_idempotent(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        self._setup_share(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        r = client.post('/api/drive/save-to-album',
                        json={'share': 'CP', 'path': '/photo.jpg'}, headers=auth_headers)
        assert r.status_code == 200, r.json
        data = r.json['data']
        assert data['id'] and data['origFileId']

        # 幂等：重复导入返回同一记录
        r2 = client.post('/api/drive/save-to-album',
                         json={'share': 'CP', 'path': '/photo.jpg'}, headers=auth_headers)
        assert r2.json['data']['id'] == data['id']

        # 已入相册列表
        r3 = client.get('/api/photos/list', headers=auth_headers)
        assert any(p['id'] == data['id'] for p in r3.json['data'])

    def test_save_to_album_missing(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        self._setup_share(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        r = client.post('/api/drive/save-to-album',
                        json={'share': 'CP', 'path': '/nope.jpg'}, headers=auth_headers)
        assert r.status_code == 404

    def test_save_to_album_dir(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        self._setup_share(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        r = client.post('/api/drive/save-to-album',
                        json={'share': 'CP', 'path': '/'}, headers=auth_headers)
        assert r.status_code == 400


class TestEvents:
    """§5.2 事件模式：photo.uploaded → 订阅方统计。"""

    def test_uploaded_event_updates_stats(self, client, admin_headers, auth_headers):
        s1 = client.get('/api/admin/stats', headers=admin_headers).json['data']['photoUploads']
        pid, _ = _upload_photo(client, auth_headers, 'ev-1')
        s2 = client.get('/api/admin/stats', headers=admin_headers).json['data']['photoUploads']
        assert s2 == s1 + 1


@pytest.fixture(scope='module')
def backup_dir(tmp_path_factory):
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'backup_db', os.path.join(project, 'scripts', 'backup_db.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.run(str(tmp_path_factory.mktemp('dbbackup')))
    return out


class TestFolderDownload:
    """§6.5 下载整个文件夹：zip=1 流式 ZIP 打包。"""

    def _setup(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        import config
        share = tmp_path / 'share'
        (share / 'folder').mkdir(parents=True, exist_ok=True)
        (share / 'folder' / 'a.txt').write_text('hello a')
        (share / 'folder' / 'sub').mkdir()
        (share / 'folder' / 'sub' / 'b.bin').write_bytes(b'BBB')
        monkeypatch.setattr(config, 'DRIVE_SHARES', {'ZIP': str(share)})
        client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)

    def test_folder_zip(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        import io
        import zipfile
        self._setup(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        r = client.get('/api/drive/download?share=ZIP&path=/folder&zip=1', headers=auth_headers)
        assert r.status_code == 200, r.status_code
        assert r.headers.get('Content-Type', '').startswith('application/zip')
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = zf.namelist()
        assert 'folder/a.txt' in names
        assert 'folder/sub/b.bin' in names
        assert zf.read('folder/a.txt') == b'hello a'
        assert zf.read('folder/sub/b.bin') == b'BBB'

    def test_zip_on_file_serves_file(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        self._setup(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        r = client.get('/api/drive/download?share=ZIP&path=/folder/a.txt&zip=1', headers=auth_headers)
        assert r.status_code == 200
        assert r.data == b'hello a'

    def test_zip_anonymous_requires_sig(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        self._setup(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        r = client.get('/api/drive/download?share=ZIP&path=/folder&zip=1')
        assert r.status_code == 401

    def test_zip_root_share(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        import io
        import zipfile
        self._setup(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        r = client.get('/api/drive/download?share=ZIP&path=/&zip=1', headers=auth_headers)
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = zf.namelist()
        # 盘根打包：以 share 名为根目录
        assert any(n.startswith('ZIP/') for n in names)


class TestUploadAutoMkdir:
    """§6.5 上传整个文件夹：path 含未存在子目录时自动建父目录。"""

    def _setup(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        import config
        share = tmp_path / 'share'
        share.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, 'DRIVE_SHARES', {'UP': str(share)})
        client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)

    def test_upload_creates_parent_dirs(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        import config
        self._setup(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        nested = '/课程/2026/结构图.pdf'  # 父目录都不存在
        r = client.post('/api/drive/upload/init',
                        json={'share': 'UP', 'path': nested}, headers=auth_headers)
        assert r.status_code == 200, r.json
        upload_id = r.json['data']['uploadId']
        client.put(f'/api/drive/upload/{upload_id}/0', data=b'PDF-DATA', headers=auth_headers)
        r = client.post(f'/api/drive/upload/{upload_id}/complete', headers=auth_headers)
        assert r.status_code == 200, r.json

        real = os.path.join(config.DRIVE_SHARES['UP'], '课程', '2026', '结构图.pdf')
        assert os.path.isfile(real)
        with open(real, 'rb') as f:
            assert f.read() == b'PDF-DATA'

    def test_upload_to_existing_dir_rejected(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        self._setup(client, admin_headers, auth_headers, tmp_path, monkeypatch)
        r = client.post('/api/drive/upload/init',
                        json={'share': 'UP', 'path': '/'}, headers=auth_headers)
        assert r.status_code == 400


class TestTrashSweep:
    """§1.1 超期自动清除：超过保留期（TRASH_RETAIN_DAYS=30）的回收站项被物理清理。"""

    @staticmethod
    def _backdate(db_path, sql):
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def test_remain_days_semantics(self, auth_headers, client):
        from core.trash import remain_days, is_expired
        from core.util import now_iso
        fresh = now_iso()
        assert remain_days(fresh) in (29, 30)  # 刚删除：约 30 天（毫秒级取整可能 29）
        assert not is_expired(fresh)
        # 过期：40 天前
        import time
        old_ts = time.time() - 40 * 86400
        old = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(old_ts))
        assert remain_days(old) == 0
        assert is_expired(old)

    def test_photos_sweep(self, client, auth_headers):
        from core import file_store
        from plugins.photos import db as pdb
        # 建照片并软删进回收站
        pid, data = _upload_photo(client, auth_headers, 'sweep-photo')
        orig = data['origFileId']
        client.delete(f'/api/photos/{pid}', headers=auth_headers)
        assert os.path.isfile(file_store.path_for(orig, 'orig'))

        # 把 deleted_at 回拨到 40 天前
        self._backdate(pdb.PHOTOS_DB,
                       f"UPDATE photos SET deleted_at='2000-01-01T00:00:00Z' WHERE id='{pid}'")

        from plugins.photos.api import sweep_expired
        assert sweep_expired() == 1
        assert _ref_count(orig) == 0
        file_store.gc()
        assert not os.path.isfile(file_store.path_for(orig, 'orig'))

    def test_drive_sweep(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        import config
        from plugins.drive import db as ddb
        share = tmp_path / 'sweepshare'
        share.mkdir(parents=True, exist_ok=True)
        (share / 'gone.txt').write_text('x')
        monkeypatch.setattr(config, 'DRIVE_SHARES', {'SW': str(share)})
        client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)
        client.delete('/api/drive/delete', query_string={'share': 'SW', 'path': '/gone.txt'},
                      headers=auth_headers)
        trash_dir = share / '回收站'
        assert any(trash_dir.iterdir())  # 回收站里有物理文件

        self._backdate(ddb.DRIVE_DB,
                       "UPDATE trash_index SET deleted_at='2000-01-01T00:00:00Z'")
        from plugins.drive.api import sweep_expired
        assert sweep_expired() == 1
        assert not any(trash_dir.iterdir())

    def test_notes_sweep(self, client, auth_headers):
        from plugins.notes import db as ndb
        r = client.post('/api/notes', json={'title': 'sweep note', 'content': 'x'},
                        headers=auth_headers)
        nid = r.json['data']['id']
        client.delete(f'/api/notes/{nid}', headers=auth_headers)
        self._backdate(ndb.NOTES_DB,
                       f"UPDATE notes SET deleted_at='2000-01-01T00:00:00Z' WHERE id='{nid}'")
        from plugins.notes.notes_api import sweep_expired
        assert sweep_expired() == 1
        import sqlite3
        conn = sqlite3.connect(ndb.NOTES_DB)
        try:
            assert conn.execute('SELECT COUNT(*) FROM notes WHERE id=?', (nid,)).fetchone()[0] == 0
        finally:
            conn.close()

    def test_trash_sweep_dispatch(self):
        """core/trash.sweep_expired 经插件注册表分派到各插件。"""
        import core.trash as trash
        from core.plugin_registry import PluginRegistry
        from plugins.photos.plugin import PLUGIN as _p
        registry = PluginRegistry(enabled=['photos', 'drive', 'notes'])
        registry.plugins = {n: getattr(__import__(f'plugins.{n}.plugin', fromlist=['PLUGIN']), 'PLUGIN')
                            for n in ['photos', 'drive', 'notes']}
        trash.set_registry(registry)
        result = trash.sweep_expired()
        assert set(result.keys()) >= {'photos', 'drive', 'notes'}


class TestSqliteBackup:
    """§8 SQLite 一致性备份（sqlite3 .backup，WAL 安全）。"""

    def test_all_dbs_backed_up(self, backup_dir):
        for name in ('core', 'notes', 'photos', 'drive'):
            p = os.path.join(backup_dir, f'{name}.db')
            assert os.path.isfile(p), f'{name}.db 应已备份'

    def test_backup_integrity(self, backup_dir):
        for name in ('core', 'notes', 'photos', 'drive'):
            with sqlite3.connect(os.path.join(backup_dir, f'{name}.db')) as c:
                row = c.execute('PRAGMA integrity_check').fetchone()
                assert row[0] == 'ok'
