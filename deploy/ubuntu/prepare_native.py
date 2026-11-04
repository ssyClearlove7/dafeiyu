"""无 Docker 部署：先准备 .venv 和 bin/wx，再用当前用户运行本脚本。"""
import json
import os
from pathlib import Path
import secrets


def main():
    if os.name != "posix":
        raise SystemExit("请在 Ubuntu 上运行")
    root = Path(__file__).resolve().parents[2]
    data = root / "ubuntu-native-data"
    for folder in (data, data / "bot", data / "wechat"):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = data / "api-token"
    if not token.exists():
        token.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    token.chmod(0o600)
    (data / "server.toml").write_text(
        'schemaVersion = 1\nlisten = "127.0.0.1:6174"\n'
        'dataDir = %s\ntokenFile = %s\n' % (json.dumps(str(data / "wechat")), json.dumps(str(token))), encoding="utf-8")
    env = {"WX_BACKEND": "native", "WX_CODE_DIR": str(root / "V1.2.4"),
           "WX_RUNTIME_DIR": str(data / "bot"), "WX_STICKER_LIB": str(data / "bot/stickers/library"),
           "AGENT_WECHAT_URL": "http://127.0.0.1:6174", "AGENT_WECHAT_TOKEN_FILE": str(token),
           "AGENT_WECHAT_ACCOUNT": "daughter", "TZ": "Asia/Shanghai", "PYTHONUTF8": "1",
           "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    runtime_env = data / "runtime.env"
    runtime_env.write_text("".join(k + "=" + json.dumps(v) + "\n" for k, v in env.items()), encoding="utf-8")
    runtime_env.chmod(0o600)
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    for name in ("dafy-wechat.service", "dafy-bot.service"):
        text = (Path(__file__).parent / name).read_text(encoding="utf-8")
        (units / name).write_text(text.replace("@ROOT@", str(root)).replace("@DATA@", str(data)), encoding="utf-8")
    print("配置已生成。运行 systemctl --user daemon-reload，然后启用 dafy-bot.service。")


if __name__ == "__main__":
    main()
