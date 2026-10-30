# HL 执行器 Runbook（部署 / 回滚 / 应急）

> 面向「出事时照着做」的操作手册。设计背景见 `docs/HL_EXECUTOR_PLAN.md`。

## 1. 三种模式与切换

| 模式 | 行为 | 切换方式 |
| --- | --- | --- |
| `off`（默认） | 只提醒，不碰交易所 | 面板 `POST /admin/hl/mode` 或 `HL_EXEC_MODE=off` |
| `dry` | 每轮算单、落库 `hl_orders(status=planned)`、不下单 | 同上选 `dry` |
| `live` | 真实下单 + 挂止损 + 对账 | 同上选 `live`（要求已配 `HL_ACCOUNT_ADDRESS` + `HL_PRIVATE_KEY`） |

**回滚 = 面板上把模式切回 `off`**（或 `docker compose up -d` 后不带 HL env）。数据卷与 HL 侧仓位不受影响。

## 2. 需要的环境变量（写进 1Panel compose，600 权限）

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `HL_EXEC_MODE` | off/dry/live（DB 值优先） | `off` |
| `HL_ACCOUNT_ADDRESS` | 主钱包地址（只读也用） | — |
| `HL_PRIVATE_KEY` | **agent 钱包**私钥（不能提币）。永不进日志；面板「更新 API」写入的值存在 DB `hl_private_key`，**DB 优先于 env** | — |
| `AGENT_EXPIRY_CLEANUP` | 到期前自动清仓（DB `agent_expiry_cleanup` 优先） | `1`（开） |
| `HL_AGENT_VALID_UNTIL_MS` | 手工兜底的有效期（毫秒；接口查不到时用，DB 优先） | 空 |
| `HL_BASE_URL` / `HL_TESTNET` | 主网/测试网 | 主网 |
| `HL_COIN_MAP` | `{"BTC_USDT":"BTC","ETH_USDT":"ETH"}` | 同左 |
| `HL_MAX_SLIPPAGE_PCT` | IOC 限价偏离上限 | 0.003 |
| `HL_MAX_NOTIONAL_MULT` / `HL_MAX_TOTAL_NOTIONAL_MULT` | 单标的/总名义 ÷ 权益 | 1.5 / 2.5 |
| `HL_MAX_ORDERS_PER_DAY` | 当日下单次数上限 | 4 |
| `HL_MAX_SIGNAL_AGE_MIN` | 信号时效闸：信号 bar 收盘超过这么多分钟就**拒绝开仓**（只拦开仓，离场/止损不读）。`0` = 关闭，负数/脏值退回 30 | 30 |
| `HL_MAX_FAIL_STREAK` | 近 1h 失败多少次自动 kill | 3 |
| `HL_MAX_DAILY_LOSS_PCT` | 当日亏损熔断线 | 0.05 |
| `HL_DEADMAN_MINUTES` | 死手开关窗口（每分钟自动续期） | 30 |
| `HL_MIN_NOTIONAL` | 交易所最小名义 | 10.0 |
| `HL_CHURN_MAX` | 同一 coin-side 10 分钟内撤单/改价重挂达这次数就停手（0 = 关闭熔断）。DB `hl_churn_max` 优先 | 5 |
| `HL_FILL_CLOSE` | 交易所侧已平仓时按成交价自动关账（见 §6 B 前的「止损已成交」小节）。DB `hl_fill_close` 优先 | `1`（开） |

改 env 必须 `docker compose up -d` 重建（容器不热读 env）；改 `hl_exec_mode` / kill 走面板或 DB，立即生效。

## 3. 上线顺序（严格按序，每步验收）

1. **P0 只读**：只填 `HL_ACCOUNT_ADDRESS` → 打开 `https://ma.222677.xyz/hl?k=<ADMIN_TOKEN>`。
   验收：账户权益/仓位/挂单/权益曲线都能显示；`curl -s .../health` 里有 `"hl_mode":"off"`。
2. **P1 影子**：`HL_EXEC_MODE=dry` + `HL_ACCOUNT_ADDRESS`（不需要私钥）。
   验收：真信号来时 `hl_orders` 出现 `planned` 行，面板「执行器事件」可见决策；连续 ≥10 个信号与人工判断逐条比对。
3. **P2 小额实盘**：`HL_EXEC_MODE=live` + 私钥；`RISK_PCT` 建议先降到 0.005；先只开 BTC。
   验收：首单核对交易所 fills 与 `hl_orders`（价格/数量/手续费/止损单在场）；面板 `has_stop` 为 ✅。
4. **P3 常态化**：恢复约定风险比例、开 ETH、装上看守 cron（见 §5）。

## 4. 日常运维命令

### 控制接口（2026-10-13 在本机真服务上实测）

```bash
BASE=https://ma.222677.xyz ; K=$ADMIN_TOKEN      # 面板与后台接口都要 ?k=，否则 401

curl -s "$BASE/health"                            # 含 hl_mode / hl_kill
curl -s "$BASE/api/hl/status?k=$K" | python3 -m json.tool | head -40   # 面板数据源
curl -s "$BASE/hl?k=$K" -o /tmp/hl.html           # 监控面板

# 切换模式（form 编码；切 live 时若无凭据会被 400 拒绝 —— 这是故意的）
curl -s -X POST -d "mode=dry&k=$K" "$BASE/admin/hl/mode"
curl -s -X POST -d "mode=off&k=$K" "$BASE/admin/hl/mode"

# kill switch（on=1 暂停自动交易，仍允许人工平仓；解除用 on=0）
curl -s -X POST -d "on=1&k=$K" "$BASE/admin/hl/kill"
curl -s -X POST -d "on=0&k=$K" "$BASE/admin/hl/kill"

# 手动触发一次 tick（跑真实行情，dry 模式不会下单）
curl -s -X POST "$BASE/admin/tick?k=$K"

# 为什么没开仓（/why，只读诊断；PR-5）
curl -s "$BASE/api/hl/why?k=$K" | python3 -m json.tool   # 每标的：六步 + 闸门状态
# 容器内纯文本版（同一条链路；不进面板也能看）
ssh oracle1 'sudo -n docker exec crypto-ma-alert python -m app.why'
```

面板上这块已经不单独成段：它的「拦截原因 / 闸门异常」并进了总览最顶部的「三标的动态」
卡片（原因行 + 闸门异常行），同一屏不再出现两个现价、两套状态词。

`/api/hl/why` 的字段口径：

- `symbols[].why.steps[]` —— 六步（`bars` 根数 → `squeeze` 密集 → `breakout` 破线 →
  `regime` MA200 → `stop` 止损距离 → `decision` 结论），每步 `ok` + 一句人话 `detail`；
- `symbols[].sma20` —— 最后一根**已收盘** bar 的 SMA20（与触发线同索引；面板「距自动离场线」
  用它画，只作展示，不参与判定）；
- `symbols[].why.blocked_by` —— **第一个**不成立的步骤（`ok` = 有信号）；
- `symbols[].why.drift` —— 六步推导与 `evaluate_entry` 的返回值不一致（正常恒 `false`，
  出现即说明诊断与信号口径已分叉，需排查）；
- `symbols[].gates.{long,short}` —— 闸门状态（静置剩余秒 / 10 分钟 churn 次数 / 是否熔断）。
  **只读**：读它不会续期静置窗口、不会加 churn 计数、不写事件、不置 kill。

### 服务器侧

```bash
# 容器状态与日志
ssh oracle1 'sudo -n docker ps --format "{{.Names}}\t{{.Status}}" | grep crypto-ma'
ssh oracle1 'sudo -n docker logs --since 30m crypto-ma-alert 2>&1 | grep -iE "hl|executor" | tail -30'

# 账户/仓位真相（以交易所为准）
ssh oracle1 'curl -s -X POST https://api.hyperliquid.xyz/info -H "Content-Type: application/json" \
  -d "{\"type\":\"clearinghouseState\",\"user\":\"<地址>\"}" | head -c 800'

# 最近的下单动作与事件
ssh oracle1 'sudo -n docker exec crypto-ma-alert python -c "
import sqlite3; c=sqlite3.connect(\"/app/data/crypto_ma.db\"); c.row_factory=sqlite3.Row
for r in c.execute(\"select symbol,purpose,status,sz,avg_px,error,datetime(created_at,\\\"unixepoch\\\") t from hl_orders order by id desc limit 10\"): print(dict(r))
for r in c.execute(\"select kind,message,datetime(created_at,\\\"unixepoch\\\") t from hl_events order by id desc limit 10\"): print(dict(r))
"'

# 看守（异常才输出）
ssh oracle1 'sudo -n docker exec crypto-ma-alert python -m app.hl.watchdog --dry-run'
```

## 5. 看守 cron（2026-10-14 已装）

```
job: 「HL 执行器看守」 id=b18f371574bd（Hermes cron，每 5 分钟，no_agent 静默，deliver=origin）
wrapper: /opt/data/scripts/hl-watch/hl_watch.py
  → ssh 重试 3 次（退避 5s/10s），链路抖动不再误报
  → 远程执行容器内 `python -m app.hl.watchdog`
仅当 watchdog 打出 ⚠️ 时才输出（正常完全静默，cron 收到空 stdout 不投递）。
排障：`ssh oracle1 'sudo -n docker exec crypto-ma-alert python -m app.hl.watchdog --dry-run'`
演练：`HLW_SSH_HOST=localhost python3 /opt/data/scripts/hl-watch/hl_watch.py`（应 ~16s 后报警）
```

## 5.5 写路径真单冒烟（切 live 前 / 改过写路径后）

```bash
# 1) 先把模式切 off（避免看守把这张冒烟仓判成「本地无记录的人工单」→ 拉 kill）
curl -s -X POST -d "mode=off&k=$ADMIN_TOKEN" "$BASE/admin/hl/mode"
# 2) 跑冒烟：约 12 USDC 名义，开仓 → 挂止损 → 核对 → 撤止损 → 平仓，≈5 秒、成本 ≈0.01 USDC
cat scripts/hl_live_smoke.py | ssh oracle1 'sudo -n docker exec -i crypto-ma-alert python -'
# 3) 看 ⑧ has_stop=True、⑫ 仓位/挂单归零，再切回 dry 或 live
```

**不跑这一步就会踩的坑（2026-10-14 实测）**：cloid 类型错误（下单直接失败）、
`openOrders` 缺 isTrigger/triggerPx（止损单被误判成裸奔 → 对账拉 kill switch）。
单元测试用假 SDK 全绿也发现不了这两类问题 —— 真单冒烟是唯一能验证写路径的手段。

## 6. 应急手册

### A. 面板 `has_stop` = ❌（有仓无止损）
**最高优先级**。立刻：① 面板/网页手动挂回止损单；② 若来不及，手动平仓；③ 查 `hl_events` 找原因（`止损挂单失败` → 执行器已自动平仓并标记 `aborted`）。
执行器的设计是「止损挂失败 → 立刻平仓」，所以出现裸奔仓位通常意味着**平仓也失败了**（如交易所不可达），此时人工介入。

### B. 对账 mismatch（`hl_events` 有「对账异常」）
执行器会**自动 kill**（停止新开仓）。处理：① 用 §4 的 `clearinghouseState` 看真实仓位；② 与本地 `positions` 表核对；③ 人工把仓位与本地记录对齐（平掉多余的仓 / 在确认页补录）；④ 面板解除 kill 前，先确认 `hl_orders` 里没有卡在 `intent/sent` 的单。

#### 止损/人工已在交易所平仓（`hl_fill_close`，默认开）
对账发现「本地有仓、交易所无仓」时，**先**用本轮已同步的 `hl_fills` 自证仓位已平：成交必须是开仓后（`opened_at−120s` 起）、`reduce_only`/`Close` 腿、合计数量落在「持仓量（`hl_sz`，回落 `actual_contracts`）± 1 tick」区间内（**低于** = 只平了一部分；**高于** = 窗口里混进了不属于这仓的成交，账不可归因）。全部满足才按**加权成交均价**关账（`exit_reason` = 全部成交出自 `hl_stop_oid` → `stop`，否则 `exchange`）、把对应挂单落 `filled`、推一条提醒，且**不** kill；任一条件不满足（无成交/数量不足或超额/精度不可知/oid 不明）完全退回旧行为（mismatch + kill）。
`exit_reason=exchange`（人工/外部平掉，止损单没成交）时，执行器会顺手撤掉该 symbol 仍 `resting/sent` 的止损类挂单（`stop` / `stop:fix` / `exit:rehang`，只撤有明确 oid 的行）并落 `cancelled` —— 否则那张孤儿 reduce-only 单会把下一次开仓平掉。撤单失败：行保持 `resting` + `hl_events` 记 error + 推一条告警，需人工到 HL 撤单。
面板「策略与保护」可关掉该开关（DB `hl_fill_close=0`）：**两条路径一起**回到旧行为 —— 对账不再按成交证据关账，`on_exit` 的「mismatch + stop」放行也一并失效（mismatch 行直接让出接管权，引擎走人工提醒流）。关掉后止损成交只能人工补录，别指望 4h 收盘的 `on_exit` 代关。
两条容易踩的边界（对抗审查后收紧）：① **挂单落 `filled` 只认 oid 相等** —— 成交里没有 oid（升级前已同步的老成交恒为 NULL）时一行都不改，只记一条 `hl_events`（候选来自 `hl_recent_orders` 的跨 bar 扫描，「恰好一行」也可能是别的仓那张没成交的止损）；② `on_exit` 的止损关账**必须有 `Close` 腿**，只有开仓腿时 `_last_fill_px` 返回 `None`（不再拿开仓价冒充成交价）→ mismatch 行让出接管权、不关账；同一轮对账里一批成交只归一个仓（tid 已消耗即 fail-closed）。

### B2. 数量对账不一致（`hl_events` 有「数量对账」/「持仓数量对账不一致」）
reconcile 除了比「有没有仓 / 有没有止损」，还比**数量**：本地 `hl_fills` 的净量
（ΣOpen − ΣClose，窗口 `opened_at−300s` 至今）对交易所 `szi`，容差 1 tick
（`10^-szDecimals`）。两击规则：
1. 首次超差 → 记事件 + **自动全量回捞一次** `userFillsByTime`（从开仓前 300s）重算；
   回捞后一致 = 自愈（**不会**动 kill、不计击）；
2. 回捞后仍超差 → 计一击（第 1 击只观察，不 kill）；
3. 连续 2 击 → 自动 kill + 推「对账异常」告警（fail-closed）。

strike 计数只在进程内存里（重启清零），且按 (仓位, 标的) 计。排查：
① `hl_events` 里搜「数量对账」看两边的数字；② 面板「记录」页看该标的 `hl_fills` 是否
缺腿（部分成交、手工加减仓、IOC 只成一半都会让两边分叉）；③ 真实数量以交易所为准，
本地 `hl_sz` 错要人工核对（执行器的止损量是按下单时 `hl_sz` 挂的，量错了保护也是错的）；
④ 若确认是「本地流水缺数据」而非真错仓，回捞会把成交补进 `hl_fills` → 下一轮自动清零
strike，再解除 kill 即可。

容差用的 `szDecimals` 来自 `meta`（一次真实 POST），按 (info 对象, coin) 在进程内缓存
**1 小时**、失败不缓存（对抗审查 M6：执行器不健康时这一步原本每 5 分钟打一次 + 3 次重试）。
交易所若真的调整了某标的精度，最多 1 小时内仍按旧 tick 判容差；怀疑精度变了就重启进程。

### B3. 关账净额差异（`hl_events` 有「关账口径差异」）
每笔关账都会用 `hl_fills` 聚合出流水口径净额（`positions.net_pnl`），并与本地价差口径
（`r_multiple` / 价差×币量）比对：`|ΔR| > max(0.15, 0.10×|R|)` 或
`|Δnet| > max(1.0, 0.0005×名义)` 就记一条 `reconcile` 事件（**纯观测**，不 kill、不告警）。
看到这条说明「本地记的价/量」与「交易所流水」有出入，去面板「记录」页按 `hl_fills` 核对；
`pnl_source`：`fills` = 已算（有出入场腿）、`no_exit_fills` = 平仓成交还没同步（净额留空）、
`no_fills` = 查不到任何成交（人工流/未同步）。
「开关」页「策略与保护 → 记录页净额改用流水口径」（`hl_pnl_from_fills`，**默认关**）只决定
记录页要不要显示「净额（流水）」列，不改变入库行为 —— 打开后如果数字可疑，关回去即可。

### B4. 闸门拦截 / churn 熔断（`hl_events` 有「动作被闸门拦下」/「churn 熔断」）
两道闸门都只想解决一件事：**别在状态还没收敛时重复动手**，以及**别陷进撤-挂-撤-挂**。

1. **静置闸门（`settle`，15s）**：`on_entry` 拿到成交回报那一刻，该 coin-side 进入
   15s 静置期。期间**禁止**：补挂重试（修复类）、新开仓；**豁免**（永不延迟）：
   首次止损挂单、平仓、一切只读查询，以及「止损已被撤掉之后的离场重挂」
   （那条路径再等 15s 就是纯裸仓，它的频率问题交给 churn）。
   - 窗口**有界且不可续期**：只有「又一次成交回报」会重新起算，对账/补挂/其它事件
     都不能把它推后（可续期 = 可以被无限拖延）。
   - 修复类**首次**补挂豁免：`churn=0`（10 分钟内没撤挂过）说明这是「仓刚成交、
     止损没挂上」的第一手保护，拦它就是裸仓等 15s。被拦的只有**重试**。
   - 看到这条事件通常**不需要人工动作**：下一轮 tick 就过了窗口。真正要关心的是
     「同一 coin-side 反复出现」——那时候看下一条。
2. **churn 熔断（`churn`，10 分钟 ≥ N 次撤单/改价重挂）**：阈值 `hl_churn_max`
   （默认 5，0 = 关闭熔断只留静置）。达到阈值 → **停手**（补挂/复价暂停）+
   `decision` 事件 + 走既有告警通道推一条（同一窗口只推一次）。
   - **不置 kill**：这不是对账异常，仓位与本地记录可能都是对的，只是动作太密。
   - **不重挂 ≠ 不保护**：修复路径在熔断时直接市价平（保护动作不过闸）；
     离场路径的「重挂止损」被拦时也落到既有的「重挂失败 → 市价平仓」分支。
   - **自动恢复**：窗口滑出后计数归零，不需要任何人解锁（也不该去清 ——
     熔断状态只在进程内存里，重启即清零）。
   - 怎么处理：① 看 `hl_events` 找补挂失败的原因（多半是止损单被交易所拒：
     触发价方向/价格精度/持仓量变化）；② 看面板 `has_stop` 是否 ❌ → 按
     §6.A 人工挂回止损；③ 若只是自己反复改价，等 10 分钟窗口滑出即可。
3. 两个计数都只在**进程内存**里（类属性，跨 tick 新建的 Executor 共用）：重启清零
   是可接受的代价 —— 重启本身打断了撤挂风暴；写 DB 则会引入「几天前的旧记录把标的
   永久熔断」的风险。

### C. 执行器卡死 / 心跳超 15 分钟
① 先看容器是否活着；② 死手开关会在 `HL_DEADMAN_MINUTES` 后自动撤掉所有挂单（**止损单也会被撤 → 仓位裸奔**）→ 立刻人工挂回止损或平仓；③ 重启容器：`ssh oracle1 'cd /opt/1panel/docker/compose/crypto-ma-alert && sudo -n docker compose restart'`；④ 重启后先看 `hl_events` 确认对账通过。

### D. API 大面积失败（连续 3 次）
执行器会自动 kill 并推送。处理：① 确认 `api.hyperliquid.xyz` 可达（本机/服务器各测一次）；② 确认时钟偏差 <2s；③ 若交易所维护，保持 kill，等恢复后人工解除。

### E. 当日亏损熔断
推送里带实际百分比。这是**人工决定**：默认保持 kill 到次日（基线重置）。要看真实亏损：面板「当日已实现/累计」或 `hl_fills` 的 `closed_pnl` 之和。

### F. 私钥疑似泄露
① 立刻在 HL 网页 → Settings → API 删除该 agent wallet（不需要主钱包私钥即可撤销）；② 新建 agent wallet、更新 `.env`、`docker compose up -d`；③ 检查 `hl_orders`/`hl_fills` 有无非本系统的动作（执行器会记录自己发的每一单，对不上就是别人下的）。

### G. API 钱包（agent）快到期 / 已失效
有效期最长 180 天、默认 90 天，到期那一刻执行器**开仓、平仓、撤单全部被拒**（而本地记录看着一切正常）。
- 系统行为：每小时 :11 检查一次，剩余 ≤14 天开始提醒（≤7 天升级文案，独立 kind 所以升级不会被去重压住）；≤3 天且开关开 → **先置 kill → 撤全部挂单 → 逐仓市价平 → 复查无仓 → 本地仓位同步关账**；已过期（毫秒精确，`now >= validUntil`）→ 置 kill + 每 24h 提醒。面板「参数」页末尾「API 有效期」卡片可看状态（数据来自 DB 缓存，不现调接口）。
- 到期当天不再「整天算过期」：只要 `validUntil` 还没到，当天仍走清仓/重试档（还有几个小时可以平仓）。
- 授权里 `validUntil` 为空 = **不限期**：面板显示「不限期」，不告警、不清仓，也不会拿「参数」页的手工兜底值去算（那是陈旧值，曾导致误平仓）。
- 续期（唯一正确姿势）：**HL 网页 → More → API**，用**同名 `Hermes`** 生成新 API 钱包（有效期选 180 天）→ 复制新私钥 → 面板「API 有效期」卡片里展开「更新 API」→ 粘贴 → 校验并保存。保存后下一轮 tick 自动用新私钥（`hl_params()` 每轮重建执行器），**无需重启**。
- 校验失败的原因都在 400 文案里：格式不对 / 名字不是刚好 `Hermes` / 地址与粘贴的不一致（说明网页上还没授权成功）/ 授权已过期 / 就是当前这把。**首次配置**（库里还没有私钥）跳过「同一把」检查。
- 手工兜底：接口查不到同名授权时，到「参数」页「API 有效期」组填毫秒时间戳（`hl_agent_valid_until_ms`）。**留空保存 = 保持原值**（不动它）；要清空点该页的「清除兜底」按钮。
- 关掉自动清仓：「开关」页「策略与保护」把「到期前自动清仓」取消勾选（原样保存即生效；识别不了的值一律按「开」——漏清仓的代价比提前平仓大）。
- 执行器处于 `off`（只提醒）时：不会清仓，面板状态显示「未自动清仓（执行器 off）」，并推一条「请自行决定是否手动处理」（7 天节流一次，不会每小时刷）。
- **清完仓后恢复交易**：先续期并更新 API，再到「开关」页解除 kill —— 清仓本身不清 kill（故意的）。**本地仓位已在清仓时自动关账（`exit_reason=agent_expiry_cleanup`、`auto_closed=1`），不需要手工改库**；对账不会再报 mismatch，解 kill 后不会被重新拉回去。若真实成交价与参考价有差异，到「记录」页按 `hl_fills` 核对，需要的话在确认页覆盖。


## 7. 备份 / 恢复

- 沿用现有每日备份（`/opt/backups/crypto-ma-alert/backup.sh`，容器内 sqlite3 backup → 保留 14 天）；`hl_*` 表在同一 DB 内，自动包含。
- 恢复：停容器 → 用备份覆盖 `crypto_ma.db` → 起容器 → 跑 `watchdog --dry-run` 与面板核对 → 解除 kill。

## 8. 验证清单（每次改动后必跑）

```bash
# 全量单测（本地/CI 同源）
/opt/data/cache/scratch/cma-venv/bin/python -m unittest discover -s tests -t .
# 装配冒烟（路由 + 面板挂载）
/opt/data/cache/scratch/cma-venv/bin/python -m unittest tests.test_app_smoke
# API 到期监控 / 自动清仓 / 面板更新 API
/opt/data/cache/scratch/cma-venv/bin/python -m unittest tests.test_agent_expiry tests.test_agent_renew
# /why 只读诊断（六步 + 闸门只读性 + 与 evaluate_entry 一致性）
/opt/data/cache/scratch/cma-venv/bin/python -m unittest tests.test_why
# 只读客户端（不需要凭据）
/opt/data/cache/scratch/cma-venv/bin/python -c "from app.hl.info import HlInfo; print(len(HlInfo().specs()))"
```

## 真机验证发现（2026-10-14，切 live 前最后一轮）

1. **走应用自身链路的 live 真单已通过**（不再只靠旁路彩排脚本）：
   `_job_tick()` → `engine.tick(executor=build_executor())` → 引擎建仓 → 执行器 live `on_entry`
   → 真开仓 + 真挂止损 + 对账 → 真 `on_exit` 平仓。实测：成交 83,775 / 止损 81,682（-2.5%）/
   交易所仓位与挂单与 DB 一致 / 终态仓位 0 挂单 0。空头方向单独验证：止损挂在上方（+2.5%）、
   强平价在入场价**上方**（83,762 → liq 99,236），方向正确。
2. **死手开关（HL `scheduleCancel`）在本账户不可用**：接口要求账户有足够成交量，返回
   `Cannot set scheduled cancel time until enough volume traded`。原实现每轮都记一条错误事件
   （live 下 288 条/天），已改为状态变化时才记（`hl_deadman_err`）。
3. **待决策（未改代码）**：`scheduleCancel` 的语义是「机器人失联就把所有挂单撤掉」。对**持仓型**
   策略这可能是反向的保险 —— 我们的止损单本身就是持仓的唯一保护，被它一起撤掉等于裸奔。
   倾向：不启用它，改为「交易所侧止损 + 看守每 5 分钟判活 + 巡检」。需用户拍板。
   **2026-10-22 补充（仍未启用，默认关闭）**：上面的语义意味着「kill 停手期间不续期」是最坏
   组合 —— 熔断（dd/manual 永不自解）期间定时器照常到期 → 止损被撤 → 裸仓可以持续几天。
   执行器已改为 **kill 态也续期**（`_run_locked` 第 4 步不再判 `hl_kill`）：续期本身不动任何
   挂单，只是把撤单时刻往后推一个窗口。开启前务必确认你接受它的原生副作用：**执行器进程
   真的停摆时，它仍会连 reduce-only 止损一起撤掉**。
4. **注入合成行情的教训**：非目标标的必须返回「全平 K 线」。曾把 BTC 的合成价当 ETH 行情，
   算出「ETH 止损 81,682 而 ETH 价 2,500」的荒谬信号 —— 执行器正确拒绝算仓、引擎兜住
   （不裸奔、不崩），但仍会多发一条人工操作卡（这是「执行器失败 → 退回人工」的正常设计）。
