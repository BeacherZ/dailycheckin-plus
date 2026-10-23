#!/usr/bin/env python3
"""站点元数据与面板接线的自测（离线、纯标准库、不联网）。

引擎模块本身的逻辑在 tools/test_nodeseek.py / tools/test_weibo.py 里；
这里只锁「面板这一侧」的东西 —— 这些地方出错的表现通常很隐蔽：

* catalog 里少写一个 ``required`` → 用户 cookie 复制不全也不会被拦，签到时才失败；
* 下拉字段的选项与引擎认的值不一致 → 界面上选「固定 5 鸡腿」，实际发的是随机；
* ``coerce_field`` 没校验枚举 → 手工改 config.json 能写进引擎不认识的method；
* ``SAFE_EQ_SITES`` 漏登记 → 微博的 SUB 带着 ``==``，面板一直报「会被截断」的假警告；
* 脱钩之后又把同步工具/补丁目录加回来 → 两套机制并存，改引擎时不知道该改哪边。

跑法：python3 tools/test_sites.py
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f"  :: {detail}" if not cond and detail else ""))


def section(title):
    print("\n" + "=" * 70)
    print(title)


from panel import catalog  # noqa: E402
from panel import cookies as ck  # noqa: E402
from panel import verify  # noqa: E402

# --------------------------------------------------------------------------- #
section("1) 引擎里能发现这两个站点（不导入引擎，只扫源码）")

scanned = catalog.scan_source()
check("扫到 16 个站点模块", len(scanned) == 16, f"实际 {len(scanned)}: {sorted(scanned)}")
check("扫到 NODESEEK", scanned.get("NODESEEK") == "NodeSeek", str(scanned.get("NODESEEK")))
check("扫到 WEIBO", scanned.get("WEIBO") == "微博超话", str(scanned.get("WEIBO")))

sites = {one["key"]: one for one in catalog.all_sites()}
check("合并后的站点清单包含 NODESEEK", "NODESEEK" in sites)
check("合并后的站点清单包含 WEIBO", "WEIBO" in sites)
check("两个都标成 known（有元数据）",
      sites["NODESEEK"]["known"] is True and sites["WEIBO"]["known"] is True,
      f"{sites['NODESEEK']['known']} / {sites['WEIBO']['known']}")
check("没有站点缺元数据", [k for k, v in sites.items() if not v["known"]] == [],
      str([k for k, v in sites.items() if not v["known"]]))

# --------------------------------------------------------------------------- #
section("2) NodeSeek 元数据：必填 session + 签到方式下拉")

ns = catalog.site_meta("NODESEEK")
check("label 是 NodeSeek", ns["label"] == "NodeSeek", ns["label"])
check("必填字段是 session", ns["required"] == ["session"], str(ns["required"]))
check("有在线校验", ns["verify"] == "nodeseek", str(ns["verify"]))
check("官网链接指向 nodeseek", ns["site"].startswith("https://www.nodeseek.com"), ns["site"])
check("有 LOGO", bool(ns["logo"]), ns["logo"])
# 根目录 /favicon.ico 是 NodeBB 的默认图标，不是论坛标志 —— 用论坛页面里 <img class="logo"> 指的那套 PNG
check("LOGO 用站点官方 192px 图标而不是根 favicon.ico",
      ns["logo"].endswith("/static/image/favicon/android-chrome-192x192.png"), ns["logo"])

method_field = next((f for f in ns["fields"] if f["key"] == "method"), None)
check("有 method 字段", method_field is not None, str(ns["fields"]))
if method_field:
    check("method 是 select 类型", method_field["type"] == "select", method_field["type"])
    values = [o["value"] for o in method_field.get("options") or []]
    check("选项正好是 random / fixed", values == ["random", "fixed"], str(values))
    check("默认是随机", method_field.get("default") == "random", str(method_field.get("default")))
    check("每个选项都有中文标签", all(o.get("label") for o in method_field["options"]),
          str(method_field["options"]))

# --------------------------------------------------------------------------- #
section("3) 微博元数据：必填 SUB + 默认签全部")

wb = catalog.site_meta("WEIBO")
check("label 是微博超话", wb["label"] == "微博超话", wb["label"])
check("必填字段是 SUB", wb["required"] == ["SUB"], str(wb["required"]))
check("有在线校验", wb["verify"] == "weibo", str(wb["verify"]))
check("官网指向 m.weibo.cn", "m.weibo.cn" in wb["site"], wb["site"])

fields = {f["key"]: f for f in wb["fields"]}
check("有 topics 字段", "topics" in fields, str(sorted(fields)))
check("topics 默认 all", fields.get("topics", {}).get("default") == "all", str(fields.get("topics")))
check("有 max_topics 且是 int", fields.get("max_topics", {}).get("type") == "int", str(fields.get("max_topics")))
check("max_topics 默认 20", fields.get("max_topics", {}).get("default") == 20, str(fields.get("max_topics")))
check("有代理字段（机房 IP 会 432）", "proxy" in fields, str(sorted(fields)))

# --------------------------------------------------------------------------- #
section("4) coerce_field：枚举只能写合法值，int 真的转成 int")

check("method=random 通过", catalog.coerce_field("NODESEEK", "method", "random") == "random")
check("method=fixed 通过", catalog.coerce_field("NODESEEK", "method", "fixed") == "fixed")
check("method 空值退回默认（random）",
      catalog.coerce_field("NODESEEK", "method", "") == "random",
      str(catalog.coerce_field("NODESEEK", "method", "")))
check("method 前后空格不影响", catalog.coerce_field("NODESEEK", "method", " fixed ") == "fixed")

for bad in ("5", "Random", "yes", "随机"):
    raised = ""
    try:
        catalog.coerce_field("NODESEEK", "method", bad)
    except ValueError as exc:
        raised = str(exc)
    check(f"method={bad!r} 被拒", bool(raised), "没有抛 ValueError")
check("拒绝时的提示里列出合法值", "random" in raised and "fixed" in raised, raised)

check("max_topics 字符串会转成 int（引擎要拿它做切片）",
      catalog.coerce_field("WEIBO", "max_topics", "15") == 15 and
      isinstance(catalog.coerce_field("WEIBO", "max_topics", "15"), int))
check("max_pages 空值退回默认 5", catalog.coerce_field("WEIBO", "max_pages", "") == 5)
check("topics 原样放行", catalog.coerce_field("WEIBO", "topics", "周杰伦,林俊杰") == "周杰伦,林俊杰")

# --------------------------------------------------------------------------- #
section("5) cookie 告警：SUB / session 里的 = 不该报「会被截断」")

warnings = ck.normalize("SUB=_2A25abc==; SUBP=003", required=["SUB"], site="WEIBO").warnings
check("微博不报「值含 = 会被截断」", not any("截断" in w for w in warnings), str(warnings))

warnings = ck.normalize("session=abc==; _t=1", required=["session"], site="NODESEEK").warnings
check("NodeSeek 也不报", not any("截断" in w for w in warnings), str(warnings))

warnings = ck.normalize("SESSDATA=abc==; bili_jct=x", required=["SESSDATA"], site="BILIBILI").warnings
check("其它站点仍然要报（引擎确实用 split 取值）", any("截断" in w for w in warnings), str(warnings))

sub = ck.normalize("SUB=_2A25abc==; SUBP=003", required=["SUB"], site="WEIBO")
check("规范化后 SUB 的 == 还在", sub.cookie.startswith("SUB=_2A25abc=="), sub.cookie)
check("必填检查能过", sub.ok is True and sub.missing == [], str(sub.missing))
check("缺 SUB 时 missing 里有 SUB",
      ck.normalize("SUBP=003", required=["SUB"], site="WEIBO").missing == ["SUB"])

# --------------------------------------------------------------------------- #
section("6) 在线校验器注册")

check("nodeseek 校验器已注册", "nodeseek" in verify.VERIFIERS)
check("weibo 校验器已注册", "weibo" in verify.VERIFIERS)
check("ACFUN 与其它站点一样用 Cookie", catalog.site_meta("ACFUN").get("credential") == "cookie",
      str(catalog.site_meta("ACFUN").get("credential")))
check("ACFUN 必填 acPasstoken / auth_key",
      list(catalog.site_meta("ACFUN").get("required") or []) == ["acPasstoken", "auth_key"],
      str(catalog.site_meta("ACFUN").get("required")))
check("ACFUN 不再提供账号密码字段",
      not [f for f in catalog.site_meta("ACFUN").get("fields") or [] if f.get("key") in ("phone", "password")],
      str(catalog.site_meta("ACFUN").get("fields")))
check("acfun 校验器已注册", "acfun" in verify.VERIFIERS)
form = verify.verify_account("ACFUN", {"cookie": ""})
check("ACFUN 空 cookie 走通用分支直接报缺", form["ok"] is False and "cookie" in form["message"], str(form))
empty = verify.verify_account("WEIBO", {"cookie": ""})
check("微博没填 cookie 时不发请求、直接报缺", empty["ok"] is False and "cookie" in empty["message"], str(empty))
empty = verify.verify_account("NODESEEK", {"cookie": ""})
check("NodeSeek 没填 cookie 时同样直接报缺", empty["ok"] is False and "cookie" in empty["message"], str(empty))

# --------------------------------------------------------------------------- #
section("7) 前端：select 字段有渲染与样式")

app_js = io.open(os.path.join(ROOT, "panel", "static", "app.js"), encoding="utf-8").read()
css = io.open(os.path.join(ROOT, "panel", "static", "style.css"), encoding="utf-8").read()
check("app.js 会渲染 select 类型字段", 'f.type === "select"' in app_js, "找不到 select 分支")
check("select 用 data-field 让输入监听能收到", "<select data-field=" in app_js, "select 没挂 data-field")
check("选中项按值比对（不靠 label）", "selected" in app_js and "String(value) === String(o.value)" in app_js)
check("style.css 给 select 统一了外观", "select" in css and "input[type=text], input[type=search], input[type=password], select, textarea" in css)

# --------------------------------------------------------------------------- #
section("8) 已与上游脱钩：不留同步工具与补丁目录")

up = json.load(io.open(os.path.join(ROOT, "UPSTREAM.json"), encoding="utf-8"))
check("UPSTREAM.json 记录了脱钩时间", bool(up.get("detached_at")), str(up.get("detached_at")))
check("UPSTREAM.json 不再有 patches 字段", "patches" not in up, str(sorted(up)))
check("engine_sites 含两个新站点",
      "NODESEEK" in up.get("engine_sites", []) and "WEIBO" in up.get("engine_sites", []),
      str(up.get("engine_sites")))
check("engine_sites 与磁盘上的站点数量一致",
      len(up.get("engine_sites", [])) == len(scanned),
      f"{len(up.get('engine_sites', []))} vs {len(scanned)}")

check("patches/ 目录已删掉", not os.path.isdir(os.path.join(ROOT, "patches")))
check("tools/sync_upstream.py 已删掉", not os.path.isfile(os.path.join(ROOT, "tools", "sync_upstream.py")))
check("tools/test_sync_upstream.py 已删掉", not os.path.isfile(os.path.join(ROOT, "tools", "test_sync_upstream.py")))
check("sync-upstream 工作流已删掉",
      not os.path.isfile(os.path.join(ROOT, ".github", "workflows", "sync-upstream.yml")))

# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败项：")
    for one in failed:
        print("  - " + one)
sys.exit(1 if failed else 0)
