"""执行器主逻辑：状态机驱动的更新流程。

流程（与 client-executor.md 一致）：
1. 校验清单：签名、时效、schema、目标版本（降级需显式允许）、minClientVersion；
2. 拓扑排序解析依赖（循环依赖拒绝执行）；
3. 断电恢复：未完成的动作按状态决定重试或继续回滚；
4. 依次执行动作并持久化状态；失败按 onFailure 策略处理（abort/skip/rollback）；
5. 全部成功返回 True，失败时按逆序回滚已成功动作。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .actions import ActionExecutor, create_executor
from .downloader import DownloadManager
from .exceptions import (
    DependencyError,
    DowngradeRejectedError,
    ManifestExpiredError,
    TAEError,
)
from .hooks import PlatformHooks
from .models import Action, ActionList, Event
from .reporter import Reporter
from .security import load_public_key
from .state import (
    ROLLING_BACK,
    ROLLED_BACK,
    SKIPPED,
    UPDATE_COMPLETED,
    UPDATE_FAILED,
    StateStore,
)

logger = logging.getLogger(__name__)


def parse_version(version: str) -> Tuple[int, ...]:
    """把 "1.2.0-beta" 之类版本号解析为可比较的整数元组（忽略非数字段）。"""
    return tuple(int(x) for x in re.findall(r"\d+", version))


class Executor:
    """受信任务执行器。"""

    def __init__(
        self,
        hooks: PlatformHooks,
        state: StateStore,
        downloader: DownloadManager,
        reporter: Reporter,
        public_key: Any,
    ):
        self.hooks = hooks
        self.state = state
        self.downloader = downloader
        self.reporter = reporter
        self.public_key = load_public_key(public_key)
        # 动作执行器缓存：回滚必须复用执行时的实例（如归档动作的回滚
        # journal 只存在于执行实例的内存中）
        self._executors: Dict[str, ActionExecutor] = {}

    # ---- 对外入口 ----

    def run_action_list(
        self, action_list: ActionList, allow_downgrade: bool = False
    ) -> bool:
        """执行整个动作清单，返回是否全部成功。"""
        # 每次运行独立：回滚需复用本轮执行实例，但跨轮不共享（清单可能已变更）
        self._executors.clear()
        self._validate(action_list, allow_downgrade)
        update_id = action_list.update_id
        self.state.record_update_start(update_id)
        self.reporter.report(Event(
            "update_start", self.hooks.get_device_uuid(),
            {"update_id": update_id, "target_version": action_list.target_version},
        ))

        self._recover_incomplete(update_id, action_list)

        order = self._topological_sort(action_list)
        completed: List[Action] = []

        for action in order:
            if self._already_completed(update_id, action):
                logger.info("动作 %s 已完成（状态恢复）", action.id)
                completed.append(action)
                continue

            if not self._execute_action(update_id, action):
                if not self._handle_failure(update_id, action, completed):
                    self._finish(update_id, False)
                    return False
                # skip 策略：继续执行后续动作
                continue
            completed.append(action)

        self._finish(update_id, True)
        return True

    # ---- 校验 ----

    def _validate(self, action_list: ActionList, allow_downgrade: bool) -> None:
        """清单验签、时效、schema、客户端版本与目标版本校验（fail-closed）。"""
        action_list.validate_signature(self.public_key)

        if action_list.is_expired():
            raise ManifestExpiredError(
                f"清单已过期: {action_list.expires_at}", update_id=action_list.update_id
            )

        current = self.hooks.get_current_version()
        if action_list.min_client_version:
            if parse_version(current) < parse_version(action_list.min_client_version):
                raise TAEError(
                    f"客户端版本 {current} 低于要求的最低版本 "
                    f"{action_list.min_client_version}"
                )
        # 降级检查仅对可解析的版本号生效：非数字版本（如 "local"）是自定义
        # 标识，不参与版本顺序比较，视为不受降级限制（方案 B 容错）
        target = parse_version(action_list.target_version)
        if not allow_downgrade and target and target < parse_version(current):
            raise DowngradeRejectedError(
                f"目标版本 {action_list.target_version} 低于当前版本 {current}，"
                f"降级更新需显式开启",
                update_id=action_list.update_id,
            )
        logger.info(
            "清单校验通过: update=%s target=%s", action_list.update_id,
            action_list.target_version,
        )

    # ---- 依赖排序 ----

    def _topological_sort(self, action_list: ActionList) -> List[Action]:
        by_id: Dict[str, Action] = {a.id: a for a in action_list.actions}
        visited: Dict[str, int] = {}  # 0=未访问 1=访问中 2=完成
        order: List[Action] = []

        def visit(action: Action) -> None:
            status = visited.get(action.id, 0)
            if status == 2:
                return
            if status == 1:
                raise DependencyError(
                    f"检测到循环依赖: {action.id}", update_id=action_list.update_id
                )
            visited[action.id] = 1
            for dep in action.depends_on:
                visit(by_id[dep])  # _validate_ids 已保证依赖存在
            visited[action.id] = 2
            order.append(action)

        for action in action_list.actions:
            visit(action)
        logger.debug("执行顺序: %s", [a.id for a in order])
        return order

    # ---- 断电恢复 ----

    def _recover_incomplete(self, update_id: str, action_list: ActionList) -> None:
        """根据持久化状态决定重试还是继续回滚（client-executor.md §5）。"""
        by_id = {a.id: a for a in action_list.actions}
        for row in self.state.get_incomplete_actions(update_id):
            action_id = row["action_id"]
            status = row["status"]
            if status == ROLLING_BACK:
                # 上次断电时正在回滚：标记为已回滚，本次不再执行该动作
                logger.warning("发现未完成的回滚动作 %s，标记为已回滚", action_id)
                self.state.record_rollback_status(
                    update_id, action_id, ROLLED_BACK, "recovered"
                )
            elif status == SKIPPED:
                continue
            else:
                # 下载/校验/执行阶段断电：从当前动作起点幂等重试
                # （下载缓存命中 + 哈希校验保证重复执行安全）
                logger.warning("发现未完成动作 %s (status=%s)，将从起点重试",
                               action_id, status)
                if action_id in by_id:
                    self.state.record_action_status(update_id, action_id, "PENDING")
                else:
                    # 旧清单中的动作已不存在，直接标记失败
                    self.state.record_action_failure(
                        update_id, action_id, "动作已从清单中移除"
                    )

    def _already_completed(self, update_id: str, action: Action) -> bool:
        st = self.state.get_action_status(update_id, action.id)
        return bool(st and st["status"] in ("COMPLETED", "SKIPPED"))

    # ---- 执行 ----

    def _execute_action(self, update_id: str, action: Action) -> bool:
        executor = self._make_executor(action, update_id)
        self.state.record_action_start(update_id, action.id, action.type)
        self.reporter.report(Event(
            "action_start", self.hooks.get_device_uuid(),
            {"update_id": update_id, "action_id": action.id, "type": action.type},
        ))

        if not executor.can_execute():
            error = f"动作 {action.id} 前置条件不满足"
            self._record_failure(update_id, action.id, error)
            return False

        try:
            ok = executor.execute()
        except Exception as e:  # 边界防御：动作实现异常统一转为失败
            logger.exception("动作 %s 抛出异常", action.id)
            ok = False
            error = f"{type(e).__name__}: {e}"
            self.state.record_action_failure(update_id, action.id, error)
            self.reporter.report(Event(
                "action_failed", self.hooks.get_device_uuid(),
                {"update_id": update_id, "action_id": action.id, "error": error},
            ))
            return False

        if ok:
            self.state.record_action_success(update_id, action.id)
            self.reporter.report(Event(
                "action_completed", self.hooks.get_device_uuid(),
                {"update_id": update_id, "action_id": action.id},
            ))
            return True

        self._record_failure(update_id, action.id, f"动作 {action.id} 返回失败")
        return False

    def _record_failure(self, update_id: str, action_id: str, error: str) -> None:
        self.state.record_action_failure(update_id, action_id, error)
        self.reporter.report(Event(
            "action_failed", self.hooks.get_device_uuid(),
            {"update_id": update_id, "action_id": action_id, "error": error},
        ))

    def _handle_failure(
        self, update_id: str, failed: Action, completed: List[Action]
    ) -> bool:
        """失败策略分发：abort 停止；skip 跳过继续；rollback 逆序回滚。"""
        policy = failed.on_failure
        if policy == "skip":
            logger.warning("动作 %s 失败，按策略跳过", failed.id)
            self.state.record_action_status(update_id, failed.id, SKIPPED)
            return True

        if policy == "rollback":
            logger.warning("动作 %s 失败，触发回滚", failed.id)
            self._rollback(update_id, completed + [failed])
            return False

        logger.error("动作 %s 失败，按策略中止更新", failed.id)
        return False

    def _rollback(self, update_id: str, actions: List[Action]) -> None:
        """按逆序执行动作回滚；回滚自身失败仅记录严重错误并继续（§4）。"""
        for action in reversed(actions):
            executor = self._make_executor(action, update_id)
            if not executor.has_rollback():
                logger.info("动作 %s 无回滚定义，跳过", action.id)
                continue
            self.state.record_rollback_status(update_id, action.id, ROLLING_BACK, None)
            self.reporter.report(Event(
                "action_rolling_back", self.hooks.get_device_uuid(),
                {"update_id": update_id, "action_id": action.id},
            ))
            try:
                ok = executor.rollback()
            except Exception as e:  # 回滚失败不阻断后续回滚
                logger.exception("动作 %s 回滚抛出异常", action.id)
                ok = False
            if ok:
                self.state.record_rollback_status(update_id, action.id, ROLLED_BACK, None)
                logger.info("动作 %s 已回滚", action.id)
            else:
                self.state.record_rollback_status(
                    update_id, action.id, ROLLED_BACK, "回滚失败"
                )
                self.reporter.report(Event(
                    "rollback_failed", self.hooks.get_device_uuid(),
                    {"update_id": update_id, "action_id": action.id},
                ))
                logger.error("动作 %s 回滚失败（严重错误）", action.id)

    def _make_executor(self, action: Action, update_id: str) -> ActionExecutor:
        cached = self._executors.get(action.id)
        if cached is None:
            cached = create_executor(
                action, self.hooks, self.downloader, self.state, update_id
            )
            self._executors[action.id] = cached
        return cached

    def _finish(self, update_id: str, success: bool) -> None:
        status = UPDATE_COMPLETED if success else UPDATE_FAILED
        self.state.record_update_finish(update_id, status)
        self.reporter.report(Event(
            "update_finished", self.hooks.get_device_uuid(),
            {"update_id": update_id, "status": status},
        ))


__all__ = ["Executor", "parse_version"]
