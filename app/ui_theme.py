"""面板主题三态（黑 / 白 / 跟随系统），零 JS。

实现口径：
- 所有面板样式改为 CSS 变量；html[data-theme='dark'|'light'] 决定变量取值，
  :root（不设 data-theme，即「跟随系统」）按 prefers-color-scheme 各给一套默认值。
- 用户选择存 cookie `cma_theme`（dark|light|auto，默认 auto），只影响展示、不落 DB。
- 切换入口是普通链接 GET /admin/theme?t=...：设置 cookie 后 302 回原页。
  选「黑/白」时 data-theme 直接钉死（不写媒体查询的那套），选「跟随系统」时
  不写 data-theme，交给 prefers-color-scheme。
- 本模块只出 CSS 字符串与 cookie 解析，不依赖 FastAPI，单测可直接 import。
"""
from __future__ import annotations

from typing import Optional

# 合法主题值：dark=黑、light=白、auto=跟随系统（不写 data-theme，吃媒体查询默认）
THEMES = ("auto", "dark", "light")
THEME_COOKIE = "cma_theme"
DEFAULT_THEME = "auto"

# 主题选择行（面板页脚用）：三个链接，当前项高亮。零 JS，纯 <a>。
# tab 参数由调用方传入，保证切主题后回到原来的 Tab。
_THEME_LABELS = {"auto": "跟随系统", "dark": "黑", "light": "白"}


def normalize_theme(v: Optional[str]) -> str:
    """cookie/参数 → 合法主题值。认不出一律回落 auto。"""
    s = str(v or "").strip().lower()
    return s if s in THEMES else DEFAULT_THEME


def theme_attr(theme: str) -> str:
    """主题 → <html> 上的 data-theme 属性值（auto 返回空 = 不写，吃系统默认）。"""
    t = normalize_theme(theme)
    return "" if t == "auto" else t


def theme_switch_html(tab: str, current: str, k: str = "") -> str:
    """页脚的主题切换行。current 已 normalize；k 为面板 token（保持链接可点击）。"""
    from urllib.parse import quote
    cur = normalize_theme(current)
    q = quote(str(k or ""), safe="")
    links = []
    for t in THEMES:
        label = _THEME_LABELS[t]
        cls = "btn" if t == cur else "btn gray"
        links.append(f"<a class='{cls}' href='/admin/theme?t={t}&tab={_esc_attr(tab)}&k={q}'>{label}</a>")
    return ("<div class='foot'><span class='muted'>主题：</span>" + " ".join(links) + "</div>")


def _esc_attr(v: str) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# CSS 变量主题层：拼在每套页面样式之前。
# 变量清单（各页面只许引用这些变量，不许再写死颜色）：
#   --bg 页面底色        --fg 正文字色      --card 卡片底色     --line 边框/分隔线
#   --muted 次要文字     --card2 表头/深一层底色  --link 链接色    --line2 次要线
#   --input 输入框底色   --pos --neg --warn 语义色（黑白两套下都保对比度）
THEME_CSS = """<style>
:root{--bg:#f5f6f8;--fg:#1f2430;--card:#ffffff;--card2:#eef0f4;--line:#d8dce4;
      --line2:#e3e6ec;--muted:#5b6472;--link:#2563eb;--input:#ffffff;
      --h2:#475569;--banner-line:#e2b1b1;--banner-bg:#fdf0f0;--banner-fg:#9f1d1d;
      --banner-muted:#a08585;--btn:#2563eb;--btn-gray:#6b7280;
      --pos:#15803d;--neg:#b91c1c;--warn:#a16207;
      --okb-line:#86efac;--okb-bg:#f0fdf4;--okb-fg:#15803d;
      --wb-line:#fde68a;--wb-bg:#fffbeb;--wb-fg:#a16207}
@media (prefers-color-scheme:dark){
:root{--bg:#0f1115;--fg:#e6e6e6;--card:#171a21;--card2:#14171e;--line:#262a33;
      --line2:#1e222b;--muted:#8b95a5;--link:#60a5fa;--input:#0f1115;
      --h2:#99aadd;--banner-line:#7f1d1d;--banner-bg:#2a1215;--banner-fg:#fca5a5;
      --banner-muted:#cc9999;--btn:#2563eb;--btn-gray:#374151;
      --pos:#4ade80;--neg:#f87171;--warn:#fbbf24;
      --okb-line:#14532d;--okb-bg:#0f2418;--okb-fg:#86efac;
      --wb-line:#78350f;--wb-bg:#2a1d08;--wb-fg:#fcd34d}
}
html[data-theme='light']{--bg:#f5f6f8;--fg:#1f2430;--card:#ffffff;--card2:#eef0f4;
      --line:#d8dce4;--line2:#e3e6ec;--muted:#5b6472;--link:#2563eb;--input:#ffffff;
      --h2:#475569;--banner-line:#e2b1b1;--banner-bg:#fdf0f0;--banner-fg:#9f1d1d;
      --banner-muted:#a08585;--btn:#2563eb;--btn-gray:#6b7280;
      --pos:#15803d;--neg:#b91c1c;--warn:#a16207;
      --okb-line:#86efac;--okb-bg:#f0fdf4;--okb-fg:#15803d;
      --wb-line:#fde68a;--wb-bg:#fffbeb;--wb-fg:#a16207}
html[data-theme='dark']{--bg:#0f1115;--fg:#e6e6e6;--card:#171a21;--card2:#14171e;
      --line:#262a33;--line2:#1e222b;--muted:#8b95a5;--link:#60a5fa;--input:#0f1115;
      --h2:#99aadd;--banner-line:#7f1d1d;--banner-bg:#2a1215;--banner-fg:#fca5a5;
      --banner-muted:#cc9999;--btn:#2563eb;--btn-gray:#374151;
      --pos:#4ade80;--neg:#f87171;--warn:#fbbf24;
      --okb-line:#14532d;--okb-bg:#0f2418;--okb-fg:#86efac;
      --wb-line:#78350f;--wb-bg:#2a1d08;--wb-fg:#fcd34d}
</style>"""

# 「三标的动态」区块（总览页）的样式：只这六个类，颜色一律引用上面那套变量，
# 黑白两套主题下自动跟随（**不写死任何色值**）。由 hl/views.DASH_CSS 拼进页面。
# .st.hot 的琥珀 = 变量 --warn；其余靠 --muted/--fg，与面板其它区块同色系。
SYMBOL_CSS = """
.sym-h{display:flex;align-items:baseline;gap:8px;margin-bottom:2px}
.nm{font-weight:600;color:var(--fg)}
.st{font-size:12px;color:var(--muted)}
.st.hot{color:var(--warn)}
.px{font-weight:600;color:var(--fg);font-variant-numeric:tabular-nums}
.fn{font-size:11px;color:var(--muted)}
"""


def page_shell(theme: str, title: str, body: str, head_extra: str = "",
               lang: str = "zh-CN") -> str:
    """完整自包含 HTML 页面（确认页 / keyform 等独立页共用）。

    theme: 已 normalize 的主题值；auto 时不写 data-theme。
    head_extra: 页面自己的 <style>（引用 THEME_CSS 的变量）。
    """
    attr = theme_attr(theme)
    attr_html = f" data-theme='{attr}'" if attr else ""
    return (f"<!doctype html><html lang='{lang}'{attr_html}><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc_text(title)}</title>{THEME_CSS}{head_extra}</head>"
            f"<body>{body}</body></html>")


def _esc_text(v: str) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
