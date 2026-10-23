#!/usr/bin/env python3
"""定时校验自测（离线，纯标准库，不发任何网络请求）。

锁住四件事，这四件错了就是「每天被假警报吵」或者「真失效了却不吭声」：

1. cron 解析：5 字段、步长、范围、周日的 0/7 两种写法；
2. 下一次时间算得对（按本地时区，分钟粒度），算不出来才不会死循环；
3. 通知只在**失效**时发，且同一批失效 24 小时内不重复吵、修好后能重新提醒；
4. 「无法判定」（网络/风控）绝不进通知。

跑法：python3 tools/test_autoverify.py
"""
import importlib
import json
import os
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 临时工作目录，别碰真实数据；环境变量必须在导入 paths 之前设好
TMP = tempfile.mkdtemp(prefix="dc-test-autoverify-")
os.environ["DC_HOME"] = TMP
os.environ["DC_CONFIG_PATH"] = os.path.join(TMP, "config", "config.json")
os.environ["DC_DATA_DIR"] = os.path.join(TMP, "data")
os.makedirs(os.path.join(TMP, "config"), exist_ok=True)

sys.path.insert(0, ROOT)
pkg = types.ModuleType("panel")
pkg.__path__ = [os.path.join(ROOT, "panel")]
sys.modules["panel"] = pkg

paths = importlib.import_module("panel.paths")
autoverify = importlib.import_module("panel.autoverify")
runner = importlib.import_module("panel.runner")

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name if cond else f"{name} :: {detail}")
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else " :: " + str(detail)))


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def stamp(y, m, d, hh=0, mm=0):
    return time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))


# --------------------------------------------------------------------------- #
print("=" * 66)
print("1) cron 解析：字段数 / 范围 / 步长 / 周日")

check("每 6 小时是合法表达式", autoverify.parse_cron("0 */6 * * *")["hour"] == {0, 6, 12, 18})
check("列表和区间都认", autoverify.parse_cron("0 8,20 * * *")["hour"] == {8, 20}
      and autoverify.parse_cron("0 9-11 * * *")["hour"] == {9, 10, 11})
check("周日写成 7 也归一成 0", autoverify.parse_cron("0 9 * * 7")["weekday"] == {0})
check("周日写成 0 也是 0", autoverify.parse_cron("0 9 * * 0")["weekday"] == {0})
check("字段数不对要报错", raises(lambda: autoverify.parse_cron("0 9 * *")))
check("超范围要报错", raises(lambda: autoverify.parse_cron("0 24 * * *")))
check("乱七八糟的字符要报错", raises(lambda: autoverify.parse_cron("0 9 * * 一")))
check("步长 0 要报错", raises(lambda: autoverify.parse_cron("*/0 * * * *")))
check("区间的日和周都写了 → 满足其一即可", autoverify.parse_cron("0 9 1 * 1")["dom_restricted"]
      and autoverify.parse_cron("0 9 1 * 1")["dow_restricted"])

# --------------------------------------------------------------------------- #
print("\n2) 下一次执行时间")

base = stamp(2026, 10, 19, 10, 3)  # 周一 10:03
nxt = autoverify.next_run("0 9 * * *", base)
check("每天 09:00 → 次日 09:00", time.localtime(nxt)[:5] == (2026, 10, 20, 9, 0),
      time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)))
nxt6 = autoverify.next_run("0 */6 * * *", base)
check("每 6 小时 → 当天 12:00", time.localtime(nxt6)[:5] == (2026, 10, 19, 12, 0),
      time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt6)))
nxt_mon = autoverify.next_run("0 9 * * 1", base)
check("周一 09:00 且当前已是周一 10:03 → 下周一", time.localtime(nxt_mon)[:5] == (2026, 10, 26, 9, 0),
      time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt_mon)))
check("永远不可能命中的表达式返回 0（别死循环）", autoverify.next_run("0 0 31 2 *", base) == 0.0)
check("下一次一定在未来", nxt > base)

# --------------------------------------------------------------------------- #
print("\n3) 只在失效时通知，且不重复吵")

TITLES, BLOCKS = [], []
SEND_OK = {"ok": True, "message": "已提交推送"}


def fake_push(title, blocks):
    TITLES.append(title)
    BLOCKS.append(list(blocks))
    return dict(SEND_OK)


autoverify._push = fake_push


def set_state(**kw):
    autoverify._STATE = dict(autoverify.DEFAULT, **kw)
    autoverify._STATE["notified"] = dict(kw.get("notified") or {})


def write_config(**notice):
    data = {"TG_BOT_TOKEN": ""}
    data.update(notice)
    with open(paths.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


BAD = [{"key": "BILIBILI", "label": "哔哩哔哩", "user": "ABC", "message": "登录已失效"}]

write_config(TG_BOT_TOKEN="BOT")
set_state()
now = stamp(2026, 10, 19, 15, 0)
r1 = autoverify.notify_invalid(BAD, now)
check("首次失效 → 发一条", r1["sent"] and len(TITLES) == 1, r1)
check("标题是「<项目名> 校验提醒」", TITLES and TITLES[0].endswith("校验提醒"), TITLES[:1])
check("正文点名哪个站点失效", BLOCKS and "哔哩哔哩" in BLOCKS[0][0], BLOCKS[:1])
check("正文带上失效的账号", BLOCKS and "ABC" in BLOCKS[0][0], BLOCKS[:1])

r2 = autoverify.notify_invalid(BAD, now + 6 * 3600)
check("6 小时后同一批失效 → 不重复发", not r2["sent"] and len(TITLES) == 1, r2)

autoverify._STATE["notified"]["BILIBILI"]["at"] = now - 25 * 3600
r3 = autoverify.notify_invalid(BAD, now)
check("过了 24 小时还没修 → 再提醒一次", r3["sent"] and len(TITLES) == 2, r3)

set_state()
autoverify.notify_invalid(BAD, now)
r4 = autoverify.notify_invalid([], now)
check("全部有效 → 静默", not r4["sent"] and r4["message"] == "没有失效站点", r4)
check("恢复后清掉通知记录", "BILIBILI" not in autoverify._STATE["notified"], autoverify._STATE["notified"])
r5 = autoverify.notify_invalid(BAD, now + 60)
check("再坏一次能立刻推（不用等 24 小时）", r5["sent"], r5)

set_state()
BAD2 = [{"key": "TIEBA", "label": "百度贴吧", "user": "a", "message": "cookie 已过期"}]
r6 = autoverify.notify_invalid(BAD2, now)
r7 = autoverify.notify_invalid(
    [{"key": "TIEBA", "label": "百度贴吧", "user": "a、b", "message": "cookie 已过期"}], now + 60)
check("账号集合变了 → 重新提醒", r7["sent"] and len(TITLES) >= 1 and r6["sent"], (r6, r7))

set_state(notify=False)
r8 = autoverify.notify_invalid(BAD, now)
check("「失效时推送通知」关掉 → 不发", not r8["sent"], r8)

set_state()
write_config()  # 所有渠道留空
r9 = autoverify.notify_invalid(BAD, now)
check("没配推送渠道 → 不发，并记下原因",
      not r9["sent"] and "渠道" in (autoverify._STATE.get("last_error") or ""), r9)

# --------------------------------------------------------------------------- #
print("\n4) 一轮校验的判定：unknown 不算失效")

set_state()
write_config(TG_BOT_TOKEN="BOT")
TITLES.clear()
seen = {}


def fake_verify_all(auto=False):
    seen["auto"] = auto
    return {
        "valid": [{"key": "V2EX", "label": "V2EX", "user": "x", "message": "ok"}],
        "invalid": [],
        "unknown": [{"key": "BILIBILI", "label": "哔哩哔哩", "user": None, "message": "网络不通"}],
        "skipped": [{"key": "ACFUN", "label": "AcFun", "reason": "无在线接口"}],
    }


runner.verify_all = fake_verify_all
res = autoverify.run_once()
check("没失效站点 → 一条都不发", res["ok"] and not res["notice"]["sent"] and not TITLES, res)
check("unknown 只记进结果，不进通知", autoverify._STATE["last_result"]["unknown"] == 1)
check("运行记录标成定时触发", seen.get("auto") is True, seen)
check("next_run_at 落盘了", autoverify._STATE.get("next_run_at", 0) > time.time())

# 定时配置的持久化
autoverify.set_config(True, "0 */12 * * *", True)
saved = json.load(open(paths.VERIFY_PATH, encoding="utf-8"))
check("设置写进 data/panel_verify.json", saved["cron"] == "0 */12 * * *" and saved["enabled"] is True, saved)
check("非法 cron 拒绝保存", raises(lambda: autoverify.set_config(True, "0 99 * * *", True)))
check("永远命不中的 cron 也拒绝（别让人以为开了）",
      raises(lambda: autoverify.set_config(True, "0 0 31 2 *", True)))
check("关掉后下次时间归零", autoverify.set_config(False, "0 */12 * * *", True)["next_run_at"] == 0)

# --------------------------------------------------------------------------- #
print("\n5) 错过时间点（容器停机）：显示下一轮，但不能把「该补跑」的信号擦掉")

past = time.time() - 6 * 3600
set_state(enabled=True, cron="0 3 * * *", next_run_at=past, last_run_at=0, notify=False)
autoverify._save()
snap = autoverify.parse()
disk = json.load(open(paths.VERIFY_PATH, encoding="utf-8"))
check("过期时页面不再显示过去的时刻", snap["next_run_at"] > time.time(), snap["next_run_at"])
check("parse 只改展示、不改落盘（否则页面一刷新就不补跑了）",
      abs(disk["next_run_at"] - past) < 1, disk["next_run_at"])

calls = []


def tick_verify_all(**kw):
    calls.append(1)
    return {"valid": [], "invalid": [], "unknown": [], "skipped": []}


runner.verify_all = tick_verify_all
autoverify._tick()
check("tick 发现过期 → 补跑一次", len(calls) == 1, calls)
check("补跑后写上了「上次」时间", autoverify._STATE.get("last_run_at", 0) > 0,
      autoverify._STATE.get("last_run_at"))
check("补跑后「下次」推到未来（跑的过程中也不会又显示过去）",
      autoverify._STATE.get("next_run_at", 0) > time.time(),
      autoverify._STATE.get("next_run_at"))

print("=" * 66)
print(f"通过 {len(passed)}  失败 {len(failed)}")
for one in failed:
    print("  ✗", one)
sys.exit(1 if failed else 0)
