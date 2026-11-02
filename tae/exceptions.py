"""自定义异常体系。

统一继承自 TAEError，便于上层一次捕获；每个异常类型对应一个失败场景。
"""

from typing import Optional


class TAEError(Exception):
    """所有 TAE 异常的基类。"""


class ManifestError(TAEError):
    """动作清单本身不合法（格式、字段缺失、依赖错误等）。"""

    def __init__(self, message: str, update_id: Optional[str] = None):
        self.update_id = update_id
        super().__init__(message)


class SignatureVerificationError(ManifestError):
    """清单签名无效或缺少签名。"""


class ManifestExpiredError(ManifestError):
    """清单已过期（防重放）。"""


class DowngradeRejectedError(ManifestError):
    """目标版本低于当前版本且未允许降级。"""


class UnsupportedSchemaError(ManifestError):
    """清单 schemaVersion 不被支持。"""


class DependencyError(ManifestError):
    """依赖关系错误：循环依赖或引用不存在的动作。"""


class DownloadError(TAEError):
    """所有下载源均失败。"""


class HashMismatchError(TAEError):
    """文件哈希校验失败。"""


class ActionExecutionError(TAEError):
    """动作执行失败（前置不满足、命令返回非零等）。"""


class RollbackError(TAEError):
    """回滚执行失败。"""


class StateError(TAEError):
    """状态持久化（SQLite）读写失败。"""
