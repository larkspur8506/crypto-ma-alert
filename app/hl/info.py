"""Hyperliquid 只读客户端（纯 stdlib，无第三方依赖）。

设计要点：
- **只读路径零依赖**：面板（P0）只需要一个钱包地址即可工作，不需要 SDK、不需要私钥。
  写路径在 `app/hl/exchange.py`，只有它 import `hyperliquid` 包。
- 所有请求走 POST /info，带退避重试（只读是幂等的，可以放心重试）。
- `snapshot()` 是**冻结接口**：执行器、看护、面板都消费它的输出，字段不得随意改名。

参考实现：官方 `hyperliquid-dex/hyperliquid-python-sdk` 的 `Info` 类（info.py），
字段名与归一化口径向其对齐，便于与官方示例互相印证。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"


class HlInfoError(Exception):
    """只读接口失败（网络/解析）。调用方应把它当作「读不到」，绝不据此下单。"""


class HlInfo:
    def __init__(self, base_url: str = MAINNET, timeout: float = 10.0,
                 retries: int = 3, backoff=(1.0, 3.0, 9.0), opener=None):
        self.base_url = (base_url or MAINNET).rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = tuple(backoff)
        self._opener = opener or urllib.request.urlopen   # 测试注入用

    # ------------------------------------------------------------ 底层
    def post(self, payload: Dict[str, Any]) -> Any:
        body = json.dumps(payload).encode()
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            req = urllib.request.Request(
                self.base_url + "/info", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with self._opener(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:                                  # noqa: BLE001
                last = e
                if attempt < self.retries - 1:
                    pause = self.backoff[min(attempt, len(self.backoff) - 1)]
                    logger.warning("[hl-info] %s 失败（第 %d 次）：%s，%.1fs 后重试",
                                   payload.get("type"), attempt + 1, e, pause)
                    time.sleep(pause)
        raise HlInfoError(f"{payload.get('type')} 连续 {self.retries} 次失败: {last}")

    # ------------------------------------------------------------ 直接接口
    def meta(self) -> dict:
        return self.post({"type": "meta"})

    def all_mids(self) -> Dict[str, float]:
        raw = self.post({"type": "allMids"})
        if not isinstance(raw, dict):
            raise HlInfoError(f"allMids 返回了非预期结构：{str(raw)[:120]}")
        out: Dict[str, float] = {}
        for k, v in (raw or {}).items():
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def user_state(self, address: str) -> dict:
        return self.post({"type": "clearinghouseState", "user": address})

    def open_orders(self, address: str) -> List[dict]:
        data = self.post({"type": "openOrders", "user": address})
        return data if isinstance(data, list) else []

    def user_fills(self, address: str) -> List[dict]:
        data = self.post({"type": "userFills", "user": address})
        return data if isinstance(data, list) else []

    def user_fills_by_time(self, address: str, start_ms: int, end_ms: Optional[int] = None,
                           aggregate_by_time: bool = False) -> List[dict]:
        payload: Dict[str, Any] = {"type": "userFillsByTime", "user": address,
                                   "startTime": int(start_ms), "aggregateByTime": bool(aggregate_by_time)}
        if end_ms is not None:
            payload["endTime"] = int(end_ms)
        data = self.post(payload)
        return data if isinstance(data, list) else []

    def user_funding_history(self, address: str, start_ms: int, end_ms: Optional[int] = None) -> List[dict]:
        payload: Dict[str, Any] = {"type": "userFunding", "user": address, "startTime": int(start_ms)}
        if end_ms is not None:
            payload["endTime"] = int(end_ms)
        data = self.post(payload)
        return data if isinstance(data, list) else []

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> List[dict]:
        data = self.post({"type": "candleSnapshot",
                          "req": {"coin": coin, "interval": interval,
                                  "startTime": int(start_ms), "endTime": int(end_ms)}})
        return data if isinstance(data, list) else []

    def funding_history(self, coin: str, start_ms: int, end_ms: Optional[int] = None) -> List[dict]:
        payload: Dict[str, Any] = {"type": "fundingHistory", "coin": coin, "startTime": int(start_ms)}
        if end_ms is not None:
            payload["endTime"] = int(end_ms)
        data = self.post(payload)
        return data if isinstance(data, list) else []

    def user_fees(self, address: str) -> dict:
        return self.post({"type": "userFees", "user": address})

    # ------------------------------------------------------------ 归一化
    def specs(self, coins: Optional[List[str]] = None) -> Dict[str, dict]:
        """{coin: {"sz_decimals", "max_leverage", "only_isolated"}}（只含永续）。"""
        universe = (self.meta() or {}).get("universe") or []
        out: Dict[str, dict] = {}
        for a in universe:
            name = a.get("name")
            if not name:
                continue
            if coins is not None and name not in coins:
                continue
            out[name] = {"sz_decimals": int(a.get("szDecimals", 0)),
                         "max_leverage": int(a.get("maxLeverage", 1)),
                         "only_isolated": bool(a.get("onlyIsolated", False))}
        return out

    @staticmethod
    def _num(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def snapshot(self, address: str, mids: Optional[Dict[str, float]] = None) -> dict:
        """归一化账户快照（**冻结接口**）。

        返回：
        {
          "ts": float, "address": str,
          "equity": float,          # 账户总价值
          "withdrawable": float,
          "margin_used": float,     # 已用保证金
          "notional": float,        # 持仓名义
          "positions": [ {coin, side, size, entry_px, mark_px, position_value, liq_px,
                          leverage, isolated, margin_used, unrealized_pnl, roe,
                          funding_since_open} ],
          "open_orders": [ {coin, oid, is_buy, sz, limit_px, reduce_only, trigger_px,
                            is_trigger, timestamp} ],
        }
        side: +1 多 / -1 空（与 positions.side 口径一致）。

        注意：`clearinghouseState` **不返回标记价**，所以 mark_px 取自 allMids（可外部传入以省一次请求）。
        """
        st = self.user_state(address) or {}
        ms = st.get("marginSummary") or {}
        if mids is None:
            try:
                mids = self.all_mids()
            except HlInfoError:
                mids = {}
        positions: List[dict] = []
        for item in st.get("assetPositions") or []:
            p = item.get("position") or {}
            szi = self._num(p.get("szi"))
            if szi == 0:
                continue
            coin = p.get("coin") or ""
            lev = p.get("leverage") or {}
            value = self._num(p.get("positionValue"))
            mark = self._num((mids or {}).get(coin))
            if mark <= 0:
                # 兜底：用名义价值 ÷ 数量 反推（mark ≈ positionValue / |szi|）
                mark = value / abs(szi) if szi else 0.0
            positions.append({
                "coin": coin,
                "side": 1 if szi > 0 else -1,
                "size": abs(szi),
                "entry_px": self._num(p.get("entryPx")),
                "mark_px": mark,
                "position_value": value,
                "liq_px": self._num(p.get("liquidationPx")),
                "leverage": int(self._num(lev.get("value"), 1)),
                "isolated": (lev.get("type") == "isolated"),
                "margin_used": self._num(p.get("marginUsed")),
                "unrealized_pnl": self._num(p.get("unrealizedPnl")),
                "roe": self._num(p.get("returnOnEquity")) if p.get("returnOnEquity") is not None else None,
                "funding_since_open": self._num((p.get("cumFunding") or {}).get("sinceOpen")) if isinstance(p.get("cumFunding"), dict) else 0.0,
            })
        orders: List[dict] = []
        for o in self.open_orders(address):
            trig = o.get("triggerPx")
            orders.append({
                "coin": o.get("coin"),
                "oid": o.get("oid"),
                "is_buy": bool(o.get("side") == "B"),
                "sz": self._num(o.get("sz")),
                "limit_px": self._num(o.get("limitPx")),
                "reduce_only": bool(o.get("reduceOnly")),
                "trigger_px": self._num(trig) if trig is not None else 0.0,
                "is_trigger": bool(o.get("isTrigger")),
                "timestamp": o.get("timestamp"),
            })
        return {
            "ts": time.time(),
            "address": address,
            "equity": self._num(ms.get("accountValue")),
            "withdrawable": self._num(st.get("withdrawable")),
            "margin_used": self._num(ms.get("totalMarginUsed")),
            "notional": self._num(ms.get("totalNtlPos")),
            "positions": positions,
            "open_orders": orders,
        }
