"""bot_launcher.py — 按 openid 启停独立的 bot 进程。

设计:
- 每个 OAuth 扫码的用户 → 一个 ``clawbot@<short_id>.service`` 实例
- ``short_id = openid[:12]`` 用作 systemd 模板占位符 %i、env 文件名、cookie 值
- 端口从 :18301 起分配(alice 固定占 :18301,新实例从 :18302 起递增)
- ``var/bot_sessions.json`` 持久化 short_id → {openid, port, env_path, started_at}
- 复用 systemd ``clawbot@.service`` 模板(已存在),只负责:
  1. 创建 ``/etc/clawbot/<short_id>.env``(含 CLAWBOT_WEB_PORT + CLAWBOT_WEB_TOKEN)
  2. ``systemctl daemon-reload`` + ``systemctl start clawbot@<short_id>.service``
  3. 等端口监听 = bot 进程就绪
"""
from __future__ import annotations

import json
import logging
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("clawbot.bot_launcher")

# 默认路径与端口分配
DEFAULT_SESSIONS_PATH = Path("var/bot_sessions.json")
DEFAULT_ENV_DIR = Path("/etc/clawbot")
BASE_PORT = 18301          # alice 固定占 :18301
PORT_RANGE_END = 19301     # 上限 :19300(留 1000 个端口)
BOT_PORT_TIMEOUT_S = 30    # 等 bot 进程端口监听的最大秒数
BOT_PORT_POLL_S = 0.5


class BotLauncherError(RuntimeError):
    """bot 启动失败(端口分配/daemon-reload/systemctl start/端口超时)。"""


class BotLauncher:
    def __init__(
        self,
        sessions_path: Path = DEFAULT_SESSIONS_PATH,
        env_dir: Path = DEFAULT_ENV_DIR,
        base_port: int = BASE_PORT,
    ):
        self.sessions_path = Path(sessions_path)
        self.env_dir = Path(env_dir)
        self.base_port = base_port

    # ---------- sessions 持久化 ----------

    def _load_sessions(self) -> dict:
        if not self.sessions_path.exists():
            return {}
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            log.warning("bot_sessions.json 损坏,初始化为空 dict")
            return {}

    def _save_sessions(self, data: dict) -> None:
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.sessions_path.with_suffix(self.sessions_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.sessions_path)

    # ---------- 端口分配 ----------

    def _allocate_port(self, sessions: dict, short_id: str) -> int:
        if short_id in sessions:
            return int(sessions[short_id]["port"])
        used = {int(v["port"]) for v in sessions.values() if "port" in v}
        for port in range(self.base_port + 1, PORT_RANGE_END):
            if port not in used:
                return port
        raise BotLauncherError(f"无可用端口(已用 {len(used)} 个,区间 {self.base_port+1}–{PORT_RANGE_END})")

    # ---------- systemd / 端口检测 ----------

    def _is_active(self, short_id: str) -> bool:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", f"clawbot@{short_id}.service"],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip() == "active"
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("systemctl is-active failed short_id=%s err=%s", short_id, exc)
            return False

    def _wait_port(self, port: int, timeout: float = BOT_PORT_TIMEOUT_S) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(("127.0.0.1", port))
                    return True
            except OSError:
                time.sleep(BOT_PORT_POLL_S)
        return False

    def _create_env_file(self, short_id: str, port: int) -> Path:
        """为 short_id 创建一个最小 bot env 文件(只含端口 + token)。

        其它 env(API key / iLink 配置 / ima 等)从 /etc/clawbot/ima.env 共享继承
        —— bot.py 内部 load_dotenv(override=False) 会按 user → ima 顺序加载。
        """
        self.env_dir.mkdir(parents=True, exist_ok=True)
        env_path = self.env_dir / f"{short_id}.env"
        token = secrets.token_hex(32)
        env_path.write_text(
            f"CLAWBOT_WEB_PORT={port}\n"
            f"CLAWBOT_WEB_TOKEN={token}\n",
            encoding="utf-8",
        )
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
        return env_path

    def _start_systemd(self, short_id: str) -> None:
        # daemon-reload 让新增的 env 文件被 unit 看到
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=10)
        r = subprocess.run(
            ["systemctl", "start", f"clawbot@{short_id}.service"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise BotLauncherError(
                f"systemctl start clawbot@{short_id}.service failed: {r.stderr.strip()}"
            )

    # ---------- public API ----------

    def start_or_get(self, short_id: str, openid: str) -> dict:
        """启动或拿已存在的 bot 实例。返回 session dict(含 port)。"""
        if not short_id or not openid:
            raise BotLauncherError("short_id 和 openid 不能为空")

        sessions = self._load_sessions()

        # 已记录且活跃 → 复用
        if short_id in sessions and self._is_active(short_id):
            port = int(sessions[short_id]["port"])
            if self._wait_port(port, timeout=2):
                log.info("bot reuse short_id=%s port=%d", short_id, port)
                return sessions[short_id]

        # 分配端口 + 写 env + 启 systemd
        port = self._allocate_port(sessions, short_id)
        env_path = self._create_env_file(short_id, port)
        log.info("bot start short_id=%s port=%d env=%s", short_id, port, env_path)

        self._start_systemd(short_id)
        if not self._wait_port(port, timeout=BOT_PORT_TIMEOUT_S):
            raise BotLauncherError(
                f"bot {short_id} 端口 {port} 在 {BOT_PORT_TIMEOUT_S}s 内未监听"
            )

        sessions[short_id] = {
            "openid": openid,
            "port": port,
            "env_path": str(env_path),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save_sessions(sessions)
        log.info("bot ready short_id=%s port=%d", short_id, port)
        return sessions[short_id]

    def list_active(self) -> list[dict]:
        """列所有 active bot 实例(便于管理面板展示)。"""
        sessions = self._load_sessions()
        out = []
        for short_id, info in sessions.items():
            if self._is_active(short_id):
                out.append({"short_id": short_id, **info})
        return out

    def stop(self, short_id: str) -> bool:
        """停止一个 bot 实例(可选,管理用)。"""
        try:
            subprocess.run(
                ["systemctl", "stop", f"clawbot@{short_id}.service"],
                capture_output=True, timeout=10, check=False,
            )
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# 辅助:从 openid 计算 short_id
# ---------------------------------------------------------------------------


def short_id_from_openid(openid: str, length: int = 12) -> str:
    """``oXyz_abCd_12...`` → ``oXyz_abCd_12``

    systemd 实例名 ``clawbot@<name>.service`` 和 env 文件名 ``/etc/clawbot/<name>.env``
    都要求短 ID 太长容易触发 PATH_MAX,12 字符 + 下划线足够区分。
    """
    return openid[:length].replace("-", "_").replace(".", "_")