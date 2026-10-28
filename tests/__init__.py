"""测试包初始化。

离线测试默认关掉「启动预热 K 线」（app/main.py::_start_why_preheat，生产默认开）：
`with TestClient(app.main.app)` 会触发 lifespan，若开着预热就会在后台真的去打 HL 行情，
CI 里既慢又可能因为出网受限而刷一堆告警日志。需要验证预热逻辑的用例直接调
`app.why.preheat()` / `app.main._start_why_preheat()`（可注入假 fetch），不依赖这个开关。
用 setdefault：外部显式设 WHY_PREHEAT=1 时仍然按外部值跑。
"""
import os

os.environ.setdefault("WHY_PREHEAT", "0")
