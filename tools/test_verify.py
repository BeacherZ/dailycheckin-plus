#!/usr/bin/env python3
"""在线校验器（panel/verify.py）的离线自测 —— 打桩 http，不联网。

锁的是「判断逻辑本身」，因为这些地方错了用户是看不出来的：

* AcFun 用账号密码试登录，``result=0`` 必须算有效；只有 ``error_msg``
  明说密码/账号不对才算失效，服务器忙之类不能把人好好的密码标成错的；
* 表单站点（没有 cookie 字段）不能被「没有填写 cookie」挡在门外；
* NodeSeek 只要 **200 + JSON** 就该判有效 —— 旧版要求响应里出现 ``count``
  字段，站点一改字段名就整站变「无法判定」，这就是用户踩到的那个 bug；
* Cloudflare 挑战页会返回 **403 + HTML**，那和 cookie 失效是两件事，
  一律只能算「无法判定」，否则定时校验会天天误报。

跑法：python3 tools/test_verify.py
"""
import base64
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
from panel import verify  # noqa: E402

CALLS = []


def stub(status=200, text="{}", url="https://example.com/", exc=None):
    """替换 verify.http：记录调用，按预设返回。"""

    def _http(target, *args, **kwargs):
        CALLS.append({"url": target, "kwargs": kwargs, "args": args})
        if exc is not None:
            raise exc
        return verify.Resp(status, text, url, {})

    return _http


def reset(status=200, text="{}", exc=None):
    CALLS.clear()
    verify.http = stub(status=status, text=text, exc=exc)


def make_jwt(name="ABC"):
    """造一个只有 payload 的假票据（和 NodeSeek 的 pjwt 同形状）。"""
    payload = base64.urlsafe_b64encode(json.dumps({"id": 1, "name": name, "ts": 1700000000}).encode()).decode()
    return f"{payload}.fakesignature"


# --------------------------------------------------------------------------- #
section("1) AcFun：只认 Cookie（acPasstoken + auth_key），和别的站点一致")

COOKIE = "acPasstoken=CA123; auth_key=456; ac_username=%E5%95%8A%E5%93%88"

reset(200, '{"result":0,"info":{"userName":"ABC","level":5,"banana":12}}')
r = verify.v_acfun(COOKIE, {})
check("personalInfo 返回 result=0 判有效", r["ok"] is True, str(r))
check("查的是用户信息接口（不走登录、不碰密码）", "personalInfo" in CALLS[0]["url"], CALLS[0]["url"])
check("Cookie 原样带给接口", "acPasstoken=CA123" in str(CALLS[0]["kwargs"].get("cookie")), str(CALLS[0]["kwargs"]))
check("昵称取自用户信息的 userName", r["user"] == "ABC", str(r))

reset(200, '{"result":0,"info":{}}')
r = verify.v_acfun(COOKIE, {})
check("用户信息里没昵称时用 ac_username 兜底（网址解码）", r["user"] == "啊哈", str(r))

reset(401, "")
r = verify.v_acfun(COOKIE, {})
check("HTTP 401 判失效", r["ok"] is False, str(r))

reset(200, '{"result":-1,"error_msg":"请先登录"}')
r = verify.v_acfun(COOKIE, {})
check("result!=0 且提示登录判失效", r["ok"] is False, str(r))

reset(200, '{"result":0,"info":{}}')
r = verify.v_acfun("auth_key=456", {})
check("Cookie 里没有 acPasstoken 直接报缺（不发请求）", r["ok"] is False and not CALLS, str(r))

reset(200, '<html><body>verify you are human</body></html>')
r = verify.v_acfun(COOKIE, {})
check("风控网页算无法判定", r["ok"] is None and "网页" in r["message"], str(r))

reset(exc=TimeoutError("timed out"))
r = verify.v_acfun(COOKIE, {})
check("网络异常算无法判定", r["ok"] is None and "失败" in r["message"], str(r))

reset(200, '{"result":0,"info":{}}')
r = verify.verify_account("ACFUN", {"cookie": ""})
check("空 cookie 走通用分支，报「没有填写 cookie」", r["ok"] is False and "cookie" in r["message"] and not CALLS, str(r))

meta = catalog.site_meta("ACFUN")
check("ACFUN 是 cookie 凭据", meta.get("credential") == "cookie", str(meta.get("credential")))
check("必填 acPasstoken 与 auth_key", list(meta.get("required") or []) == ["acPasstoken", "auth_key"],
      str(meta.get("required")))
check("界面上不再有账号密码字段", not [f for f in meta.get("fields") or [] if f.get("key") in ("phone", "password")],
      str(meta.get("fields")))
check("acfun 校验器已注册", "acfun" in verify.VERIFIERS)

# --------------------------------------------------------------------------- #
section("2) NodeSeek：只要服务端回了 200 + JSON 就判有效")

GOOD = "session=abc; pjwt=" + make_jwt("ABC")

reset(200, '{"count":3}')
r = verify.v_nodeseek(GOOD, {})
check("200 + {count} 判有效", r["ok"] is True, str(r))
check("未读数带进 message", "未读通知 3" in r["message"], r["message"])
check("从 pjwt 解出用户名", r["user"] == "ABC", str(r))

reset(200, '{"success":true,"data":{"unreadCount":5}}')
r = verify.v_nodeseek(GOOD, {})
check("结构换成 data.unreadCount 也判有效（旧版就是卡在这里）", r["ok"] is True, str(r))
check("嵌套结构里的数字也能读出来", "未读通知 5" in r["message"], r["message"])

reset(200, '{"status":"ok"}')
r = verify.v_nodeseek(GOOD, {})
check("不认识的 JSON 结构一样算有效", r["ok"] is True, str(r))

reset(200, '{"message":"请先登录"}')
r = verify.v_nodeseek(GOOD, {})
check("JSON 里说没登录判失效", r["ok"] is False, str(r))

reset(401, '{"message":"unauthorized"}')
r = verify.v_nodeseek(GOOD, {})
check("401 + JSON 判失效", r["ok"] is False, str(r))

# 关键分支：CF 挑战页是 403 + HTML，绝不能判失效
reset(403, '<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>')
r = verify.v_nodeseek(GOOD, {})
check("403 + HTML（Cloudflare）算无法判定，不算失效", r["ok"] is None, str(r))
check("提示里点明是被 CF 拦了", "Cloudflare" in r["message"], r["message"])

reset(503, '<html><body>maintenance</body></html>')
r = verify.v_nodeseek(GOOD, {})
check("503 + HTML 算无法判定", r["ok"] is None, str(r))

reset(200, '<html><body><form>login</form></body></html>')
r = verify.v_nodeseek(GOOD, {})
check("200 + 登录页 HTML 算无法判定", r["ok"] is None, str(r))

reset(200, "not json at all")
r = verify.v_nodeseek(GOOD, {})
check("纯文本响应算无法判定", r["ok"] is None, str(r))

reset(exc=TimeoutError("timed out"))
r = verify.v_nodeseek(GOOD, {})
check("网络异常算无法判定", r["ok"] is None and "失败" in r["message"], str(r))

# 本地判定：没有 session 就不该发请求
reset(200, '{"count":3}')
r = verify.v_nodeseek("pjwt=" + make_jwt("ABC"), {})
check("只有 pjwt 没有 session 直接判失效", r["ok"] is False, str(r))
check("这时一个请求都没发", not CALLS, str(CALLS))

reset(200, '{"count":3}')
r = verify.v_nodeseek("", {})
check("空 cookie 直接判失效", r["ok"] is False and not CALLS, str(r))

# --------------------------------------------------------------------------- #
section("3) 公共行为：没有校验器的站点仍然是「无法判定」，文案不能误导")

r = verify.verify_account("MIMOTION", {"phone": "13800000001", "password": "x"})
check("没有校验器时返回无法判定", r["ok"] is None, str(r))
check("文案指向「立即签到」而不是让人干等", "立即签到" in r["message"], r["message"])

r = verify.verify_account("NODESEEK", {"cookie": ""})
check("cookie 站点空 cookie 判缺失，且不发请求", r["ok"] is False and not CALLS, str(r))

check("_looks_html 认得 doctype / html", verify._looks_html("  <!doctype html><p>") is True)
check("_looks_html 不把 JSON 当网页", verify._looks_html('{"html":"no"}') is False)
check("_unread_count 认得 count", verify._unread_count({"count": 7}) == 7)
check("_unread_count 不会把 bool 当数字", verify._unread_count({"count": True}) is None)
check("_unread_count 找不到就返回 None", verify._unread_count({"foo": 1}) is None)

# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
print(f"通过 {len(passed)}  失败 {len(failed)}")
if failed:
    for name in failed:
        print("  FAIL:", name)
sys.exit(1 if failed else 0)
