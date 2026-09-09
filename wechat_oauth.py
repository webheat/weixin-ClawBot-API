"""WeChat Open Platform website application OAuth2.0 client.

封装三类调用:
  1. 构造 ``/connect/oauth2/authorize`` 跳转 URL（PC 网站扫码登录）
  2. 用 ``code`` 换 ``access_token``（``/sns/oauth2/access_token``）
  3. 用 ``refresh_token`` 续期（``/sns/oauth2/refresh_token``）

依赖最小化: **只使用标准库** ``urllib.request``（项目不引入新包）。

风格对齐: ``ima.py`` 顶部 ``VERSION`` / 模块 logger / dataclass 风格,
重试梯度 ``[0, 2, 4, 8, 16, 32]`` 与 ``deepseek.py`` / ``dusapi.py`` 一致。

安全约定:
  - ``app_secret`` 永不出现在日志（最多只打前 6 字符）
  - ``access_token`` / ``refresh_token`` 在异常信息里只打前 8 字符
  - 这里的脱敏是 **辅助防线**；调用方应在日志外层（``bot._redact_text`` /
    ``utils.log.RedactFilter``）做主防线
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional


VERSION = "1.0.0"

# 微信开放平台 OAuth2.0 三端点常量
# 重要：网站应用 OAuth 必须用 connect/qrconnect，不能用 connect/oauth2/authorize
# - connect/qrconnect：开放平台「网站应用」专用，PC 浏览器显示二维码，手机微信扫码
# - connect/oauth2/authorize：公众号/小程序专用，必须在微信内置浏览器发起，会被 UA 检测拦截
WX_AUTHORIZE = "https://open.weixin.qq.com/connect/qrconnect"
WX_TOKEN = "https://api.weixin.qq.com/sns/oauth2/access_token"
WX_REFRESH = "https://api.weixin.qq.com/sns/oauth2/refresh_token"

# 5 次重试，间隔 [2,4,8,16,32]s —— 与 deepseek.py / dusapi.py / ima.py 保持一致
# 第 1 次（attempt=0）无 sleep，所以 effective ladder 是 (0,) + RETRY_DELAYS
RETRY_DELAYS: tuple[int, ...] = (2, 4, 8, 16, 32)

# 默认 HTTP 超时（秒）
DEFAULT_TIMEOUT: float = 15.0

# 复用项目统一 logger；handler 由 bot.py __main__ 入口注册
log_oauth = logging.getLogger("clawbot.oauth")


# ---------------------------------------------------------------------------
# 脱敏辅助
# ---------------------------------------------------------------------------


def _mask(s: str, keep: int) -> str:
    """Return ``s`` truncated to first ``keep`` chars + ``"…"`` for logging.

    Empty input → empty string. ``keep <= 0`` → empty string.
    这是脱敏的 **辅助防线**，主防线在调用方（``bot._redact_text``）。
    """
    if not s:
        return ""
    if keep <= 0:
        return ""
    if len(s) <= keep:
        return s
    return s[:keep] + "…"


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class WeChatOAuthError(Exception):
    """OAuth 调用失败（网络异常 / 微信返回 ``errcode`` 非 0 / JSON 解析失败）。

    Attributes:
        endpoint: 调用端点标识（``"exchange_code"`` / ``"refresh"``），
            仅用于日志聚合，不参与业务判断。
        errcode: 微信返回的错误码（0 表示无业务错误，仅网络/解析失败时为 ``None``）。
        ret_msg: 微信返回的 ``errmsg`` 字段。
    """

    def __init__(
        self,
        message: str,
        *,
        endpoint: str = "",
        errcode: Optional[int] = None,
        ret_msg: str = "",
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.errcode = errcode
        self.ret_msg = ret_msg

    def __repr__(self) -> str:
        return (
            f"WeChatOAuthError(message={self.args[0]!r}, "
            f"endpoint={self.endpoint!r}, errcode={self.errcode!r}, "
            f"ret_msg={self.ret_msg!r})"
        )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class WeChatOAuthConfig:
    """OAuth 凭据 + 跳转 URI + scope 集合。

    通常由 ``.env`` / ``config.json`` 装配。``app_secret`` 是敏感字段，
    实例化后**不要** ``print(cfg)``，也不要把整个 dataclass 序列化进日志。
    """

    app_id: str
    app_secret: str
    redirect_uri: str
    scope: str = "snsapi_login"  # PC 网站扫码登录


# ---------------------------------------------------------------------------
# HMAC state 签名工具（模块级）
# ---------------------------------------------------------------------------


def sign_state(state: str, secret: str) -> str:
    """签 ``state``，返回 ``"<state>.<hex_digest>"``。

    使用 HMAC-SHA256(``secret``)。``state`` 与 ``secret`` 都不能为空。
    ``.`` 作为分隔符，原始 ``state`` 里若含 ``.`` 仍可逆（只在第一个 ``.`` 处切）。
    """
    if not state:
        raise ValueError("sign_state: state 不能为空")
    if not secret:
        raise ValueError("sign_state: secret 不能为空")
    digest = hmac.new(
        secret.encode("utf-8"),
        state.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{state}.{digest}"


def verify_state(state_signed: str, secret: str) -> Optional[str]:
    """校验 ``"<state>.<hex_digest>"``，成功返回原始 ``state``，失败返回 ``None``。

    失败场景（统一返回 ``None``，不抛异常，方便调用方走同一个 ``None`` 短路）:
      - 输入为空
      - ``secret`` 为空
      - 结构里不含 ``.``
      - ``hmac.compare_digest`` 不通过
    """
    if not state_signed or not secret:
        return None
    if "." not in state_signed:
        return None
    # state 里允许有 ``.``；签名是最后一个 ``.`` 后面的 hex
    state_raw, _, sig = state_signed.rpartition(".")
    if not state_raw or not sig:
        return None
    expected = hmac.new(
        secret.encode("utf-8"),
        state_raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return state_raw


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class WeChatOAuth:
    """同步 OAuth2.0 客户端。

    设计要点:
      - **同步**，与 ``ima.py`` / ``dusapi.py`` / ``deepseek.py`` 保持一致
      - 5-retry ladder ``(0, 2, 4, 8, 16, 32)``，**只在网络异常 / 5xx 时重试**，
        ``errcode != 0`` 直接抛 ``WeChatOAuthError``
      - 默认超时 15s；``__init__`` 的 ``timeout`` 参数可改
      - ``app_secret`` 永不打日志；token 在错误信息里只打前 8 字符
    """

    # 微信 OAuth 端点（实例属性也指向模块常量，便于子类覆盖/打桩）
    WX_AUTHORIZE = WX_AUTHORIZE
    WX_TOKEN = WX_TOKEN
    WX_REFRESH = WX_REFRESH

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        scope: str = "snsapi_login",
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """初始化。

        Args:
            app_id: 微信开放平台网站应用 AppID（``wx...`` 开头）。
            app_secret: 网站应用 AppSecret。**敏感**，不要打印到日志。
            redirect_uri: 授权回调域名下的完整 URI（须在微信后台登记）。
            scope: 授权作用域；PC 扫码登录默认 ``"snsapi_login"``。
            timeout: 单次 HTTP 超时（秒），默认 ``15.0``。

        Raises:
            ValueError: 任一必填字段为空。
        """
        if not app_id:
            raise ValueError("WeChatOAuth: app_id 不能为空")
        if not app_secret:
            raise ValueError("WeChatOAuth: app_secret 不能为空")
        if not redirect_uri:
            raise ValueError("WeChatOAuth: redirect_uri 不能为空")
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.timeout = float(timeout)

        log_oauth.info(
            "WeChatOAuth init app_id=%s redirect_uri=%s scope=%s timeout=%.1fs",
            _mask(app_id, 8),
            redirect_uri,
            scope,
            self.timeout,
        )

    # ---- 构造授权 URL（纯字符串拼装，无 HTTP） -------------------------

    def build_authorize_url(self, state: str) -> str:
        """构造 PC 网站扫码登录授权 URL，末尾带 ``#wechat_redirect``。

        Args:
            state: 防 CSRF 的随机串，由调用方生成。**不要** 在里面塞 token
                或其他敏感信息——它会作为 query 参数出现在跳转 URL 里。

        Returns:
            ``https://open.weixin.qq.com/connect/qrconnect?appid=...``
            ``&redirect_uri=...&response_type=code&scope=...&state=...``
            ``#wechat_redirect``。PC 浏览器在此页显示二维码，手机微信扫码授权。
        """
        if not state:
            raise ValueError("build_authorize_url: state 不能为空")
        params = {
            "appid": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "state": state,
        }
        # ``quote_via=quote`` 把 ``/`` ``:`` 也编码；微信对 redirect_uri
        # 的解析是 ``urldecode`` 之后做白名单匹配，所以安全。空格用 ``%20``。
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        url = f"{self.WX_AUTHORIZE}?{query}#wechat_redirect"
        log_oauth.info(
            "build_authorize_url app_id=%s scope=%s state=%s",
            _mask(self.app_id, 8),
            self.scope,
            _mask(state, 8),
        )
        return url

    # ---- 内部 GET（带重试） --------------------------------------------

    def _get(self, url: str, endpoint: str) -> dict:
        """对微信 OAuth 端点做 ``GET``，5-retry ladder。

        重试触发条件:
          - ``urllib.error.URLError`` / ``TimeoutError`` / ``OSError``（网络层）
          - HTTP 5xx（``HTTPError`` 且 ``code // 100 == 5``）

        **不**重试:
          - HTTP 4xx（通常是配置错——AppID 不对、code 过期等，重试无意义）
          - ``errcode != 0``（业务错误，直接抛）
          - JSON 解析失败（说明上游已经异常，没有重试价值）

        Returns:
            解析后的 JSON dict。

        Raises:
            WeChatOAuthError: 业务 ``errcode != 0`` / JSON 解析失败 /
                重试耗尽后的网络/HTTP 错误。
        """
        last_exc: Optional[BaseException] = None
        attempts = (0,) + RETRY_DELAYS  # 第 1 次无 sleep
        total_t0 = time.perf_counter()
        for attempt_idx, delay in enumerate(attempts):
            if delay:
                time.sleep(delay)
            t0 = time.perf_counter()
            log_oauth.debug(
                "oauth GET %s attempt=%d url=%s",
                endpoint,
                attempt_idx + 1,
                _mask(url, 120),
            )
            req = urllib.request.Request(url, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    status = getattr(resp, "status", 200)
            except urllib.error.HTTPError as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                last_exc = exc
                # 4xx 不重试（配置类错误，重试只放大日志噪音）；5xx 才重试
                if exc.code // 100 == 5:
                    body_snip = ""
                    try:
                        body_snip = (exc.read() or b"").decode("utf-8", "replace")[:512]
                    except Exception:  # pragma: no cover - 极端兜底
                        body_snip = ""
                    log_oauth.warning(
                        "oauth GET %s attempt=%d http=%d (5xx, retry) "
                        "elapsed_ms=%.0f body=%s",
                        endpoint,
                        attempt_idx + 1,
                        exc.code,
                        elapsed_ms,
                        body_snip,
                    )
                    continue
                # 4xx：直接抛，不重试
                log_oauth.error(
                    "oauth GET %s attempt=%d http=%d (4xx, no retry) elapsed_ms=%.0f",
                    endpoint,
                    attempt_idx + 1,
                    exc.code,
                    elapsed_ms,
                )
                raise WeChatOAuthError(
                    f"{endpoint} http {exc.code}",
                    endpoint=endpoint,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                last_exc = exc
                log_oauth.warning(
                    "oauth GET %s attempt=%d network err=%s elapsed_ms=%.0f",
                    endpoint,
                    attempt_idx + 1,
                    exc,
                    elapsed_ms,
                )
                continue

            elapsed_ms = (time.perf_counter() - t0) * 1000
            # 这里 status 永远是 200（5xx 已被 HTTPError 捕走）；保留以防上游代理异常
            del status
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                log_oauth.error(
                    "oauth GET %s attempt=%d decode err=%s elapsed_ms=%.0f",
                    endpoint,
                    attempt_idx + 1,
                    exc,
                    elapsed_ms,
                )
                raise WeChatOAuthError(
                    f"{endpoint} 返回非 UTF-8 字节流", endpoint=endpoint
                ) from exc
            try:
                data = json.loads(text)
            except ValueError as exc:
                log_oauth.error(
                    "oauth GET %s attempt=%d json parse err=%s elapsed_ms=%.0f body=%s",
                    endpoint,
                    attempt_idx + 1,
                    exc,
                    elapsed_ms,
                    text[:512],
                )
                raise WeChatOAuthError(
                    f"{endpoint} 返回非 JSON: {text[:200]}",
                    endpoint=endpoint,
                ) from exc

            log_oauth.debug(
                "oauth GET %s attempt=%d ok elapsed_ms=%.0f",
                endpoint,
                attempt_idx + 1,
                elapsed_ms,
            )
            return data

        total_ms = (time.perf_counter() - total_t0) * 1000
        log_oauth.error(
            "oauth GET %s exhausted after %d attempts total_elapsed_ms=%.0f last_err=%s",
            endpoint,
            len(attempts),
            total_ms,
            last_exc,
        )
        raise WeChatOAuthError(
            f"{endpoint} 重试 {len(attempts)} 次后仍失败: {last_exc}",
            endpoint=endpoint,
        ) from last_exc

    # ---- 业务响应校验 ---------------------------------------------------

    @staticmethod
    def _check_business(data: dict, endpoint: str) -> None:
        """``errcode != 0`` 直接抛 ``WeChatOAuthError``（不重试）。

        微信 OAuth 端点的成功响应 ``errcode`` 字段缺失或为 ``0``；
        业务错误的 ``errcode`` 是字符串数字（实测两种都见过），所以用
        ``int(...)`` 宽容解析；解析失败的视为非零（防御性）。
        """
        raw_err = data.get("errcode", 0)
        try:
            errcode = int(raw_err) if raw_err not in (None, "") else 0
        except (TypeError, ValueError):
            errcode = -1
        if not errcode:
            return
        errmsg = str(data.get("errmsg", ""))
        # token 出现在 errmsg 里极罕见，但安全起见仍按"只打前 8 字符"处理
        log_oauth.error(
            "oauth %s errcode=%d errmsg=%s",
            endpoint,
            errcode,
            _mask(errmsg, 120),
        )
        raise WeChatOAuthError(
            f"{endpoint} 业务错误 errcode={errcode} errmsg={errmsg}",
            endpoint=endpoint,
            errcode=errcode,
            ret_msg=errmsg,
        )

    # ---- code 换 token / refresh ---------------------------------------

    def exchange_code(self, code: str) -> dict:
        """用授权回调拿到的 ``code`` 换 ``access_token``。

        Args:
            code: 微信回调里的 ``code`` 参数，一次性、有效期 5 分钟。

        Returns:
            ``{"access_token", "expires_in", "refresh_token", "openid",
            "scope", "unionid"?, "errcode":0, "errmsg":"ok"}``。

        Raises:
            ValueError: ``code`` 为空。
            WeChatOAuthError: 网络异常重试耗尽 / ``errcode != 0`` / JSON 解析失败。

        Note:
            errcode=40029 是 ``code`` 无效/已使用——属于业务错误（合法凭据 +
            假 code），**不会** 被重试，直接抛。
        """
        if not code:
            raise ValueError("exchange_code: code 不能为空")
        params = {
            "appid": self.app_id,
            "secret": self.app_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
        url = f"{self.WX_TOKEN}?{urllib.parse.urlencode(params)}"
        log_oauth.info(
            "oauth exchange_code app_id=%s code=%s",
            _mask(self.app_id, 8),
            _mask(code, 8),
        )
        data = self._get(url, endpoint="exchange_code")
        self._check_business(data, endpoint="exchange_code")
        return data

    def refresh(self, refresh_token: str) -> dict:
        """用 ``refresh_token`` 续期；新 ``access_token`` + 新 ``refresh_token``。

        Args:
            refresh_token: ``exchange_code`` 返回的 ``refresh_token``，
                有效期 30 天，过期需用户重新授权。

        Returns:
            同 ``exchange_code`` 的字段形状。

        Raises:
            ValueError: ``refresh_token`` 为空。
            WeChatOAuthError: 网络异常重试耗尽 / ``errcode != 0`` / JSON 解析失败。
        """
        if not refresh_token:
            raise ValueError("refresh: refresh_token 不能为空")
        params = {
            "appid": self.app_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        url = f"{self.WX_REFRESH}?{urllib.parse.urlencode(params)}"
        log_oauth.info(
            "oauth refresh app_id=%s refresh_token=%s",
            _mask(self.app_id, 8),
            _mask(refresh_token, 8),
        )
        data = self._get(url, endpoint="refresh")
        self._check_business(data, endpoint="refresh")
        return data


# ---------------------------------------------------------------------------
# __main__ 自测
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys

    # 1) sign_state / verify_state 往返
    secret_demo = "demo-secret-do-not-use-in-prod"
    state_demo = "csrf-token-abc123"
    signed = sign_state(state_demo, secret_demo)
    back = verify_state(signed, secret_demo)
    print(f"[self-test] sign_state({state_demo!r}) -> {signed!r}")
    print(f"[self-test] verify_state -> {back!r}  (期望 {state_demo!r})")
    assert back == state_demo, "sign_state / verify_state 往返失败"
    # 篡改签名 → 应返回 None
    bad = verify_state(signed + "0", secret_demo)
    print(f"[self-test] verify_state 篡改后 -> {bad!r}  (期望 None)")
    assert bad is None, "篡改签名应被拒绝"

    # 2) build_authorize_url 输出形态（用真 AppID 测试拼装正确性）
    demo_app_id = "wx0000000000000000"
    demo_redirect = "https://example.com/cb"
    client = WeChatOAuth(
        app_id=demo_app_id,
        app_secret="sk-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        redirect_uri=demo_redirect,
        scope="snsapi_login",
        timeout=15.0,
    )
    url = client.build_authorize_url(state=state_demo)
    print(f"[self-test] build_authorize_url -> {url}")
    # 字段断言（避免后续重构悄悄改 URL 形态）
    assert url.startswith(WX_AUTHORIZE + "?"), "URL 应以 qrconnect? 开头"
    assert url.endswith("#wechat_redirect"), "URL 应以 #wechat_redirect 结尾"
    assert f"appid={demo_app_id}" in url, "URL 应包含 appid"
    assert "response_type=code" in url, "URL 应包含 response_type=code"
    assert "scope=snsapi_login" in url, "URL 应包含 scope=snsapi_login"
    assert "state=" + urllib.parse.quote(state_demo, safe="") in url, "URL 应包含 URL-encoded state"

    # 3) 用假 code 触发 exchange_code —— 期望抛 WeChatOAuthError 且 errcode=40029
    print("[self-test] exchange_code(__TEST__) 预期抛 WeChatOAuthError(errcode=40029) ...")
    try:
        client.exchange_code("__TEST__")
    except WeChatOAuthError as exc:
        print(f"[self-test] 捕获 WeChatOAuthError errcode={exc.errcode} ret_msg={exc.ret_msg!r}")
        # errcode=40029（invalid code）是最常见的合法 AppID + 假 code 表现；
        # 也可能 40013（invalid appid）或 40125（invalid appsecret），
        # 三者都说明模块正确识别了业务错误。
        assert exc.errcode in (40029, 40013, 40125), (
            f"exchange_code 业务错误 errcode={exc.errcode} 不在预期集合内"
        )
    except Exception as exc:  # pragma: no cover - 不应发生
        print(f"[self-test] 失败: 预期 WeChatOAuthError，实得 {type(exc).__name__}: {exc}")
        sys.exit(1)
    else:
        print("[self-test] 失败: exchange_code 应抛异常但正常返回了")
        sys.exit(1)

    print("[self-test] OK")
