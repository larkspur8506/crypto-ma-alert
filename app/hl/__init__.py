"""Hyperliquid 执行/监控子包（feat/hl-executor）。

模块边界（重要，别打破）：
- `info.py`   只读，纯 stdlib，无凭据 → 面板/看护/执行器都用它读状态
- `exchange.py` **唯一** import hyperliquid SDK、唯一持私钥、唯一能下单
- `sizing.py` 纯函数：数量/价格取整规则、风险换算、强平价
- `executor.py` 状态机：闸门 → 下单 → 挂止损 → 对账 → 落库 → 告警
- `views.py`  面板（服务器渲染 HTML + 内联 SVG）与 /api/hl/status
- `watchdog.py` 独立看守检查（供 Hermes cron 调用）

模式（`hl_exec_mode`，DB 优先于 env，默认 off）：
    off  只提醒（现状） | dry 影子：算单落库不发单 | live 真实交易
"""
from __future__ import annotations

MODE_OFF = "off"
MODE_DRY = "dry"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_DRY, MODE_LIVE)

# 执行器在 hl_orders.purpose 里用的取值（唯一键 (symbol, bar_ms, purpose) 的一部分）
PURPOSE_ENTRY = "entry"
PURPOSE_STOP = "stop"
PURPOSE_EXIT = "exit"
PURPOSE_EXIT_REHANG = "exit:rehang"   # 平仓未成交后重挂的止损（与开仓止损/补挂的 cloid 都不同源）

# hl_orders.status 状态机
ST_INTENT = "intent"      # 已落库，尚未发请求（崩溃后据此判断「可能已发出」）
ST_PLANNED = "planned"    # dry 模式
ST_SENT = "sent"          # 请求已发出，结果未回
ST_RESTING = "resting"    # 交易所已接受（挂单中）
ST_FILLED = "filled"
ST_CANCELLED = "cancelled"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"    # 被闸门拒绝

# hl_events.kind
EV_DECISION = "decision"
EV_ORDER = "order"
EV_RECONCILE = "reconcile"
EV_KILL = "kill"
EV_ERROR = "error"
EV_HEARTBEAT = "heartbeat"
# 回撤熔断（DD Guard）：锚点建立/口径不可用/指数异常这类「口径自身」的事件。
# 单独一个 kind，而不是塞进 error —— 看守按 error 数问题，「没有正权益快照」在
# 刚上线/刚清库时是正常的过渡态，不该被当成故障。
EV_DD = "dd"
