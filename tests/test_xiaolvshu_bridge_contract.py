"""消费者驱动的契约测试：ClawBot（消费者）↔ xiaolvshu 桥（提供者）。

现在两个项目在**两个仓库**里（``/opt/weixin-ClawBot-API`` 和
``/opt/obsidian-xiaolvshu``），靠一个「子进程 + stdout 最后一行 JSON」的
接口耦合。这份测试把那个接口的形状钉住：

- 改了桥的命令行参数 → 这里红；
- 改了桥的返回 JSON 的 key → 这里红；
- 桥整个不见了 / 起不来 → 这里红。

这样「改了 B，用户的『发贴图号』静默变报错」就不会发生了 —— 桥那边自己的
CI 只能测它自己，**只有消费者这边能测出契约是否还成立**。

没有桥就跳过（这台机器上桥是可选组件；但生产上必须有）。

用法::

    XIAOLVSHU_BRIDGE=/opt/obsidian-xiaolvshu/scripts/xiaolvshu_bridge.py \
        ./venv/bin/pytest tests/test_xiaolvshu_bridge_contract.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

DEFAULT_BRIDGE = "/opt/obsidian-xiaolvshu/scripts/xiaolvshu_bridge.py"

# utils/xiaolvshu.py 读到的返回字段（少一个就会走「❌ 发布失败」的兜底）
REQUIRED_TOP_KEYS = {
    "ok",          # 成功与否
    "code",        # 错误码：need_select / duplicate / not_found / no_creds ...
    "error",       # 错误说明
    "results",     # search：候选笔记
    "candidates",  # need_select：让用户 /选 N
    "entries",     # status：发过的台账
    "entry",       # duplicate：那一篇的台账条目
    "kind",        # newspic / article
    "media_id",    # 草稿箱里的 media_id
    "cards",       # 出了几张图
    "title",       # 标题
    "vault",       # 该用户的 vault 路径
    "keyword",     # 没找到时回显的关键词
    "hint",        # 缺凭据时给管理员的操作提示
    "missing",     # 缺哪些凭据
}

# utils/xiaolvshu.py 里 format_result 认识的错误码 —— 分成「桥产出」和
# 「消费者产出」两半。timeout 是消费者自己的（子进程超时），桥不产它。
BRIDGE_ERROR_CODES = {
    "need_select", "duplicate", "not_found", "no_creds",
    "cross_vault", "no_pending", "bad_index",
}
CONSUMER_ERROR_CODES = {"timeout"}

# 契约是**按命令**的：每个命令必须带哪些 key。
# 消费者用 res.get() 读，缺 key 不一定炸；但缺了它就拿不到该给用户看的东西。
REQUIRED_BY_COMMAND = {
    "info": {"ok", "action", "uid", "slug", "vault", "home"},
    "search": {"ok", "results"},
    "status": {"ok", "entries"},
}
# 发布成功 / 各类错误码下，消费者要展示的那些字段
REQUIRED_BY_CODE = {
    "need_select": {"candidates"},
    "duplicate": {"entry"},
    "not_found": {"keyword", "vault"},
    "no_creds": {"hint"},
}

# 消费者会调的子命令
USED_SUBCOMMANDS = ("search", "publish", "select", "status")


def _bridge_path() -> Path:
    raw = os.environ.get("XIAOLVSHU_BRIDGE") or DEFAULT_BRIDGE
    return Path(raw)


def _run_bridge(args: list[str], tmp_path: Path) -> tuple[int, str, str]:
    """跑桥的一个子命令，用临时 STORE/CREDS_DIR（不碰生产目录）。"""
    env = dict(os.environ)
    env.update({
        "XIAOLVSHU_STORE": str(tmp_path / "store"),
        "XIAOLVSHU_CREDS_DIR": str(tmp_path / "creds"),
    })
    proc = subprocess.run(
        [os.environ.get("XIAOLVSHU_PYTHON", "/usr/bin/python3"),
         str(_bridge_path()), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_last_json(stdout: str) -> dict:
    """消费者怎么解析桥的输出 —— 最后一行以 { 开头的行。"""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"桥的 stdout 里没有 JSON 行：{stdout[-400:]!r}")


@pytest.fixture(scope="module")
def bridge() -> Path:
    path = _bridge_path()
    if not path.exists():
        pytest.skip(f"桥不在 {path}（可选组件；生产上必须有）")
    return path


def test_bridge_help_lists_the_commands_the_consumer_uses(bridge: Path, tmp_path: Path):
    """消费者会调的子命令必须在桥的 CLI 里。"""
    code, out, err = _run_bridge(["--help"], tmp_path)
    assert code == 0, err
    for name in USED_SUBCOMMANDS:
        assert name in out, f"桥少了 {name} 子命令（消费者会调它）"


def test_bridge_info_returns_the_expected_shape(bridge: Path, tmp_path: Path):
    """info 的返回必须带它该带的 key。"""
    code, out, err = _run_bridge(["--user", "contract@test", "info"], tmp_path)
    assert code == 0, f"桥失败：{err[-400:]}"
    data = _parse_last_json(out)
    assert data["ok"] is True
    for key in REQUIRED_BY_COMMAND["info"]:
        assert key in data, f"info 的返回少了 {key!r}"
    assert data["uid"] == "contract@test"
    assert data["slug"]
    assert data["vault"]


def test_bridge_search_returns_the_expected_shape(bridge: Path, tmp_path: Path):
    """search 的返回：ok + results（空也算数）。"""
    code, out, err = _run_bridge(
        ["--user", "contract@test", "search", "--keyword", "不存在的关键词xyz"],
        tmp_path,
    )
    assert code == 0, f"桥失败：{err[-400:]}"
    data = _parse_last_json(out)
    assert data["ok"] is True
    assert isinstance(data.get("results"), list)
    for row in data["results"]:
        assert "title" in row, "search 的每条候选都要有 title"


def test_bridge_status_returns_the_expected_shape(bridge: Path, tmp_path: Path):
    """status 的返回：ok + entries（列表）。"""
    code, out, err = _run_bridge(["--user", "contract@test", "status"], tmp_path)
    assert code == 0, f"桥失败：{err[-400:]}"
    data = _parse_last_json(out)
    assert data["ok"] is True
    assert isinstance(data.get("entries"), list)


def test_bridge_can_still_emit_every_error_code_the_consumer_handles(
    bridge: Path,
):
    """消费者认识的那几个错误码，桥还得能产出。

    消费者 ``format_result`` 按 code 分支做不同的中文提示；桥要是改了码
    （或删了某个分支），用户的微信里就会变成干巴巴的「❌ 发布失败」。
    """
    source = bridge.read_text(encoding="utf-8")
    for code in BRIDGE_ERROR_CODES:
        assert f'"{code}"' in source, (
            f"桥不再产出 {code!r} —— 消费者对它的处理会退化成通用失败提示"
        )


def test_consumer_source_and_bridge_agree_on_error_codes():
    """两边都还认得这些码（防止一边改了另一边没跟）。"""
    consumer = Path(__file__).resolve().parent.parent / "utils" / "xiaolvshu.py"
    text = consumer.read_text(encoding="utf-8")
    for code in BRIDGE_ERROR_CODES | CONSUMER_ERROR_CODES:
        assert f'"{code}"' in text, f"消费者丢了错误码 {code!r} 的处理"


def test_bridge_emits_the_fields_each_error_code_needs(bridge: Path):
    """每个错误码分支要用的字段，桥在产出该码时也得带上。

    比如 ``no_creds`` 少了 ``hint``，用户看到的就只是一句「还没配公众号
    凭据」而没有「让管理员执行 xxx」的下一步。
    """
    source = bridge.read_text(encoding="utf-8")
    for code, keys in REQUIRED_BY_CODE.items():
        assert f'"{code}"' in source, f"桥不再产出 {code!r}"
        for key in keys:
            # 字段可能以 dict 键（"candidates"）或 BridgeError 的关键字参数
            # （candidates=）出现 —— 两种都算「桥还产得出它」。
            produced = (f'"{key}"' in source) or (f"{key}=" in source)
            assert produced, (
                f"{code!r} 分支需要的字段 {key!r} 桥不再产出了"
            )


def test_bridge_does_not_leak_into_production_paths(bridge: Path, tmp_path: Path):
    """契约测试自己不能写生产目录 —— STORE/CREDS_DIR 必须被临时目录覆盖。"""
    prod_store = Path("/opt/clawbot-xiaolvshu")
    prod_creds = Path("/etc/clawbot/xiaolvshu")
    before_store = sorted(p.name for p in prod_store.iterdir()) if prod_store.is_dir() else []
    before_creds = sorted(p.name for p in prod_creds.iterdir()) if prod_creds.is_dir() else []

    _run_bridge(["--user", "contract@test", "info"], tmp_path)

    after_store = sorted(p.name for p in prod_store.iterdir()) if prod_store.is_dir() else []
    after_creds = sorted(p.name for p in prod_creds.iterdir()) if prod_creds.is_dir() else []
    assert before_store == after_store, "契约测试碰了生产 STORE"
    assert before_creds == after_creds, "契约测试碰了生产凭据目录"


def test_version_drift_is_logged(monkeypatch, tmp_path, caplog):
    """pin 跟生产上的桥对不上 → 记一条版本漂移告警。

    跨仓库的另一个风险：桥被改了/被切了分支，ClawBot 这边却不知道。
    ``XIAOLVSHU_BRIDGE_REF`` 就是那个「期望值」，对不上要说一声。
    """
    import importlib

    import utils.xiaolvshu as mod

    bridge = _bridge_path()
    if not bridge.exists():
        pytest.skip("桥不在，装不了版本检查")
    repo = bridge.resolve().parent.parent
    if not (repo / ".git").exists():
        pytest.skip("桥不是 git 部署的，没法比版本")

    monkeypatch.setenv("XIAOLVSHU_BRIDGE_REF", "0" * 40)
    reloaded = importlib.reload(mod)
    reloaded._version_checked = False

    with caplog.at_level("WARNING", logger="clawbot.message"):
        reloaded._check_bridge_version()

    assert any("bridge version drift" in r.message for r in caplog.records), (
        "pin 对不上时要告警 —— 否则改了桥没人知道"
    )
    monkeypatch.undo()
    importlib.reload(mod)
