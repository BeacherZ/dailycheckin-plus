import json
import os
import re

import requests
import urllib3

from dailycheckin_plus import CheckIn
from dailycheckin_plus.utils import cookie_store

urllib3.disable_warnings()


class V2ex(CheckIn):
    name = "V2EX 论坛"

    def __init__(self, check_item):
        self.check_item = check_item

    @staticmethod
    def sign(session):
        msg = []
        response = session.get(url="https://www.v2ex.com/mission/daily", verify=False)
        pattern = (
            r"<input type=\"button\" class=\"super normal button\""
            r" value=\".*?\" onclick=\"location\.href = \'(.*?)\';\" />"
        )
        urls = re.findall(pattern=pattern, string=response.text)
        url = urls[0] if urls else None
        if url is None:
            return [{"name": "签到失败", "value": "cookie 可能过期"}]
        elif url != "/balance":
            headers = {"Referer": "https://www.v2ex.com/mission/daily"}
            data = {"once": url.split("=")[-1]}
            _ = session.get(
                url="https://www.v2ex.com" + url,
                verify=False,
                headers=headers,
                params=data,
            )
        response = session.get(url="https://www.v2ex.com/balance", verify=False)
        total = re.findall(
            pattern=r"<td class=\"d\" style=\"text-align: right;\">(\d+\.\d+)</td>",
            string=response.text,
        )
        total = total[0] if total else "签到失败"
        today = re.findall(
            pattern=r'<td class="d"><span class="gray">(.*?)</span></td>',
            string=response.text,
        )
        today = today[0] if today else "签到失败"
        username = re.findall(
            pattern=r"<a href=\"/member/.*?\" class=\"top\">(.*?)</a>",
            string=response.text,
        )
        username = username[0] if username else "用户名获取失败"
        msg += [
            {"name": "帐号信息", "value": username},
            {"name": "今日签到", "value": today},
            {"name": "帐号余额", "value": total},
        ]
        response = session.get(url="https://www.v2ex.com/mission/daily", verify=False)
        # 连续签到天数：V2EX 换过几次页面结构，老正则（`<div class="cell">…天</div>`）
        # 早就匹配不上了。这里两种都试，还是认不出来就不输出这一行 —— 通知里挂一句
        # 「获取连续签到天数失败」既不是用户的问题，也没法操作，纯属噪音。
        days = re.findall(pattern=r"<div class=\"cell\">(.*?)天</div>", string=response.text)
        if not days:
            days = re.findall(pattern=r"连续签到[^0-9]{0,12}(\d+)\s*天", string=response.text)
        if days:
            msg += [{"name": "签到天数", "value": f"已连续签到{days[0]}天"}]
        return msg

    def keep_alive(self, session, raw_cookie: str) -> None:
        """把服务端刚下发的续期 cookie 写回 config.json。

        V2EX 的 A2 是滑动续期的：带着它访问页面，响应里就会带一个新的 A2。
        不落盘的话新值随进程一起消失，下次还是用配置里的老 cookie，久了就得重登。
        这一步只是为了延长登录态，任何环节出问题都不该影响签到结果 —— 全部安静跳过。
        """
        jar = session.cookies.get_dict()
        if not jar.get("A2"):
            return
        merged = cookie_store.merge_cookie(raw_cookie, jar)
        if merged == str(raw_cookie or "").strip():
            return
        if cookie_store.write_back(
            self.check_item.get("_config_path", ""),
            self.check_item.get("_site_key", "V2EX"),
            int(self.check_item.get("_account_index", 0)),
            "cookie",
            merged,
        ):
            print("V2EX 登录态已续期，新 cookie 已写回 config.json")

    def main(self):
        raw_cookie = str(self.check_item.get("cookie") or "")
        # 按第一个等号切：V2EX 的 cookie 值里本身可能带 `=`（base64 填充），
        # 老写法 `split("=")[1]` 会把这种值截断，登录态时灵时不灵。
        cookie = {}
        for segment in raw_cookie.split("; "):
            if "=" in segment:
                key, _, value = segment.partition("=")
                cookie[key.strip()] = value
        session = requests.session()
        if self.check_item.get("proxy", ""):
            proxies = {
                "http": self.check_item.get("proxy", ""),
                "https": self.check_item.get("proxy", ""),
            }
            session.proxies.update(proxies)
        requests.utils.add_dict_to_cookiejar(session.cookies, cookie)
        session.headers.update(
            {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36 Edg/87.0.664.66",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        msg = self.sign(session=session)
        self.keep_alive(session, raw_cookie)
        msg = "\n".join([f"{one.get('name')}: {one.get('value')}" for one in msg])
        return msg


if __name__ == "__main__":
    with open(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json"),
        encoding="utf-8",
    ) as f:
        datas = json.loads(f.read())
    _check_item = datas.get("V2EX", [])[0]
    print(V2ex(check_item=_check_item).main())
