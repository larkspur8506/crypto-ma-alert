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
| `HL_PRIVATE_KEY` | **agent 钱包**私钥（不能提币）。永不落库、永不进日志 | — |
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

## 5. 看守 cron（建议装）

```
job: HL 执行器看守（Hermes cron，每 5 分钟，no_agent 静默）
command: ssh oracle1 'sudo -n docker exec crypto-ma-alert python -m app.hl.watchdog'
仅当输出以「⚠️」开头时推送（脚本设计如此：正常时无输出）
```

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

## 7. 备份 / 恢复

- 沿用现有每日备份（`/opt/backups/crypto-ma-alert/backup.sh`，容器内 sqlite3 backup → 保留 14 天）；`hl_*` 表在同一 DB 内，自动包含。
- 恢复：停容器 → 用备份覆盖 `crypto_ma.db` → 起容器 → 跑 `watchdog --dry-run` 与面板核对 → 解除 kill。

## 8. 验证清单（每次改动后必跑）

```bash
# 全量单测（本地/CI 同源）
/opt/data/cache/scratch/cma-venv/bin/python -m unittest discover -s tests -t .
# 装配冒烟（路由 + 面板挂载）
/opt/data/cache/scratch/cma-venv/bin/python -m unittest tests.test_app_smoke
# 只读客户端（不需要凭据）
/opt/data/cache/scratch/cma-venv/bin/python -c "from app.hl.info import HlInfo; print(len(HlInfo().specs()))"
```
