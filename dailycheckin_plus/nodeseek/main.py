"""NodeSeek 论坛签到。

接口只有一个：``POST /api/attendance``，两种签到方式**只差一个 query 参数**：

* ``?random=true``  —— 随机鸡腿（签到方式 1）
* ``?random=false`` —— 固定 5 鸡腿（签到方式 2）

两者返回同一个结构：``{"success": bool, "gain": int, "current": int, "message": str}``。

有两个坑必须按实际情况处理，否则会天天误报失败：

1. **今天已经签过时返回的是 HTTP 500，但 body 仍然是 JSON** ——
   ``{"success": false, "message": "今天已完成签到，请勿重复操作"}``。
   所以判断只能看 body 里的 ``success``，不能看状态码；``requests.raise_for_status()``
   在这里是错的写法。
2. **``cf_clearance`` 不是必需的**。实测容器出口只带 ``session``（和登录时那几个
   ``_t`` / ``smidV2`` 之类）就能签到成功；Cloudflare 的挑战页目前不会拦 API。
   真被拦时通常拿到一张 HTML，这里会明确报「返回了非 JSON」而不是莫名失败。
"""

import base64
import json
import os

import requests

from dailycheckin_plus import CheckIn

BASE = "https://www.nodeseek.com"
ATTENDANCE_URL = f"{BASE}/api/attendance"

# 服务端的「今天已经签过」文案可能微调，这里按关键词判定，避免逐字比对
DONE_HINTS = ("已完成签到", "已经签到", "已签到", "请勿重复")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class NodeSeek(CheckIn):
    name = "NodeSeek"

    def __init__(self, check_item):
        self.check_item = check_item or {}
        self.method = self.parse_method(self.check_item.get("method"))
        self.timeout = int(self.check_item.get("timeout") or 20)

        cookie = str(self.check_item.get("cookie") or "")
        if not cookie.strip():
            raise ValueError("必须提供 NodeSeek Cookie（至少包含 session）")
        if "session=" not in cookie:
            raise ValueError("Cookie 中未找到 session，请重新登录后复制")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": f"{BASE}/",
                "Origin": BASE,
            }
        )
        cookies = self.parse_cookie(cookie)
        self.username = self.decode_jwt_name(cookies.get("pjwt"))
        requests.utils.add_dict_to_cookiejar(self.session.cookies, cookies)

    # ------------------------------------------------------------------ #
    # 配置与 cookie
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_method(raw) -> str:
        """把配置里的签到方式归一成 ``random`` / ``fixed``。

        面板下发的永远是这两个字符串（catalog 里是下拉框，后端还会再校验一次），
        这里额外兼容手工改 config.json 的情况：``5`` / ``fixed5`` 当固定，
        ``true`` / ``1`` 当随机，不认识的一律回落到随机（和生产默认一致）。
        """
        text = str(raw if raw is not None else "").strip().lower()
        if text in ("fixed", "fixed5", "5", "constant"):
            return "fixed"
        return "random"

    @staticmethod
    def parse_cookie(cookie: str) -> dict:
        """``k=v; k2=v2`` -> dict，只切第一个 ``=``（值里带 ``=`` 不会被截断）。

        换行也当分隔符：从开发者工具里复制多行 cookie 是很常见的粘贴形态，
        而 cookie 值本身不允许包含换行，所以这样处理不会有歧义。
        """
        out = {}
        text = str(cookie or "").replace("\r\n", "\n").replace("\n", ";")
        for part in text.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            if key:
                out[key] = value.strip()
        return out

    @staticmethod
    def decode_jwt_name(pjwt) -> str:
        """从 ``pjwt`` 里取用户名 —— 它是个标准 JWT，payload 形如
        ``{"id": 8673, "name": "ABC", "ts": 1792409633}``。

        走这条路而不是再请求一次接口，是因为它**完全离线**：不发请求、不受风控影响、
        也不需要 cf_clearance。取不到（老 cookie 里没有 pjwt、格式变了）就返回空串，
        通知里不写这一行，绝不影响签到本身。
        """
        parts = str(pjwt or "").strip().split(".")
        if len(parts) < 2:
            return ""
        # 标准 JWT 是 header.payload.signature；也兼容只有 payload.signature 的写法
        payload = parts[-2] + "=" * (-len(parts[-2]) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("name") or "").strip()

    # ------------------------------------------------------------------ #
    # 签到
    # ------------------------------------------------------------------ #
    def attend(self) -> dict:
        """调用签到接口，返回 ``{success, gain, current, message, already}``。

        已签到时 ``success`` 也记为 True（对用户来说这就是「今天没问题」），
        但带上 ``already=True`` 让上层能换个说法，不至于把「重复签到」说成新签到。
        """
        random_flag = "true" if self.method == "random" else "false"
        resp = self.session.post(f"{ATTENDANCE_URL}?random={random_flag}", timeout=self.timeout)
        try:
            data = resp.json()
        except ValueError:
            body = (resp.text or "").strip().replace("\n", " ")[:120]
            raise ValueError(
                f"签到接口返回了非 JSON（HTTP {resp.status_code}）：{body or '空响应'}。"
                "多半是被 Cloudflare 拦了，换出口 IP 或稍后再试"
            ) from None

        if not isinstance(data, dict):
            raise ValueError(f"签到接口返回了预期之外的结构：{str(data)[:120]}")

        message = str(data.get("message") or data.get("msg") or "").strip()
        if data.get("success"):
            return {
                "success": True,
                "already": False,
                "gain": data.get("gain"),
                "current": data.get("current"),
                "message": message or "签到成功",
            }
        # HTTP 500 + success=false + 「已完成签到」：不是错误，是幂等
        if any(hint in message for hint in DONE_HINTS):
            return {
                "success": True,
                "already": True,
                "gain": None,
                "current": data.get("current"),
                "message": message or "今天已经签到过了",
            }
        raise ValueError(f"签到失败：{message or f'HTTP {resp.status_code}'}")

    def result_text(self, result: dict) -> str:
        """把结果拼成一行给人看的文字（通知和面板共用）。"""
        if result["already"]:
            text = "今日已签到"
            if result.get("current") is not None:
                text += f"（当前 {result['current']} 鸡腿）"
            return f"✅ {text}"

        gain, current = result.get("gain"), result.get("current")
        if gain is None:
            return f"✅ {result.get('message') or '签到成功'}"
        text = f"✅ +{gain} 鸡腿"
        if current is not None:
            text += f"，当前 {current} 鸡腿"
        return text

    def main(self) -> str:
        result = self.attend()
        label = "随机鸡腿" if self.method == "random" else "固定 5 鸡腿"
        msg = []
        # 有用户名就先写出来：同一个站点配了多个账号时，这是唯一能一眼分辨的地方
        if self.username:
            msg.append({"name": "账号信息", "value": self.username})
        msg += [
            {"name": "签到方式", "value": label},
            {"name": "签到结果", "value": self.result_text(result)},
        ]
        return "\n".join(f"{one.get('name')}: {one.get('value')}" for one in msg)


if __name__ == "__main__":
    with open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"),
        encoding="utf-8",
    ) as f:
        datas = json.loads(f.read())
    _check_item = datas.get("NODESEEK", [])[0]
    print(NodeSeek(check_item=_check_item).main())
