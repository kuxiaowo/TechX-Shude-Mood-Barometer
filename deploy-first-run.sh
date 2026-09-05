#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${MOOD_SERVICE_NAME:-techx-shude-mood-barometer.service}"
PORT="${MOOD_PORT:-5000}"
CONDA_ENV_NAME="${MOOD_CONDA_ENV:-techx-shude-mood-barometer}"
PYTHON_VERSION="${MOOD_PYTHON_VERSION:-3.12}"
INSTALL_SYSTEMD=1
START_SERVICE=1

usage() {
  cat <<'EOF'
TechX Shude Mood Barometer 第一次部署脚本

用法：
  ./deploy-first-run.sh [选项]

选项：
  --no-systemd   只初始化数据库，不创建/启动 systemd 用户服务
  --no-start     创建 systemd 用户服务，但不立即启动
  -h, --help     显示帮助

可选环境变量：
  MOOD_SERVICE_NAME     systemd 服务名，默认 techx-shude-mood-barometer.service
  MOOD_CONDA_ENV        Conda 环境名，默认 techx-shude-mood-barometer
  MOOD_PYTHON_VERSION   Python 版本，默认 3.12
  MOOD_PORT             仅用于脚本输出提示；实际监听端口由 .env、外部环境变量或程序默认值决定

示例：
  chmod +x deploy-first-run.sh
  ./deploy-first-run.sh

说明：
  - 数据库使用 SQLite，默认文件位于 ./data/mood_barometer.sqlite3
  - 不需要安装 MySQL/PostgreSQL
  - Python 依赖安装在独立 Conda 环境中
  - 管理员身份来自迁移后的 TechX 本地角色，不会自动创建首个管理员
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-systemd)
      INSTALL_SYSTEMD=0
      ;;
    --no-start)
      START_SERVICE=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

log() {
  printf '[mood-barometer deploy] %s\n' "$*"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "缺少命令: $1" >&2
    echo "请先安装 Miniconda 或 Anaconda，并确保 conda 命令可用。" >&2
    exit 1
  fi
}

need_cmd conda

log "应用目录: $APP_DIR"
cd "$APP_DIR"

if [[ ! -f main.py || ! -f requirements.txt || ! -f .env.example || ! -d templates || ! -d static ]]; then
  echo "项目文件不完整，请在完整的 TechX 项目根目录运行。" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  log "已从 .env.example 创建 .env；启动前必须填写 OIDC 客户端密钥。"
fi
chmod 600 .env

if ! conda run -n "$CONDA_ENV_NAME" python -c 'import sys' >/dev/null 2>&1; then
  log "创建 Conda 环境 $CONDA_ENV_NAME (Python $PYTHON_VERSION)"
  conda create --yes --name "$CONDA_ENV_NAME" "python=$PYTHON_VERSION" pip
fi
PYTHON_BIN="$(conda run -n "$CONDA_ENV_NAME" python -c 'import sys; print(sys.executable)')"
PYTHON_BIN="${PYTHON_BIN//$'\r'/}"
[[ -x "$PYTHON_BIN" ]] || { echo "无法确定 Conda Python 路径。" >&2; exit 1; }

log "安装 Python 依赖"
"$PYTHON_BIN" -m pip install --upgrade -r requirements.txt

log "创建数据目录并初始化 SQLite 数据库。"
mkdir -p "$APP_DIR/data"
chmod 700 "$APP_DIR/data"

"$PYTHON_BIN" - "$INSTALL_SYSTEMD" "$START_SERVICE" <<'PY'
import sys
import main

main.init_db(main.app)
print(f'database ready: {main.database_path(main.app)}')
print('admin bootstrap skipped: TechX roles are preserved locally after OIDC mapping')
if sys.argv[1:] == ['1', '1']:
    secret = main.app.state.config['OIDC_CLIENT_SECRET']
    if not secret or secret.startswith('replace-'):
        raise SystemExit('请先在 .env 中填写真实的 ACCOUNTS_CLIENT_SECRET，再启动服务。')
PY

if [[ "$INSTALL_SYSTEMD" == "1" ]]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "未找到 systemctl，已完成数据库初始化；请用 python3 main.py 手动运行。" >&2
    exit 0
  fi

  SERVICE_DIR="$HOME/.config/systemd/user"
  SERVICE_FILE="$SERVICE_DIR/$SERVICE_NAME"
  mkdir -p "$SERVICE_DIR"

  log "写入 systemd 用户服务: $SERVICE_FILE"
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=TechX Shude Mood Barometer
After=network.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON_BIN $APP_DIR/main.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE_NAME"

  if [[ "$START_SERVICE" == "1" ]]; then
    systemctl --user restart "$SERVICE_NAME"
    log "服务状态: $(systemctl --user is-active "$SERVICE_NAME")"
  else
    log "已创建服务，但按 --no-start 要求未启动。"
  fi

  if command -v loginctl >/dev/null 2>&1; then
    log "提示：如需退出 SSH 后服务继续运行，可执行：loginctl enable-linger $USER"
  fi
else
  log "已按 --no-systemd 要求跳过 systemd 服务创建。"
fi

log "部署完成。本机监听地址通常是: http://127.0.0.1:${PORT}"
log "请通过 Caddy 暴露 HTTPS，不要直接向公网开放应用端口。"
