"""把服务端下发的续期 cookie 写回 config.json。

有些站点的登录态是**滑动续期**的：只要带着 cookie 访问页面，服务端就会在
响应里重新下发一个新的会话 cookie（V2EX 的 `A2` 就是典型）。requests 会把它
收进 session 的 cookie jar，但进程一退出就没了 —— 下次运行用的还是配置里那串
老 cookie，时间久了就得重新登录。

这里负责把新值落盘。写法和面板保持一致（`ensure_ascii=False`、`indent=4`、
结尾换行、先写临时文件再 `os.replace`），所以两边改出来的文件长得一模一样，
不会因为格式差异产生无意义的 diff。
"""

import json
import os


def merge_cookie(raw: str, jar: dict) -> str:
    """把 cookie jar 里的新值合并进原来的 cookie 串。

    保持原有键的顺序、只替换同名键的值，桶里多出来的键追加在末尾。
    直接 `"; ".join(jar.items())` 也行，但那样每次运行都会因为顺序变化
    重写一遍配置文件，白白多出一堆改动。
    """
    parts = []
    seen = set()
    for segment in str(raw or "").split(";"):
        segment = segment.strip()
        if not segment or "=" not in segment:
            continue
        key, _, value = segment.partition("=")
        key = key.strip()
        seen.add(key)
        parts.append(f"{key}={jar.get(key, value)}")
    for key, value in (jar or {}).items():
        if key not in seen:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def write_back(config_path: str, site_key: str, index: int, field: str, value: str) -> bool:
    """把某个站点第 index 个账号的 field 字段改成 value。

    只在值真的变了才落盘；返回是否发生了写入。任何一步不对（文件没了、
    结构不是预期的样子、账号被面板删掉了）都安静返回 False —— 续期是
    锦上添花，不能因此把整个签到搞崩。
    """
    if not config_path or not os.path.exists(config_path):
        return False
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(data, dict):
        return False
    items = data.get(site_key)
    if not isinstance(items, list) or not (0 <= index < len(items)):
        return False
    item = items[index]
    if not isinstance(item, dict):
        return False
    if str(item.get(field) or "") == str(value or ""):
        return False
    item[field] = value
    try:
        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, config_path)
    except Exception:  # noqa: BLE001
        return False
    return True
