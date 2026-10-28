"""面板快照读取层（阶段 B）：面板从库里读「调度器上一轮看到的读数」。

**为什么还要这一层**（A1 已经有 10s 进程内缓存了）
    A1 只是把重复请求压到 10s 一次，**到期那一刻仍然由某个访问者现付**：
    一次渲染 4 个 HL 只读端点（1.5–1.8s），K 线过期时更是 10–14s。而同一份账户读数
    调度器每 5 分钟本来就要读一次（hl_monitor），六步结论每根 4h bar 本来就要算一次
    （tick）。面板再自己打一遍 = 同一份数据在请求路径里重算第二遍。

    写进 `panel_snapshots` 之后，面板渲染是**零网络、零重算**的一次 SQLite 查询。

**红线**
    - 写只发生在调度器线程（`main.capture_panel_snapshot`），面板永不写。
    - 这张表是观测表：交易 / 风控 / 对账 / 看护一律不读它（与 `hl_snapshots` 同一约定）。
    - 快照缺失或过期时**自动回落到实时读取**（回落对象是 `PanelInfo`，即 A1 那层 10s 缓存），
      所以「调度器挂了」的后果是面板变慢，而不是面板空白或显示假数字。
    - 面板写操作（参数/模式/kill）后调用 `invalidate()`：下一次渲染强制走实时，别让人
      刚改完参数还看到 5 分钟前的旧解释。

**滞后是多少、为什么可接受**
    hl_monitor 每 5 分钟一轮 ⇒ 权益/挂单/现价最多滞后 5 分钟；六步结论跟着 tick 走（UTC
    :03/:33）⇒ 最多滞后一个 tick 周期。4h 级别的人工看盘不需要更细，而交易路径读的始终是
    `get_hl_info()` 的实时值，与本模块无关。
"""
from __future__ import annotations

import copy
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 超过这个年龄就不再用快照（回落实时读取）。默认 15 分钟 = 连丢 3 轮 monitor 才回落。
# 选 15 而不是 5：APScheduler 偶发迟到、容器重启后的第一个 5 分钟窗口都不该让面板突然变慢。
DEFAULT_STALE_S = 900.0


class PanelSnapshotStore:
    """`panel_snapshots` 的读写门面（SQL 全部在 `app.db.DB` 里）。"""

    def __init__(self, db: Any, keep: int = 288) -> None:
        self.db = db
        self.keep = int(keep)

    def write(self, snapshot: Dict[str, Any], mids: Dict[str, Any],
              why: Optional[List[Any]] = None, ts: Optional[float] = None,
              source: str = "") -> float:
        """写一行。`ts` 缺省取当前时间。异常向上抛，由调用方决定要不要吞。"""
        return self.db.panel_snapshot_write(
            snapshot=snapshot, mids=mids, why=why,
            ts=float(ts if ts is not None else time.time()), source=source)

    def latest(self) -> Optional[Dict[str, Any]]:
        try:
            return self.db.panel_snapshot_latest()
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[panel] 读面板快照失败（回落实时读取）：%s", e)
            return None

    def latest_why(self) -> Optional[List[Any]]:
        try:
            got = self.db.panel_snapshot_why()
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[panel] 读面板快照 /why 失败：%s", e)
            return None
        return None if got is None else got[1]

    def prune(self) -> int:
        try:
            return self.db.prune_panel_snapshots(self.keep)
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[panel] 清理面板快照失败（忽略）：%s", e)
            return 0


class SnapshotInfo:
    """面板侧的 `HlInfo` 替身：先读快照，读不到/过期就交给 `fallback`（= PanelInfo）。

    只实现面板真正用到的两个读数（`snapshot` / `all_mids`），其余属性透传给实时客户端，
    这样视图层偶尔读 `base_url`、`unified` 之类的属性仍然有效。
    """

    def __init__(self, store: PanelSnapshotStore, fallback: Any,
                 clock: Callable[[], float] = time.time,
                 stale_s: float = DEFAULT_STALE_S) -> None:
        self.store = store
        self.fallback = fallback
        self._clock = clock
        self.stale_s = float(stale_s)
        self._skip_next = False          # 写操作后强制走一次实时

    # ---------------------------------------------------------------- 读数
    def snapshot(self, address: str, mids: Optional[Dict[str, float]] = None) -> dict:
        """面板用：返回快照里的账户读数；显式传 `mids=` 的调用透传（执行器口径）。"""
        if mids is not None:
            return self._fallback_snapshot(address, mids)
        snap = self.stored_row()
        if snap is None:
            return self._fallback_snapshot(address, None)
        data = copy.deepcopy(snap["snapshot"])
        # 让视图层知道「这份数据是几分钟前的」，用于状态条标注；不改变任何数值。
        data.setdefault("panel_snapshot_age_s",
                        round(max(0.0, self._clock() - float(snap.get("ts") or 0.0)), 1))
        data.setdefault("panel_snapshot_source", snap.get("source") or "")
        return data

    def all_mids(self) -> Dict[str, float]:
        snap = self.stored_row()
        if snap is not None and snap.get("mids"):
            return copy.deepcopy(dict(snap["mids"]))
        return self.fallback.all_mids()

    # ---------------------------------------------------------------- 状态
    def stored_row(self) -> Optional[Dict[str, Any]]:
        """可取用的快照行；过期、缺失、或被 invalidate() 标记跳过时返回 None。"""
        if self._skip_next:
            self._skip_next = False
            return None
        row = self.store.latest()
        if row is None:
            return None
        age = self._clock() - float(row.get("ts") or 0.0)
        if age < 0 or age > self.stale_s:        # 时钟回拨 / 过期：宁可慢也别给旧数
            logger.info("[panel] 面板快照过期（%.0fs），本次回落实时读取", age)
            return None
        return row

    def age_s(self) -> float:
        row = self.store.latest()
        if row is None:
            return -1.0
        return max(0.0, self._clock() - float(row.get("ts") or 0.0))

    def _fallback_snapshot(self, address: str, mids: Optional[Dict[str, float]]) -> dict:
        if mids is None:
            return self.fallback.snapshot(address)
        return self.fallback.snapshot(address, mids)

    def invalidate(self) -> None:
        """面板写操作成功后调用：下一次渲染走实时读数（快照由下一轮 tick/monitor 刷新）。"""
        self._skip_next = True
        inv = getattr(self.fallback, "invalidate", None)
        if callable(inv):
            inv()

    # ---------------------------------------------------------------- 其余全部透传
    def __getattr__(self, name: str) -> Any:
        return getattr(self.fallback, name)
