#!/usr/bin/env bash
set -euo pipefail
program_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "$EUID" == 0 ]]; then
  [[ "$program_root" == /opt/dafeiyu ]] || { echo '卸载目录校验失败，未删除'; exit 1; }
  [[ -f "$program_root/current.json" && -f "$program_root/launch.py" ]] || { echo '不是有效的大肥鱼安装目录'; exit 1; }
  systemctl stop dafeiyu.service
  systemctl disable dafeiyu.service
  rm -f -- /etc/systemd/system/dafeiyu.service
  systemctl daemon-reload
else
  [[ "$program_root" == "$HOME/.local/lib/dafeiyu" ]] || { echo '卸载目录校验失败，未删除'; exit 1; }
  [[ -f "$program_root/current.json" && -f "$program_root/launch.py" ]] || { echo '不是有效的大肥鱼安装目录'; exit 1; }
  systemctl --user stop dafeiyu.service
  systemctl --user disable dafeiyu.service
  rm -f -- "$HOME/.config/systemd/user/dafeiyu.service"
  systemctl --user daemon-reload
fi
[[ ! -L "$program_root" && "$(realpath -- "$program_root")" == "$program_root" ]] || { echo '程序目录已变化，未删除'; exit 1; }
rm -rf --one-file-system -- "$program_root"
echo '程序和服务已卸载，记忆、设置、备份及迁移后的数据目录均保留。'
