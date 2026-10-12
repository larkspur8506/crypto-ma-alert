# crypto-ma-alert

双均线（6MA）趋势系统的信号提醒服务 —— MEXC 永续 `BTC_USDT` / `ETH_USDT`，4h 级别，**只提醒不下单**。

部署在 oracle1，走 GitHub Actions → GHCR → 1Panel compose，推送复用 qqq-leaps 的企业微信 + Telegram 通道。

## 策略（冻结版 v1）

| 项 | 规则 |
|---|---|
| 指标 | SMA20/60/120 + EMA20/60/120（收盘价） |
| 均线密集 | `band = (6均线max − 6均线min)/收盘 ≤ 过去 250 根 4h 的 20% 分位`，且**连续 ≥6 根** |
| 入场（只做多） | 本根收盘 > 上一根的 `6均线max` → **次根开盘**市价入场 |
| 止损 | 本根 `6均线min`（band 下沿）；**止损距离须在 1.5%~10%**，否则放弃 |
| 离场 | 任一根收盘 < SMA20（止损优先）。**无固定止盈** |
| 仓位 | `输入金额(USDT) = 权益 × 风险% ÷ 止损距离%`（MEXC 的 USDT 单位填的是**名义价值**，不是保证金） |
| 杠杆 | **逐仓 5x**（强平 -19.5%，永远晚于止损） |
| 上限 | 每标的至多 1 仓，最多同时 2 仓 |

**为什么离场不用固定赔率**：实测「固定 3R」在 2021 年后失效（均R +0.02/+0.06），改用 MA20 跟踪后三段时代均为正。
**为什么止损过滤收紧到 10%**：配合 5x，保证强平永远晚于止损；实测与 1.5~20% 版本等价。

回测口径（BTC+ETH，4h，含 MEXC 0.02%/边手续费与最小下单约束，单笔风险 2%）：
378 笔、胜率 36.8%、均R **+0.445**（t=3.51）、9.1 年 1000U→16,475U、最大回撤 23.4%。
分时代：2017-20 +0.57R / 2021-23 +0.39R / 2024-26 +0.24R。

## 运行

```bash
pip install -r requirements.txt
export ADMIN_TOKEN=<随机串> TOKEN_SECRET=<随机串> BASE_URL=https://ma.222677.xyz
export WECHAT_WEBHOOK_URL=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
uvicorn app.main:app --port 8000
```

环境变量：

| 变量 | 说明 |
|---|---|
| `SYMBOLS` | 默认 `BTC_USDT,ETH_USDT` |
| `ACCOUNT_EQUITY` | 账户权益（U），算仓位用；后台可改 |
| `RISK_PCT` | 单笔风险比例，默认 0.02 |
| `LEVERAGE` | 默认 5 |
| `STOP_MIN` / `STOP_MAX` | 止损距离过滤，默认 0.015 / 0.10 |
| `BASE_URL` | 确认链接的域名 |
| `ADMIN_TOKEN` | `/admin` 访问口令（`?k=<token>`） |
| `TOKEN_SECRET` | 签名链接密钥 |
| `DRY_RUN` | `true` 时只打印不发送 |
| `ENABLE_SCHEDULER` | `false` 时不起调度器（本地调试用） |

## 调度

- **检查**：每小时 `:03` 和 `:33`（每根 4h 收盘后 3 分钟必跑，`:33` 是重试机会）
- **心跳**：每天 08:00 UTC（数据源健康 + 当前持仓 + 近 24h 提醒成败）
- **周报**：每周一 08:00 UTC（笔数 / 均R / 胜率 / 未确认笔数）

幂等键 = `symbol + bar_open_ms + kind`，且**只在推送成功后才标记**（失败下次重试）。
停机期间错过的信号会在下次 tick 补报：可交易的只有**最近一根已收盘 bar**，更早的发「错过的信号」提示。

## 确认流（方案 B）

入场提醒里带一个签名链接（无需登录）：`/c/<id>?a=view&t=<hmac>`。点开后：

- **实际成交价**（预填信号价）
- **实际张数**（预填建议值）
- **实际止损触发价**（预填 band 下沿）
- **☑ 已挂止损单**
- 按钮：按以上数值确认 / 未开仓·忽略

离场提醒同理带 `/c/<id>?a=close&t=...`，填实际平仓价 + 离场原因。
未确认的仓位按假设值计入统计，周报会标注「未确认 N 笔」。

## 目录

```
app/strategy.py   纯 stdlib 核心（指标 / 密集 / 信号 / 离场）—— 唯一口径来源
app/market.py     MEXC K 线 + 降级链（MEXC合约 → MEXC现货 → Binance → OKX）+ 校验
app/sizing.py     以损定仓 → MEXC 张数 / 名义 / 保证金 / 强平价
app/engine.py     walk() 纯状态机 + 提醒文案 + tick + 心跳/周报
app/notify.py     企业微信 + Telegram 双通道（重试 / 分段 / 成功后才去重）
app/main.py       FastAPI + APScheduler + 状态页/确认页/参数页
scripts/dry_run.py            离线回放（与线上同一份 walk）
scripts/xvalidate.py          信号级交叉验证（实盘引擎 vs 回测引擎）
scripts/xvalidate_roundtrips.py 回合级交叉验证（入场/离场 bar 逐笔一致）
```

## 上线前的两道验证

```bash
python -m unittest discover -s tests -t .     # 32 项单测
python3 scripts/xvalidate.py                  # 信号集合必须与回测引擎完全一致
python3 scripts/xvalidate_roundtrips.py       # 完整回合必须逐笔一致
python3 scripts/dry_run.py --days 90          # 看会推什么
```

## 部署（oracle1）

```bash
# 1Panel compose 工程目录
/opt/1panel/docker/compose/crypto-ma-alert/docker-compose.yml
```

```yaml
services:
  crypto-ma-alert:
    image: ghcr.io/larkspur8506/crypto-ma-alert:latest
    container_name: crypto-ma-alert
    restart: unless-stopped
    ports:
      - "127.0.0.1:8100:8000"
    environment:
      TZ: UTC
      BASE_URL: https://ma.222677.xyz
      ADMIN_TOKEN: <随机串>
      TOKEN_SECRET: <随机串>
      WECHAT_WEBHOOK_URL: <复用 qqq 的企业微信 webhook>
      TELEGRAM_BOT_TOKEN: <复用 qqq 的 bot>
      TELEGRAM_CHAT_ID: <用户 DM chat_id>
    volumes:
      - cma_data:/app/data
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
volumes:
  cma_data:
```

站点 `ma.222677.xyz` → openresty 反代 `127.0.0.1:8100`（1Panel 建站 + 证书）。

## 风险与对策

- MEXC 接口变更/限流 → 降级链并在提醒里标注来源；数据陈旧或根数不足直接报「数据源异常」
- 容器/网络停机 → 每 tick 回看未处理的 bar，补报标注「历史信号」
- 策略失效 → 周报统计均R；连续 20 笔均R<0 或权益回撤 >30% 建议停
- **不做自动下单**（与「只做只读数据类工具」的原则一致）
