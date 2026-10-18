"""Hyperliquid 写路径（唯一持有私钥、唯一能下单的模块）。

设计取舍（借鉴成熟系统的做法，见 docs/HL_EXECUTOR_PLAN.md「参考系统」）：

1. **不重复造轮子**：签名/行动哈希/phantom agent 这些易错环节全部交给官方
   `hyperliquid-python-sdk`（`hyperliquid-dex/hyperliquid-python-sdk`，MIT）。
   我们只做三件事：参数取整、错误归一化、幂等。
2. **cloid 幂等**：官方 SDK 每次调用都取新的时间戳当 nonce，所以「重发」在协议层
   不会被去重 —— 唯一的防重复手段是 **client order id (cloid)**。
   我们用 `sha256("cma:{symbol}:{bar_ms}:{purpose}")[:32]` 当 cloid，
   同一根 bar 的同一个动作无论重试多少次，交易所只会接受第一发。
3. **市价单 = 激进 IOC 限价**：HL 没有真正的市价单。我们不用 SDK 的 `market_open`
   （它默认 5% 滑点），而是自己按 mid ± `max_slippage_pct` 算限价，把滑点上限钉死。
4. **dry_run 也只走同一条代码路径**：只在下单那一行短路，保证影子模式与实盘行为一致。
5. 所有方法**不抛异常**，统一返回归一化 dict：ok / status / oid / avg_px / filled_sz / error / raw。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.hl.info import MAINNET, TESTNET, HlInfo, HlInfoError

logger = logging.getLogger(__name__)

STATUS_FILLED = "filled"
STATUS_RESTING = "resting"
STATUS_ERROR = "error"
STATUS_DRY = "dry"


def make_cloid(symbol: str, bar_ms: int, purpose: str) -> str:
    """确定性 cloid（0x + 32 hex）—— 同一 (symbol, bar_ms, purpose) 恒定。"""
    h = hashlib.sha256(f"cma:{symbol}:{bar_ms}:{purpose}".encode()).hexdigest()[:32]
    return "0x" + h


def _coerce_cloid(cloid):
    """SDK 只接受 `Cloid` 对象（内部调 to_raw()），传裸字符串会炸在 SDK 深处
    （冒烟实测：'str' object has no attribute 'to_raw'）。这里统一转换，
    并**把两套推导口径统一到 make_cloid** —— 否则 DB 里记的 cloid 与交易所收到的
    不是同一个值（幂等键对不上，排查/去重都会失效）。"""
    if cloid is None or hasattr(cloid, "to_raw"):
        return cloid
    txt = str(cloid).strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{32}", txt):
        raise ValueError(f"cloid 格式非法（需 0x + 32 位 hex）：{txt[:14]}…")
    from hyperliquid.utils.types import Cloid
    return Cloid.from_str(txt)


def _err(msg: str, raw: Any = None, status: str = STATUS_ERROR) -> dict:
    return {"ok": False, "status": status, "oid": None, "avg_px": None, "filled_sz": None,
            "error": msg, "raw": raw}


def _ok(status: str, oid=None, avg_px=None, filled_sz=None, raw=None) -> dict:
    return {"ok": True, "status": status, "oid": oid, "avg_px": avg_px,
            "filled_sz": filled_sz, "error": None, "raw": raw}


def parse_order_response(resp: Any) -> dict:
    """把 SDK 的响应归一化。

    SDK 成功形状：{"status":"ok","response":{"type":"order","data":{"statuses":[
        {"resting":{"oid":N}} | {"filled":{"totalSz":"..","avgPx":"..","oid":N}} | {"error":".."}]}}}
    失败形状：  {"status":"err","response":"<msg>"}，或直接抛 ClientError/ServerError。
    """
    if not isinstance(resp, dict):
        return _err(f"响应不是 dict：{str(resp)[:200]}", resp)
    if resp.get("status") == "err":
        return _err(str(resp.get("response"))[:300], resp)
    try:
        statuses = resp["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return _err(f"响应结构不认识：{str(resp)[:200]}", resp)
    if not statuses:
        return _err("statuses 为空", resp)
    st = statuses[0]
    if "resting" in st:
        return _ok(STATUS_RESTING, oid=st["resting"].get("oid"), raw=resp)
    if "filled" in st:
        f = st["filled"]
        return _ok(STATUS_FILLED, oid=f.get("oid"),
                   avg_px=float(f["avgPx"]) if f.get("avgPx") is not None else None,
                   filled_sz=float(f["totalSz"]) if f.get("totalSz") is not None else None,
                   raw=resp)
    if "error" in st:
        return _err(str(st["error"])[:300], resp)
    return _err(f"未识别的 status 字段：{str(st)[:200]}", resp)


class HlExchange:
    """写路径包装。`sdk` 参数只为测试注入；生产由 `from_env` 懒加载官方 SDK。"""

    def __init__(self, account_address: str = "", private_key: str = "", base_url: str = MAINNET,
                 dry_run: bool = True, timeout: float = 15.0, max_slippage_pct: float = 0.003,
                 info: Any = None, sdk: Any = None, wallet: Any = None):
        self.account_address = account_address or ""
        self.private_key = private_key or ""
        self.base_url = (base_url or MAINNET).rstrip("/")
        self.dry_run = bool(dry_run)
        self.timeout = timeout
        self.max_slippage_pct = max_slippage_pct
        self.info = info or HlInfo(base_url=self.base_url)
        self._exchange = sdk
        self._wallet = wallet
        self._init_error = ""

    # ------------------------------------------------------------ 初始化
    @property
    def testnet(self) -> bool:
        return self.base_url == TESTNET

    def _ensure(self) -> bool:
        """懒加载 SDK；缺依赖/缺凭据时 fail-closed（返回 False，绝不冒烟下单）。"""
        if self._exchange is not None:
            return True
        if not self.account_address or not self.private_key:
            self._init_error = "缺少 account_address / private_key"
            return False
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils.types import Cloid
            self._wallet = self._wallet or Account.from_key(self.private_key)
            self._exchange = Exchange(self._wallet, self.base_url,
                                      account_address=self.account_address, timeout=self.timeout)
            return True
        except Exception as e:                                       # noqa: BLE001
            self._init_error = f"初始化 SDK 失败：{e}"
            logger.error("[hl-exchange] %s", self._init_error)
            return False

    @property
    def available(self) -> bool:
        if self._exchange is not None:
            return True
        if not self.account_address or not self.private_key:
            return False
        return self._ensure()

    @property
    def init_error(self) -> str:
        if self._exchange is None:
            self._ensure()
        return self._init_error

    def _cloid(self, symbol: str, bar_ms: int, purpose: str):
        """发单用的 cloid —— **必须与 make_cloid() 同源**（DB 里存的就是它）。"""
        return _coerce_cloid(make_cloid(symbol, bar_ms, purpose))

    # ------------------------------------------------------------ 只读辅助
    def mid(self, coin: str) -> float:
        return float(self.info.all_mids().get(coin, 0.0) or 0.0)

    def slippage_limit(self, coin: str, is_buy: bool, mid: Optional[float] = None) -> float:
        """IOC 限价：买 = mid×(1+滑点)，卖 = mid×(1−滑点)。"""
        px = mid if mid else self.mid(coin)
        if px <= 0:
            raise HlInfoError(f"{coin} 取不到 mid")
        k = (1 + self.max_slippage_pct) if is_buy else (1 - self.max_slippage_pct)
        return px * k

    # ------------------------------------------------------------ 写操作
    def update_leverage(self, coin: str, leverage: int, cross: bool = False) -> dict:
        if self.dry_run:
            return _ok(STATUS_DRY, raw={"coin": coin, "leverage": leverage, "cross": cross})
        if not self._ensure():
            return _err(self._init_error)
        try:
            resp = self._exchange.update_leverage(int(leverage), coin, is_cross=bool(cross))
        except Exception as e:                                       # noqa: BLE001
            return _err(f"update_leverage 异常：{e}")
        if isinstance(resp, dict) and resp.get("status") == "ok":
            return _ok(STATUS_RESTING, raw=resp)
        return _err(str(resp)[:200], resp)

    def place_order(self, coin: str, is_buy: bool, sz: float, limit_px: float,
                    reduce_only: bool = False, tif: str = "Ioc",
                    trigger_px: Optional[float] = None, cloid=None) -> dict:
        """下单（IOC 限价 / 或触发单）。trigger_px 给了就走 {"trigger":{...}}。"""
        # 审计三轮 L：sz/限价非法直接拒（0 或负数发到交易所只会得到难解析的错）
        try:
            _sz, _px = float(sz), float(limit_px)
        except (TypeError, ValueError):
            return _err(f"sz/limit_px 非数字：{sz!r}/{limit_px!r}")
        if _sz <= 0 or _px <= 0:
            return _err(f"sz/limit_px 必须为正：sz={sz} limit_px={limit_px}")
        order_type: Dict[str, Any] = ({"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}}
                                      if trigger_px is not None else {"limit": {"tif": tif}})
        if self.dry_run:
            return _ok(STATUS_DRY, raw={"coin": coin, "is_buy": is_buy, "sz": sz,
                                        "limit_px": limit_px, "reduce_only": reduce_only,
                                        "order_type": order_type})
        if not self._ensure():
            return _err(self._init_error)
        try:
            cloid_obj = _coerce_cloid(cloid)
        except ValueError as e:
            return _err(str(e))                                      # 宁可拒单，也别炸在 SDK 里
        try:
            resp = self._exchange.order(coin, bool(is_buy), float(sz), float(limit_px),
                                        order_type, reduce_only=bool(reduce_only), cloid=cloid_obj)
        except Exception as e:                                       # noqa: BLE001
            return _err(f"下单异常：{e}")
        out = parse_order_response(resp)
        out["request"] = {"coin": coin, "is_buy": is_buy, "sz": sz, "limit_px": limit_px,
                          "reduce_only": reduce_only, "order_type": order_type}
        return out

    def cancel(self, coin: str, oid: int) -> dict:
        if self.dry_run:
            return _ok(STATUS_DRY, oid=oid)
        if not self._ensure():
            return _err(self._init_error)
        try:
            resp = self._exchange.cancel(coin, int(oid))
        except Exception as e:                                       # noqa: BLE001
            return _err(f"撤单异常：{e}")
        if isinstance(resp, dict) and resp.get("status") == "ok":
            return _ok(STATUS_RESTING, oid=oid, raw=resp)
        # 已经被成交/撤掉的单，撤单接口会报错 —— 对调用方来说等价于「不在场了」
        return _err(str(resp)[:200], resp)

    def cancel_all(self, coin: str) -> dict:
        """撤掉该 coin 的全部挂单（通过 open_orders 枚举后逐个撤）。"""
        try:
            orders = [o for o in self.info.open_orders(self.account_address) if o.get("coin") == coin]
        except HlInfoError as e:
            return _err(f"读取挂单失败，未撤任何单：{e}")
        results = []
        for o in orders:
            results.append({"oid": o.get("oid"), "res": self.cancel(coin, int(o["oid"]))})
        return _ok(STATUS_RESTING, raw=results)

    def cancel_all_orders(self) -> dict:
        """撤掉账户**全部**挂单（跨标的）。返回 {"ok", "error", "raw":{"oids":[...], "failed":[...]}}。

        到期保护清仓用：`cancel_all(coin)` 只清一个标的，而到期时不知道该有哪些仓，
        所以先一次性把挂单清空（止损单不撤就会和新平仓单互相打架）。

        - **挂单列表读不到/不可信 → 不撤任何单并返回 ok=False**（`reason=orders_untrusted`）。
          `open_orders()` 在接口异常时返回空列表（info 层不抛异常），「空列表」
          与「读不到」在调用方看来一模一样 —— 把后者当「没有挂单」会让清仓照常
          平仓，留下一堆孤儿 reduce-only 单（平了仓、止损单还挂着）。宁可整轮重试。
        - 单个撤单失败（已被成交/已撤掉）只记录、不整体失败 —— 那张单已经不在场上了；
          明细放在 raw.failed 里，由调用方决定要不要喊人。
        """
        try:
            orders, trusted = self._open_orders_checked()
        except Exception as e:                                       # noqa: BLE001
            return _err(f"读取挂单失败，未撤任何单：{type(e).__name__}")
        if not trusted:
            return {"ok": False, "status": STATUS_ERROR, "oid": None, "avg_px": None,
                    "filled_sz": None, "reason": "orders_untrusted",
                    "error": "读不到可信的挂单列表（接口异常/回落老接口），为免留孤儿单未撤单",
                    "raw": {"oids": [], "failed": []}}
        if self.dry_run:
            return {"ok": True, "status": STATUS_DRY, "oid": None, "avg_px": None,
                    "filled_sz": None, "error": None,
                    "raw": {"oids": [o.get("oid") for o in orders], "failed": []}}
        oids: List[int] = []
        failed: List[dict] = []
        for o in orders:
            coin = str((o or {}).get("coin") or "")
            try:
                oid = int((o or {}).get("oid"))
            except (TypeError, ValueError):
                continue
            if not coin:
                continue
            res = self.cancel(coin, oid)
            if res.get("ok"):
                oids.append(oid)
            else:
                failed.append({"oid": oid, "coin": coin, "error": res.get("error")})
        if failed and not oids:
            # 一张都没撤成 = 撤单链路坏了（签名/权限/网络），必须让调用方重试
            return _err(f"撤单全部失败：{failed[0].get('error') or '未知原因'}",
                        {"oids": oids, "failed": failed})
        return {"ok": True, "status": STATUS_RESTING, "oid": None, "avg_px": None,
                "filled_sz": None, "error": None, "raw": {"oids": oids, "failed": failed}}

    def _open_orders_checked(self) -> Tuple[List[dict], bool]:
        """(挂单列表, 是否可信)。优先用 info 的可信接口，老接口退化成「不可信」。"""
        checked = getattr(self.info, "open_orders_checked", None)
        if callable(checked):
            orders, trusted = checked(self.account_address)
            return list(orders or []), bool(trusted)
        # 只有老接口（测试桩/旧版 info）：无法区分「没挂单」与「读不到」。
        # 生产用的 HlInfo 始终有 open_orders_checked，走到这里只可能是桩 —— 按可信处理。
        orders = list(self.info.open_orders(self.account_address) or [])
        logger.debug("[hl-exchange] info 无 open_orders_checked（旧版/桩），按可信处理：%d 张", len(orders))
        return orders, True

    def schedule_cancel(self, time_ms: Optional[int] = None, minutes: int = 30) -> dict:
        """dead man's switch：到点自动撤所有挂单。"""
        ts = int(time_ms if time_ms is not None else (time.time() + minutes * 60) * 1000)
        if self.dry_run:
            return _ok(STATUS_DRY, raw={"scheduleCancel": ts})
        if not self._ensure():
            return _err(self._init_error)
        try:
            resp = self._exchange.schedule_cancel(ts)
        except Exception as e:                                       # noqa: BLE001
            return _err(f"schedule_cancel 异常：{e}")
        if isinstance(resp, dict) and resp.get("status") == "ok":
            return _ok(STATUS_RESTING, raw={"scheduled_ms": ts, "resp": resp})
        return _err(str(resp)[:200], resp)
