"""drive 传输层：分片写盘 / 原子落盘 / 文件夹打包（ARCHITECTURE.md §6.5/§6.8）。

- Range 下载直接用 Flask send_file(conditional=True)（HTTP 206，见 api.download）。
- 分片上传：chunk 落临时目录 → complete 时合并 + md5 校验 → 原子写入目标（沙箱内）。
- 下载整个文件夹：增量写 ZIP（不整读进内存），响应流式发出。
"""
import hashlib
import os
import posixpath
import shutil
import tempfile
import zipfile

from config import DRIVE_HIDDEN_DIRS
from plugins.drive.resolver import is_within_root


def merge_chunks(chunk_dir, out_path):
    """按文件名（零填充序号）顺序合并分片到 out_path；返回合并后的字节数。"""
    parts = sorted(p for p in os.listdir(chunk_dir) if p.endswith('.part'))
    if not parts:
        raise ValueError('no chunks uploaded')
    with open(out_path, 'wb') as out:
        for p in parts:
            with open(os.path.join(chunk_dir, p), 'rb') as f:
                shutil.copyfileobj(f, out)
    return os.path.getsize(out_path)


def verify_md5(path, expected):
    """校验文件 md5 与期望一致。"""
    if not expected:
        return True
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest() == expected


def atomic_write(root, rel_path, tmp_path):
    """把合并后的临时文件原子写入沙箱内目标（覆盖语义，目录自动创建）。"""
    real_root = os.path.realpath(root)
    rel = rel_path.replace('\\', '/').lstrip('/')
    real = os.path.realpath(os.path.join(real_root, rel))
    if not is_within_root(real, real_root):
        raise ValueError('path escapes share root')
    os.makedirs(os.path.dirname(real), exist_ok=True)
    if os.path.isfile(real):
        os.remove(real)  # 覆盖语义（幂等）
    shutil.move(tmp_path, real)
    return real


def zip_folder(share, real, path):
    """把文件夹增量打成 ZIP（逐文件写入临时文件，不整读进内存）。

    - ZIP 根目录：path=='/' 用 share 名，否则用文件夹名；备份目录默认不打包。
    - 返回 (临时zip路径, 下载文件名)；调用方负责响应后删除临时文件。
    """
    zip_root = share if (path == '/' or not path) else os.path.basename(real.rstrip(os.sep))
    zip_name = f'{zip_root}.zip'
    tmp = tempfile.NamedTemporaryFile(prefix='drive_zip_', suffix='.zip', delete=False)
    tmp_path = tmp.name
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, filenames in os.walk(real):
                dirnames[:] = [d for d in dirnames if d not in DRIVE_HIDDEN_DIRS]
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, real).replace(os.sep, '/')
                    arc = posixpath.join(zip_root, rel)
                    try:
                        zf.write(full, arc)
                    except OSError:
                        continue  # 单文件读失败不中断打包
    except Exception:
        os.remove(tmp_path)
        raise
    return tmp_path, zip_name
