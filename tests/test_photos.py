"""photos 插件测试：register / 整包上传 / 分片 / sync / download / 永不404。"""
import io
import uuid

from PIL import Image


def _make_jpeg_bytes(size=(300, 200)):
    buf = io.BytesIO()
    Image.new('RGB', size, (0, 128, 255)).save(buf, 'JPEG')
    return buf.getvalue()


class TestRegister:
    def test_register_idempotent(self, client, auth_headers):
        client_id = 'pc-' + uuid.uuid4().hex
        body = {'clientId': client_id, 'mediaId': 'm1', 'filename': 'a.jpg',
                'width': 300, 'height': 200, 'size': 1234, 'md5': ''}
        r1 = client.post('/api/photos/register', json=body, headers=auth_headers)
        r2 = client.post('/api/photos/register', json=body, headers=auth_headers)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json['data']['id'] == r2.json['data']['id']

    def test_register_requires_client_id(self, client, auth_headers):
        r = client.post('/api/photos/register', json={}, headers=auth_headers)
        assert r.status_code == 400


class TestUpload:
    def test_whole_file(self, client, auth_headers):
        client_id = 'pc-' + uuid.uuid4().hex
        r = client.post('/api/photos/register',
                        json={'clientId': client_id, 'mediaId': 'm2', 'md5': ''},
                        headers=auth_headers)
        pid = r.json['data']['id']
        jpg = _make_jpeg_bytes()
        r = client.put(f'/api/photos/{pid}/file', data=jpg, headers=auth_headers)
        assert r.status_code == 200
        data = r.json['data']
        assert data['origFileId'] and data['thumbFileId']

        # 下载（匿名允许，永不 404）
        r = client.get(f'/api/photos/{pid}/download?size=thumb')
        assert r.status_code == 200
        assert r.headers.get('X-Photo-State') == 'ready'
        r.close()

    def test_upload_missing_photo(self, client, auth_headers):
        r = client.put('/api/photos/no-such/file', data=b'x', headers=auth_headers)
        assert r.status_code == 404


class TestChunked:
    def test_chunked_flow(self, client, auth_headers):
        r = client.post('/api/photos/upload/init', json={'md5': ''}, headers=auth_headers)
        upload_id = r.json['data']['uploadId']
        assert upload_id
        jpg = _make_jpeg_bytes()
        half = len(jpg) // 2
        r = client.put(f'/api/photos/upload/{upload_id}/0', data=jpg[:half], headers=auth_headers)
        assert r.status_code == 200
        r = client.put(f'/api/photos/upload/{upload_id}/1', data=jpg[half:], headers=auth_headers)
        assert r.status_code == 200
        r = client.post(f'/api/photos/upload/{upload_id}/complete', headers=auth_headers)
        assert r.status_code == 200
        assert r.json['data']['origFileId']

    def test_chunked_md5_mismatch(self, client, auth_headers):
        import hashlib
        r = client.post('/api/photos/upload/init',
                        json={'md5': '0' * 32}, headers=auth_headers)
        upload_id = r.json['data']['uploadId']
        jpg = _make_jpeg_bytes()
        client.put(f'/api/photos/upload/{upload_id}/0', data=jpg, headers=auth_headers)
        r = client.post(f'/api/photos/upload/{upload_id}/complete', headers=auth_headers)
        assert r.status_code == 400
        assert r.json['code'] == 10405

    def test_chunk_idempotent_retransmit(self, client, auth_headers):
        r = client.post('/api/photos/upload/init', json={'md5': ''}, headers=auth_headers)
        upload_id = r.json['data']['uploadId']
        jpg = _make_jpeg_bytes()
        r1 = client.put(f'/api/photos/upload/{upload_id}/0', data=jpg, headers=auth_headers)
        r2 = client.put(f'/api/photos/upload/{upload_id}/0', data=jpg, headers=auth_headers)
        assert r1.json['data']['received'] == 1
        assert r2.json['data']['received'] == 1  # 重传不重复计数


class TestSyncDownload:
    def test_sync(self, client, auth_headers):
        r = client.get('/api/photos/sync', headers=auth_headers)
        assert r.status_code == 200
        body = r.json['data']
        assert 'updated' in body and 'deletedIds' in body and 'serverTime' in body

    def test_tombstone(self, client, auth_headers):
        client_id = 'pc-' + uuid.uuid4().hex
        r = client.post('/api/photos/register', json={'clientId': client_id},
                        headers=auth_headers)
        pid = r.json['data']['id']
        r = client.delete(f'/api/photos/{pid}', headers=auth_headers)
        assert r.status_code == 200
        r = client.get('/api/photos/sync?since=2020-01-01T00:00:00Z', headers=auth_headers)
        assert pid in r.json['data']['deletedIds']

    def test_download_missing(self, client):
        r = client.get('/api/photos/nope/download')
        assert r.status_code == 200
        assert r.headers.get('X-Photo-State') == 'missing'
        r.close()

    def test_download_preserves_extension(self, client, auth_headers):
        """下载响应 Content-Disposition 保留原始文件名/扩展名（内容寻址不丢后缀）。"""
        client_id = 'ext-' + uuid.uuid4().hex
        r = client.post('/api/photos/register',
                        json={'clientId': client_id, 'filename': 'photo.jpg'},
                        headers=auth_headers)
        pid = r.json['data']['id']
        client.put(f'/api/photos/{pid}/file', data=_make_jpeg_bytes(), headers=auth_headers)

        r = client.get(f'/api/photos/{pid}/download')
        cd = r.headers.get('Content-Disposition', '')
        assert 'photo.jpg' in cd or 'photo.jpg' in r.headers.get('Content-Disposition', '')
        r.close()

        r = client.get(f'/api/photos/{pid}/download?size=thumb')
        assert '.webp' in r.headers.get('Content-Disposition', '')
        r.close()

    def test_download_appends_extension_if_missing(self, client, auth_headers):
        client_id = 'noext-' + uuid.uuid4().hex
        r = client.post('/api/photos/register',
                        json={'clientId': client_id, 'filename': 'screenshot'},
                        headers=auth_headers)
        pid = r.json['data']['id']
        client.put(f'/api/photos/{pid}/file', data=_make_jpeg_bytes(), headers=auth_headers)
        r = client.get(f'/api/photos/{pid}/download')
        assert 'screenshot.jpg' in r.headers.get('Content-Disposition', '')
        r.close()

    def test_space(self, client, auth_headers):
        r = client.get('/api/space', headers=auth_headers)
        assert r.status_code == 200
        assert 'free' in r.json['data'] and 'threshold' in r.json['data']
