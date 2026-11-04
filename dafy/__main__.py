"""入口：独立数据目录、本机/局域网控制台及后台工作。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import urllib.request
import webbrowser
from . import __version__
from .paths import Paths


class Instance:
    def __init__(self, root, open_url=None):
        self.path = root.parent / (".dafy-instance-" + hashlib.sha256(str(root).encode()).hexdigest()[:16] + ".lock")
        self.file = None
        self.open_url = open_url

    def __enter__(self):
        self.file = self.path.open("a+b")
        Paths.protect(self.path)
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            if self.open_url:
                webbrowser.open(self.open_url)
            raise SystemExit("此数据目录已有大肥鱼实例运行，请打开现有控制台")
        return self

    def __exit__(self, *args):
        self.file.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dafy")
    parser.add_argument("--data", help="独立用户数据目录")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--open", action="store_true", help="在本机浏览器打开控制台")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--check-data", action="store_true")
    parser.add_argument("--import-legacy")
    args = parser.parse_args(argv)
    from .launch import read_location
    paths = Paths(args.data or read_location())
    if args.check_data:
        if not paths.path("memory/state.sqlite3").is_file():
            raise SystemExit("未找到已有数据库，不能将新建空库作为升级验证")
        from .settings import Settings
        from .store import Store
        Settings(paths).read()
        counts = Store(paths).integrity()
        print(json.dumps({"ok": True, "counts": counts}))
        return
    from .engine import Engine
    from .updates import Updates
    from .web import make_server
    existing_url = "http://127.0.0.1:" + str(args.port) + "/"
    with Instance(paths.root, existing_url if args.open else None):
        from .recovery import preflight
        if preflight(paths):
            install = Path(os.environ["DAFY_INSTALL_DIR"])
            os.execv(sys.executable, [sys.executable, str(install / "launch.py"), *sys.argv[1:]])
        engine = Engine(paths)
        engine.restart_requested = threading.Event()
        engine.restart_data = None
        engine.updates = Updates(engine)
        if args.import_legacy:
            from .migrate import import_legacy
            result = import_legacy(args.import_legacy, engine)
            print(json.dumps(result, ensure_ascii=False))
            return
        server = make_server(engine, args.host, args.port)
        engine.start_threads()
        url = "http://127.0.0.1:" + str(args.port) + "/"
        if args.open:
            webbrowser.open(url)
        print("大肥鱼 " + __version__ + " 控制台：http://127.0.0.1:" + str(args.port) + "/", flush=True)
        print("控制台无需登录，局域网可使用本机IP访问同一端口。", flush=True)
        thread = threading.Thread(target=server.serve_forever, name="dafy-console", daemon=True)
        thread.start()
        def stop(*_):
            engine.stop_event.set()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
        try:
            while not engine.stop_event.wait(0.5) and not engine.restart_requested.is_set():
                pass
        finally:
            engine.shutdown()
            server.shutdown()
            server.server_close()
        if engine.restart_requested.is_set():
            args_list = ["--data", engine.restart_data or str(paths.root), "--port", str(args.port), "--host", args.host]
            install = os.environ.get("DAFY_INSTALL_DIR")
            if install:
                os.execv(sys.executable, [sys.executable, str(Path(install) / "launch.py"), *args_list])
            os.execv(sys.executable, [sys.executable, "-m", "dafy", *args_list])


if __name__ == "__main__":
    main()
