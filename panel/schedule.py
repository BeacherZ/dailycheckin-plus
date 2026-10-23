"""定时任务管理。

面板只维护 crontab 文件里的一段**带标记的区块**，其余行原样保留，
所以用户自己写的其它定时任务不会被覆盖：

    # >>> dailycheckin-plus managed >>>
    0 9 * * * cd /dailycheckin-plus && dailycheckin-plus >> /dailycheckin-plus/logs/dailycheckin-plus.log 2>&1
    # <<< dailycheckin-plus managed <<<

老项目名（dailycheckin-compose）留下的标记照样认，写回时会顺手升级成新标记。
不这么做的话，老部署里那段「没人认领」的签到任务会和新加的一段并存 ——
一天跑两遍、通知收两条。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from . import paths

# 第一项是当前使用的标记，后面的都是历史名字（只读兼容，写回时统一升级）
MANAGED_MARKERS = ("dailycheckin-plus", "dailycheckin-compose")
BEGIN = f"# >>> {MANAGED_MARKERS[0]} managed >>>"
END = f"# <<< {MANAGED_MARKERS[0]} managed <<<"

_MARKS = "|".join(re.escape(name) for name in MANAGED_MARKERS)
# 匹配任意一代托管区块（当前名字或历史名字），第 1 组是区块正文
BLOCK_RE = re.compile(rf"# >>> (?:{_MARKS}) managed >>>(.*?)# <<< (?:{_MARKS}) managed <<<", re.S)

def _log_clean_line() -> str:
    return f"50 23 */2 * * rm -f {paths.LOG_DIR}/*.log"

DEFAULT_CRON = "0 9 * * *"

# 「每天 8 点到 10 点之间随机」这种需求 cron 表达不了：cron 只有固定时刻。
# 做法是把基准点定在窗口起点，再用 sleep 随机延后 —— 8:00 + 随机 0~7199 秒，
# 落在 08:00–09:59 之间，小时和分钟都是随机的。
# 对外仍用一句人能看懂的值 `random 8-10` 表示，写进区块时另起一行注释记住它。
RANDOM_PREFIX = "random"
RANDOM_RE = re.compile(r"^random\s+(\d{1,2})\s*-\s*(\d{1,2})$")
RANDOM_MARK_RE = re.compile(r"^#\s*random\s+(\d{1,2})\s*-\s*(\d{1,2})\s*$")

PRESETS = [
    {"cron": "0 9 * * *", "label": "每天 09:00"},
    {"cron": "30 0 * * *", "label": "每天 00:30"},
    {"cron": "0 8,20 * * *", "label": "每天 08:00 / 20:00"},
    {"cron": "0 */6 * * *", "label": "每 6 小时"},
    {"cron": "0 9 * * 1-5", "label": "工作日 09:00"},
    {"cron": "random 8-10", "label": "每天 08:00–10:00 随机"},
]

CRON_FIELD = re.compile(r"^[\d*/,\-]+$")
# 匹配「真的在调用 dailycheckin-plus 命令」，避免误认 /dailycheckin-plus/logs 这类路径
CALL_RE = re.compile(r"(?:^|[\s;&|])dailycheckin(?:-plus)?(?:\s|$)")


def _default_text() -> str:
    """默认 crontab。

    cron 不会继承容器的环境变量，所以 TZ / PATH 要在文件里显式声明，
    否则任务可能按 UTC 时间跑，或者找不到 dailycheckin-plus 命令。
    """
    return (
        f"TZ={paths.TZ}\n"
        f"PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"DC_HOME={paths.HOME}\n\n"
        "##############默认任务##############\n"
        "# 每 2 天的 23:50 清理一次日志\n"
        f"{_log_clean_line()}\n\n"
        "##############每日签到任务##############\n"
        f"{BEGIN}\n"
        f"{_run_line(DEFAULT_CRON)}\n"
        f"{END}\n"
    )


def _run_line(cron: str) -> str:
    tail = f"cd {paths.HOME} && "
    log = f">> {paths.LOG_DIR}/dailycheckin-plus.log 2>&1"
    match = RANDOM_RE.match(cron.strip())
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        span = max(60, (end - start) * 3600)
        # shuf 属于 coreutils，正常都在；万一没有就退回「立刻跑」，绝不能因为
        # 取不到随机数就整条命令失败 —— 那样签到会彻底不执行，且没有任何提示。
        return (
            f"0 {start} * * * {tail}sleep $(shuf -i 0-{span - 1} -n 1 2>/dev/null || echo 0) "
            f"&& dailycheckin-plus {log}"
        )
    return f"{cron} {tail}dailycheckin-plus {log}"


def _block_body(enabled: bool, cron: str) -> str:
    """托管区块的正文。随机模式多一行注释，读回配置时靠它认出来。"""
    if not enabled:
        return f"{BEGIN}\n# 定时签到已在面板中关闭\n{END}"
    lines = [BEGIN]
    match = RANDOM_RE.match(cron.strip())
    if match:
        lines.append(f"# random {int(match.group(1))}-{int(match.group(2))}")
    lines.append(_run_line(cron))
    lines.append(END)
    return "\n".join(lines)


def read_text() -> str:
    if not os.path.exists(paths.CRON_PATH):
        return _default_text()
    with open(paths.CRON_PATH, encoding="utf-8") as f:
        return f.read()


def write_text(text: str) -> None:
    paths.ensure_dirs()
    os.makedirs(os.path.dirname(paths.CRON_PATH), exist_ok=True)
    tmp = paths.CRON_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        if not text.endswith("\n"):
            text += "\n"
        f.write(text)
    os.replace(tmp, paths.CRON_PATH)


def validate(cron: str) -> str | None:
    match = RANDOM_RE.match(cron.strip())
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if not (0 <= start < end <= 24):
            return "随机时间窗口要写成 random 8-10 这种，起点小于终点，且在 0–24 之间"
        return None
    if cron.strip().lower().startswith(RANDOM_PREFIX):
        return "随机时间窗口要写成 random 8-10 这种"
    fields = cron.split()
    if len(fields) != 5:
        return "cron 表达式需要 5 个字段：分 时 日 月 周"
    for field in fields:
        if not CRON_FIELD.match(field):
            return f"字段 `{field}` 含非法字符"
    return None


def _is_bare_checkin(line: str) -> bool:
    """这一行是不是「没被托管标记包起来、但确实在调用 dailycheckin-plus」的旧任务。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return False
    fields = text.split()
    return len(fields) >= 6 and bool(CALL_RE.search(text)) and "pip" not in text


def parse() -> dict:
    """读出当前的定时设置。

    没有托管标记时不直接说「未启用」—— 老用户从官方镜像迁移过来，
    crontab 里本来就只有一行 `0 9 * * * ... dailycheckin-plus ...`，应该被识别出来。
    """
    text = read_text()
    enabled = False
    cron = DEFAULT_CRON
    block = BLOCK_RE.search(text)
    if block:
        for line in block.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            # 随机模式靠区块里的注释认回来，否则读到的只是基准时刻（比如 0 8 * * *），
            # 面板会以为用户选的是「每天 08:00 整」。
            mark = RANDOM_MARK_RE.match(line)
            if mark:
                cron = f"{RANDOM_PREFIX} {int(mark.group(1))}-{int(mark.group(2))}"
                enabled = True
                continue
            if line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) >= 6 and not RANDOM_RE.match(cron):
                cron = " ".join(fields[:5])
                enabled = True
                break
    else:
        for line in text.splitlines():
            if _is_bare_checkin(line):
                cron = " ".join(line.split()[:5])
                enabled = True
                break
    return {
        "enabled": enabled,
        "cron": cron,
        "presets": PRESETS,
        "crontab_path": paths.CRON_PATH,
        "detected": block is None and enabled,
    }


def set_schedule(enabled: bool, cron: str) -> dict:
    err = validate(cron)
    if err:
        raise ValueError(err)
    text = read_text()
    body = _block_body(enabled, cron)
    # 老标记（dailycheckin-compose）也会被匹配到，替换后自然升级成新标记
    if BLOCK_RE.search(text):
        text = BLOCK_RE.sub(lambda _: body, text, count=1)
    else:
        # 托管区块还不存在，但 crontab 里可能已经有一行裸的签到任务（官方镜像迁过来、
        # 或者以前手写的）。直接再追加一段就是一天跑两遍、通知收两条 ——
        # 这里把它就地注释留档，交给面板统一接管。
        lines = []
        for line in text.splitlines():
            if _is_bare_checkin(line):
                lines.append(f"# 已由 dailycheckin-plus 面板接管（原文留档）：{line.strip()}")
            else:
                lines.append(line)
        text = "\n".join(lines)
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n##############每日签到任务##############\n{body}\n"
    write_text(text)
    applied = apply()
    return {"enabled": enabled, "cron": cron, "applied": applied}


def apply() -> dict:
    """把 crontab 文件装进容器里的 cron 服务。非容器环境（比如本地开发）会返回 skipped。"""
    binary = shutil.which("crontab")
    if not binary:
        return {"ok": False, "skipped": True, "message": "当前环境没有 crontab 命令（本地开发模式属正常）"}
    try:
        proc = subprocess.run([binary, paths.CRON_PATH], capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"执行 crontab 失败：{exc}"}
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return {"ok": False, "message": out.strip() or f"crontab 退出码 {proc.returncode}"}
    return {"ok": True, "message": "定时任务已生效"}


def installed() -> str:
    binary = shutil.which("crontab")
    if not binary:
        return ""
    try:
        proc = subprocess.run([binary, "-l"], capture_output=True, text=True, timeout=20)
        return proc.stdout or proc.stderr or ""
    except Exception:  # noqa: BLE001
        return ""
