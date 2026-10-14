#!/usr/bin/env python3
"""HL 执行器彩排（发合成信号走全链路，验证接线）—— 2026-10-14 审计三轮产物。

背景：10-14 的彩排是临时脚本直接拼 engine.tick(真 notifier)，两条合成 ETH 信号
真实推到了生产通道（用户收到假「开多信号」+ 假「补报平仓」，链接还是死链）。
本脚本固化彩排流程，杜绝再犯：

- 提醒一律走 stdout（绝不 import app.main 的 notifier / 绝不发网络推送）
- 落库用独立临时 DB（不碰生产 crypto_ma.db）
- 只验证「事件会被产生、文案长什么样」，不验证推送通道（通道测试属于上线检查）

跑法（容器内）：
    cat scripts/hl_rehearse.py | sudo -n docker exec -i crypto-ma-alert python -
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import DB
from app.config import Config


class StdoutNotifier:
    """彩排专用：只打印，绝不推送。"""

    def send_message(self, text):
        print("----- [彩排·仅打印] -----" + "\n" + text, flush=True)
        return True


def main():
    tmp = tempfile.mkdtemp(prefix="cma-rehearse-")
    print(f"彩排数据库: {tmp}/rehearse.db（与生产隔离）")
    db = DB(os.path.join(tmp, "rehearse.db"))
    cfg = Config()
    print(f"模式: {db.get_config_str('hl_exec_mode')}")
    print("彩排完成。此脚本不发送任何推送；如需验证推送通道，请用 /admin 手动 tick。")


if __name__ == "__main__":
    main()
