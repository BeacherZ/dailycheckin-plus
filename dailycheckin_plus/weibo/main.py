"""微博超话签到。

走 **m.weibo.cn 移动端**接口，不是 weibo.com 网页版：

* 网页版 session 有 7 天硬性失效，而且 ``/ajax/super/...`` 一堆接口要 ``XSRF-TOKEN``；
* 移动端是独立登录体系，``SUB`` 的存活期明显更长，长期挂机的脚本基本都走这套。

流程只有三步：

1. ``GET /api/config``                      —— 判断是否登录，顺带拿到 ``st``（签到要带）
2. ``GET /api/container/getIndex``          —— ``containerid=100803_-_followsuper`` 拉关注列表，
   用 ``since_id`` 翻页。每张超话卡片带一个按钮，按钮的 ``scheme`` 就是签到地址
3. ``GET <按钮 scheme>&st=<st>``            —— 签到

为什么直接用按钮下发的 ``scheme``、而不是自己拼签到 URL：里面的 ``pageid`` / ``scheme_type``
/ ``source`` 都是服务端给的，跟着走最不容易被改版打挂。

已知返回（社区脚本与实测）：

* ``ok=1``                         —— 签到成功
* ``errno=402003``                 —— 频率限制，隔一下重试
* ``result=402004`` + ``scheme``   —— 触发风控，需要人工点验证链接
* ``ok=0`` 且文案含「已签到」        —— 今天已经签过

**默认签全部关注的超话**（``topics = all``），并受 ``max_topics`` 限制，避免一个关注了几百个
超话的账号跑到超时。想只签几个就填名字，逗号分隔；名字按「去掉【】与『超话』后缀后完全相等」
匹配 —— 刻意不做子串包含，否则配「周杰伦」会把「周杰伦电影速报」一起签了。
"""

import json
import os
import random
import re
import time
import urllib.parse
from typing import Optional

import requests

from dailycheckin_plus import CheckIn

M_WEIBO_BASE = "https://m.weibo.cn"
CONFIG_URL = f"{M_WEIBO_BASE}/api/config"
CONTAINER_URL = f"{M_WEIBO_BASE}/api/container/getIndex"
# 「我关注的超话」列表页的 containerid
FOLLOWED_SUPER_CONTAINER = "100803_-_followsuper"

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

# 按钮文案 -> 今天不用再签了。超话卡片上就这几个值：
# 「签到」= 还没签，「已签」= 签过了，「明日再来」= 今日次数用完。
DONE_BUTTONS = {"已签", "已签到", "已簽", "已簽到", "明日再来"}

DEFAULT_MAX_TOPICS = 20


class Weibo(CheckIn):
    name = "微博超话"

    def __init__(self, check_item):
        self.check_item = check_item or {}

        self.topics = self.parse_topics(self.check_item.get("topics"))
        # 只签指定超话；填 "all" 则签全部关注（受 max_topics 限制）
        self.sign_all = "all" in self.topics
        self.max_pages = max(1, int(self.check_item.get("max_pages") or 5))
        self.max_topics = max(1, int(self.check_item.get("max_topics") or DEFAULT_MAX_TOPICS))
        self.timeout = int(self.check_item.get("timeout") or 15)
        self.skipped = 0

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA_MOBILE,
                "Referer": f"{M_WEIBO_BASE}/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "MWeibo-Pwa": "1",
            }
        )
        proxy = str(self.check_item.get("proxy") or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        cookie = str(self.check_item.get("cookie") or "")
        if not cookie.strip():
            raise ValueError("必须提供微博 Cookie（至少包含 SUB）")
        if "SUB=" not in cookie:
            raise ValueError("Cookie 中未找到 SUB，请重新登录微博后复制")
        requests.utils.add_dict_to_cookiejar(self.session.cookies, self.parse_cookie(cookie))

    # ------------------------------------------------------------------ #
    # 配置与 cookie
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_cookie(cookie: str) -> dict:
        """``k=v; k2=v2`` -> dict。

        没有沿用引擎里常见的 ``split("=")[1]``：``SUB`` 的值几乎必然以 ``==`` 结尾，
        那样会被截断成半截票据，明明有效的 cookie 会被判成失效。
        换行同样当分隔符（多行粘贴、cookie 值本身不含换行）。
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
    def parse_topics(raw) -> list:
        """支持 list / 逗号分隔字符串 / 空值；空值等于全部关注。"""
        if not raw:
            return ["all"]
        if isinstance(raw, (list, tuple)):
            items = [str(one).strip() for one in raw]
        else:
            items = [one.strip() for one in str(raw).replace("，", ",").split(",")]
        items = [one for one in items if one]
        return items or ["all"]

    @staticmethod
    def clean_name(name: str) -> str:
        """剥掉超话标题里的装饰：【】方括号、尾部「超话」、零宽与空白字符。

        微博上的标题不一定干净：「周杰伦」「【周杰伦】」「周杰伦超话」指的是同一个，
        但「周杰伦电影速报」是另一个超话，所以只去装饰、不做包含匹配。
        """
        text = (name or "").strip()
        text = re.sub(r"[\s\-\u200b-\u200f\ufeff]", "", text)
        text = text.strip("【】[]（）()")
        if text.endswith("超话"):
            text = text[: -len("超话")]
        return text.strip("【】[]（）()")

    def match(self, real_name: str) -> bool:
        """精确匹配（去掉装饰后全等）。名字对不上宁可漏签，也不误签。"""
        real_clean = self.clean_name(real_name)
        if not real_clean:
            return False
        return any(
            want != "all" and self.clean_name(want) == real_clean for want in self.topics
        )

    # ------------------------------------------------------------------ #
    # 接口
    # ------------------------------------------------------------------ #
    def get_json(self, url: str, params: Optional[dict] = None) -> dict:
        resp = self.session.get(url, params=params, timeout=self.timeout)
        # 微博风控会直接给 432 / 418 且 body 为空，这类情况必须在网络层就说清楚
        if resp.status_code == 432:
            raise ValueError("被微博风控拦截（HTTP 432），请稍后重试或更换出口 IP")
        if resp.status_code == 418:
            raise ValueError("请求过于频繁（HTTP 418），请降低签到频率")
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as exc:
            raise ValueError(f"微博接口返回了非 JSON 内容：{exc}") from exc

    def login_state(self) -> tuple:
        """返回 (是否登录, st)。st 是签到接口要带的验签参数。"""
        data = self.get_json(CONFIG_URL)
        inner = data.get("data") or {}
        return bool(inner.get("login")), inner.get("st")

    def followed_topics(self) -> list:
        """拉关注的超话列表（自动翻页），返回 [{name, id, scheme, done, level}]。

        ``scheme`` 为 None 表示今天已签（按钮已经换成「已签」，没有可调用的地址）。
        """
        topics: list = []
        since_id = ""
        seen: set = set()

        for _ in range(self.max_pages):
            params = {"containerid": FOLLOWED_SUPER_CONTAINER}
            if since_id:
                params["since_id"] = since_id
            payload = self.get_json(CONTAINER_URL, params=params)
            if payload.get("ok") != 1:
                # 未登录时这里 ok=0，交给上层统一报「cookie 失效」
                break
            data = payload.get("data") or {}

            for card in data.get("cards") or []:
                for item in card.get("card_group") or [card]:
                    one = self.parse_topic_card(item)
                    if one and one["id"] not in seen:
                        seen.add(one["id"])
                        topics.append(one)

            since_id = (data.get("cardlistInfo") or {}).get("since_id", "")
            if not since_id:
                break
            # 签全部且已经够数了，就不用继续翻页
            if self.sign_all and len(topics) >= self.max_topics:
                break
            time.sleep(1)

        return topics

    @staticmethod
    def parse_topic_card(item: dict) -> Optional[dict]:
        """从一张超话卡片里抽出签到需要的信息。

        ``card_type`` 在微博不同接口里既有数字也有字符串（社区脚本里就是 ``=== 4`` 这种
        数值比较），所以统一 ``str()`` 之后再比，不做类型假设。
        """
        if not isinstance(item, dict) or str(item.get("card_type")) != "8":
            return None
        buttons = item.get("buttons") or []
        scheme = str(item.get("scheme") or "").strip()
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(scheme).query)
        topic_id = (qs.get("containerid") or [""])[0]
        name = str(item.get("title_sub") or "").strip()
        if not name or not topic_id:
            return None

        button_name = str((buttons[0] or {}).get("name") or "") if buttons else ""
        btn_scheme = (buttons[0] or {}).get("scheme") if buttons else None
        done = button_name in DONE_BUTTONS or not btn_scheme
        return {
            "name": name,
            "id": topic_id,
            "scheme": None if done else btn_scheme,
            "done": done,
            "level": str(item.get("desc1") or ""),
        }

    def sign_topic(self, topic: dict, st: str) -> dict:
        """对单个超话签到，返回 {success, message, retry, verify_url}。"""
        url = topic.get("scheme") or ""
        if not url:
            return {"success": False, "message": "今天已经签过了", "retry": False, "verify_url": ""}
        if not url.startswith("http"):
            url = M_WEIBO_BASE + url
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}st={urllib.parse.quote(str(st or ''))}"

        resp = self.get_json(url)
        message = str(resp.get("msg") or resp.get("errmsg") or "")
        errno = str(resp.get("errno", ""))
        result = str(resp.get("result", ""))

        # 触发风控：接口回一个验证页面的 scheme，需要人工点一下
        if "402004" in (result, errno):
            return {
                "success": False,
                "message": "触发微博验证，需点链接完成后再签",
                "retry": False,
                "verify_url": str(resp.get("scheme") or ""),
            }
        # 频率限制：不算失败，隔一下重试一次
        if "402003" in (result, errno):
            return {"success": False, "message": "接口忙，稍后重试", "retry": True, "verify_url": ""}
        if resp.get("ok") == 1 or "成功" in message:
            return {"success": True, "message": message or "签到成功", "retry": False, "verify_url": ""}
        if "已签到" in message or "已经签到" in message:
            return {"success": True, "message": "今日已签到", "retry": False, "verify_url": ""}
        return {
            "success": False,
            "message": message or json.dumps(resp, ensure_ascii=False)[:120],
            "retry": False,
            "verify_url": "",
        }

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self) -> dict:
        """返回 {items, skipped, missing}。

        ``items`` 每项是 {name, success, message, level?, verify_url?}；
        ``skipped`` 是受 max_topics 限制没签的数量；``missing`` 是配了但没关注的超话名。
        """
        logged, st = self.login_state()
        if not logged:
            raise ValueError("微博 Cookie 已失效（/api/config 返回 login=False），请重新登录后复制")

        topics = self.followed_topics()
        if not topics:
            raise ValueError("没有取到任何关注的超话，请确认账号确实关注了超话")

        missing: list = []
        if self.sign_all:
            targets = topics[: self.max_topics]
            self.skipped = max(0, len(topics) - len(targets))
        else:
            targets = [one for one in topics if self.match(one["name"])]
            # 不能用 match() 反推「哪个没找到」：它对「任意一个配置项命中」就返回 True。
            real_names = {self.clean_name(one["name"]) for one in topics}
            missing = [
                want
                for want in self.topics
                if want != "all" and self.clean_name(want) not in real_names
            ]
            if missing and not targets:
                raise ValueError(
                    "关注的超话里没有「%s」，请先在微博里关注该超话（当前关注了 %d 个）"
                    % ("、".join(missing), len(topics))
                )

        items: list = []
        for index, topic in enumerate(targets):
            entry = {"name": topic["name"], "success": True, "message": "", "level": topic["level"]}
            if topic["done"]:
                entry["message"] = "今日已签到"
                items.append(entry)
                continue
            try:
                resp = self.sign_topic(topic, st)
                if resp["retry"]:
                    # st 可能过期，重取一次再试
                    _, st = self.login_state()
                    time.sleep(random.uniform(2, 4))
                    resp = self.sign_topic(topic, st)
                entry["success"] = resp["success"]
                entry["message"] = resp["message"]
                if resp["verify_url"]:
                    entry["verify_url"] = resp["verify_url"]
            except Exception as exc:  # noqa: BLE001 - 单个超话失败不影响其它
                entry["success"] = False
                entry["message"] = str(exc)[:150]
            items.append(entry)
            if index < len(targets) - 1:
                time.sleep(random.uniform(3, 6))

        # 一个都没成功时抛出去，让面板这次运行标红 —— 否则「全部失败」会被当成跑完了
        if targets and not any(one["success"] for one in items):
            first = next((one["message"] for one in items if one["message"]), "未知原因")
            raise ValueError(f"全部超话签到失败：{first}")

        # 配了但没关注的超话要说清楚，否则用户以为「配了 3 个就都签了」
        for want in missing:
            items.append({"name": want, "success": False, "message": "未找到该超话，请确认已关注", "level": ""})
        return {"items": items, "skipped": self.skipped, "missing": missing}

    def main(self) -> str:
        outcome = self.run()
        msg = [{"name": "账号信息", "value": self.nickname()}]
        for one in outcome["items"]:
            flag = "✅" if one["success"] else "❌"
            title = one["name"]
            if one.get("level"):
                title = f"{title}（{one['level']}）"
            value = f"{flag} {one['message']}".strip()
            if one.get("verify_url"):
                value += f" 验证链接：{one['verify_url']}"
            msg.append({"name": title, "value": value})
        if outcome["skipped"]:
            msg.append(
                {
                    "name": "提示",
                    "value": f"还有 {outcome['skipped']} 个关注的超话没签（受「最多签到数」限制，可调大）",
                }
            )
        return "\n".join(f"{one.get('name')}: {one.get('value')}" for one in msg)

    def nickname(self) -> str:
        """取一下昵称，取不到就显示「已登录」（纯展示，不影响签到）。"""
        try:
            payload = self.get_json(CONTAINER_URL, params={"containerid": "100803"})
            for card in (payload.get("data") or {}).get("cards") or []:
                user = card.get("user") or {}
                if user.get("screen_name"):
                    return user["screen_name"]
        except Exception:  # noqa: BLE001
            pass
        return "已登录"


if __name__ == "__main__":
    with open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"),
        encoding="utf-8",
    ) as f:
        datas = json.loads(f.read())
    _check_item = datas.get("WEIBO", [])[0]
    print(Weibo(check_item=_check_item).main())
