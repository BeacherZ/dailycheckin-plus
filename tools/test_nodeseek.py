#!/usr/bin/env python3
"""NodeSeek 站点模块自测（离线，不联网）。

锁住三件容易在后续改动里被悄悄改坏的事：

1. **两种签到方式只差 query 参数**：``random=true`` / ``random=false``；
2. **「今天已经签过」是 HTTP 500 + JSON**，必须按 body 的 ``success`` 判定 ——
   用 ``raise_for_status()`` 或看状态码都会让每天第二次运行报红；
3. **cookie 只能认 session、且值里带 ``=`` 不能被截断**。

引擎会 ``import requests``，这里塞一个假的进 ``sys.modules``，直接按路径加载模块本身，
所以 CI 里不装 requests 也能跑：python3 tools/test_nodeseek.py
"""
import importlib.util
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "dailycheckin_plus", "nodeseek", "main.py")

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f"  :: {detail}" if not cond and detail else ""))


def section(title):
    print("\n" + "=" * 70)
    print(title)


# --------------------------------------------------------------------------- #
# 假 requests + 假 CheckIn：不依赖引擎其它模块，也不需要网络
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload, ensure_ascii=False) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """记下每次请求，按 responder 返回预设响应。"""

    def __init__(self):
        self.headers = {}
        self.cookies = {}
        self.proxies = {}
        self.calls = []
        self.responder = lambda url, kw: FakeResp(payload={"success": True, "gain": 3, "current": 1226})

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.responder(url, kwargs)

    def get(self, url, params=None, **kwargs):
        self.calls.append({"method": "GET", "url": url, "params": params})
        return self.responder(url, {"params": params})


fake_requests = types.ModuleType("requests")
fake_requests.Session = FakeSession
fake_requests.utils = types.SimpleNamespace(add_dict_to_cookiejar=lambda jar, data: jar.update(data))
sys.modules["requests"] = fake_requests

fake_dc = types.ModuleType("dailycheckin_plus")


class _CheckIn:
    name = "Base"


fake_dc.CheckIn = _CheckIn
sys.modules["dailycheckin_plus"] = fake_dc

spec = importlib.util.spec_from_file_location("nodeseek_main", MOD)
ns_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns_mod)
NodeSeek = ns_mod.NodeSeek


def make(method="random", cookie="colorscheme=light; session=abc123; _t=1"):
    return NodeSeek({"cookie": cookie, "method": method})


def respond(resp):
    one = make()
    one.session.responder = lambda url, kw: resp
    return one


# --------------------------------------------------------------------------- #
section("1) 签到方式：两种方式只差 random 参数")

ns = make("random")
ns.attend()
check("随机 → random=true", ns.session.calls[0]["url"].endswith("/api/attendance?random=true"), ns.session.calls[0]["url"])

ns = make("fixed")
ns.attend()
check("固定 → random=false", ns.session.calls[0]["url"].endswith("/api/attendance?random=false"), ns.session.calls[0]["url"])

check("parse_method 认 fixed", NodeSeek.parse_method("fixed") == "fixed")
check("parse_method 认 5 / fixed5（手工改配置也认）",
      NodeSeek.parse_method("5") == "fixed" and NodeSeek.parse_method("fixed5") == "fixed")
check("parse_method 空值默认随机", NodeSeek.parse_method(None) == "random" and NodeSeek.parse_method("") == "random")
check("parse_method 不认识的值回落随机", NodeSeek.parse_method("whatever") == "random")
check("parse_method 大小写/空格不敏感", NodeSeek.parse_method(" FIXED ") == "fixed")

# --------------------------------------------------------------------------- #
section("2) 请求装配：只发一次 POST，带站点自己的 Referer/Origin")

ns = make("random")
ns.attend()
calls = ns.session.calls
check("只发了一个请求", len(calls) == 1, str(calls))
check("用的是 POST", calls[0]["method"] == "POST", calls[0].get("method"))
check("没带 query 以外的乱七八糟参数", "?" in calls[0]["url"] and calls[0]["url"].count("?") == 1, calls[0]["url"])
check("Referer 指向 nodeseek", ns.session.headers.get("Referer") == "https://www.nodeseek.com/", ns.session.headers.get("Referer"))
check("Origin 指向 nodeseek", ns.session.headers.get("Origin") == "https://www.nodeseek.com", ns.session.headers.get("Origin"))
check("UA 是桌面浏览器（CF 不拦 API，但别用 python-requests 的默认 UA）",
      "Mozilla/5.0" in ns.session.headers.get("User-Agent", ""), ns.session.headers.get("User-Agent"))
check("cookie 进了 cookiejar", ns.session.cookies.get("session") == "abc123", str(ns.session.cookies))

# --------------------------------------------------------------------------- #
section("3) cookie：只认 session，值里的 = 不能截断")

check("cookie 值里的 = 完整保留",
      NodeSeek.parse_cookie("session=abc==; _t=1").get("session") == "abc==",
      str(NodeSeek.parse_cookie("session=abc==; _t=1")))
check("空片段被忽略", NodeSeek.parse_cookie("a=1;; ;b=2") == {"a": "1", "b": "2"})
check("CRLF 也能解析", NodeSeek.parse_cookie("session=x\r\n_t=2").get("_t") == "2")

for bad, expect in (
    ({"cookie": ""}, "必须提供"),
    ({"cookie": "   "}, "必须提供"),
    ({"cookie": "colorscheme=light; _t=1"}, "未找到 session"),
):
    raised = ""
    try:
        NodeSeek(bad)
    except Exception as exc:  # noqa: BLE001
        raised = str(exc)
    check(f"{bad or '空配置'} → 报错含「{expect}」", expect in raised, raised)

# --------------------------------------------------------------------------- #
section("4) 响应分支：成功 / 已签到（HTTP 500）/ 其它失败")

ok_resp = respond(FakeResp(payload={"success": True, "gain": 3, "current": 1226, "message": "签到成功"}))
r = ok_resp.attend()
check("success=true → 记为成功", r["success"] is True and r["already"] is False, str(r))
check("gain / current 都取到了", r["gain"] == 3 and r["current"] == 1226, str(r))
check("成功文案带加号与当前值", ok_resp.result_text(r) == "✅ +3 鸡腿，当前 1226 鸡腿", ok_resp.result_text(r))

# 实测行为：今天已经签到时服务端回 HTTP 500，但 body 是正常 JSON
done = respond(FakeResp(status_code=500, payload={"success": False, "message": "今天已完成签到，请勿重复操作"}))
r = done.attend()
check("HTTP 500 + 「已完成签到」不抛异常", r["success"] is True, str(r))
check("已签到被单独标出来（already）", r["already"] is True, str(r))
check("已签到文案是「今日已签到」", "今日已签到" in done.result_text(r), done.result_text(r))

done2 = respond(FakeResp(status_code=500, payload={"success": False, "message": "你已经签到过了"}))
check("「已经签到」也算已签", done2.attend()["already"] is True)

done3 = respond(FakeResp(status_code=500, payload={"success": False, "message": "请勿重复操作"}))
check("「请勿重复」也算已签", done3.attend()["already"] is True)

fail = respond(FakeResp(status_code=403, payload={"success": False, "message": "Forbidden"}))
raised = ""
try:
    fail.attend()
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("其它失败要抛异常（否则面板会把失败当成功）", bool(raised), "没有抛异常")
check("失败文案带服务端原话", "Forbidden" in raised, raised)

cf = respond(FakeResp(status_code=503, payload=None, text="<html>Just a moment...</html>"))
raised = ""
try:
    cf.attend()
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("非 JSON（CF 挑战页）要抛异常", bool(raised), "没有抛异常")
check("非 JSON 的提示里点名 Cloudflare 与状态码", "Cloudflare" in raised and "503" in raised, raised)

weird = respond(FakeResp(payload=[1, 2, 3]))
raised = ""
try:
    weird.attend()
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("结构不对（不是对象）也要抛异常", "预期之外的结构" in raised, raised)

# --------------------------------------------------------------------------- #
section("5) main()：给人看的两行，方式与结果都在")

one = make("fixed")
one.session.responder = lambda url, kw: FakeResp(payload={"success": True, "gain": 5, "current": 1300})
text = one.main()
check("有「签到方式」一行", "签到方式" in text, text)
check("固定方式显示成「固定 5 鸡腿」", "固定 5 鸡腿" in text, text)
check("有「签到结果」一行且带 ✅", "签到结果" in text and "✅" in text, text)
check("结果里有加到的鸡腿数", "+5 鸡腿" in text, text)

two = make("random")
two.session.responder = lambda url, kw: FakeResp(status_code=500, payload={"success": False, "message": "今天已完成签到，请勿重复操作"})
text = two.main()
check("已签到时 main() 也不会炸", "今日已签到" in text, text)
check("随机方式显示成「随机鸡腿」", "随机鸡腿" in text, text)

print("\n" + "=" * 70)
section("6) 用户名：从 cookie 里的 pjwt 离线解出来，多账号才分得清")

# pjwt 是标准 JWT：header.payload.signature。这里现造一个假的，
# 绝不要把真实票据写进仓库 —— base64 只编码不加密，等于把用户名和 id 一起公开。
import base64 as _b64


def make_jwt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = _b64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.signature"


TEST_PJWT = make_jwt({"id": 1, "name": "ABC", "ts": 1700000000})
named = make(cookie=f"colorscheme=light; session=abc; pjwt={TEST_PJWT}")
check("从 pjwt 里解出用户名", named.username == "ABC", named.username)
named.session.responder = lambda url, kw: FakeResp(payload={"success": True, "gain": 5, "current": 1300})
text = named.main()
check("main() 首行是「账号信息」", text.splitlines()[0] == "账号信息: ABC", text)
check("只有 payload.signature 两段也能解",
      make(cookie="session=abc; pjwt=" + TEST_PJWT.split(".", 1)[1]).username == "ABC",
      "少了 header 段就不认了")

check("没有 pjwt 时不编造用户名", make().username == "", make().username)
check("pjwt 格式坏掉返回空串，不抛异常",
      make(cookie="session=abc; pjwt=bm90LWEtanNvbg").username == ""
      and make(cookie="session=abc; pjwt=a.b.c").username == "",
      "坏 JWT 应该安静地返回空")
check("没有用户名时 main() 仍是原来的两行",
      "账号信息" not in make().main(), make().main())

print("\n" + "=" * 70)
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败项：")
    for one in failed:
        print("  - " + one)
sys.exit(1 if failed else 0)
