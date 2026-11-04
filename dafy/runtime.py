"""让嵌入式 Python 的工作子进程使用与主程序相同的版本和依赖。"""
from pathlib import Path
import sys
from .paths import CODE


def module_command(module, *arguments):
    roots = [str(CODE / "site-packages"), str(CODE)]
    source = "import sys,runpy;sys.path[:0]=" + repr(roots) + ";runpy.run_module(" + repr(module) + ",run_name='__main__',alter_sys=True)"
    return [sys.executable, "-X", "utf8", "-c", source, *map(str, arguments)]
