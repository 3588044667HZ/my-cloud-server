"""SPEC-UPLOAD-TRANSPORT.md 测试：秒传 / haveBlocks 续传 / 块 CRC32 / 幂等 complete / batch-check / TTL。"""
import io
import os
import uuid

import pytest
from PIL import Image

from core.util import crc32_hex


_counter = [0]


def _jpg():
    _counter[0] += 1
    n = _counter[0]
    buf = io.BytesIO()
    Image.new('RGB', (200 + n % 80, 150), (n % 255, 90, 180)).save(buf, 'JPEG')
    return buf.getvalue()


def _md5(data):
    import hashlib
    return hashlib.md5(data).hexdigest()


def _drive_share(client, admin_headers, tmp_path, monkeypatch, name='T'):
    import config
    share = tmp_path / name
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, 'DRIVE_SHARES', {name.upper(): str(share)})
    client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)
    return share


class TestDriveTransport:
    def test_init_secfast_and_upload(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        share = _drive_share(client, admin_headers, tmp_path, monkeypatch, 'T1')
        data = b'0123456789abcdef'
        # 秒传：目标已存在同内容
        (share / 'a.bin').write_bytes(data)
        md5 = _md5(data)
        r = client.post('/api/drive/upload/init',
                        json={'share': 'T1', 'path': '/a.bin', 'md5': md5}, headers=auth_headers)
        assert r.json['data']['exists'] is True

        # 新会话 + haveBlocks 空
        r = client.post('/api/drive/upload/init',
                        json={'share': 'T1', 'path': '/b.bin', 'md5': md5}, headers=auth_headers)
        d = r.json['data']
        assert d['exists'] is False and d['haveBlocks'] == []
        uid = d['uploadId']
        # 分片
        parts = [data[:4], data[4:8], data[8:]]
        for i, p in enumerate(parts):
            headers = {**auth_headers, 'X-Chunk-CRC32': crc32_hex(p)}
            r = client.put(f'/api/drive/upload/{uid}/{i}', data=p, headers=headers)
            assert r.status_code == 200
        r = client.post(f'/api/drive/upload/{uid}/complete', headers=auth_headers)
        assert r.status_code == 200
        assert (share / 'b.bin').read_bytes() == data

    def test_resume_have_blocks(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        _drive_share(client, admin_headers, tmp_path, monkeypatch, 'T2')
        data = b'ABCDEFGHIJ'
        r = client.post('/api/drive/upload/init',
                        json={'share': 'T2', 'path': '/c.bin', 'md5': _md5(data)},
                        headers=auth_headers)
        uid = r.json['data']['uploadId']
        client.put(f'/api/drive/upload/{uid}/0', data=data[:4], headers=auth_headers)
        # 续传：init 带 uploadId → 服务端返回已收块
        r = client.post('/api/drive/upload/init',
                        json={'share': 'T2', 'path': '/c.bin', 'md5': _md5(data),
                              'uploadId': uid},
                        headers=auth_headers)
        assert r.json['data']['uploadId'] == uid
        assert r.json['data']['haveBlocks'] == [0]

    def test_chunk_crc_mismatch(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        _drive_share(client, admin_headers, tmp_path, monkeypatch, 'T3')
        data = b'HELLOWORLD'
        r = client.post('/api/drive/upload/init', json={'share': 'T3', 'path': '/d.bin'},
                        headers=auth_headers)
        uid = r.json['data']['uploadId']
        r = client.put(f'/api/drive/upload/{uid}/0', data=data[:4],
                       headers={**auth_headers, 'X-Chunk-CRC32': '00000000'})
        assert r.status_code == 400
        assert r.json['code'] == 10418

    def test_complete_idempotent(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        _drive_share(client, admin_headers, tmp_path, monkeypatch, 'T4')
        data = b'12345678'
        r = client.post('/api/drive/upload/init', json={'share': 'T4', 'path': '/e.bin'},
                        headers=auth_headers)
        uid = r.json['data']['uploadId']
        client.put(f'/api/drive/upload/{uid}/0', data=data, headers=auth_headers)
        r1 = client.post(f'/api/drive/upload/{uid}/complete', headers=auth_headers)
        assert r1.status_code == 200
        r2 = client.post(f'/api/drive/upload/{uid}/complete', headers=auth_headers)
        assert r2.status_code == 200
        assert r2.json['code'] == 0

    def test_batch_check(self, client, admin_headers, auth_headers, tmp_path, monkeypatch):
        share = _drive_share(client, admin_headers, tmp_path, monkeypatch, 'T5')
        (share / 'have.bin').write_bytes(b'known-content')
        r = client.post('/api/drive/upload/batch-check',
                        json={'share': 'T5', 'files': [
                            {'path': '/have.bin', 'md5': _md5(b'known-content')},
                            {'path': '/miss.bin', 'md5': _md5(b'not-there')},
                        ]}, headers=auth_headers)
        assert r.status_code == 200
        exists = r.json['data']['exists']
        assert _md5(b'known-content') in exists
        assert _md5(b'not-there') not in exists


class TestPhotosTransport:
    def _upload_to(self, client, auth_headers, client_id):
        jpg = _jpg()
        reg = client.post('/api/photos/register', json={'clientId': client_id, 'md5': _md5(jpg)},
                          headers=auth_headers)
        pid = reg.json['data']['id']
        r = client.post('/api/photos/upload/init', json={'photoId': pid, 'md5': _md5(jpg)},
                        headers=auth_headers)
        uid = r.json['data']['uploadId']
        client.put(f'/api/photos/upload/{uid}/0', data=jpg, headers=auth_headers)
        client.post(f'/api/photos/upload/{uid}/complete', headers=auth_headers)
        return pid, jpg

    def test_init_secfast(self, client, auth_headers):
        pid, jpg = self._upload_to(client, auth_headers, 'ph-sec-1')
        r = client.post('/api/photos/upload/init',
                        json={'photoId': pid, 'md5': _md5(jpg)}, headers=auth_headers)
        assert r.json['data']['exists'] is True

    def test_resume_have_blocks(self, client, auth_headers):
        jpg = _jpg()
        reg = client.post('/api/photos/register', json={'clientId': 'ph-res-1'},
                          headers=auth_headers)
        pid = reg.json['data']['id']
        r = client.post('/api/photos/upload/init', json={'photoId': pid}, headers=auth_headers)
        uid = r.json['data']['uploadId']
        client.put(f'/api/photos/upload/{uid}/0', data=jpg[:100], headers=auth_headers)
        r = client.post('/api/photos/upload/init',
                        json={'photoId': pid, 'uploadId': uid}, headers=auth_headers)
        assert r.json['data']['uploadId'] == uid
        assert r.json['data']['haveBlocks'] == [0]

    def test_chunk_crc_mismatch(self, client, auth_headers):
        jpg = _jpg()
        reg = client.post('/api/photos/register', json={'clientId': 'ph-crc-1'},
                          headers=auth_headers)
        pid = reg.json['data']['id']
        r = client.post('/api/photos/upload/init', json={'photoId': pid}, headers=auth_headers)
        uid = r.json['data']['uploadId']
        r = client.put(f'/api/photos/upload/{uid}/0', data=jpg[:100],
                       headers={**auth_headers, 'X-Chunk-CRC32': '00000000'})
        assert r.status_code == 400
        assert r.json['code'] == 10418

    def test_complete_idempotent(self, client, auth_headers):
        jpg = _jpg()
        reg = client.post('/api/photos/register', json={'clientId': 'ph-idem-1'},
                          headers=auth_headers)
        pid = reg.json['data']['id']
        r = client.post('/api/photos/upload/init', json={'photoId': pid}, headers=auth_headers)
        uid = r.json['data']['uploadId']
        client.put(f'/api/photos/upload/{uid}/0', data=jpg, headers=auth_headers)
        r1 = client.post(f'/api/photos/upload/{uid}/complete', headers=auth_headers)
        assert r1.status_code == 200
        r2 = client.post(f'/api/photos/upload/{uid}/complete', headers=auth_headers)
        assert r2.status_code == 200
        assert r2.json['code'] == 0
        assert r2.json['data']['origFileId']

    def test_batch_check(self, client, auth_headers):
        jpg = _jpg()
        reg = client.post('/api/photos/register', json={'clientId': 'ph-batch-1',
                                                        'md5': _md5(jpg)}, headers=auth_headers)
        pid = reg.json['data']['id']
        r = client.post('/api/photos/upload/init', json={'photoId': pid}, headers=auth_headers)
        uid = r.json['data']['uploadId']
        client.put(f'/api/photos/upload/{uid}/0', data=jpg, headers=auth_headers)
        client.post(f'/api/photos/upload/{uid}/complete', headers=auth_headers)
        r = client.post('/api/photos/upload/batch-check',
                        json={'files': [{'path': 'x.jpg', 'md5': _md5(jpg)},
                                        {'path': 'y.jpg', 'md5': '0' * 32}]},
                        headers=auth_headers)
        exists = r.json['data']['exists']
        assert _md5(jpg) in exists
        assert exists[_md5(jpg)]['id'] == pid
        assert '0' * 32 not in exists
