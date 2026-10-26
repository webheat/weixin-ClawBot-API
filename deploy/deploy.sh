#!/usr/bin/env bash
# 部署：先桥后 A（消费者），顺序不能反。
#
# 为什么要分两步：桥（/opt/obsidian-xiaolvshu）和 ClawBot（/opt/weixin-ClawBot-API）
# 是两个仓库，靠「子进程 + stdout 最后一行 JSON」耦合。桥改了契约而 A 没跟上，
# 用户的「发贴图号」就会静默变报错 —— 所以：
#
#   1. 先发桥（保持向后兼容：新桥还得认旧的 JSON 形状）
#   2. 跑契约测试（tests/test_xiaolvshu_bridge_contract.py）—— 桥和 A 的形状还对得上吗
#   3. 再发 A（升 XIAOLVSHU_BRIDGE_REF 到桥的新 sha）
#   4. 回滚：A 可以单独回滚，只要桥保持向后兼容
#
# 用法：
#   ./deploy/deploy.sh                 # 按 .env 里的 pin 部署（推荐）
#   ./deploy/deploy.sh --bridge-only   # 只发桥
#   ./deploy/deploy.sh --app-only      # 只发 A
#   ./deploy/deploy.sh --no-restart    # 不重启服务（只更新代码）
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/weixin-ClawBot-API}"
BRIDGE_DIR="${BRIDGE_DIR:-/opt/obsidian-xiaolvshu}"
BRIDGE_REMOTE="${BRIDGE_REMOTE:-origin}"
SERVICE="${SERVICE:-clawbot-shared.service}"
PY="${PY:-$APP_DIR/venv/bin/python}"

DO_BRIDGE=1
DO_APP=1
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --bridge-only) DO_APP=0 ;;
    --app-only)    DO_BRIDGE=0 ;;
    --no-restart)  RESTART=0 ;;
    -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

# .env 里取 pin（不让 shell 展开其中的 $）
pin_from_env() {
  sed -n 's/^XIAOLVSHU_BRIDGE_REF=//p' "$APP_DIR/.env" | tail -1 | tr -d '"'\''' | tr -d '\r'
}

if [[ $DO_BRIDGE -eq 1 ]]; then
  say "桥：拉取并 checkout 到 pin 的那一版"
  REF="$(pin_from_env)"
  cd "$BRIDGE_DIR"
  # 未提交的改动 = 生产跑的就不是 pin 那版。直接拒绝 —— 部署的必须是可复现的。
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "✗ 桥的工作区有未提交改动 —— 先提交（或 stash）再部署" >&2
    git status --short >&2
    exit 1
  fi
  if [[ -n "$REF" ]]; then
    git fetch --all --tags --quiet || true
    # ⚠ 不要用 `git checkout <sha>` —— 那会进 detached HEAD，后面在桥仓库里
    #   的 commit 会「凭空消失」（没有分支指着它）。用本地分支指着这个 sha。
    SHORT="${REF:0:7}"
    git checkout --quiet -B "deploy/$SHORT" "$REF" || {
      echo "✗ checkout $REF 失败 —— pin 写错了？" >&2
      exit 1
    }
    echo "  桥 → $(git rev-parse --short HEAD)（分支 deploy/$SHORT）"
  else
    echo "  （.env 里没有 XIAOLVSHU_BRIDGE_REF，跳过 checkout）"
  fi

  say "契约测试：桥和 ClawBot 的接口还对得上吗"
  cd "$APP_DIR"
  "$PY" -m pytest tests/test_xiaolvshu_bridge_contract.py -q
fi

if [[ $DO_APP -eq 1 ]]; then
  say "ClawBot：拉取 + 更新"
  cd "$APP_DIR"
  git pull --ff-only
  echo "  A → $(git rev-parse --short HEAD)"
fi

if [[ $RESTART -eq 1 ]]; then
  say "重启服务"
  sudo systemctl restart "$SERVICE"
  sleep 3
  systemctl is-active --quiet "$SERVICE" \
    && echo "  ✓ $SERVICE active" \
    || { echo "✗ $SERVICE 没起来 —— journalctl -u $SERVICE -n 50" >&2; exit 1; }
fi

say "完成"
echo "  桥  $(cd "$BRIDGE_DIR" && git rev-parse --short HEAD 2>/dev/null || echo '-')"
echo "  A   $(cd "$APP_DIR" && git rev-parse --short HEAD)"
echo "  线上日志： journalctl -u $SERVICE -f  |  tail -f $APP_DIR/logs/clawbot_shared.log"
