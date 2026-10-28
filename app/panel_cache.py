"""面板读数的进程内 TTL 缓存（阶段 A1：让 /admin 打开变快）。

**为什么只包面板、不碰 `app/hl/info.py`**
    面板每次渲染都会走 `status_payload` → `HlInfo.snapshot()`，而 `snapshot` 内部是 4 个
    HL 只读请求（clearinghouseState / allMids / frontendOpenOrders /
    spotClearinghouseState），实测单次 0.34–0.53s ⇒ 一页 1.5–1.8s。人手连点几个 Tab，
    这 4 个请求被原样重复打，而 10 秒内它们的变化对「4h 级别策略的人工看盘」没有任何意义。

    但执行器 / tick / 对账 / 看护**必须**读实时值：下单、止损、对账、DD 熔断都建立在这些读数
    上，陈旧一秒都可能做出错误动作（fail-closed 的前提就是「看到的就是真的」）。所以缓存只能
    挂在面板这一层（`main._admin_deps()`），`get_hl_info()` 返回的真身永远不带缓存。

**为什么是 10 秒**
    实测一次渲染 1.5–1.8s，人点开一页再翻 3–4 个 Tab 大约 4–8s —— 10s 刚好覆盖一轮连续操作，
    让「第二页起 0 请求」；同时任何陈旧都不超过 10s，不至于「点了 kill/改完参数还看到旧页面」。
    再长就会让手工操作后的确认变模糊（这个代价比省几个请求更贵）。

**缓存什么、不缓存什么**
    - 缓存：`snapshot(address)`、`all_mids()` —— 纯读数，只影响展示。
    - 不缓存：任何写路径、以及显式带 `mids=` 的调用（那是执行器口径「调用方自带现价」，
      复用缓存等于让执行器吃旧价）。
    - 闸门/仓位状态本来就不进这里（它们在 `app/why.py` 与 DB 里现读）。
"""
from __future__ import annotations

import copy
import inspect
import threading
import time
from typing import Any, Callable, Dict, Hashable, Optional, Tuple

# 见模块头「为什么是 10 秒」。给常数一个名字，测试与文档引用同一个来源。
DEFAULT_TTL_S = 10.0


class TtlCache:
    """极简 TTL 缓存：键 → (写入时刻, 值)。

    线程安全：uvicorn 的同步路由跑在线程池里，`/admin` 与 `/api/hl/*` 可能同时进入，
    而 `_fetch` 里还有网络 I/O —— 锁只保护字典本身，**不**把慢调用放在锁里（否则并发请求
    会被串行化，比不缓存还慢）。
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S,
                 clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 32) -> None:
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self._max_keys = max(1, int(max_keys))
        self._data: Dict[Hashable, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_call(self, key: Hashable, fetch: Callable[[], Any]) -> Any:
        """命中就返回（深拷贝，避免调用方改到缓存里的对象）；未命中才真的去取。"""
        if self.ttl_s > 0:
            with self._lock:
                hit = self._data.get(key)
            if hit is not None and (self._clock() - hit[0]) < self.ttl_s:
                return copy.deepcopy(hit[1])
        value = fetch()
        if self.ttl_s > 0:
            with self._lock:
                if len(self._data) >= self._max_keys:
                    self._data.clear()     # 键里带地址，地址换了旧键就没用了：整体清掉即可
                self._data[key] = (self._clock(), copy.deepcopy(value))
        return value

    def invalidate(self, key: Optional[Hashable] = None) -> None:
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


def _accepts_kwarg(fn: Any, name: str) -> bool:
    """`fn` 是否接受关键字参数 `name`（签名读不到时乐观返回 True，真 TypeError 有回落）。"""
    if fn is None:
        return False
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):          # C 实现或包装对象读不到签名
        return True
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class PanelInfo:
    """`HlInfo` 的面板侧包装：只给 `snapshot()` / `all_mids()` 加 TTL 缓存，其余透传。

    透传（`__getattr__`）是刻意的：面板/视图偶尔读 `unified`、`base_url` 之类的属性，
    换掉 `info` 之后这些读法必须继续有效，不能让包装层变成「少一个属性的假对象」。
    """

    def __init__(self, info: Any, ttl_s: float = DEFAULT_TTL_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.raw = info
        self._cache = TtlCache(ttl_s, clock)
        self._accepts_mids = _accepts_kwarg(getattr(info, "snapshot", None), "mids")

    # ---------------------------------------------------------------- 读数（带缓存）
    def snapshot(self, address: str, mids: Optional[Dict[str, float]] = None) -> dict:
        """缓存 10s。显式传 `mids=` 的调用直接透传（执行器口径，绝不复用旧价）。"""
        if mids is not None:
            return self.raw.snapshot(address, mids)
        return self._cache.get_or_call(("snapshot", str(address)),
                                       lambda: self._snapshot(address))

    def _snapshot(self, address: str) -> dict:
        """真取一次快照。

        `snapshot` 内部本来就要 `allMids`（它不返回标记价）；这里先把 mids 走自己的缓存取好
        再传进去，于是「面板 + 预览」共用一份现价，冷启动仍是 4 个请求、热缓存 0 个请求，
        不会因为多缓存一层反而多打一次接口。
        """
        mids: Optional[Dict[str, float]] = None
        try:
            mids = self.all_mids()
        except Exception:                                          # noqa: BLE001
            # 取现价失败不能让整个快照失败：交给 snapshot 自己兜底（它会再试一次并降级）
            mids = None
        if mids and self._accepts_mids:
            return self.raw.snapshot(address, mids=mids)
        return self.raw.snapshot(address)

    def all_mids(self) -> Dict[str, float]:
        """缓存 10s：面板「影响预览」只需要一个参考价，10s 的陈旧对预览没有意义。"""
        return self._cache.get_or_call(("all_mids",), self.raw.all_mids)

    # ---------------------------------------------------------------- 其余全部透传
    def __getattr__(self, name: str) -> Any:
        # 只有常规属性找不到时才会走到这里（snapshot/all_mids 是真方法，不会进来）
        return getattr(self.raw, name)

    def invalidate(self) -> None:
        """写操作成功后调用：下一次渲染重新取真值。"""
        self._cache.invalidate()

    def __len__(self) -> int:
        return len(self._cache)
