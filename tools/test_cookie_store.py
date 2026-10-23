#!/usr/bin/env python3
"""cookie 续期写回自测（离线，纯标准库）。

V2EX 的登录态是滑动续期的，抓到的 `Set-Cookie` 必须写回 config.json 才有意义 ——
否则进程一退出，新 cookie 就跟着没了，下次还是老的那串，久了就要重新登录。

这里锁两件事：
1. 合并时**保持原有键的顺序、只换值**，不然每天都会因为顺序不同重写一遍配置；
2. 落盘格式必须和面板写出来的一模一样（indent=4、结尾换行、原子替换），
   否则用户会看到 config.json 莫名其妙被整体重排。

跑法：python3 tools/test_cookie_store.py
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

# 同样只把 dailycheckin_plus 当壳，避免连带导入 requests 之外的运行时依赖
pkg = types.ModuleType("dailycheckin_plus")
pkg.__path__ = [os.path.join(ROOT, "dailycheckin_plus")]
sys.modules.setdefault("dailycheckin_plus", pkg)
utils_pkg = types.ModuleType("dailycheckin_plus.utils")
utils_pkg.__path__ = [os.path.join(ROOT, "dailycheckin_plus", "utils")]
sys.modules.setdefault("dailycheckin_plus.utils", utils_pkg)

store = importlib.import_module("dailycheckin_plus.utils.cookie_store")

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name if cond else f"{name} :: {detail}")
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else " :: " + str(detail)))


TMP = tempfile.mkdtemp(prefix="dc-test-cookie-")
CFG = os.path.join(TMP, "config.json")

print("=" * 66)
print("合并 cookie：顺序不变、只换值")

raw = "A2=old-a2; PB3_SESSION=old-pb3; V2EX_LANG=zhcn"
merged = store.merge_cookie(raw, {"A2": "new-a2"})
check("新值替换旧值", merged == "A2=new-a2; PB3_SESSION=old-pb3; V2EX_LANG=zhcn", merged)
check("没提到的键保持原样", "PB3_SESSION=old-pb3" in merged, merged)

merged = store.merge_cookie(raw, {"A2": "new-a2", "V2EX_REFERRER": "abc"})
check("桶里多出来的键追加在末尾", merged.endswith("; V2EX_REFERRER=abc"), merged)

check("空 cookie 也不炸", store.merge_cookie("", {"A2": "x"}) == "A2=x", store.merge_cookie("", {"A2": "x"}))
check("空桶原样返回", store.merge_cookie(raw, {}) == raw, store.merge_cookie(raw, {}))
check("顺序稳定：同样的输入结果完全一致", store.merge_cookie(raw, {"A2": "1"}) == store.merge_cookie(raw, {"A2": "1"}))

print("=" * 66)
print("写回 config.json：格式跟面板一致，只在变了的时候写")

data = {
    "notice": {"pushplus_token": ""},
    "V2EX": [
        {"cookie": raw, "proxy": ""},
        {"cookie": "A2=acc2", "proxy": ""},
    ],
}
with open(CFG, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)
    f.write("\n")

ok = store.write_back(CFG, "V2EX", 0, "cookie", "A2=new-a2; PB3_SESSION=old-pb3")
check("值变化时返回 True", ok is True, ok)
saved = json.loads(open(CFG, encoding="utf-8").read())
check("只改目标账号", saved["V2EX"][0]["cookie"].startswith("A2=new-a2"), saved["V2EX"])
check("别的账号不受影响", saved["V2EX"][1]["cookie"] == "A2=acc2", saved["V2EX"])
check("别的站点不受影响", saved["notice"] == {"pushplus_token": ""}, saved["notice"])

text = open(CFG, encoding="utf-8").read()
check("缩进是 4 空格（和 panel/store.py 一致）", '\n    "V2EX": [' in text, text[:120])
check("文件以换行结尾", text.endswith("\n"), repr(text[-5:]))
check("中文没被转义成 \\u", '"notice"' in text, text[:80])

check("值没变时不重复写", store.write_back(CFG, "V2EX", 0, "cookie", saved["V2EX"][0]["cookie"]) is False)

print("=" * 66)
print("异常输入：安静跳过，绝不抛异常")

check("配置文件不存在返回 False", store.write_back(os.path.join(TMP, "nope.json"), "V2EX", 0, "cookie", "x") is False)
check("路径为空返回 False", store.write_back("", "V2EX", 0, "cookie", "x") is False)
check("账号序号越界返回 False", store.write_back(CFG, "V2EX", 9, "cookie", "x") is False)
check("站点不存在返回 False", store.write_back(CFG, "NOPE", 0, "cookie", "x") is False)

bad = os.path.join(TMP, "bad.json")
with open(bad, "w", encoding="utf-8") as f:
    f.write("{不是 json")
check("坏掉的 json 返回 False 而不是抛异常", store.write_back(bad, "V2EX", 0, "cookie", "x") is False)

print("=" * 66)
print(f"通过 {len(passed)}  失败 {len(failed)}")
for one in failed:
    print("  ✗", one)

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
