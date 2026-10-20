"""Cookie 在线有效性校验。

设计原则：**宁可说「无法判定」，也不误报「已失效」。**
网络不通、接口改版、返回结构不认识 —— 这些情况一律返回 `ok=None`，
只有站点接口明确回了「未登录 / 已过期」才判定为失效，避免把好 cookie 标成坏的。

另外所有请求都是独立的 12 秒超时，不会卡住面板。
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import cookies as ck

__all__ = ["verify_account", "VERIFIERS", "verify_site"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 12

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class Resp:
    def __init__(self, status: int, text: str, url: str, headers=None) -> None:
        self.status = status
        self.text = text
        self.url = url
        self.headers = headers or {}


def http(
    url: str,
    cookie: str | None = None,
    headers: dict | None = None,
    data: bytes | None = None,
    method: str | None = None,
    timeout: int = TIMEOUT,
) -> Resp:
    """极简 GET/POST。只用标准库，容器里没有任何额外依赖。

    HTTP 4xx/5xx **不抛异常**，而是把状态码和响应体一起返回 —— 有些站点
    （比如有道）恰恰是用 500 + 一段 JSON 来表达「登录已失效」的。
    """
    hdr = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
    }
    if headers:
        hdr.update(headers)
    if cookie:
        hdr["Cookie"] = cookie
    if data is not None and "Content-Type" not in hdr:
        hdr["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)

    def _read(resp) -> Resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return Resp(resp.status, text, resp.url, dict(resp.headers))

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            return _read(resp)
    except urllib.error.HTTPError as exc:
        return _read(exc)


def _json(resp: Resp):
    try:
        return json.loads(resp.text)
    except Exception:  # noqa: BLE001
        return None


def ok(message: str, user: str | None = None, **extra) -> dict:
    out = {"ok": True, "user": user, "message": message}
    out.update(extra)
    return out


def bad(message: str, **extra) -> dict:
    out = {"ok": False, "user": None, "message": message}
    out.update(extra)
    return out


def unknown(message: str, **extra) -> dict:
    out = {"ok": None, "user": None, "message": message}
    out.update(extra)
    return out


def _looks_html(text: str) -> bool:
    """响应体是不是网页（多半是 Cloudflare 挑战页/登录页），而不是 JSON。

    这个判据很关键：被 CF 拦时状态码常常也是 403，**不能**据此判 cookie 失效。
    """
    head = (text or "")[:400].lstrip().lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<html" in head


def _jwt_name(token) -> str:
    """离线解 NodeSeek 的 ``pjwt``（标准 JWT）拿用户名。

    只依赖 base64，不发请求、不受风控影响，解不出来就返回空串。
    """
    parts = str(token or "").strip().split(".")
    if len(parts) < 2:
        return ""
    payload = parts[-2] + "=" * (-len(parts[-2]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("name") or "").strip()


# --------------------------------------------------------------------------- #
# 各站点校验器
# --------------------------------------------------------------------------- #
def v_bilibili(cookie: str, account: dict) -> dict:
    try:
        resp = http(
            "https://api.bilibili.com/x/web-interface/nav",
            cookie=cookie,
            headers={"Referer": "https://www.bilibili.com/"},
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求 B 站接口失败：{exc}")
    data = _json(resp)
    if not isinstance(data, dict):
        return unknown("B 站接口返回了非 JSON 内容，可能被风控拦截")
    code = data.get("code")
    if code == -101:
        return bad("B 站返回「账号未登录」，cookie 已失效或复制不完整")
    if code != 0:
        return unknown(f"B 站接口返回 code={code}：{data.get('message')}")
    inner = data.get("data") or {}
    if not inner.get("isLogin"):
        return bad("B 站接口显示未登录")
    money = inner.get("money")
    vip = "大会员" if inner.get("vipStatus") else "普通会员"
    msg = f"{vip}｜硬币 {money}｜等级 {inner.get('level_info', {}).get('current_level')}"
    return ok(msg, user=inner.get("uname"))


def v_tieba(cookie: str, account: dict) -> dict:
    try:
        resp = http("https://tieba.baidu.com/dc/common/tbs", cookie=cookie)
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求贴吧接口失败：{exc}")
    data = _json(resp)
    if not isinstance(data, dict):
        return unknown("贴吧接口返回了非 JSON 内容")
    if not data.get("is_login"):
        return bad("贴吧返回未登录，BDUSS 已失效")
    user = None
    try:
        info = _json(http("https://zhidao.baidu.com/api/loginInfo", cookie=cookie))
        if isinstance(info, dict) and info.get("userName"):
            user = info.get("userName")
    except Exception:  # noqa: BLE001
        pass
    return ok("登录态正常", user=user)


def v_youdao(cookie: str, account: dict) -> dict:
    """有道失效时接口会返回 HTTP 500 + {"error":"207","message":"AUTHENTICATION_FAILURE..."}。"""
    user = None
    try:
        pairs = dict(ck.items_from_cookie(cookie))
        parts = pairs.get("YNOTE_PERS", "").split("||")
        if len(parts) >= 2:
            user = parts[-2] or None
    except Exception:  # noqa: BLE001
        pass
    try:
        resp = http(
            "https://note.youdao.com/yws/api/daupromotion?method=sync",
            cookie=cookie,
            data=b"",
            method="POST",
            headers={"Referer": "https://note.youdao.com/web/"},
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求有道接口失败：{exc}")
    body = resp.text or ""
    if "AUTHENTICATION_FAILURE" in body or ("207" in body and '"error"' in body):
        return bad("有道返回未认证，YNOTE_PERS / YNOTE_SESS 已失效")
    if "rewardSpace" in body or "space" in body.lower():
        return ok("登录态正常", user=user)
    return unknown(f"有道返回了预期之外的内容（HTTP {resp.status}），无法判定")


def v_iqiyi(cookie: str, account: dict) -> dict:
    pairs = dict(ck.items_from_cookie(cookie))
    p00001 = pairs.get("P00001", "")
    if not p00001:
        return bad("cookie 里没有 P00001，无法校验")
    user = None
    raw_p2 = pairs.get("P00002", "")
    if raw_p2:
        try:
            info = json.loads(urllib.parse.unquote(raw_p2))
            user = info.get("nickname") or info.get("user_name")
        except Exception:  # noqa: BLE001
            pass
    try:
        resp = http(
            "http://serv.vip.iqiyi.com/vipgrowth/query.action?P00001=" + urllib.parse.quote(p00001),
            headers={"Referer": "https://www.iqiyi.com/"},
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求爱奇艺接口失败：{exc}")
    data = _json(resp)
    if not isinstance(data, dict):
        return unknown("爱奇艺接口返回了非 JSON 内容")
    if data.get("code") == "A00000":
        inner = data.get("data") or {}
        return ok(f"VIP 等级 {inner.get('level')}｜成长值 {inner.get('growthvalue')}", user=user)
    return bad(f"爱奇艺返回：{data.get('msg') or data.get('code')}")


def v_v2ex(cookie: str, account: dict) -> dict:
    try:
        resp = http(
            "https://www.v2ex.com/settings",
            cookie=cookie,
            headers={"Referer": "https://www.v2ex.com/"},
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求 V2EX 失败：{exc}")
    if resp.status != 200:
        return unknown(f"V2EX 返回 HTTP {resp.status}，无法判定（可能被风控拦截）")
    if "/signin" in (resp.url or ""):
        return bad("V2EX 跳转到了登录页，A2 已失效")
    text = resp.text
    if "需要先登录" in text or "请输入用户名" in text or "请先登录" in text:
        return bad("V2EX 提示需要登录，A2 已失效")
    m = re.search(r'href="/member/([^"/?]+)"', text)
    return ok("登录态正常", user=m.group(1) if m else None)


def v_smzdm(cookie: str, account: dict) -> dict:
    try:
        resp = http(
            "https://zhiyou.smzdm.com/user/",
            cookie=cookie,
            headers={"Referer": "https://m.smzdm.com/"},
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求什么值得买失败：{exc}")
    if "/user/login" in resp.url or "passport.smzdm.com" in resp.url:
        return bad("被重定向到登录页，cookie 已失效")
    m = re.search(r'<a href="https://zhiyou\.smzdm\.com/user">\s*(.*?)\s*</a>', resp.text, re.S)
    name = (m.group(1).strip() if m else "") or None
    if name:
        return ok("登录态正常", user=name)
    return unknown("页面结构与预期不同，未能确认登录态")


def v_baidupan(cookie: str, account: dict) -> dict:
    """未登录时返回 {"errno":-6,"errmsg":"Invalid Bduss"}。"""
    try:
        resp = http(
            "https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo&clienttype=0&app_id=250528&web=1",
            cookie=cookie,
            headers={"Referer": "https://pan.baidu.com/"},
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求百度网盘失败：{exc}")
    data = _json(resp)
    if not isinstance(data, dict):
        return unknown(f"百度网盘返回了非 JSON 内容（HTTP {resp.status}）")
    errno = data.get("errno")
    if errno == 0:
        return ok("登录态正常", user=data.get("baidu_name") or data.get("netdisk_name"))
    if data.get("errmsg") or errno in (-6, 2):
        return bad(f"百度网盘返回 {data.get('errmsg') or ('errno=' + str(errno))}，BDUSS 已失效")
    return unknown(f"百度网盘返回 errno={errno}，无法判定")


def _discuz(forum: str, name: str):
    def _verify(cookie: str, account: dict) -> dict:
        try:
            resp = http(
                forum + "/home.php?mod=spacecp",
                cookie=cookie,
                headers={"Referer": forum + "/forum.php"},
            )
        except Exception as exc:  # noqa: BLE001
            return unknown(f"请求{name}失败：{exc}")
        text = resp.text
        if re.search(r"需要先登录|还没有登录|尚未登录|请先登录|立即登录", text):
            return bad(f"{name}提示需要登录，cookie 已失效")
        user = None
        for pattern in (
            r'<a href="home\.php\?mod=space&uid=\d+"[^>]*>([^<]{1,24})</a>',
            r'class="dropmenu"[^>]*>\s*<a[^>]*>([^<]{1,24})</a>',
            r'id="loginuser"[^>]*>([^<]{1,24})<',
        ):
            m = re.search(pattern, text)
            if m and m.group(1).strip():
                user = m.group(1).strip()
                break
        return ok("登录态正常", user=user)

    return _verify


def v_acfun(cookie: str, account: dict) -> dict:
    """AcFun：只要 Cookie（``acPasstoken`` + ``auth_key``），和别的站点一致。

    判据很干脆：查一次 ``personalInfo``，``result=0`` 就是登录态有效，
    未登录是 HTTP 401 或 ``result != 0``。顺带把昵称（``userName``）带回去 ——
    面板和通知里显示网站昵称，比显示手机号有辨识度。
    """
    pairs = dict(ck.items_from_cookie(cookie))
    if not str(pairs.get("acPasstoken") or "").strip():
        return bad("Cookie 里没有 acPasstoken，请重新登录后整块复制")
    try:
        resp = http(
            "https://www.acfun.cn/rest/pc-direct/user/personalInfo",
            cookie=cookie,
            headers={"Referer": "https://www.acfun.cn/"},
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求 AcFun 用户信息失败：{exc}")

    if resp.status in (401, 403):
        return bad(f"AcFun 返回未登录（HTTP {resp.status}），Cookie 已失效，请重新登录后复制")
    data = _json(resp)
    if not isinstance(data, dict):
        if _looks_html(resp.text):
            return unknown(f"AcFun 返回了网页（HTTP {resp.status}），可能被风控拦了，无法判定")
        return unknown(f"AcFun 返回了非 JSON 内容（HTTP {resp.status}），无法判定")
    if data.get("result") == 0:
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        user = ""
        for key in ("userName", "username", "nickName", "nickname", "name"):
            user = str(info.get(key) or "").strip()
            if user:
                break
        if not user:
            user = urllib.parse.unquote(str(pairs.get("ac_username") or "")).strip()
        return ok("Cookie 有效", user=user or None)

    message = str(data.get("error_msg") or data.get("errorMsg") or "").strip()
    if "登录" in message:
        return bad(f"AcFun 返回「{message}」，Cookie 已失效")
    return unknown(f"AcFun 返回 result={data.get('result')}：{message or '没有说明'}，无法判定")


def v_nodeseek(cookie: str, account: dict) -> dict:
    """NodeSeek：读一条只属于登录用户的接口（未读通知数）。

    判定分三层，尽量少落到「无法判定」：

    1. **本地就能定的**：cookie 里连 ``session`` 都没有 → 直接失效，不用发请求。
    2. **服务端明说的**：HTTP 401/403 且返回 JSON → 失效；JSON 里写着「请先登录」
       之类 → 失效。
    3. **其余 200 + JSON → 有效**。NodeBB 的接口在未登录时一律 401/403 或返回登录页，
       200 还带结构化 JSON 就说明服务端认了这个 session —— 这正是以前太保守的地方：
       旧版要求响应里必须出现 ``count`` 字段，站点一改字段名就全变「无法判定」。

    Cloudflare 挑战页（HTML，状态码可能是 200/403/503）一律算无法判定 —— 前面的
    CF 拦没拦跟 cookie 好不好是两件事，把它当失效会天天误报。
    """
    pairs = dict(ck.items_from_cookie(cookie))
    user = _jwt_name(pairs.get("pjwt"))
    if not str(pairs.get("session") or "").strip():
        return bad("Cookie 里没有 session（只有 pjwt 不足以登录），请重新登录后整块复制 Cookie")

    try:
        resp = http(
            "https://www.nodeseek.com/api/notification/unread-count",
            cookie=cookie,
            headers={"Referer": "https://www.nodeseek.com/", "Accept": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求 NodeSeek 失败：{exc}")

    data = _json(resp)
    if isinstance(data, dict):
        message = str(data.get("message") or data.get("msg") or data.get("error") or "").strip()
        lowered = message.lower()
        if any(word in message for word in ("未登录", "请先登录", "重新登录", "登录已过期")) or any(
            word in lowered for word in ("login", "unauthorized", "not logged in", "permission")
        ):
            return bad(f"NodeSeek 提示「{message[:40]}」，session 已失效")
        if resp.status in (401, 403):
            return bad(f"NodeSeek 返回未登录（HTTP {resp.status}），session 已失效")
        count = _unread_count(data)
        detail = f"，未读通知 {count}" if count is not None else ""
        return ok(f"登录态正常{detail}", user=user)

    # 到这里说明响应不是 JSON：CF 挑战页 / 5xx / 结构改版
    if _looks_html(resp.text):
        return unknown(f"NodeSeek 返回了网页（HTTP {resp.status}），多半被 Cloudflare 拦了，无法判定（可在站点参数里配代理）")
    if resp.status in (401, 403):
        return bad(f"NodeSeek 返回未登录（HTTP {resp.status}），session 已失效")
    return unknown(f"NodeSeek 返回了预期之外的内容（HTTP {resp.status}），无法判定")


def _unread_count(data: dict):
    """从未读通知接口里找那个数字，兼容常见几种外壳。

    NodeBB 的返回结构在不同版本/插件下会变（``count`` / ``unreadCount`` /
    ``data.count``），找得到就展示、找不到也不影响「有效」这个结论。
    """
    keys = ("count", "unreadCount", "unread_count", "unread")
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    for key in keys:
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        value = inner.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def v_weibo(cookie: str, account: dict) -> dict:
    """微博登录态：直接问 /api/config 要 login 字段。

    这是少数几个能明确回答「登没登录」的接口 —— 未登录时它照样返回 200 +
    ``{"data":{"login":false}}``，所以不会像别的站点那样把网络问题误判成 cookie 失效。
    """
    try:
        resp = http(
            "https://m.weibo.cn/api/config",
            cookie=cookie,
            headers={
                "Referer": "https://m.weibo.cn/",
                "X-Requested-With": "XMLHttpRequest",
                "MWeibo-Pwa": "1",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return unknown(f"请求微博接口失败：{exc}")
    if resp.status == 432:
        return unknown("被微博风控拦截（HTTP 432），无法判定；可稍后重试或在站点里配置代理")
    data = _json(resp)
    if not isinstance(data, dict) or "data" not in data:
        return unknown(f"微博返回了预期之外的内容（HTTP {resp.status}），无法判定")
    inner = data.get("data") or {}
    if not inner.get("login"):
        return bad("微博返回「未登录」，SUB 已失效，请重新登录后复制 Cookie")
    user = inner.get("user")
    if isinstance(user, dict):
        user = user.get("screen_name") or None
    elif not isinstance(user, str):
        user = None
    else:
        user = user or None
    return ok("登录态正常", user=user)


VERIFIERS = {
    "bilibili": v_bilibili,
    "tieba": v_tieba,
    "youdao": v_youdao,
    "iqiyi": v_iqiyi,
    "v2ex": v_v2ex,
    "smzdm": v_smzdm,
    "baidupan": v_baidupan,
    "acfun": v_acfun,
    "nodeseek": v_nodeseek,
    "weibo": v_weibo,
    "enshan": _discuz("https://www.right.com.cn/forum", "恩山论坛"),
    "fnnasclub": _discuz("https://club.fnnas.com", "飞牛论坛"),
}

NO_VERIFIER_HINT = "该站点没有公开的登录态接口，点「立即签到」用运行结果来判断更准确"


def verify_account(site_key: str, account: dict, verifier: str | None = None) -> dict:
    """校验单个账号；返回 {ok, user, message}。`ok=None` 表示无法判定。"""
    meta_verifier = verifier
    if meta_verifier is None:
        from . import catalog

        meta_verifier = catalog.site_meta(site_key).get("verify")
    if not meta_verifier:
        return unknown(NO_VERIFIER_HINT)
    func = VERIFIERS.get(meta_verifier)
    if func is None:
        return unknown(NO_VERIFIER_HINT)
    cookie = str(account.get("cookie") or "")
    if not cookie.strip():
        return bad("没有填写 cookie")
    return func(cookie, account)


def verify_site(site_key: str, accounts: list[dict], verifier: str | None = None) -> list[dict]:
    return [verify_account(site_key, account, verifier) for account in accounts]
