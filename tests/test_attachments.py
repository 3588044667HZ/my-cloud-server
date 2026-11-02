"""附件两阶段上传测试（ATTACHMENT_TWO_PHASE_UPLOAD_SPEC）。"""
import io
import uuid

from PIL import Image


def _make_jpeg_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (200, 100), (255, 0, 0)).save(buf, 'JPEG')
    return buf.getvalue()


class TestTwoPhaseUpload:
    def test_register_pending(self, client, auth_headers):
        r = client.post('/api/attachments',
                        json={'attachId': 'att-' + uuid.uuid4().hex, 'noteId': 'n1', 'type': 0},
                        headers=auth_headers)
        assert r.status_code in (200, 201)
        assert r.json['data']['state'] == 'pending'

    def test_register_bad_type(self, client, auth_headers):
        r = client.post('/api/attachments', json={'type': 9}, headers=auth_headers)
        assert r.status_code == 400
        assert r.json['code'] == 10415

    def test_idempotent_register(self, client, auth_headers):
        attach_id = 'att-' + uuid.uuid4().hex
        r1 = client.post('/api/attachments', json={'attachId': attach_id, 'type': 0},
                         headers=auth_headers)
        r2 = client.post('/api/attachments', json={'attachId': attach_id, 'type': 0},
                         headers=auth_headers)
        assert r1.status_code == 201
        assert r2.status_code == 200

    def test_conflict_10402(self, client, auth_headers):
        attach_id = 'att-' + uuid.uuid4().hex
        client.post('/api/attachments', json={'attachId': attach_id, 'type': 0},
                    headers=auth_headers)
        r = client.post('/api/attachments', json={'attachId': attach_id, 'type': 1},
                        headers=auth_headers)
        assert r.status_code == 409
        assert r.json['code'] == 10402

    def test_upload_ready_and_download(self, client, auth_headers):
        attach_id = 'att-' + uuid.uuid4().hex
        client.post('/api/attachments', json={'attachId': attach_id, 'type': 0},
                    headers=auth_headers)
        jpg = _make_jpeg_bytes()
        r = client.put(f'/api/attachments/{attach_id}/file', data=jpg, headers=auth_headers)
        assert r.status_code == 200
        assert r.json['data']['state'] == 'ready'

        r = client.get(f'/api/attachments/{attach_id}/download?size=thumb')
        assert r.status_code == 200
        assert r.headers.get('X-Attachment-State') == 'ready'
        r.close()

    def test_download_never_404(self, client):
        r = client.get('/api/attachments/does-not-exist/download')
        assert r.status_code == 200
        assert r.headers.get('X-Attachment-State') == 'missing'
        r.close()

    def test_pending_placeholder(self, client, auth_headers):
        attach_id = 'att-' + uuid.uuid4().hex
        client.post('/api/attachments', json={'attachId': attach_id, 'type': 0},
                    headers=auth_headers)
        r = client.get(f'/api/attachments/{attach_id}/download')
        assert r.status_code == 200
        assert r.headers.get('X-Attachment-State') == 'pending'
        r.close()

    def test_upload_with_bad_md5(self, client, auth_headers):
        import base64
        attach_id = 'att-' + uuid.uuid4().hex
        client.post('/api/attachments', json={'attachId': attach_id, 'type': 0},
                    headers=auth_headers)
        jpg = _make_jpeg_bytes()
        bad = base64.b64encode(b'not-the-md5').decode()
        r = client.put(f'/api/attachments/{attach_id}/file', data=jpg,
                       headers={**auth_headers, 'Content-MD5': bad})
        assert r.status_code == 400
        assert r.json['code'] == 10405

    def test_upload_without_register_404(self, client, auth_headers):
        r = client.put('/api/attachments/no-such-id/file', data=b'xxx', headers=auth_headers)
        assert r.status_code == 404
        assert r.json['code'] == 10404


class TestLegacyUpload:
    def test_single_step_upload(self, client, auth_headers):
        jpg = _make_jpeg_bytes()
        data = {'file': (io.BytesIO(jpg), 'pic.jpg'), 'noteId': 'n1'}
        r = client.post('/api/files/upload', data=data, headers=auth_headers,
                        content_type='multipart/form-data')
        assert r.status_code == 200
        assert 'fileId' in r.json['data']
