#!/usr/bin/env python3
"""V2EX 登录态续期写回自测（离线，不联网、不需要装 requests）。

V2EX 的 A2 是滑动续期的：带着它访问页面，响应里就会下发一个新的 A2。
不落盘的话，这个新值随进程一起消失，下次还是配置里的老 cookie ——
时间一长就得回手机上重新登录，这正是这个功能要解决的问题。

这里用假 session 模拟「服务端换了 A2」，验证：
1. 变了才写回，且只改它自己那一项；
2. 没变化 / 没拿到 A2（未登录）/ 没有配置文件路径时，安静跳过，绝不抛异常。

跑法：python3 tools/test_v2ex_keepalive.py
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _CheckIn:
    name = "Base"


# 只把 dailycheckin_plus 当壳，别跑 __init__.py（那里会 walk 所有站点、顺带导入 requests）
pkg = types.ModuleType("dailycheckin_plus")
pkg.__path__ = [os.path.join(ROOT, "dailycheckin_plus")]
pkg.CheckIn = _CheckIn
sys.modules["dailycheckin_plus"] = pkg

utils_pkg = types.ModuleType("dailycheckin_plus.utils")
utils_pkg.__path__ = [os.path.join(ROOT, "dailycheckin_plus", "utils")]
sys.modules["dailycheckin_plus.utils"] = utils_pkg

v2ex_pkg = types.ModuleType("dailycheckin_plus.v2ex")
v2ex_pkg.__path__ = [os.path.join(ROOT, "dailycheckin_plus", "v2ex")]
sys.modules["dailycheckin_plus.v2ex"] = v2ex_pkg

# v2ex/main.py 顶层只用到 requests 模块名和 urllib3.disable_warnings()，给两个假货即可
if "requests" not in sys.modules:
    fake_requests = types.ModuleType("requests")
    fake_requests.session = lambda: None
    fake_requests.utils = types.SimpleNamespace(add_dict_to_cookiejar=lambda *a, **k: None)
    sys.modules["requests"] = fake_requests
if "urllib3" not in sys.modules:
    fake_urllib3 = types.ModuleType("urllib3")
    fake_urllib3.disable_warnings = lambda *a, **k: None
    sys.modules["urllib3"] = fake_urllib3

cookie_store = importlib.import_module("dailycheckin_plus.utils.cookie_store")
v2ex_main = importlib.import_module("dailycheckin_plus.v2ex.main")
V2ex = v2ex_main.V2ex

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name if cond else f"{name} :: {detail}")
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else " :: " + str(detail)))


TMP = tempfile.mkdtemp(prefix="dc-test-v2ex-")
CFG = os.path.join(TMP, "config.json")
RAW = "A2=old-a2; PB3_SESSION=old-pb3"


class FakeSession:
    """假 session：只提供 cookies.jar 的 get_dict()，够 keep_alive 用。"""

    def __init__(self, jar):
        self.cookies = types.SimpleNamespace(get_dict=lambda: dict(jar))


def reset_config():
    data = {"V2EX": [{"cookie": RAW, "proxy": ""}, {"cookie": "A2=second; PB3_SESSION=s2", "proxy": ""}]}
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")


def current(index=0):
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)["V2EX"][index]["cookie"]


def run(jar, index=0, config_path=CFG):
    item = {"cookie": RAW, "_config_path": config_path, "_site_key": "V2EX", "_account_index": index}
    V2ex(item).keep_alive(FakeSession(jar), RAW)


print("=" * 66)
print("服务端换了 A2：要写回，且只动它自己那一项")

reset_config()
run({"A2": "new-a2", "PB3_SESSION": "old-pb3"})
check("A2 已更新到 config.json", current() == "A2=new-a2; PB3_SESSION=old-pb3", current())
check("另一个账号原封不动", current(1) == "A2=second; PB3_SESSION=s2", current(1))

print("=" * 66)
print("没变化 / 未登录 / 没有配置路径：一律安静跳过")

reset_config()
before = os.path.getmtime(CFG)
run({"A2": "old-a2", "PB3_SESSION": "old-pb3"})
check("A2 没变就不写文件", current() == RAW and os.path.getmtime(CFG) == before, current())

reset_config()
run({"PB3_SESSION": "old-pb3"})
check("拿不到 A2（未登录）不写", current() == RAW, current())

reset_config()
try:
    run({"A2": "new-a2", "PB3_SESSION": "x"}, config_path="")
    check("没有配置文件路径不抛异常", True)
except Exception as exc:  # noqa: BLE001
    check("没有配置文件路径不抛异常", False, exc)
check("没有路径时配置不被改动", current() == RAW, current())

reset_config()
try:
    run({"A2": "new-a2"}, index=9)
    check("账号序号越界不抛异常", True)
except Exception as exc:  # noqa: BLE001
    check("账号序号越界不抛异常", False, exc)

print("=" * 66)
print("续期后能原样再读回来（下次运行用的就是新 cookie）")

reset_config()
run({"A2": "new-a2"})
merged = current()
check("新串里带着新 A2", "A2=new-a2" in merged, merged)
check("没被塞进重复的键", merged.count("A2=") == 1, merged)
check("老键顺序没乱", merged.startswith("A2=new-a2; PB3_SESSION="), merged)

print("=" * 66)
print(f"通过 {len(passed)}  失败 {len(failed)}")
for one in failed:
    print("  ✗", one)

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
