#!/usr/bin/env bash
set -euo pipefail
umask 077
bundle="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
[[ "$(uname -m)" == x86_64 ]] || { echo '仅支持 x86_64'; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu && ( "$VERSION_ID" == 24.04 || "$VERSION_ID" == 26.04 ) ]] || { echo '仅支持 Ubuntu 24.04/26.04'; exit 1; }
if [[ "$EUID" == 0 ]]; then
  install_root=/opt/dafeiyu
  data_root=/var/lib/dafeiyu
  id dafy >/dev/null 2>&1 || useradd --system --home-dir "$data_root" --shell /usr/sbin/nologin dafy
else
  install_root="$HOME/.local/lib/dafeiyu"
  data_root="${XDG_DATA_HOME:-$HOME/.local/share}/dafeiyu"
  systemctl --user show-environment >/dev/null || { echo '当前会话无法注册用户后台服务，请使用 sudo bash install.sh 安装系统服务'; exit 1; }
fi
if [[ -f "$install_root/current.json" ]]; then
  echo '已安装；数据已保留。请从现有控制台执行签名升级，避免覆盖运行中的程序。'
  exit 0
fi
mkdir -p "$install_root" "$data_root"
echo '1/4 校验程序文件'
(cd "$bundle" && sha256sum --strict -c SHA256SUMS)
cp -a "$bundle/versions" "$bundle/launch.py" "$bundle/run.sh" "$bundle/uninstall.sh" "$bundle/current.json" "$bundle/uv.tar.gz" "$install_root/"
mkdir -p "$install_root/bootstrap"
tar -xzf "$install_root/uv.tar.gz" -C "$install_root/bootstrap" --strip-components=1
chmod 700 "$install_root/bootstrap/uv"
export UV_PYTHON_INSTALL_DIR="$install_root/runtime"
export UV_PYTHON_BIN_DIR="$install_root/python-bin"
echo '2/4 准备独立 Python 3.12 运行环境'
"$install_root/bootstrap/uv" python install 3.12
"$install_root/bootstrap/uv" python find --managed-python 3.12 > "$install_root/python-path"
chmod 755 "$install_root/run.sh"
find "$install_root/versions" -type f -name wx -exec chmod 755 {} +
echo '3/4 注册后台服务'
if [[ "$EUID" == 0 ]]; then
  chown -R dafy:dafy "$install_root"
  chown dafy:dafy "$data_root"
  chmod 700 "$data_root"
  unit=/etc/systemd/system/dafeiyu.service
  cat > "$unit" <<EOF
[Unit]
Description=Dafeiyu companion
After=network-online.target
Wants=network-online.target
[Service]
User=dafy
Group=dafy
WorkingDirectory=$install_root
ExecStart=$install_root/run.sh --data $data_root
Restart=on-failure
RestartSec=5
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now dafeiyu.service
  echo '控制台无需登录，本机或局域网可直接访问。'
else
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/dafeiyu.service" <<EOF
[Unit]
Description=Dafeiyu companion
After=network-online.target
[Service]
WorkingDirectory=$install_root
ExecStart="$install_root/run.sh" --data "$data_root"
Restart=on-failure
RestartSec=5
KillMode=control-group
UMask=0077
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now dafeiyu.service
  echo '控制台无需登录，本机或局域网可直接访问。'
fi
echo '4/4 安装完成。控制台：http://本机IP:8790/（无需登录）'
