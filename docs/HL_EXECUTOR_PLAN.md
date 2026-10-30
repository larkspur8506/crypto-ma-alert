# 双均线系统 → Hyperliquid 自动交易改造计划（feat/hl-executor）

> 状态：**开发中**（2026-10-13 夜）。作者：Hermes Agent。所有结论均基于本仓库现有代码与实测数据，凡未实测的都标注为「待验证」。

## 0. 目标与非目标

**目标**

1. 把现有「信号 → 提醒 → 人工下单」链路扩成「信号 → 自动下单 + 提醒（可选静默）」，
   执行场所为 **Hyperliquid 永续**（BTC / ETH，4h 口径不变）。
2. 提供一套**后台监控页**：价格、仓位、浮盈浮亏、保证金/强平价、止损单状态、当日与累计盈亏、
   资金费、执行器健康度、对账状态、kill switch 状态，随时打开可看。
3. 全链路**可验证**：每个动作落库可审计；对账以交易所为准；测试充分到可以在 CI 上复现。
4. 在拿到 API 凭据之前，系统**只能以只读 + 影子模式运行**，绝不产生真实订单。

**非目标（本期不做，明确写下来防漂移）**

- 不做多交易所路由、不做跨所套利、不做加仓/金字塔、不做手工干预的 UI 下单按钮。
- 不改策略口径（入场/离场规则冻结在当前版本）。任何口径变更必须走两次交叉验证脚本 + 用户确认。
- 不做移动端 App；监控页是响应式网页，手机浏览器可看。
- 不做「自动调参」「自动加减风险」这类自适应逻辑。

## 1. 现状与改造点

现有系统（`app/`，19 个文件，86 个单测）：

| 模块 | 现状 | 改造 |
| --- | --- | --- |
| `strategy.py` | 4h 双均线信号（密集突破/跌破 + MA200 做空过滤） | **不动**（口径冻结） |
| `engine.py` | 纯状态机 `walk()` + 文案 + tick | 增加「执行钩子」：信号产生后交给执行器（可关） |
| `sizing.py` | MEXC 面值/张数换算 | **不动**；新增 `hl/sizing_hl.py`（HL 连续数量） |
| `market.py` | 行情源（MEXC 合约 → OKX 降级） | **不动**（信号数据源仍用 MEXC，基差极小；HL 的 4h 历史只回溯到 2024-07，不能作为回测证据） |
| `db.py` | positions / alert_logs / bar_state / feed_health | 新增 4 张表：`hl_orders` / `hl_fills` / `hl_snapshots` / `hl_events`；`positions` 增列 |
| `main.py` | /admin 状态页 + 确认页 | 新增 `/hl` 监控页 + `/api/hl/*` JSON |
| `notify.py` | 企业微信 + Telegram 双通道 | 复用（新增 `kind=hl_*` 告警类型） |

## 2. 架构

```
                      ┌──────────────── app/hl/info.py ────────────────┐
 4h 收盘 ──┐          │  meta / all_mids / clearinghouseState /        │
           │          │  openOrders / userFills / fundingHistory      │  ← 只读，无需密钥
 engine.tick ├──────▶ ├───────────────────────────────────────────────┤
 (信号/离场) │          │  app/hl/exchange.py（EIP-712 签名，agent 钱包）│  ← 唯一写路径
           │          └───────────────────────────────────────────────┘
           ▼                          ▲              ▲
   app/hl/executor.py ────────────────┘              │
   状态机 + 风控闸门 + 对账 + 幂等 + kill switch     │
           │                                         │
           ├──▶ app/hl/sizing_hl.py（连续数量/价格规则/强平价）  ← 纯函数
           ├──▶ db.py（hl_orders / hl_fills / hl_events / hl_snapshots）
           ├──▶ notify.py（成交/失败/对账异常告警）
           └──▶ app/hl/views.py → /hl 监控页 + /api/hl/status（JSON）
                                   ▲
                        app/hl/watchdog.py（独立看守，每 5 分钟）
```

**三条硬边界**

1. **信号与执行解耦**：`strategy.py` 只产信号，`executor.py` 只负责「把这个信号变成订单」。
   执行器可以被整体关掉（`HL_EXEC_MODE=off`），系统立刻退回纯提醒模式。
2. **只读与写路径分离**：`info.py` 无凭据，`exchange.py` 是唯一持私钥、唯一能下单的模块。
   除它以外的任何代码不得 import 签名库。
3. **单一写者**：`positions.hl_*` 列与 `hl_orders` / `hl_fills` 只由执行器写；
   人工确认页（`/c/{pid}`）已于 2026-10-30 PR B 退役 —— 执行器自动开仓后它再无入口，
   现在 `positions` 的开/平仓只有执行器与引擎两条写路径（改库属应急，见 runbook §6.H）。

## 2.5 参考系统（GitHub 调研，2026-10-13 用 GitHub API 实测）

用户要求「不重复造轮子」。下面是实际搜到的成熟项目与**明确的采纳/不采纳决定**（星数/许可证/最后推送均为当日查询结果）。

### 采纳

| 项目 | 关键事实 | 我们采纳什么 |
| --- | --- | --- |
| **hyperliquid-dex/hyperliquid-python-sdk** | 1.8k★ MIT 官方，2026-06 仍在推；`hyperliquid/exchange.py` 38KB、`info.py` 27KB、43 个官方示例（含 `basic_tpsl.py` / `basic_schedule_cancel.py` / `basic_agent.py` / `rounding.py`） | **全部签名与行动哈希**。`hyperliquid-python-sdk==0.24.0` 固定版本，只在 `app/hl/exchange.py` 懒加载；缺依赖时写路径 fail-closed。示例里的 `rounding.py` 直接决定了我们的取整口径（5 位有效数字 / `6-szDecimals` 小数位 / >100k 取整 / 数量按 szDecimals floor）。 |
| 官方 `examples/basic_tpsl.py` | 止损 = `{"trigger":{"triggerPx":…,"isMarket":True,"tpsl":"sl"}}` + `reduce_only=True` | 我们的止损单形态，逐字对齐官方写法。 |
| 官方 `examples/basic_schedule_cancel.py` + 文档 | `scheduleCancel`（dead man's switch，到点自动撤全部挂单） | 每轮对账成功后自动续期 → 「本机挂了」的最后一道防线。 |
| **hummingbot/hummingbot** 20.4k★ Apache-2.0，`connector/derivative/hyperliquid_perpetual` | 生产级订单跟踪（inflight order / order tracker / 非对称重试） | **client order id 幂等纪律**与「先记录意图再发请求」的账本模式（只借鉴设计，不抄 GPL 之外的代码也仍按设计重写）。 |
| **freqtrade/freqtrade** 55.4k★ GPL-3.0 | dry-run 模式、protections（max drawdown / stoploss guard）、Telegram+REST 运维面 | **dry-run 与实盘共用同一条代码路径**、当日亏损熔断、下单次数闸门。GPL 项目**只借鉴设计，不复制代码**（避免传染许可）。 |
| **enarjord/passivbot** 2.1k★ Unlicense（公版） | 已支持 Hyperliquid，网格/趋势参数体系完整 | 只参考其「交易所适配层与策略层分离」的目录划分（Unlicense 可复用，但本项目未直接抄代码）。 |

### 明确不采纳（附理由）

- **nautilus_trader**（29.9k★ LGPL-3.0）：事件驱动引擎过于庞大（Rust 内核 + 大量依赖），本系统只有两个标的、4 小时级别，引入它等于把自己绑到别人的发布节奏上。
- **freqtrade / OctoBot / Hummingbot 整套框架**：它们自带策略引擎、交易所抽象与 UI，但我们的策略（双均线 + 密集分位 + MA200 过滤）和状态机（pending/open/closed + auto_closed 语义）已经在现有代码里跑通并有 100+ 单测，整体迁移的风险远大于收益。
- **自研 EIP-712 签名**：官方 SDK 已经把 phantom agent、msgpack 行动哈希、nonce 处理做对了；自己写是典型的重复造轮子且容易签错。
- **`market_open`（SDK 默认 5% 滑点）**：改用自算 IOC 限价（默认 0.3%），把滑点上限钉死。

### 从 SDK 源码读出来的关键事实（决定了我们的设计）

1. **每次调用都取新的时间戳当 nonce**（`exchange.py` 里 20+ 处 `get_timestamp_ms()`）→ **重发在协议层不会被去重**。
   所以防重复**只能靠 cloid**：我们用 `sha256("cma:{symbol}:{bar_ms}:{purpose}")[:32]`（16 字节 hex），
   同一根 bar 的同一动作重试多少次，交易所只接受第一发。
2. `Cloid` 必须 `0x` + 恰好 32 个 hex 字符（SDK 在 `utils/types.py` 里强校验）。
3. 订单响应有两层错误：HTTP 层 `{"status":"err","response":"…"}` 与 statuses 层 `{"error":"…"}`，
   两者都要归一化（`parse_order_response`，已有 8 个用例覆盖各种畸形响应）。
4. 错误类型 `ClientError(status_code, error_code, error_message, header, error_data)` / `ServerError`
   → 我们的包装层统一转成 `{ok, status, error, …}` dict，执行器不再需要 try/except 遍地开花。

## 3. 执行器设计（可靠性的核心）

### 3.1 模式

| 模式 | 行为 | 用途 |
| --- | --- | --- |
| `off`（默认） | 只提醒，不碰交易所 | 现状，随时可退回 |
| `dry`（影子） | 每轮拉真实行情/账户，算出**将要下的单**并落库 `hl_orders(status=planned)`，不发单 | 上线前比对，验证决策正确性 |
| `live` | 真实下单 + 挂止损 + 对账 | 正式运行 |

模式由 `HL_EXEC_MODE`（DB 可覆盖）决定，**默认 off**；切 live 需要显式设置且通过启动自检。

### 3.2 入场路径（一次信号 = 一串有顺序的动作）

1. **前置闸门**（任一不满足即放弃并告警）：kill switch 未触发 / 对账一致 / 无同标的重仓 /
   名义与保证金在限内 / 当日亏损未超限 / 距上次下单 ≥ 最小间隔 / 本 bar 未执行过（幂等）。
2. `set_leverage(coin, leverage, cross=False)`（逐仓）——失败则放弃（宁可不交易也不要错杠杆）。
3. **入场**：用 **IOC 限价**（限价 = 当前 mid ± `HL_MAX_SLIPPAGE_PCT`，默认 0.3%）而非纯市价，
   避免插针成交在离谱价位；未完全成交 → 记录实际成交量，按实际量继续（**不做补单**，避免越滑越追）。
4. **挂止损**：`reduce_only` 触发单（触发价 = 策略止损价，触发参考 = mark price）。
   **止损单是硬要求**：挂失败则立刻撤单平仓并告警（不允许裸奔持仓）。
5. 落库：真实成交价/数量/手续费 → `positions.actual_*`、`hl_fills`、`hl_orders`；
   同时更新 `positions.stop_placed=1`、`hl_status='open'`。
6. 推送成交回报（含实际 R、止损距离、实际风险 vs 目标）。

### 3.3 离场路径

1. 撤掉挂着的止损单（先撤销再平，避免两张单打架）。
2. `reduce_only` IOC 限价平仓（同滑点保护）；部分成交则记录并按剩余量重试一次，仍不完整就告警。
3. 落库离场价/手续费 → `r_multiple`、`hl_status='closed'`，推送离场回报。

### 3.4 对账（每轮 tick 与看守都跑）

- 以 `clearinghouseState` 为**唯一权威**：本地 `open` 但交易所无仓 → 标 `hl_status='mismatch'`，
  **halt 执行**并告警；交易所有仓但本地无记录 → 告警 + 暂停自动执行（人工接管）。
- 检查「有仓但无止损单」→ 高优先级告警（这是唯一会放大利风险的状态）。
- 检查 `openOrders` 里是否有非本系统挂的单（人工挂单）→ 提示但不自动撤。

### 3.5 幂等与防重复

- `hl_orders` 唯一键 `(symbol, bar_ms, purpose)`：同一根 bar 的 entry/stop 各只能落一次。
- 下单前查 `openOrders`：若已有同 coin 的挂单 → 不重复下单。
- nonce 用单调递增毫秒（`max(now_ms, last_nonce+1)`），落库；进程重启后从库里读回。
- 所有写操作**先落 `hl_orders(status=intent)` 再发请求**，响应回来后更新状态 ——
  这样即使进程在请求中途崩溃，重启后也能看到「有意图但无结果」的单，进入人工/自动核对流程。

### 3.6 Kill switch（多级）

1. 手动：`HL_EXEC_KILL=1`（env 或 DB）→ 立刻拒绝所有新写操作，只允许撤单/平仓（安全的单向出口）。
2. 自动：连续 N（默认 3）次 API 失败 / 对账 mismatch / 止损单挂失败 / 当日亏损超 `HL_MAX_DAILY_LOSS_PCT`
   （默认 5%）→ 自动置 kill + 告警。
3. 死手开关：每轮成功对账后调 `schedule_cancel(now + 30min)`（HL 的 dead man's switch）。
   若执行器卡死超过 30 分钟，交易所侧自动撤掉所有挂单 —— 这是「我方服务器挂了」的最后一道防线。

### 3.7 风控闸门（默认值，全部可配）

| 闸门 | 默认 | 说明 |
| --- | --- | --- |
| 单笔风险 | 沿用 `risk_pct`（样本期建议 0.5%~1%） | 与现有配置一致 |
| 每标的最大名义 | `权益 × 1.5` | 防止算错数量导致爆仓 |
| 总名义上限 | `权益 × 2.5` | 两仓同时开的极端情形 |
| 最小名义 | 10 USDC（交易所下限） | 小于则放弃并告警 |
| 最大滑点 | 0.3% | IOC 限价的偏离上限 |
| 每日下单次数 | 4 | 防重试风暴 |
| 当日亏损熔断 | -5% 权益 | 触发即 kill |
| 自检 | 启动时核对 coin/szDecimals/最大杠杆/min notional | 不一致则拒绝进入 live |

## 4. 鲁棒性

### 4.1 网络

- **超时与重试**：info 读写超时 10s；exchange 写超时 15s；只读请求 3 次退避重试（1s/3s/9s）；
  **写请求不盲目重试** —— 先查 `openOrders`/`userFills` 判断上一发是否已生效（nonce 相同即视为重复），
  确认未生效才重发（这是「重试导致双开」的唯一正确解法）。
- **出口路径**：实测 `api.hyperliquid.xyz` 从 oracle1 / oracle2 / google 均 200（0.21~0.32s），
  US IP 不受限（封的是网页前端）。执行器与提醒服务同机（oracle1），避免跨机依赖。
- **时钟**：nonce 依赖系统时间，容器需与宿主/NTP 对齐；看护检查 `date` 与 HL 服务器时间偏差 >2s 即告警。
- **降级**：网络不可用时**只丢机会、不丢安全** —— 无法确认状态时一律不写；已在场的仓位若失联，
  依赖 HL 侧的止损单 + 死手开关兜底。

### 4.2 对方（Hyperliquid）

- 无官方 SLA、无客服：设计上假设「API 可能不可用」「返回可能部分失败」。
- 协议风险自担：资金自托管，主钱包私钥**永不落服务器**，只用 agent 钱包（不能提币）。
- 已知事件（HLP 2025-03 单日亏损、L1 停机历史）写入 runbook 的应急处置章节。

### 4.3 自身服务器

- 资源：执行器是纯 IO 等待型，额外内存 <60MB；oracle1 现有余量 ~495MB，无需升配。
- 崩溃恢复：所有状态落 SQLite；重启后先对账再恢复执行（`hl_events` 记录崩溃前后动作）。
- 看护：`app/hl/watchdog.py`（Hermes cron 每 5 分钟，静默模式）检查：容器存活、API 可达、
  执行器心跳（`hl_events` 最近写入 <15min）、对账状态、**有无无止损持仓**、kill switch、
  死手开关是否在有效期、权益回撤。异常才推送。
- 备份：沿用现有每日 sqlite backup；新增 `hl_*` 表自动包含（同一 DB 文件）。
- 日志：执行器每个决策点写 `hl_events`（结构化），容器日志保留格式不变。

## 5. 监控面板（`/hl`）

服务器渲染 HTML + 内联 SVG，**无外部 CDN/JS 构建**（国内网络与离线可用性优先）。

参考的成熟做法（GitHub）：freqtrade（Vue 前端 + REST API，功能全但需 JS 构建）、
Hummingbot Dashboard（Streamlit，重依赖）、以及各家交易所的持仓面板。
本项目体量小、已用 FastAPI，选**服务端渲染 + 内联 SVG + 自动刷新**（10s/30s 可切），
理由：零前端构建、零外网依赖、出问题能直接 curl 看到数据。

页面内容：

- **顶部状态条**：模式（off/dry/live）、kill switch、对账状态、API 延迟、服务器时间与 HL 时间偏差。
- **账户**：权益、可用保证金、逐仓保证金占用、当日盈亏、累计盈亏、手续费/资金费累计。
- **仓位**：每仓方向/数量/入场价/标记价/**浮盈浮亏（U 与 R）**/止损价与距离/强平价/保证金/
  **止损单是否在场（红/绿）**/持仓时长。
- **权益曲线**：`hl_snapshots` 每小时快照 → 内联 SVG 折线（近 7 天 / 30 天切换）。
- **行情**：BTC/ETH 标记价、4h bar 收盘进度、信号状态（idle/pending/open）。
- **执行器**：最近 20 条 `hl_events`（含被闸门拒绝的原因）、待处理意图单、失败计数。
- **成交**：最近 20 笔 fills（价格/数量/手续费/是否 reduce-only/关联 bar）。
- **系统**：数据源健康、bar 进度、看守最后检查时间、告警通道自检。

`/api/hl/status` 返回同数据的 JSON（给看守/巡检/自动化用），字段稳定并带 `schema_version`。

## 6. 分阶段上线（每阶段可独立回滚）

| 阶段 | 内容 | 需要的凭据 | 风险 |
| --- | --- | --- | --- |
| **P0 只读** | info 客户端 + 面板 + 数量/价格规则 + 测试 | **只需钱包地址**（只读） | 无 |
| **P1 影子** | 执行器跑 `dry`：算单、落库、告警，不发单 | 同上 | 无 |
| **P2 小额实盘** | `live` + 单标的（BTC）+ 风险降至 0.5% + 全部闸门 | agent 钱包私钥 + 入金 | 小 |
| **P3 常态化** | 双标的 + 风险回到约定值 + 死手开关 + 看守告警 | 同上 | 中 |

**回滚**：任一步都是「改 `HL_EXEC_MODE` 回 off」+ `docker compose up -d`（~20s），
DB 与 HL 侧仓位不受影响；HL 侧如有残仓按 runbook 手工平。

## 7. 验证策略（可验证性的核心）

1. **单元测试（CI 阻断）**：
   - `sizing_hl`：数量向下取整、价格规则（5 位有效数字 / 小数位上限）、最小名义、
     强平价必须晚于止损价、方向校验。
   - `executor`：用 mock 交易所注入场景 —— 正常成交、部分成交、IOC 未成交、
     止损挂失败（必须平仓）、对账 mismatch（必须 halt）、重复信号（幂等）、
     API 失败 3 连（kill switch）、kill switch 下只允许平仓。
   - `views`：无凭据时页面 200、JSON schema 稳定、字段缺失不炸。
2. **回放测试**：把历史 K 线喂给「信号 + 执行器（dry）」，断言生成的订单序列与人工核对一致。
3. **Testnet 冒烟**（P2 前）：`api.hyperliquid-testnet.xyz` + 测试网钱包，跑一遍
   开仓 → 挂止损 → 成交回报 → 离场 → 撤单，核对所有落库字段。
4. **影子期比对**（P1→P2 门禁）：至少 10 个真实信号，逐条比对「执行器算的单」vs「你实际的操作」，
   差异必须能解释；否则不进 P2。
5. **实盘验收**：首笔小额单，逐项核对交易所 fills 与本地记录（价格/数量/手续费/R）。

## 8. 评估口径（预注册，防止事后挑数字）

- 样本期**降风险**（0.5%）运行：R 统计量与风险比例无关，降风险只降低「赌错」的绝对代价。
- 评估时点：满 **20 笔**或 **12 个月**（以先到为准）；均R < 0 → 停。
- 中途：回撤 > 30% 权益 → 主动告警；任一「无止损持仓」事件 → 立即人工介入。
- 必须同时记录**执行落差**（现实成交价 vs 信号价、滑点、手续费/资金费占比），
  这是自动化相对人工的核心增量证据。
- 样本期内**冻结实现**：任何执行器改动记版本锚点（`hl_events` 里带 `code_rev`），
  评估时按版本分段统计。

## 9. 需要用户提供（阻塞部署，不阻塞开发）

1. **HL 钱包地址**（只读用，随时可给）→ P0 面板即可上线。
2. **agent 钱包私钥 + 主钱包地址**（写权限）→ 走 `.env`（600 权限），**不要贴在聊天里**。
   创建方式：HL 网页 → Settings → API → 生成 API Wallet，绑定到主钱包；该 key 无法提币。
3. **入金**：Arbitrum 上的 USDC → HL（最小 5 USDC，提现固定 1 USDC/笔）。
4. 确认风控默认值（风险比例、最大滑点、当日熔断线）。

## 10. 交付物清单（本次开发）

- `docs/HL_EXECUTOR_PLAN.md`（本文件）
- `docs/HL_RUNBOOK.md`（部署、回滚、应急处置、密钥轮换）
- `app/hl/info.py`、`exchange.py`、`sizing_hl.py`、`executor.py`、`views.py`、`watchdog.py`
- `app/db.py` 迁移（4 张新表 + positions 新列）
- `app/main.py` 挂载面板与执行器钩子；`app/engine.py` 执行钩子
- `tests/test_hl_*.py`（目标：新增 ≥40 个用例，全绿）
- `requirements.txt` 增加 HL 签名依赖（固定版本）
