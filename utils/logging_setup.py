"""统一日志初始化：终端 + 按天滚动文件，保留 7 天。

入口 ``setup_logging()`` 在 ``bot.py`` ``__main__`` 最早位置调用一次；
``qr_web.py`` / ``ima.py`` 不调用，仅通过 ``get_logger(name)`` 获取 logger。

输出策略：
  - 终端：按用户级别过滤（默认 INFO），格式 ``HH:MM:SS LEVEL [name] msg``
  - 文件：总写 DEBUG，格式 ``YYYY-MM-DD HH:MM:SS.mmm LEVEL [name] msg``
    → 默认级别下看不到 DEBUG，但 ``CLAWBOT_LOG_LEVEL=DEBUG`` 重启即生效

脱敏：
  - 主防线在调用方：所有新加 ``logger.info(...)`` 必须自行用
    ``bot._redact_text`` / ``_redact_path`` / 只打前 8 字符
  - 防御层：``setup_logging(redactor=...)`` 接受可选回调，挂在所有
    handler 的 ``RedactFilter`` 上兜底，避免误打 token / verify_code

⚠️ rotate 时间按系统本地时区（``utc=False``）；改系统时区会改变
切日时机。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Callable, Optional


LOG_ROOT = "clawbot"

DEFAULT_LOG_DIR = Path("logs")
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "clawbot.log"
DEFAULT_BACKUPS = 7

# 终端格式：短时间戳，刷屏不刺眼
_FMT_CONSOLE = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATEFMT_CONSOLE = "%H:%M:%S"

# 文件格式：精确到毫秒 + 完整时间，便于和 aiohttp trace / iLink 时间戳对齐
_FMT_FILE = (
    "%(asctime)s.%(msecs)03d %(levelname)-5s [%(name)s] %(message)s"
)
_DATEFMT_FILE = "%Y-%m-%d %H:%M:%S"


_INITIALIZED = False


class RedactFilter(logging.Filter):
    """对 ``record.getMessage()`` 跑回调脱敏的兜底 filter。

    主防线在调用方；这里是最后一道：即使未来有人写了
    ``logger.info(f"got token {tok}")``，filter 也会兜住。
    """

    def __init__(self, redactor: Optional[Callable[[str], str]] = None) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if self._redactor is None:
            return True
        try:
            msg = record.getMessage()
            record.msg = self._redactor(msg)
            record.args = ()
        except Exception:
            # 脱敏失败绝不能让日志失败；保持原样继续走
            pass
        return True


def get_logger(name: str) -> logging.Logger:
    """便捷函数：``get_logger("qr")`` → ``logging.getLogger("clawbot.qr")``。"""
    return logging.getLogger(f"{LOG_ROOT}.{name}")


def _build_console_handler(level: str) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FMT_CONSOLE, datefmt=_DATEFMT_CONSOLE))
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    return handler


def _build_file_handler(log_file: Path, backups: int) -> Optional[logging.Handler]:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=backups,
            encoding="utf-8",
            utc=False,
            delay=False,
        )
        handler.setFormatter(logging.Formatter(_FMT_FILE, datefmt=_DATEFMT_FILE))
        # 文件总写 DEBUG；终端按用户级别过滤，避免生产刷屏
        handler.setLevel(logging.DEBUG)
        return handler
    except OSError as exc:
        # 权限 / 磁盘满 → 仅终端，stderr 提示
        print(
            f"[logging_setup] 无法创建 {log_file}: {exc}；仅启用终端日志",
            file=sys.stderr,
        )
        return None


def setup_logging(
    level: Optional[str] = None,
    *,
    log_dir: Optional[Path] = None,
    backups: Optional[int] = None,
    redactor: Optional[Callable[[str], str]] = None,
) -> None:
    """初始化进程内日志。幂等：二次调用直接 return。

    参数：
      level: 终端级别（DEBUG/INFO/WARNING/ERROR）。默认读
             ``CLAWBOT_LOG_LEVEL`` 环境变量，再退到 ``INFO``。
      log_dir / backups: 覆盖 ``CLAWBOT_LOG_DIR`` / ``CLAWBOT_LOG_BACKUPS``。
      redactor: 可选回调，对每条日志消息兜底脱敏。bot.py 注入
             ``bot._redact_text``。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    lvl = (level or os.getenv("CLAWBOT_LOG_LEVEL", "INFO")).upper()
    if lvl not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        lvl = "INFO"

    log_dir = log_dir or Path(os.getenv("CLAWBOT_LOG_DIR", str(DEFAULT_LOG_DIR)))
    backups = backups if backups is not None else int(
        os.getenv("CLAWBOT_LOG_BACKUPS", str(DEFAULT_BACKUPS))
    )
    log_file = log_dir / DEFAULT_LOG_FILE.name

    console = _build_console_handler(lvl)
    console.addFilter(RedactFilter(redactor))

    file_h = _build_file_handler(log_file, backups)
    if file_h is not None:
        file_h.addFilter(RedactFilter(redactor))

    root = logging.getLogger(LOG_ROOT)
    # 清掉可能的旧 handler（pytest / reimport 场景）
    root.handlers.clear()
    root.addHandler(console)
    if file_h is not None:
        root.addHandler(file_h)
    root.setLevel(logging.DEBUG)  # 子 logger 各自决定是否 emit
    root.propagate = False
