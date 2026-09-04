"""二维码 / 登录态网页服务（aiohttp）。

启动一个内嵌 HTTP 服务，让 `bx.mengxa.com/clawbot/` 暴露：
  - 当前二维码 PNG
  - 登录状态机（idle / qr_pending / scanned / logged_in / error）
  - 数字配对码提交端点
  - 一个 0 依赖的 vanilla-JS 页面

nginx 反代路径：
  location ^~ /clawbot/ { proxy_pass http://127.0.0.1:18300/; }

失败模式：
  - 端口被占用 → start() 打 warning 后 return，不阻塞 bot.py 主循环
  - 浏览器关掉 → wait_for_verify_code 走 asyncio.wait_for 超时，向上返回 None，
    触发 wait_login_confirmation 走 {"timeout": True} 路径
"""

import asyncio
import base64
import io
import os
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

import aiohttp
from aiohttp import web

from utils.logging_setup import get_logger

# 诊断日志；不调 setup_logging（由 bot.py __main__ 统一注册）
log_web = get_logger("web")


WEB_ENABLED_ENV = "CLAWBOT_WEB_ENABLED"
WEB_TOKEN_ENV = "CLAWBOT_WEB_TOKEN"
WEB_HOST_ENV = "CLAWBOT_WEB_HOST"
WEB_PORT_ENV = "CLAWBOT_WEB_PORT"

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 18300


@dataclass
class QrFlowState:
    """单一来源：bot.py 写，aiohttp handler 读。"""
    current_qr_png: Optional[bytes] = None
    current_qr_url: Optional[str] = None
    qr_seq: int = 0
    verify_prompt: Optional[str] = None
    verify_retry: bool = False
    verify_value: Optional[str] = None
    verify_event: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "idle"  # idle | qr_pending | scanned | logged_in | error
    last_error: Optional[str] = None
    logged_in_at: Optional[float] = None

    def reset_for_new_qr(self) -> None:
        self.verify_prompt = None
        self.verify_retry = False
        self.verify_value = None
        self.verify_event.clear()
        self.last_error = None

    def set_qr_png(self, png: bytes, source_url: Optional[str]) -> None:
        self.current_qr_png = png
        self.current_qr_url = source_url
        self.qr_seq += 1

    def submit_verify_code(self, code: str) -> None:
        self.verify_value = code.strip()
        self.verify_event.set()

    def consume_verify_code(self) -> Optional[str]:
        """读出当前提交过的 code，并在一次原子操作里清空 value / prompt / event。
        防止 wait_for_verify_code 被多次唤醒时取到旧值。"""
        value = self.verify_value
        self.verify_value = None
        self.verify_prompt = None
        self.verify_retry = False
        self.verify_event.clear()
        return value

    def to_state_dict(self) -> dict:
        return {
            "status": self.status,
            "qr_seq": self.qr_seq,
            "qr_url": self.current_qr_url,
            "has_qr_png": bool(self.current_qr_png),
            "verify_prompt": self.verify_prompt,
            "verify_retry": self.verify_retry,
            "last_error": self.last_error,
            "logged_in_at": self.logged_in_at,
        }


async def to_png_bytes(session: aiohttp.ClientSession, content: str) -> tuple[bytes, Optional[str]]:
    """把 iLink 返回的 qr_content 字符串转成 PNG bytes 和源 URL。

    iLink 2.4.6 通常给两种 payload：
      - data:image/png;base64,...  → 直接 base64 解码
      - https://liteapp.weixin.qq.com/q/<token>?...  → SPA 落地页，fetch 拿不到图，
        所以本地用 `qrcode` 库把 content（就是 URL 字符串）当成 payload 重新渲染
      - data:image/svg+xml;base64,... → cairosvg 栅格化
      - 裸 base64 → 试着当 PNG 解码
    """
    if not content:
        return b"", None
    if content.startswith("data:image/png;base64,"):
        return base64.b64decode(content.split(",", 1)[1]), None
    if content.startswith("data:image/svg+xml;base64,"):
        svg_bytes = base64.b64decode(content.split(",", 1)[1])
        try:
            import cairosvg  # noqa: WPS433 - 延迟导入，缺包走 fallback
            return cairosvg.svg2png(bytestring=svg_bytes), None
        except Exception as exc:
            print(f"[qr_web] cairosvg 渲染 SVG 失败: {exc}；请 pip install cairosvg")
            log_web.warning("qr svg render failed (cairosvg missing/broken) err=%s", exc)
            return b"", None
    # http(s) URL 或裸 base64：本地用 qrcode 库把 content 重新渲染成 PNG。
    # 这样即便上游给的是 liteapp 落地页（fetch 拿不到图），也能给浏览器一个可扫的码。
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), (content if content.startswith("http") else None)
    except Exception as exc:
        print(f"[qr_web] 本地生成二维码 PNG 失败: {exc}")
        log_web.warning("qr png local-render failed err=%s", exc)
        return b"", None


def make_web_on_qrcode(
    state: QrFlowState, session: aiohttp.ClientSession
) -> Callable[[str], Awaitable[None]]:
    """返回 async 回调，签名兼容 bot.py 的 on_qrcode(content: str)。"""
    async def _cb(content: str) -> None:
        try:
            png, url = await to_png_bytes(session, content)
            if not png:
                print("[qr_web] 未能生成 PNG 缓存，网页将显示占位符")
                log_web.warning("qr png generation failed (empty result) source_len=%d", len(content))
                state.last_error = "无法生成二维码 PNG"
                state.status = "error"
                return
            state.reset_for_new_qr()
            state.set_qr_png(png, url)
            state.status = "qr_pending"
            log_web.info("qr png cached seq=%d source=%s bytes=%d",
                         state.qr_seq, "rendered" if url is None else "url",
                         len(png))
        except Exception as exc:
            print(f"[qr_web] 缓存二维码失败: {exc}")
            log_web.error("qr png cache crashed err=%s", exc, exc_info=True)
            state.last_error = str(exc)
            state.status = "error"
    return _cb


async def wait_for_verify_code(
    state: QrFlowState, prompt: str, retry: bool, timeout: float
) -> Optional[str]:
    """替换 bot.py:1121 的 asyncio.to_thread(input, prompt)。
    超时返回 None，调用方据此走 {"timeout": True}。"""
    state.verify_prompt = prompt
    state.verify_retry = retry
    state.verify_event.clear()
    log_web.info("verify_code prompt set retry=%s timeout_s=%.1f", retry, timeout)
    try:
        await asyncio.wait_for(state.verify_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        state.verify_prompt = None
        log_web.warning("verify_code timeout after %.1fs", timeout)
        return None
    code = state.consume_verify_code()
    if code is not None:
        # 严格：永不打 code 内容，只打长度与 retry 标记
        log_web.info("verify_code received len=%d retry=%s", len(code), retry)
    return code


# ---------------- HTTP 路由 ----------------

_INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClawBot 登录</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 480px; margin: 24px auto; padding: 0 16px; color: #222; }
  h1 { font-size: 20px; margin: 0 0 12px; }
  .card { border: 1px solid #ddd; border-radius: 12px; padding: 20px; text-align: center;
          box-shadow: 0 2px 8px rgba(0,0,0,.04); }
  .status { font-size: 14px; color: #555; margin-bottom: 12px; min-height: 18px; }
  .qr { display: inline-block; padding: 12px; background: #fff; border: 1px solid #eee;
        border-radius: 8px; min-width: 220px; min-height: 220px; line-height: 220px; }
  .qr img { display: block; max-width: 280px; height: auto; }
  form { margin-top: 16px; display: flex; gap: 8px; justify-content: center; }
  input[type=number] { flex: 1; font-size: 18px; padding: 8px 12px; border: 1px solid #ccc;
                        border-radius: 6px; min-width: 0; }
  button { font-size: 16px; padding: 8px 16px; border: 0; border-radius: 6px;
           background: #07c160; color: #fff; cursor: pointer; }
  button:disabled { background: #aaa; cursor: not-allowed; }
  .err { color: #c00; font-size: 13px; margin-top: 8px; }
  .ok { color: #07c160; font-size: 16px; font-weight: 600; }
  a { color: #07c160; }
</style></head>
<body>
  <h1>ClawBot 微信登录</h1>
  <div class="card">
    <div class="status" id="status">等待登录...</div>
    <div class="qr" id="qrwrap"><span id="placeholder">—</span></div>
    <form id="vform" style="display:none">
      <input type="number" id="vcode" placeholder="配对码" minlength="4" maxlength="8" inputmode="numeric" pattern="\\d+" required>
      <button type="submit" id="vbtn">提交</button>
    </form>
    <div class="err" id="err"></div>
  </div>
<script>
const TOKEN = "__WEB_TOKEN__";
const POLL_MS = 1000;

async function poll() {
  try {
    // 用相对路径：浏览器在 /clawbot/ 下访问时自动加前缀，不会绕过 nginx location
    const r = await fetch("state", { cache: "no-store" });
    if (!r.ok) throw new Error("state http " + r.status);
    const s = await r.json();
    render(s);
  } catch (e) {
    document.getElementById("err").textContent = "拉取状态失败：" + e.message;
  }
  setTimeout(poll, POLL_MS);
}

function render(s) {
  const status = document.getElementById("status");
  const qrwrap = document.getElementById("qrwrap");
  const vform = document.getElementById("vform");
  const err = document.getElementById("err");
  err.textContent = "";

  if (s.status === "logged_in") {
    status.innerHTML = '<span class="ok">登录成功 ✓</span>';
    qrwrap.innerHTML = '<span style="font-size:48px">✓</span>';
    vform.style.display = "none";
    return;
  }
  if (s.status === "error") {
    status.textContent = "登录失败：" + (s.last_error || "未知错误");
    vform.style.display = "none";
    return;
  }
  if (s.status === "scanned") {
    status.textContent = "已扫码，请在手机上确认...";
  } else if (s.status === "qr_pending") {
    status.textContent = "请用微信扫描下方二维码";
  } else if (s.status === "idle") {
    status.textContent = "等待登录...";
  } else {
    status.textContent = s.status;
  }

  if (s.has_qr_png) {
    const src = "qrcode.png?v=" + s.qr_seq;
    qrwrap.innerHTML = '<img alt="QR" src="' + src + '">';
  } else if (s.status !== "error") {
    qrwrap.innerHTML = '<span id="placeholder">—</span>';
  }

  if (s.verify_prompt) {
    vform.style.display = "flex";
    document.getElementById("vcode").placeholder = s.verify_retry ? "重新输入配对码" : "输入配对码";
  } else if (s.status === "qr_pending") {
    vform.style.display = "none";
  }
}

document.getElementById("vform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const code = document.getElementById("vcode").value.trim();
  const btn = document.getElementById("vbtn");
  const err = document.getElementById("err");
  err.textContent = "";
  if (!/^\\d{4,8}$/.test(code)) {
    err.textContent = "配对码必须是 4-8 位数字";
    return;
  }
  if (!TOKEN) {
    err.textContent = "页面缺少 token（请用 ?token=... 打开）";
    return;
  }
  btn.disabled = true;
  try {
    const r = await fetch("verify_code", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN },
      body: JSON.stringify({ code }),
    });
    if (!r.ok) {
      const t = await r.text();
      err.textContent = "提交失败：" + r.status + " " + t;
      btn.disabled = false;
    } else {
      document.getElementById("vcode").value = "";
    }
  } catch (e) {
    err.textContent = "网络错误：" + e.message;
    btn.disabled = false;
  }
});

poll();
</script>
</body></html>
"""


def _expected_token() -> str:
    return (os.getenv(WEB_TOKEN_ENV) or "").strip()


def _check_bearer(request: web.Request) -> bool:
    expected = _expected_token()
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return secrets_compare(auth[len("Bearer "):].strip(), expected)


def secrets_compare(a: str, b: str) -> bool:
    """常量时间比较，避免 token timing attack。"""
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def handle_healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok", content_type="text/plain")


async def handle_state(request: web.Request) -> web.Response:
    state: QrFlowState = request.app["qr_state"]
    return web.json_response(state.to_state_dict())


async def handle_qr_png(request: web.Request) -> web.Response:
    state: QrFlowState = request.app["qr_state"]
    if not state.current_qr_png:
        return web.Response(status=204)
    return web.Response(body=state.current_qr_png, content_type="image/png",
                        headers={"Cache-Control": "no-store"})


async def handle_verify_code(request: web.Request) -> web.Response:
    if not _check_bearer(request):
        log_web.warning("verify_code unauthorized remote=%s", request.remote)
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        payload = await request.json()
    except Exception:
        log_web.warning("verify_code rejected reason=bad_json remote=%s", request.remote)
        return web.json_response({"error": "invalid json"}, status=400)
    code = str((payload or {}).get("code") or "").strip()
    if not code.isdigit() or not (4 <= len(code) <= 8):
        log_web.warning("verify_code rejected reason=bad_format len=%d remote=%s",
                        len(code), request.remote)
        return web.json_response({"error": "code must be 4-8 digits"}, status=400)
    state: QrFlowState = request.app["qr_state"]
    if not state.verify_prompt:
        # 没有等待配对码的 prompt：忽略（页面可能晚到）
        log_web.debug("verify_code ignored reason=no_prompt remote=%s", request.remote)
        return web.json_response({"ok": True, "ignored": True})
    state.submit_verify_code(code)
    return web.json_response({"ok": True})


async def handle_index(request: web.Request) -> web.Response:
    """?token=... 注入 WEB_TOKEN 后返回页面，避免跨页脚本读到其它来源。"""
    token = request.query.get("token", "")
    page = _INDEX_HTML.replace("__WEB_TOKEN__", token)
    return web.Response(text=page, content_type="text/html")


async def start(state: QrFlowState, host: Optional[str] = None, port: Optional[int] = None) -> None:
    """启动 aiohttp 服务直到被取消。绑端口失败时打 warning 并 return。"""
    bind_host = host or os.getenv(WEB_HOST_ENV, DEFAULT_WEB_HOST)
    bind_port = port if port is not None else int(os.getenv(WEB_PORT_ENV, str(DEFAULT_WEB_PORT)))

    @web.middleware
    async def access_log_mw(request: web.Request, handler):
        """统一记录每个 HTTP 请求：method/path/status/dur_ms。
        状态 < 400 用 DEBUG；>= 400 升 INFO 方便异常时一眼看到。"""
        t0 = time.perf_counter()
        try:
            resp = await handler(request)
            dur_ms = (time.perf_counter() - t0) * 1000
            log_web.log(
                logging.DEBUG if resp.status < 400 else logging.INFO,
                "web req method=%s path=%s status=%d dur_ms=%.1f",
                request.method, request.rel_url.path, resp.status, dur_ms,
            )
            return resp
        except web.HTTPException as exc:
            dur_ms = (time.perf_counter() - t0) * 1000
            log_web.info("web req method=%s path=%s status=%d dur_ms=%.1f",
                         request.method, request.rel_url.path, exc.status, dur_ms)
            raise

    import logging  # 局部导入避免顶层多一个 import（同时拿到 logging.DEBUG/INFO 常量）

    app = web.Application(middlewares=[access_log_mw])
    app["qr_state"] = state
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/state", handle_state)
    app.router.add_get("/qrcode.png", handle_qr_png)
    app.router.add_post("/verify_code", handle_verify_code)
    app.router.add_get("/", handle_index)

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, bind_host, bind_port)
        await site.start()
    except (OSError, RuntimeError) as exc:
        print(f"[qr_web] 无法绑定 {bind_host}:{bind_port}: {exc}；回退到终端模式")
        log_web.warning("web bind failed host=%s port=%d err=%s; fallback to terminal mode",
                        bind_host, bind_port, exc)
        await runner.cleanup()
        return
    print(f"[qr_web] 监听 http://{bind_host}:{bind_port}/  (nginx /clawbot/ 反代)")
    log_web.info("web bound http://%s:%d/ (reverseproxy=/clawbot/)", bind_host, bind_port)
    try:
        # 永久 sleep；外层 cancel 触发 finally 里的 cleanup
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        log_web.debug("web runner cleanup complete")


def web_enabled() -> bool:
    return os.getenv(WEB_ENABLED_ENV, "1") == "1"
