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
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"


class HlInfoError(Exception):
    """只读接口失败（网络/解析）。调用方应把它当作「读不到」，绝不据此下单。"""


class HlInfo:
    def __init__(self, base_url: str = MAINNET, timeout: float = 10.0,
                 retries: int = 3, backoff=(1.0, 3.0, 9.0), opener=None,
                 unified: Optional[bool] = None):
        self.base_url = (base_url or MAINNET).rstrip("/")
        # 统一账户（HL 新账户默认）：余额与占用都在现货状态里，永续 accountValue 恒为 0。
        # 默认按统一账户读（多一次 spotClearinghouseState 请求）；标准账户设 HL_UNIFIED_ACCOUNT=0。
        if unified is None:
            unified = (os.environ.get("HL_UNIFIED_ACCOUNT", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
        self.unified = bool(unified)
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
        return self.post({"type": "clearinghouseState", "user": address}) or {}

    def spot_state(self, address: str) -> dict:
        """现货状态。**统一账户（HL 新账户默认）下余额与占用都在这里**：
        官方文档 Important details #3 ——「对 API 用户，统一账户/组合保证金把余额和占用
        显示在现货状态里，各个 perp dex 的用户状态没有意义」。"""
        return self.post({"type": "spotClearinghouseState", "user": address}) or {}

    def agent_status(self, address: str) -> dict:
        """agent（API 钱包）授权信息。

        **必须以 `extraAgents` 为准**：`webData2.agentAddress` 只反映「主」agent，
        在只配置了额外 agent 的账户上返回 None —— 用它核对会误报「没有授权」
        （2026-10-14 真踩过：用户的 agent 叫 Hermes、180 天，webData2 说 None，extraAgents 列得清清楚楚）。

        返回 {"agents": [{name,address,valid_until(ms)}], "max_valid_until": ms|None}。
        """
        agents: List[dict] = []
        data = self.post({"type": "extraAgents", "user": address})
        for a in (data or []):
            if isinstance(a, dict):
                agents.append({"name": a.get("name"), "address": a.get("address"),
                               "valid_until": a.get("validUntil")})
        if not agents:                                   # 兼容老账户：回落到 webData2
            w = self.post({"type": "webData2", "user": address}) or {}
            if w.get("agentAddress"):
                agents.append({"name": None, "address": w.get("agentAddress"),
                               "valid_until": w.get("agentValidUntil")})
        untils = [a["valid_until"] for a in agents if a.get("valid_until")]
        return {"agents": agents, "max_valid_until": max(untils) if untils else None}

    def extra_agents(self, address: str) -> List[dict]:
        """原始 extraAgents 列表（API 钱包授权）。

        与 `agent_status` 分开：后者做归一化并回落 webData2，适合「有没有授权」的展示；
        到期监控要的是**原始行**（按 name 精确匹配、拿 validUntil 毫秒），不能掺回落值
        —— webData2 只反映「主」agent，混进来会读成别人的到期时间。

        `validUntil` 为 null/0 = 该授权不过期（老账户）。接口异常照常抛 HlInfoError，
        由调用方走「查不到 → 手工兜底」的回落路径。
        """
        data = self.post({"type": "extraAgents", "user": address})
        return data if isinstance(data, list) else []

    def open_orders(self, address: str) -> List[dict]:
        """挂单列表（旧接口，只要列表）。需要「是否可信」请用 open_orders_checked。"""
        return self.open_orders_checked(address)[0]

    def open_orders_checked(self, address: str) -> Tuple[List[dict], bool]:
        """返回 (挂单列表, 是否可信)。

        实测（2026-10-14）：`openOrders` 对触发单**不返回** `isTrigger` / `triggerPx`
        （触发价只藏在 limitPx 里），于是「有仓 + 止损单在场」会被判成**裸奔** → 面板 ❌、
        看守误报、对账拉 kill；若据此自动补挂/平仓，就会**误平仓**。
        `frontendOpenOrders`（UI 同款）才带 isTrigger/triggerPx/orderType/isPositionTpsl。

        所以：回落 openOrders 时把列表标成**不可信**，调用方（对账）只提示、不动作。
        """
        try:
            data = self.post({"type": "frontendOpenOrders", "user": address})
            if not isinstance(data, list):
                # 非 list（错误对象/限流响应）≠ 空挂单列表。当可信空表会把
                # 「有仓 + 止损单在场」判成裸仓 → 触发自动补挂/平仓真实仓位。
                logger.warning("[hl-info] frontendOpenOrders 返回非列表（%s），标记为不可信",
                               str(data)[:120])
                return [], False
            return data, True
        except HlInfoError as e:                                     # 老接口兜底（不可信）
            logger.warning("[hl-info] frontendOpenOrders 不可用，回落 openOrders"
                           "（挂单列表标记为不可信，不做止损判断）：%s", e)
            data = self.post({"type": "openOrders", "user": address})
            return (data if isinstance(data, list) else []), False

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
        orders_list, orders_trusted = self.open_orders_checked(address)
        for o in orders_list:
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
                "order_type": o.get("orderType"),
                "is_position_tpsl": bool(o.get("isPositionTpsl")),
                "timestamp": o.get("timestamp"),
            })
        # 统一账户（HL 新账户默认）：余额在**现货**状态里，永续 accountValue 恒为 0。
        # 只看永续会误报「没资金」，而且日内熔断的基准会变成 0（永远不触发）。
        spot_usdc = 0.0
        spot_hold = 0.0          # USDC 的仓位/挂单占用（可提 = total - hold）
        spot_ok = True           # 现货状态是否读取成功（失败时 equity_total 不可信）
        spot_balances: List[dict] = []
        if self.unified:
            try:
                for b in (self.spot_state(address).get("balances") or []):
                    coin = str(b.get("coin") or "")
                    total = self._num(b.get("total"))
                    hold = self._num(b.get("hold"))
                    if total or hold:
                        spot_balances.append({"coin": coin, "total": total, "hold": hold})
                    if coin == "USDC":
                        spot_usdc += total
                        spot_hold += hold
            except HlInfoError as e:                                     # noqa: BLE001
                spot_ok = False
                logger.warning("[hl-info] 现货状态读取失败（统一账户余额将显示为 0）：%s", e)
        perp_equity = self._num(ms.get("accountValue"))
        upnl = sum(p["unrealized_pnl"] for p in positions)
        # 统一账户：账户总值 = 抵押品（现货 USDC）+ 未实现盈亏；标准账户：永续 accountValue 已含盈亏
        equity_total = (spot_usdc + upnl) if self.unified else perp_equity
        return {
            "ts": time.time(),
            "address": address,
            "equity": perp_equity,              # 永续账户口径（标准模式=总值；统一模式通常为 0）
            "equity_total": equity_total,       # 账户总值：面板与熔断都用它
            "unified": self.unified,
            "spot_usdc": spot_usdc,
            "spot_hold": spot_hold,           # USDC 占用（可提 = spot_usdc - spot_hold）
            "spot_ok": spot_ok,               # False = 现货读取失败，equity_total 不可信
            "spot_balances": spot_balances,
            "withdrawable": self._num(st.get("withdrawable")),
            "margin_used": self._num(ms.get("totalMarginUsed")),
            "notional": self._num(ms.get("totalNtlPos")),
            "positions": positions,
            "open_orders": orders,
            # 挂单列表是否可信（False = 回落老接口，读不到 isTrigger → 不得据此判断止损有无）
            "orders_trusted": orders_trusted,
        }
