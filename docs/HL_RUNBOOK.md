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
| `HL_MAX_FAIL_STREAK` | 近 1h 失败多少次自动 kill | 3 |
| `HL_MAX_DAILY_LOSS_PCT` | 当日亏损熔断线 | 0.05 |
| `HL_DEADMAN_MINUTES` | 死手开关窗口（每分钟自动续期） | 30 |
| `HL_MIN_NOTIONAL` | 交易所最小名义 | 10.0 |

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
```

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
- 系统行为：每小时 :11 检查一次，剩余 ≤14 天开始提醒（≤7 天升级文案）；≤3 天且开关开 → **先置 kill → 撤全部挂单 → 逐仓市价平 → 复查无仓**；已过期 → 置 kill + 每 24h 提醒。面板总览最下方「API 有效期」卡片可看状态（数据来自 DB 缓存，不现调接口）。
- 续期（唯一正确姿势）：**HL 网页 → More → API**，用**同名 `Hermes`** 生成新 API 钱包（有效期选 180 天）→ 复制新私钥 → 面板「API 有效期」卡片里展开「更新 API」→ 粘贴 → 校验并保存。保存后下一轮 tick 自动用新私钥（`hl_params()` 每轮重建执行器），**无需重启**。
- 校验失败的原因都在 400 文案里：格式不对 / 名字不是刚好 `Hermes` / 地址与粘贴的不一致（说明网页上还没授权成功）/ 授权已过期 / 就是当前这把。
- 手工兜底：接口查不到同名授权时，到「参数」页「API 有效期」组填毫秒时间戳（`hl_agent_valid_until_ms`），留空表示不兜底。
- 关掉自动清仓：「参数」页把「到期前自动清仓」取消勾选（识别不了的值一律按「开」——漏清仓的代价比提前平仓大）。
- 清完仓后恢复交易：先续期并更新 API，再到「开关」页解除 kill（清仓本身不清 kill，这是故意的）。

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
4. **注入合成行情的教训**：非目标标的必须返回「全平 K 线」。曾把 BTC 的合成价当 ETH 行情，
   算出「ETH 止损 81,682 而 ETH 价 2,500」的荒谬信号 —— 执行器正确拒绝算仓、引擎兜住
   （不裸奔、不崩），但仍会多发一条人工操作卡（这是「执行器失败 → 退回人工」的正常设计）。
