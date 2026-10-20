#!/usr/bin/env python3
"""打印项目版本号（从 ``panel/version.py`` 里读，唯一来源）。

给 CI 用：构建镜像时把版本号烤进 OCI 标签 ``cn.dailycheckin-plus.version``，
面板「关于本项目」再把它读出来 —— 这样"远端是什么版本"这个信息来自**镜像本身**，
而不是另外去问一遍 GitHub，也就不会出现"仓库改了版本号但没出镜像"的错位。

刻意不 import panel：这里只需要一个正则，不想为了打一行字把整个包拉起来。

跑法：python3 tools/version.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_version() -> str:
    with open(os.path.join(ROOT, "panel", "version.py"), encoding="utf-8") as f:
        src = f.read()
    match = re.search(r"""^VERSION\s*=\s*["']([^"']+)["']""", src, re.M)
    return match.group(1).strip() if match else ""


def main() -> int:
    version = read_version()
    if not version:
        print("panel/version.py 里没有 VERSION", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
