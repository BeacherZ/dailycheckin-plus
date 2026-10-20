"""定时校验：按 cron 定期在线校验 cookie，**只有失效才通知**。

几条刻意的取舍：

1. **全绿就一声不响**。定时校验的价值是「坏了才找你」，每天都报「10 个站点全部有效」
   只会让人学会无视通知。
2. **「无法判定」不算失效**。网络抖动、风控、接口改版都会落到 unknown，
   当成失效会天天误报。只有站点接口明确回「未登录 / 已过期」才通知。
3. **同一批失效不会每轮都吵**。首次发现立刻推一条；之后账号集合没变的话，
   最多每 24 小时再提醒一次（`REMIND_AFTER`），cookie 修好（变回有效）就清掉记录，
   下次再坏会重新推。
4. **计时落盘**。`next_run_at` 写在 data/ 里，面板重启不会把 6 小时的间隔重置掉。

调度用 5 字段 cron（和「定时签到」同一套写法），自己算下一次时间 —— 不走系统
crontab：那样要往托管区块里再插一行，还会和签到任务的标记纠缠。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from . import paths, runner, store

DEFAULT_CRON = "0 */6 * * *"  # 每 6 小时整点
# 同一批失效的重复提醒间隔：首次立刻推，之后每 24 小时最多一次
REMIND_AFTER = 24 * 3600
# 后台线程的检查粒度。定时校验是「分钟级」任务，20 秒足够，也不会白烧 CPU。
TICK = 20

PRESETS = [
    {"cron": "0 */6 * * *", "label": "每 6 小时"},
    {"cron": "0 */12 * * *", "label": "每 12 小时"},
    {"cron": "0 9 * * *", "label": "每天 09:00"},
    {"cron": "0 8,20 * * *", "label": "每天 08:00 / 20:00"},
    {"cron": "0 9 * * 1-5", "label": "工作日 09:00"},
]

DEFAULT = {
    "enabled": True,
    "cron": DEFAULT_CRON,
    "notify": True,
    "last_run_at": 0,
    "next_run_at": 0,
    "last_result": None,
    "last_notify_at": 0,
    "notified": {},
    "last_error": "",
}

_LOCK = threading.RLock()
_STATE: dict | None = None
_WAKE = threading.Event()
_THREAD: threading.Thread | None = None
_IN_RUN = False

_FIELDS = (
    ("minute", 0, 59, "分"),
    ("hour", 0, 23, "时"),
    ("day", 1, 31, "日"),
    ("month", 1, 12, "月"),
    ("weekday", 0, 7, "周"),
)


# --------------------------------------------------------------------------- #
# cron 解析 / 下一次时间
# --------------------------------------------------------------------------- #
def _values(text: str, lo: int, hi: int, label: str) -> set[int]:
    out: set[int] = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"「{label}」字段里有空项")
        step = 1
        chunk = part
        if "/" in chunk:
            chunk, step_text = chunk.split("/", 1)
            if not step_text.isdigit() or int(step_text) <= 0:
                raise ValueError(f"「{label}」字段的步长必须是正整数：{part}")
            step = int(step_text)
        if chunk == "*":
            start, end = lo, hi
        elif "-" in chunk:
            first, last = chunk.split("-", 1)
            if not (first.isdigit() and last.isdigit()):
                raise ValueError(f"「{label}」字段含非法字符：{part}")
            start, end = int(first), int(last)
        else:
            if not chunk.isdigit():
                raise ValueError(f"「{label}」字段含非法字符：{part}")
            start = end = int(chunk)
            if step != 1:
                end = hi
        if start < lo or end > hi or start > end:
            raise ValueError(f"「{label}」字段超出范围 {lo}-{hi}：{part}")
        out.update(range(start, end + 1, step))
    return out


def parse_cron(cron: str) -> dict:
    """解析 5 字段 cron。非法就抛 ValueError（调用方转成 400）。"""
    fields = str(cron or "").split()
    if len(fields) != 5:
        raise ValueError("cron 表达式需要 5 个字段：分 时 日 月 周")
    spec: dict = {}
    for text, (name, lo, hi, label) in zip(fields, _FIELDS):
        values = _values(text, lo, hi, label)
        if name == "weekday":
            # cron 里 0 和 7 都是周日，统一成 0
            values = {0 if one == 7 else one for one in values}
        spec[name] = values
    spec["dom_restricted"] = fields[2].strip() != "*"
    spec["dow_restricted"] = fields[4].strip() != "*"
    return spec


def _day_matches(lt: time.struct_time, spec: dict) -> bool:
    dom_ok = lt.tm_mday in spec["day"]
    # Python 的 tm_wday: 周一 = 0；cron 的周字段: 周日 = 0、周一 = 1
    dow_ok = (lt.tm_wday + 1) % 7 in spec["weekday"]
    # cron 的老规矩：日和周都写了具体值时，满足任意一个就算命中
    if spec["dom_restricted"] and spec["dow_restricted"]:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_run(cron: str, after: float | None = None) -> float:
    """下一次满足 cron 的时间戳（分钟粒度，按容器本地时区）。算不出来返回 0。"""
    spec = parse_cron(cron)
    stamp = int(after if after is not None else time.time())
    stamp -= stamp % 60
    stamp += 60
    for _ in range(60 * 24 * 366):
        lt = time.localtime(stamp)
        if (
            lt.tm_min in spec["minute"]
            and lt.tm_hour in spec["hour"]
            and lt.tm_mon in spec["month"]
            and _day_matches(lt, spec)
        ):
            return float(stamp)
        stamp += 60
    return 0.0


# --------------------------------------------------------------------------- #
# 配置持久化
# --------------------------------------------------------------------------- #
def _load() -> dict:
    global _STATE
    if _STATE is not None:
        return _STATE
    data = dict(DEFAULT)
    try:
        with open(paths.VERIFY_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            data.update({k: v for k, v in saved.items() if k in DEFAULT})
    except Exception:  # noqa: BLE001
        pass
    if not isinstance(data.get("notified"), dict):
        data["notified"] = {}
    _STATE = data
    return _STATE


def _save() -> None:
    data = _load()
    paths.ensure_dirs()
    tmp = paths.VERIFY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, paths.VERIFY_PATH)


def parse() -> dict:
    """给前端/接口用的快照。"""
    data = _load()
    # 别每次 /api/state 都重新算一遍：`0 0 31 2 *` 这种「永远不会命中」的表达式
    # 要扫满一年（50 多万次），轮询会把它放大成卡顿。算过一次就记在内存里。
    next_at = data.get("next_run_at") or 0
    if data["enabled"] and (not next_at or (next_at <= time.time() and not _IN_RUN)):
        # 计划时间已经过去（容器停机错过了点）时，页面不该继续显示过去的时刻，
        # 但这里**只改展示值，绝不写回 data** —— 「已经过期」正是 _tick 判断
        # 「该补跑一次」的唯一依据，被 /api/state 顺手擦掉就永远不补跑了
        # （页面开着反而让它不执行，最坏的一种 bug）。
        try:
            next_at = next_run(data["cron"], time.time())
        except ValueError:
            next_at = 0
    return {
        "enabled": bool(data["enabled"]),
        "cron": data["cron"],
        "notify": bool(data["notify"]),
        "presets": PRESETS,
        "last_run_at": data.get("last_run_at") or 0,
        "next_run_at": next_at,
        "last_result": data.get("last_result"),
        "last_notify_at": data.get("last_notify_at") or 0,
        "last_error": data.get("last_error") or "",
        "remind_after_hours": REMIND_AFTER // 3600,
    }


def set_config(enabled: bool, cron: str, notify: bool) -> dict:
    cron = str(cron or "").strip()
    nxt = next_run(cron)  # 只为触发语法校验，非法就抛 ValueError
    if enabled and not nxt:
        # 语法合法但永远命不中（比如 2 月 31 日）：存下来只会让人以为「定时校验开了」
        raise ValueError("这个 cron 永远不会执行（比如 2 月 31 日），换一个")
    with _LOCK:
        data = _load()
        changed_cron = data.get("cron") != cron or bool(data.get("enabled")) != bool(enabled)
        data["enabled"] = bool(enabled)
        data["cron"] = cron
        data["notify"] = bool(notify)
        if changed_cron or not data.get("next_run_at"):
            data["next_run_at"] = nxt if enabled else 0
        _save()
    _WAKE.set()
    return parse()


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
def _notice_configured() -> bool:
    try:
        data = store.load()
    except Exception:  # noqa: BLE001
        return False
    return any(str(data.get(key) or "").strip() for key in store.NOTICE_KEY_NAMES)


def _push(title: str, blocks: list[str]) -> dict:
    """用**子进程**调引擎的推送模块：面板本身零依赖，不能把 requests 拉进来。"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify_push.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["PYTHONPATH"] = paths.HOME + os.pathsep + env.get("PYTHONPATH", "")
    payload = json.dumps({"title": title, "blocks": blocks}, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, script, paths.CONFIG_PATH],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            cwd=paths.HOME,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"调用推送失败：{exc}"}
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return {"ok": False, "message": out[-400:] or f"推送进程退出码 {proc.returncode}"}
    return {"ok": True, "message": out[-400:]}


def _signature(entry: dict) -> str:
    users = sorted(u for u in str(entry.get("user") or "").split("、") if u)
    return "|".join(users)


def _content_blocks(invalid: list[dict], now: float) -> list[str]:
    blocks = []
    for one in invalid:
        who = f"（{one['user']}）" if one.get("user") else ""
        reason = (one.get("message") or "cookie 已失效").strip()
        blocks.append(f"「{one['label']}」{who}\n{reason}")
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    blocks.append(
        f"校验时间: {stamp}\n"
        f"失效站点: {len(invalid)} 个\n"
        "处理方式: 打开面板，点开对应站点重新填一次 cookie"
    )
    return blocks


def notify_invalid(invalid: list[dict], now: float | None = None) -> dict:
    """只对「新失效 / 隔了 24 小时还没修 / 账号集合变了」的站点发通知。"""
    now = now or time.time()
    with _LOCK:
        data = _load()
        notified = data.setdefault("notified", {})
        bad_keys = {one["key"] for one in invalid}
        # 已经恢复的站点把记录清掉：下次再坏要能立刻推
        for key in list(notified):
            if key not in bad_keys:
                notified.pop(key, None)

        if not invalid:
            # 全绿就是一声不响，只把「已经提醒过谁」清干净
            _save()
            return {"sent": False, "message": "没有失效站点", "count": 0}

        targets = []
        for one in invalid:
            rec = notified.get(one["key"]) or {}
            if (
                not rec
                or rec.get("sig") != _signature(one)
                or now - float(rec.get("at") or 0) >= REMIND_AFTER
            ):
                targets.append(one)

        if not targets:
            _save()
            return {"sent": False, "message": "这批失效已经提醒过，等 24 小时后再提醒", "count": 0}
        if not data.get("notify"):
            _save()
            return {"sent": False, "message": "「失效时推送通知」是关着的", "count": 0}
        if not _notice_configured():
            data["last_error"] = "没有配置推送渠道，失效通知发不出去"
            _save()
            return {"sent": False, "message": "没有配置推送渠道", "count": 0}

        title = f"{paths.TITLE} 校验提醒"
        result = _push(title, _content_blocks(targets, now))

    with _LOCK:
        data = _load()
        if result["ok"]:
            for one in targets:
                data.setdefault("notified", {})[one["key"]] = {
                    "at": now,
                    "sig": _signature(one),
                }
            data["last_notify_at"] = now
            data["last_error"] = ""
        else:
            data["last_error"] = result["message"]
        _save()
    return {"sent": bool(result["ok"]), "message": result["message"], "count": len(targets)}


# --------------------------------------------------------------------------- #
# 跑一轮
# --------------------------------------------------------------------------- #
def run_once(notify: bool = True) -> dict:
    """同步跑一遍全部已配置站点，返回汇总。手动/定时共用。"""
    global _IN_RUN
    with _LOCK:
        if _IN_RUN:
            return {"ok": False, "message": "上一轮校验还没结束"}
        _IN_RUN = True
    try:
        now = time.time()
        with _LOCK:
            # 先把「下次」推到新周期再开跑：一轮可能要几分钟，这期间 tick 每 20 秒
            # 都会看到同一个「已经到点」的时间，页面也会显示过去的时刻。
            data = _load()
            try:
                data["next_run_at"] = (next_run(data["cron"], now) if data["enabled"] else 0) or data["next_run_at"]
            except ValueError:
                pass
            _save()
        summary = runner.verify_all(auto=True)
        invalid = summary.get("invalid") or []
        notice = {"sent": False, "message": "没有失效站点"}
        if invalid and notify:
            notice = notify_invalid(invalid, now)
        elif invalid:
            notice = {"sent": False, "message": "「失效时推送通知」是关着的"}
        with _LOCK:
            data = _load()
            data["last_run_at"] = now
            data["last_result"] = {
                "at": now,
                "valid": len(summary.get("valid") or []),
                "invalid": [{"key": one["key"], "label": one["label"]} for one in invalid],
                "unknown": len(summary.get("unknown") or []),
                "skipped": len(summary.get("skipped") or []),
                "notified": bool(notice.get("sent")),
                "notice": notice.get("message", ""),
            }
            # 这里**不再**动 next_run_at：开头已经推好一轮，跑完再按「开始的时刻」算一次
            # 反而可能算出刚跑过的那个点（校验耗时跨过周期时），parse() 也会兜底纠正。
            _save()
        return {"ok": True, "summary": summary, "notice": notice}
    finally:
        with _LOCK:
            _IN_RUN = False


def _tick() -> None:
    data = _load()
    if not data.get("enabled"):
        return
    try:
        if not data.get("next_run_at"):
            with _LOCK:
                data["next_run_at"] = next_run(data["cron"])
                _save()
        nxt = float(data.get("next_run_at") or 0)
        if not nxt:
            # 算不出下一次（cron 永远命不中）：宁可一动不动，也别每 20 秒跑一轮校验
            print(f"[定时校验] cron「{data['cron']}」永远不会执行，请到设置页换一个", flush=True)
            return
        if time.time() < nxt:
            return
        print(f"[定时校验] 开始（{data['cron']}）", flush=True)
        result = run_once()
        if not result.get("ok"):
            print(f"[定时校验] 跳过：{result.get('message')}", flush=True)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            data = _load()
            data["last_error"] = str(exc)[:300]
            try:
                data["next_run_at"] = next_run(data["cron"])
            except ValueError:
                data["next_run_at"] = 0
            _save()
        print(f"[定时校验] 出错：{exc}", flush=True)


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001
            print(f"[定时校验] 循环异常：{exc}", flush=True)
        _WAKE.wait(TICK)
        _WAKE.clear()


def start() -> None:
    """幂等启动后台线程（面板进程内只会有一个）。"""
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_loop, name="autoverify", daemon=True)
        _THREAD.start()
