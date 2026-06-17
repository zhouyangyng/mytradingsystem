#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mytradingsystem}"
WEB_DIR="${WEB_DIR:-/var/www/mytradingsystem}"
REPO_URL="${REPO_URL:-https://github.com/zhouyangyng/mytradingsystem.git}"
BRANCH="${BRANCH:-main}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行，或使用 sudo。"
  exit 1
fi

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y git python3 nginx curl util-linux
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git python3 nginx curl util-linux
  else
    echo "暂不支持当前系统包管理器，请手动安装 git/python3/nginx/curl/util-linux。"
    exit 1
  fi
}

install_packages

mkdir -p "$APP_DIR" "$WEB_DIR" /var/log/mytradingsystem

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --all --prune || true
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only || true
else
  rm -rf "$APP_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

install -m 0755 "$APP_DIR/deploy/domestic-server/update_report.sh" /usr/local/bin/mytradingsystem-update
install -m 0644 "$APP_DIR/deploy/domestic-server/nginx.conf" /etc/nginx/conf.d/mytradingsystem.conf
install -m 0644 "$APP_DIR/deploy/domestic-server/mytradingsystem.cron" /etc/cron.d/mytradingsystem

rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

/usr/local/bin/mytradingsystem-update

nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl reload nginx >/dev/null 2>&1 || systemctl restart nginx

echo "部署完成。"
echo "站点目录: $WEB_DIR"
echo "更新日志: /var/log/mytradingsystem/update.log"
echo "访问地址: http://服务器公网IP/"
