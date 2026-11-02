"""TAE CLI 入口：python -m tae.cli.main <子命令>。

子命令：
- serve   读取 tae-config.yaml → 打包 → 启动临时 HTTP 服务 → 打印客户端命令
- package 仅打包项目并打印包路径与哈希（不启动服务）
- deploy  客户端部署：从服务端拉取清单与包并执行（委托 tae.client.deploy）
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from typing import Optional

from ..exceptions import TAEError
from .config import DeployConfig
from .packager import create_package
from .server import DeployServer

logger = logging.getLogger("tae.cli")


def _local_ip() -> str:
    """尽力获取本机局域网 IP，失败时回退到 localhost。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # 仅触发路由，不真正发包
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def cmd_serve(args: argparse.Namespace) -> int:
    config = DeployConfig.from_file(args.config)

    package_path, package_hash = create_package(
        config.project_dir, config.ignore_file
    )
    server = DeployServer(config, package_path, package_hash, port=config.port)
    server.start()
    try:
        display_host = args.advertise or _local_ip()
        print("=" * 60)
        print("TAE 热部署服务已启动")
        print(f"  服务端: http://{display_host}:{server.port}")
        print(f"  公钥指纹: {server.public_key_fingerprint()}")
        print(f"  部署包: {os.path.basename(package_path)} (sha256={package_hash[:16]}…)")
        print(f"  目标目录: {config.target_dir}")
        print()
        print("在目标服务器上运行客户端部署命令：")
        print(f"  python -m tae.client.deploy --server http://{display_host}:{server.port}"
              f" --token {config.token}")
        print("=" * 60)
        # 阻塞等待 Ctrl+C（无需常驻系统服务）
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止部署服务…")
    finally:
        server.stop()
        _cleanup_package(package_path)
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    config = DeployConfig.from_file(args.config)
    package_path, package_hash = create_package(
        config.project_dir, config.ignore_file
    )
    print(f"包路径: {package_path}")
    print(f"SHA256: {package_hash}")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    from ..client.deploy import run

    return run(args.server, args.token, workspace=args.workspace)


def _cleanup_package(package_path: str) -> None:
    try:
        os.remove(package_path)
        logger.info("已清理临时部署包: %s", package_path)
    except OSError as e:
        logger.warning("清理部署包失败: %s", e)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tae", description="TAE 热更新工具")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="打包并启动临时部署服务")
    serve.add_argument("--config", default="tae-config.yaml", help="配置文件路径")
    serve.add_argument("--advertise", default=None, help="对外公布的 IP（默认自动探测）")
    serve.set_defaults(func=cmd_serve)

    pkg = sub.add_parser("package", help="仅打包项目")
    pkg.add_argument("--config", default="tae-config.yaml", help="配置文件路径")
    pkg.set_defaults(func=cmd_package)

    deploy = sub.add_parser("deploy", help="客户端：执行部署")
    deploy.add_argument("--server", required=True, help="服务端地址，如 http://192.168.1.10:8000")
    deploy.add_argument("--token", required=True, help="部署凭证 token")
    deploy.add_argument("--workspace", default=None, help="工作目录（默认系统临时目录）")
    deploy.set_defaults(func=cmd_deploy)

    return parser


def main(argv: Optional[list] = None) -> int:
    # CLI 必须有可见日志（serve 启动信息、公钥指纹等）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TAEError as e:
        logger.error("%s", e)
        print(f"错误: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
