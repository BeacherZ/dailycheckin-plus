"""引擎版本号。

2026-10-19 起引擎不再跟上游同步，这个号也就跟着项目走了 —— 与
``panel/version.py`` 的 VERSION 保持一致（tools/test_version.py 锁着）。
面板不再单独显示「引擎版本」，改版本只改 ``panel/version.py``。
"""

__version__ = "1.0.0"
