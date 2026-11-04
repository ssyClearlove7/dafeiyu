#!/usr/bin/env bash
set -euo pipefail
install_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$(cat "$install_root/python-path")" "$install_root/launch.py" "$@"
