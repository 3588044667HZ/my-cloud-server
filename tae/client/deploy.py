"""客户端一次性部署（目标服务器端）。

流程（stage2-pached.md §5.1）：
1. 携带 token 拉取客户端公钥与签名 Action List；
2. 校验 token（HTTP 状态码）+ 客户端验签（Executor 内完成）；
3. 下载部署包到本地临时目录，二次校验哈希；
4. 把清单中 archive.extract 的下载 URL 替换为本地 file:// 路径，
   交给核心 Executor 执行（此后全程离线，无需再访问服务端）；
5. 打印结果并退出（不留常驻进程）。

平台钩子使用通用实现（subprocess 执行命令，与 MockHooks 等价），
Linux 上的 kill/nohup 等命令由用户配置在 tae-config.yaml 中。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
from typing import Optional

import requests

from ..downloader import DownloadManager
from ..exceptions import SignatureVerificationError, TAEError
from ..executor import Executor
from ..hooks import MockHooks
from ..models import ActionList, ArchiveExtractAction
from ..reporter import Reporter
from ..state import StateStore

logger = logging.getLogger("tae.deploy")

_CLIENT_VERSION = "0.0.0"  # 客户端自身版本（不参与比较，targetVersion=local 已容错）


def run(
    server_url: str,
    token: str,
    workspace: Optional[str] = None,
    timeout: int = 60,
) -> int:
    """执行一次部署，返回进程退出码（0 成功，非 0 失败）。"""
    base = server_url.rstrip("/")
    if workspace is None:
        workspace = tempfile.mkdtemp(prefix="tae-deploy-")
    os.makedirs(workspace, exist_ok=True)
    # token 经 Authorization 头传递（不进 URL，避免出现在访问日志/历史记录）
    auth_headers = {"Authorization": f"Bearer {token}"}

    # 1. 拉取公钥（token 保护；生产环境应内置公钥）
    try:
        pub_resp = requests.get(
            f"{base}/public_key", headers=auth_headers, timeout=timeout
        )
    except requests.RequestException as e:
        print(f"错误: 无法连接服务端 {base}: {e}", file=sys.stderr)
        return 1
    if pub_resp.status_code == 403:
        print("错误: token 无效（服务端拒绝）", file=sys.stderr)
        return 2
    pub_resp.raise_for_status()
    public_key = pub_resp.content
    _print_public_key_info(public_key)
    logger.info("已获取客户端公钥（%d 字节）", len(public_key))

    # 诊断产物：公钥与清单原文落盘，验签失败时可离线对比（对比完可删除）
    with open(os.path.join(workspace, "pubkey.bin"), "wb") as f:
        f.write(public_key)

    # 2. 拉取签名 Action List
    try:
        list_resp = requests.get(
            f"{base}/action_list", headers=auth_headers, timeout=timeout
        )
    except requests.RequestException as e:
        print(f"错误: 无法获取动作清单: {e}", file=sys.stderr)
        return 1
    if list_resp.status_code == 403:
        print("错误: token 无效（服务端拒绝）", file=sys.stderr)
        return 2
    list_resp.raise_for_status()
    # 强制按 UTF-8 解码响应字节（服务端已显式 charset=utf-8 输出；
    # 不依赖 requests 对无 charset 响应的编码猜测）
    raw_list_text = list_resp.content.decode("utf-8")
    action_list = ActionList.from_json(raw_list_text)
    logger.info(
        "已获取动作清单 update=%s target=%s actions=%d",
        action_list.update_id, action_list.target_version, len(action_list.actions),
    )
    # 诊断产物：清单原文落盘（验签失败时可与开发机 curl 结果对比）
    with open(os.path.join(workspace, "action_list.json"), "w", encoding="utf-8") as f:
        f.write(raw_list_text)
    # 诊断日志：验签前的规范字节哈希，与 serve 端签名时日志对比可定位内容差异
    _print_canonical_hash(action_list, "客户端收到的清单 canonical sha256")

    # 3. 预下载部署包到执行器的确定性缓存路径
    #    （清单保持原样执行，签名不受影响；DownloadManager 幂等命中缓存，
    #    部署中途服务端断开也能用本地包完成）
    try:
        _predownload_package(action_list, workspace, timeout, token)
    except (TAEError, requests.RequestException) as e:
        logger.error("部署包下载失败: %s", e)
        print(f"错误: 部署包下载失败: {e}", file=sys.stderr)
        return 1

    # 4. 核心执行器执行
    hooks = MockHooks(
        device_uuid="tae-deploy-client",
        current_version=_CLIENT_VERSION,
        public_key=public_key,
    )
    state = StateStore(os.path.join(workspace, "state.db"))
    downloader = DownloadManager(os.path.join(workspace, "cache"))
    reporter = Reporter(async_enabled=False)
    executor = Executor(hooks, state, downloader, reporter, public_key)
    try:
        ok = executor.run_action_list(action_list)
    except SignatureVerificationError as e:
        # 验签失败：补充诊断信息（canonical 哈希在 models/security 日志中已打印）
        _print_canonical_hash(action_list, "客户端验签失败时的 canonical sha256")
        print(
            f"诊断: 原始响应已保存到 {os.path.join(workspace, 'action_list.json')} "
            f"和 {os.path.join(workspace, 'pubkey.bin')}，可在开发机用 curl 对比是否被改动",
            file=sys.stderr,
        )
        reporter.close()
        state.close()
        raise e
    except Exception:
        reporter.close()
        state.close()
        raise
    reporter.close()

    print(f"\n部署结果: {'成功' if ok else '失败'} (update={action_list.update_id})")
    for row in state.get_all_actions(action_list.update_id):
        rollback = row["rollback_status"] or "-"
        print(
            f"  {row['action_id']:<16} {row['status']:<12} "
            f"attempt={row['attempt']} rollback={rollback}"
        )
    print(f"状态库: {state.db_path}")
    state.close()
    return 0 if ok else 1


def _predownload_package(
    action_list: ActionList, workspace: str, timeout: int, token: str
) -> None:
    """预下载部署包到 DownloadManager 的确定性缓存路径。

    缓存路径 = downloader.cache_path(action_id, 期望哈希)，与执行时
    _download_to_cache 计算一致，幂等命中后 DownloadManager 直接跳过下载。
    清单本身不做任何修改，签名保持有效。
    """
    downloader = DownloadManager(os.path.join(workspace, "cache"))
    for action in action_list.actions:
        if not isinstance(action, ArchiveExtractAction):
            continue
        if not action.download or not action.download.urls:
            raise TAEError(f"archive.extract 动作 {action.id} 缺少下载源")
        url = action.download.urls[0].url
        expected_hash = action.download.primary_hash

        # token 经 Authorization 头传递（包 URL 不含 token）
        resp = requests.get(
            url, timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        content = resp.content

        # 下载后哈希校验（防传输篡改，与 downloader 语义一致）
        if expected_hash and expected_hash.value:
            actual = hashlib.sha256(content).hexdigest()
            if actual != expected_hash.value:
                raise TAEError(
                    f"部署包哈希不符: 期望 {expected_hash.value[:16]}… "
                    f"实际 {actual[:16]}…"
                )

        cache_path = downloader.cache_path(action.id, expected_hash)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(content)
        logger.info(
            "部署包已预下载到缓存: %s (%d 字节)", cache_path, len(content)
        )
        return

    raise TAEError("清单中未找到 archive.extract 部署动作")


def _print_public_key_info(public_key: bytes) -> None:
    """打印公钥指纹，便于与服务端 serve 输出的指纹对账（验签失败时定位）。"""
    import hashlib

    fingerprint = hashlib.sha256(public_key).hexdigest()[:16]
    print(f"服务端公钥指纹: {fingerprint} (长度 {len(public_key)} 字节)")


def _print_canonical_hash(action_list: ActionList, label: str) -> None:
    """打印清单规范字节的 sha256（前 16 位），用于签名内容对账。"""
    import hashlib

    digest = hashlib.sha256(action_list.canonical_bytes()).hexdigest()[:16]
    print(f"{label}: {digest}")


def main(argv: Optional[list] = None) -> int:
    """CLI 入口：python -m tae.client.deploy --server URL --token TOKEN。"""
    import argparse

    parser = argparse.ArgumentParser(prog="tae deploy", description="TAE 客户端部署")
    parser.add_argument("--server", required=True, help="服务端地址，如 http://192.168.1.10:8000")
    parser.add_argument("--token", required=True, help="部署凭证 token")
    parser.add_argument("--workspace", default=None, help="工作目录（默认系统临时目录）")
    args = parser.parse_args(argv)
    return run(args.server, args.token, workspace=args.workspace)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run", "main"]
