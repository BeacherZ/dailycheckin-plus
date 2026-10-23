"""读取 UPSTREAM.json —— 面板「环境信息」里的引擎来源就来自这里。

引擎源码**内置**在本仓库（`dailycheckin_plus/`），自 2026-10-19 起已与上游脱钩：不再有同步
脚本、不重放补丁，引擎就是本仓库的代码。这个文件只保留一件事 —— **fork 自哪儿**：

* `repo` / `commit` / `version`：当初 fork 的上游提交与版本号，方便回溯「这行代码是哪来的」；
* `detached_at`：脱钩时间。之后引擎的每一次改动都是本项目自己的提交。

面板不再显示「同步于 / 本地补丁 N 个」—— 那两个概念随着脱钩已经不存在了，
留着只会让人以为还有个后台任务在跟着上游跑。
"""

from __future__ import annotations

import json
import os

from . import paths

_CACHE: dict = {"mtime": None, "data": {}}


def _candidates() -> list:
    # 容器里 UPSTREAM.json 和 panel/ 一起放在 DC_HOME；
    # 本地开发时 DC_HOME 可能是临时目录，所以再退回项目根目录找一次。
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(paths.HOME, "UPSTREAM.json"), os.path.join(root, "UPSTREAM.json")]


def info() -> dict:
    """返回归一化后的溯源信息；读不到就返回空字典（面板会降级显示）。"""
    for path in _candidates():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if _CACHE["mtime"] == (path, mtime):
            return _CACHE["data"]
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or not raw.get("commit"):
            continue
        data = {
            "repo": raw.get("repo") or "",
            "ref": raw.get("ref") or "",
            "commit": (raw.get("commit") or "")[:10],
            "commit_date": raw.get("commit_date") or "",
            "version": raw.get("version") or "",
            "detached_at": raw.get("detached_at") or "",
            "sites": list(raw.get("engine_sites") or []),
            "url": raw.get("url") or ("https://github.com/%s" % raw["repo"] if raw.get("repo") else ""),
            "source": path,
        }
        _CACHE["mtime"] = (path, mtime)
        _CACHE["data"] = data
        return data
    return {}
