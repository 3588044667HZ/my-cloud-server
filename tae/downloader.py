"""下载管理器：多源优先级切换 + 断点续传 + 哈希校验。

- 按优先级（数字小优先）依次尝试 URL；超时 / 4xx / 5xx 自动切换下一个源；
- 断点续传：使用 HTTP Range 头，已下载内容保留在 ``<dest>.part``；
- 下载完成（或目标已存在且哈希匹配）后校验哈希，通过则原子移动到 dest；
- 支持 file:// 协议，便于本地测试与离线 demo 完全在本地运行。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import urllib.parse
from typing import List, Optional

import requests

from .exceptions import DownloadError
from .models import DownloadItem, HashInfo
from .verifier import file_matches

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024
_PART_SUFFIX = ".part"


class DownloadManager:
    """下载管理器。``cache_dir`` 存放各动作的临时下载文件。"""

    def __init__(
        self,
        cache_dir: str,
        request_timeout: float = 30.0,
        verify_tls: bool = True,
    ):
        self.cache_dir = cache_dir
        self.request_timeout = request_timeout
        self.verify_tls = verify_tls
        os.makedirs(cache_dir, exist_ok=True)

    # ---- 公共入口 ----

    def download(
        self,
        downloads: List[DownloadItem],
        dest: str,
        expected_hash: Optional[HashInfo] = None,
        expected_size: Optional[int] = None,
    ) -> bool:
        """按优先级尝试所有源，任一个成功即返回 True。

        若 ``dest`` 已存在且哈希匹配，视为已下载完成（幂等，避免断电后重复下载）。
        """
        if len(downloads) == 0:
            logger.error("下载列表为空，目标: %s", dest)
            return False

        # 幂等检查：目标文件已存在且内容正确则直接复用
        if file_matches(dest, expected_hash, expected_size):
            logger.info("缓存命中，跳过下载: %s", dest)
            return True

        # 按优先级升序尝试各源
        for item in sorted(downloads, key=lambda i: i.priority):
            logger.info("尝试下载源 [priority=%s]: %s", item.priority, item.url)
            start = time.monotonic()
            try:
                self._download_single(item, dest, expected_size)
            except DownloadError as e:
                logger.warning("下载源失败 %s: %s", item.url, e)
                self._cleanup(dest)
                continue

            if not file_matches(dest, expected_hash, expected_size):
                logger.warning(
                    "下载后校验失败（hash=%s size=%s），文件: %s",
                    expected_hash, expected_size, dest,
                )
                self._cleanup(dest)
                continue

            logger.info(
                "下载完成: %s (%.2fs, %d bytes)", dest,
                time.monotonic() - start, os.path.getsize(dest),
            )
            return True

        return False

    def cache_path(self, action_id: str, hash_info: Optional[HashInfo]) -> str:
        """为动作生成确定性的缓存路径（含哈希前缀），断电重启后可复用。"""
        prefix = (hash_info.value[:12] if hash_info and hash_info.value else action_id)
        safe_action_id = "".join(
            c if c.isalnum() or c in "-_." else "_" for c in action_id
        )
        return os.path.join(self.cache_dir, f"{safe_action_id}-{prefix}")

    # ---- 内部实现 ----

    def _download_single(
        self, item: DownloadItem, dest: str, expected_size: Optional[int]
    ) -> None:
        """下载单个源到 dest（失败抛 DownloadError）。"""
        if item.url.startswith("file://"):
            self._download_local(item, dest)
        elif item.url.startswith(("http://", "https://")):
            self._download_http(item, dest, expected_size)
        else:
            raise DownloadError(f"不支持的协议: {item.url}")

    def _download_local(self, item: DownloadItem, dest: str) -> None:
        src_path = urllib.parse.unquote(
            urllib.parse.urlparse(item.url).path
        )
        # Windows 下 file:///C:/... 的路径解析
        if os.name == "nt" and src_path.startswith("/"):
            src_path = src_path.lstrip("/")
        if not os.path.isfile(src_path):
            raise DownloadError(f"本地源文件不存在: {src_path}")
        part = dest + _PART_SUFFIX
        # file:// 源直接整体复制（本地无断点续传需求）
        shutil.copyfile(src_path, part)
        os.replace(part, dest)

    def _download_http(
        self, item: DownloadItem, dest: str, expected_size: Optional[int]
    ) -> None:
        part = dest + _PART_SUFFIX
        resume_from = os.path.getsize(part) if os.path.isfile(part) else 0

        headers = {}
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
        try:
            resp = requests.get(
                item.url,
                headers=headers,
                stream=True,
                timeout=self.request_timeout,
                verify=self.verify_tls,
            )
        except requests.RequestException as e:
            raise DownloadError(f"HTTP 请求失败: {e}")

        if resp.status_code in (404, 410):
            raise DownloadError(f"HTTP {resp.status_code}: 资源不存在")
        if resp.status_code in (500, 502, 503, 504):
            raise DownloadError(f"HTTP {resp.status_code}: 服务端错误")
        if resp.status_code not in (200, 206):
            raise DownloadError(f"HTTP {resp.status_code}: 无法处理")
        if expected_size is not None:
            length = _content_length(resp)
            if length is not None and length > expected_size:
                raise DownloadError(
                    f"内容长度 {length} 超过预期 {expected_size}（疑似错误响应）"
                )

        mode = "ab" if resume_from > 0 and resp.status_code == 206 else "wb"
        try:
            with open(part, mode) as f:
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
        except OSError as e:
            raise DownloadError(f"写入临时文件失败: {e}")
        finally:
            resp.close()

        if expected_size is not None and os.path.getsize(part) < expected_size:
            # 服务端忽略 Range 或响应被截断，保留 part 供下次续传
            raise DownloadError(f"下载不完整: {os.path.getsize(part)}/{expected_size}")

        os.replace(part, dest)

    def _cleanup(self, dest: str) -> None:
        for p in (dest, dest + _PART_SUFFIX):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError as e:
                logger.warning("清理失败 %s: %s", p, e)


def _content_length(resp: requests.Response) -> Optional[int]:
    raw = resp.headers.get("Content-Length")
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


__all__ = ["DownloadManager"]
