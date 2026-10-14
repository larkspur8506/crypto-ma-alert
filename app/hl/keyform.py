"""一次性私钥录入页（给「不方便用 SSH」的场景兜底，例如手机）。

为什么要这个：私钥不该贴在聊天里，也不该让我（agent）经手；但手机在蜂窝网络下
连不上 oracle1:22 是常事。于是让用户在**手机浏览器**里粘贴提交 —— 走 HTTPS 直达服务器。

安全设计（逐条对应实现与测试）：
1. **独立的一次性令牌**（不是 ADMIN_TOKEN）：`secrets.token_urlsafe(32)`，库里**只存 sha256**，
   默认 15 分钟过期，**提交成功即作废**（防重复提交/重放）。
2. **只写一个专用 env 文件**（compose 的 `env_file:` 指向它），容器只 bind 这**一个文件**：
   即使应用被攻破，也只能注入环境变量，拿不到改 compose 的能力（改 compose = 提权到宿主）。
3. **不落日志、不回显**：请求体只用于校验与写文件；响应只给脱敏摘要（前 6 位…后 4 位 + 长度）。
   私钥**永不进数据库**、不进 hl_events。
4. **拒绝覆盖**：文件里已存在未注释的 HL_PRIVATE_KEY 时直接拒绝（要换先手工删）。
5. 格式校验 `0x` + 64 位 hex；不合法直接拒绝且**不动任何文件**；写入前备份原文件。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from html import escape as _html_escape
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)

TOKEN_TTL_MIN = 15
CFG_HASH = "hl_key_token_hash"
CFG_EXP = "hl_key_token_exp"
# compose 里 `env_file: - /opt/data/hl-secrets.env` + bind `…:/host/hl-secrets.env`
DEFAULT_ENV_PATH = os.environ.get("HL_KEY_ENV_PATH", "/host/hl-secrets.env")
KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
KEY_LINE = re.compile(r"^[ \t]*(#?)[ \t]*HL_PRIVATE_KEY[ \t]*=.*$", re.M)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _mask(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}（长度 {len(key)}）"


# ---------------------------------------------------------------- 令牌

def issue_token(db, ttl_minutes: int = TOKEN_TTL_MIN) -> Tuple[str, float]:
    """签发一次性令牌，返回 (明文令牌, 过期时间戳)。库里只存哈希。"""
    token = secrets.token_urlsafe(32)
    exp = time.time() + ttl_minutes * 60
    db.set_config(CFG_HASH, _hash(token))
    db.set_config(CFG_EXP, exp)
    return token, exp


def check_token(db, token: Optional[str]) -> Tuple[bool, str]:
    """校验令牌（不消费）。返回 (是否有效, 失败原因)。"""
    if not token or not str(token).strip():
        return False, "缺少令牌"
    stored = db.get_config_str(CFG_HASH)
    try:
        exp = float(db.get_config_str(CFG_EXP) or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if not stored:
        return False, "没有待用的录入令牌（需要重新签发）"
    if time.time() > exp:
        return False, "令牌已过期（默认 15 分钟）"
    if not hmac.compare_digest(stored, _hash(str(token).strip())):
        return False, "令牌无效"
    return True, ""


def consume_token(db) -> None:
    db.set_config(CFG_HASH, "")
    db.set_config(CFG_EXP, 0)


def token_state(db) -> dict:
    """给页面显示用：是否还有有效令牌、还有多久过期。"""
    stored = db.get_config_str(CFG_HASH)
    try:
        exp = float(db.get_config_str(CFG_EXP) or 0)
    except (TypeError, ValueError):
        exp = 0.0
    left = max(0, int(exp - time.time()))
    return {"issued": bool(stored), "expires_in_s": left if stored else 0}


# ---------------------------------------------------------------- 文件读写

def has_key(path: str = DEFAULT_ENV_PATH) -> bool:
    """文件里是否已有未注释的 HL_PRIVATE_KEY=…"""
    try:
        with open(path) as f:
            txt = f.read()
    except OSError:
        return False
    for m in KEY_LINE.finditer(txt):
        if not txt[m.start():m.end()].lstrip().startswith("#") and txt[m.start():m.end()].split("=", 1)[-1].strip():
            return True
    return False


def write_key(key: str, path: str = DEFAULT_ENV_PATH) -> Tuple[bool, str]:
    """把私钥写进专用 env 文件。返回 (成功?, 脱敏摘要/错误)。绝不回显私钥。"""
    key = (key or "").strip().strip('"').strip("'")
    if not key:
        return False, "没有收到私钥，未改动任何文件"
    if not KEY_RE.match(key):
        return False, f"格式不对：应为 0x + 64 位 hex（实际长度 {len(key)}），未改动任何文件"
    if "\n" in key or "\r" in key:
        return False, "私钥里含换行，拒绝写入"
    if has_key(path):
        return False, "文件里已经存在 HL_PRIVATE_KEY —— 如需更换请先手工删除原有行"
    try:
        with open(path) as f:
            txt = f.read()
    except OSError as e:
        return False, f"读不到 {path}：{e}"
    if not KEY_LINE.search(txt):
        return False, f"{path} 里找不到 HL_PRIVATE_KEY 占位行"
    try:
        shutil.copy2(path, path + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    except OSError as e:
        logger.warning("[hl-keyform] 备份失败（继续写）：%s", e)
    out = KEY_LINE.sub("HL_PRIVATE_KEY=" + key, txt, count=1)
    try:
        with open(path, "w") as f:
            f.write(out)
        os.chmod(path, 0o600)
    except OSError as e:
        return False, f"写入失败：{e}"
    logger.warning("[hl-keyform] 私钥已写入 %s（%s）", path, _mask(key))
    return True, _mask(key)


# ---------------------------------------------------------------- HTTP

@dataclass
class KeyFormDeps:
    db: Any
    env_path: str = DEFAULT_ENV_PATH

    @classmethod
    def from_env(cls, db) -> "KeyFormDeps":
        return cls(db=db, env_path=DEFAULT_ENV_PATH)


_STYLE = """
*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#0f1115;color:#e6e6e6;line-height:1.6;font-size:15px;padding:18px}
.wrap{max-width:560px;margin:0 auto}.card{background:#171a21;border:1px solid #262a33;border-radius:10px;
padding:16px;margin-bottom:14px}label{display:block;color:#8b95a5;font-size:12.5px;margin:10px 0 4px}
input[type=password]{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #3a4150;
background:#0f1115;color:#e6e6e6;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
button{margin-top:14px;background:#2563eb;color:#fff;border:0;border-radius:8px;padding:11px 18px;font-size:14px}
h1{font-size:18px;margin:0 0 6px}.muted{color:#8b95a5;font-size:12.5px}
.ok{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}a{color:#60a5fa}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{_STYLE}</style></head><body><div class='wrap'>"
            f"{body}</div></body></html>")
    return HTMLResponse(html, status_code=status)


def render_form(deps: KeyFormDeps, token: str = "", message: str = "", ok: bool = True) -> HTMLResponse:
    st = token_state(deps.db)
    already = has_key(deps.env_path)
    msg = f"<div class='card'><span class='{'ok' if ok else 'bad'}'>{message}</span></div>" if message else ""
    if already:
        body = (msg + "<div class='card'><h1>已存在私钥</h1>"
                "<div class='muted'>文件里已有 HL_PRIVATE_KEY，本页拒绝覆盖。<br>"
                "如需更换：先在服务器上删掉那一行，再重新签发令牌。</div></div>")
    elif not st["issued"]:
        body = (msg + "<div class='card'><h1>没有可用的录入令牌</h1>"
                "<div class='muted'>令牌是一次性的、默认 15 分钟有效。请让 Hermes 重新签发一个。</div></div>")
    else:
        mins = st["expires_in_s"] // 60
        body = (msg + "<div class='card'><h1>录入 Hyperliquid 代理钱包私钥</h1>"
                f"<div class='muted'>令牌有效：约 {mins} 分钟 · 提交一次即作废 · 私钥只写入服务器上的 env 文件，"
                "不进数据库、不进日志</div>"
                "<form method='post' autocomplete='off'>"
                f"<input type='hidden' name='t' value='{_html_escape(str(token), quote=True)}'>"
                "<label>代理钱包（API wallet）私钥</label>"
                "<input type='password' name='key' autocomplete='off' spellcheck='false' "
                "placeholder='0x…（64 位 hex）' required>"
                "<button type='submit'>提交</button></form></div>"
                "<div class='muted'>提交后还需重启容器才会生效（Hermes 会做）。</div>")
    return _page("录入私钥", body)


def build_router(deps: KeyFormDeps) -> APIRouter:
    r = APIRouter()

    @r.get("/admin/hl/key", response_class=HTMLResponse)
    def key_form(t: str = Query("")):
        ok, why = check_token(deps.db, t)
        if not ok:
            return _page("录入私钥", f"<div class='card'><span class='bad'>{why}</span>"
                                    "<div class='muted'>链接是一次性的、默认 15 分钟有效。请让 Hermes 重新签发。</div></div>",
                         status=403)
        return render_form(deps, token=t.strip())

    @r.post("/admin/hl/key", response_class=HTMLResponse)
    def key_submit(request: Request, key: str = Form(""), t: str = Form("")):
        ok, why = check_token(deps.db, t)
        if not ok:
            return _page("录入私钥", f"<div class='card'><span class='bad'>{why}</span></div>", status=403)
        # 注意：不打印 key、不写日志、不进 DB
        good, info = write_key(key, deps.env_path)
        if good:
            consume_token(deps.db)
            # 成功页不走 render_form：此时 has_key() 已为 True，会把「已存在/拒绝覆盖」
            # 的矛盾文案拼在成功提示下面。
            return _page("录入私钥",
                         f"<div class='card'><span class='ok'>✅ 已写入（{info}）。</span>"
                         "<div class='muted'>令牌已作废；请让 Hermes 重启容器并核对后切换模式。</div></div>")
        return render_form(deps, token=t.strip(), message=f"❌ {info}", ok=False)

    return r
