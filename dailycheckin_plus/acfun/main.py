"""AcFun 签到。

凭据只有 **Cookie**，和本项目其它站点保持一致：网页登录后取
``acPasstoken`` + ``auth_key`` 两条就够签到（约一个月过期），
不必把手机号和密码留在 config.json 里。

通知里的「账号信息」用**网站昵称**：先看 Cookie 里的 ``ac_username``
（网址编码的昵称，离线可解），再退回用户信息接口的 ``userName`` ——
同一站点配多个账号时，昵称才是能一眼分辨的东西。
"""

import json
import os
import re
from urllib.parse import unquote

import requests
import urllib3

from dailycheckin_plus import CheckIn

urllib3.disable_warnings()

BASE = "https://www.acfun.cn"
INFO_URL = f"{BASE}/rest/pc-direct/user/personalInfo"


class AcFun(CheckIn):
    name = "AcFun"

    def __init__(self, check_item: dict):
        self.check_item = check_item or {}
        self.contentid = "27259341"
        self.st = None
        self.info = {}
        self.username = ""
        self.cookie = str(self.check_item.get("cookie") or "").strip()

    # ------------------------------------------------------------------ #
    # 凭据
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_cookie(cookie: str) -> dict:
        """``k=v; k2=v2`` -> dict，只切第一个 ``=``（值里带 ``=`` 不会被截断）。

        换行也当分隔符：从开发者工具里复制多行 cookie 是很常见的粘贴形态。
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

    @property
    def account_label(self) -> str:
        """通知里「账号信息」那一行：昵称，取不到就写站点名。"""
        return self.username or "AcFun 账号"

    def get_token(self, session):
        url = "https://id.app.acfun.cn/rest/web/token/get?sid=acfun.midground.api"
        res = session.post(url=url).json()
        self.st = res.get("acfun.midground.api_st") if res.get("result") == 0 else ""
        return self.st

    def get_video(self, session):
        url = "https://www.acfun.cn/rest/pc-direct/rank/channel"
        res = session.get(url=url).json()
        self.contentid = res.get("rankList")[0].get("contentId")
        return self.contentid

    @staticmethod
    def sign(session):
        url = "https://www.acfun.cn/rest/pc-direct/user/signIn"
        response = session.post(url=url)
        return {"name": "签到信息", "value": response.json().get("msg")}

    def danmu(self, session):
        url = "https://www.acfun.cn/rest/pc-direct/new-danmaku/add"
        data = {
            "mode": "1",
            "color": "16777215",
            "size": "25",
            "body": "123321",
            "videoId": "26113662",
            "position": "2719",
            "type": "douga",
            "id": "31224739",
            "subChannelId": "1",
            "subChannelName": "动画",
        }
        response = session.get(url=f"https://www.acfun.cn/v/ac{self.contentid}")
        video_id = re.findall(r'"currentVideoId":(\d+),', response.text)
        sub_channel = re.findall(r'{subChannelId:(\d+),subChannelName:"([\u4e00-\u9fa5]+)"}', response.text)
        if video_id:
            data["videoId"] = video_id[0]
            data["subChannelId"] = sub_channel[0][0]
            data["subChannelName"] = sub_channel[0][1]
        res = session.post(url=url, data=data).json()
        msg = "弹幕成功" if res.get("result") == 0 else "弹幕失败"
        return {"name": "弹幕任务", "value": msg}

    def throwbanana(self, session):
        url = "https://www.acfun.cn/rest/pc-direct/banana/throwBanana"
        data = {"resourceId": self.contentid, "count": "1", "resourceType": "2"}
        res = session.post(url=url, data=data).json()
        msg = "投🍌成功" if res.get("result") == 0 else "投🍌失败"
        return {"name": "香蕉任务", "value": msg}

    def like(self, session):
        like_url = "https://kuaishouzt.com/rest/zt/interact/add"
        unlike_url = "https://kuaishouzt.com/rest/zt/interact/delete"
        body = (
            f"kpn=ACFUN_APP&kpf=PC_WEB&subBiz=mainApp&interactType=1&"
            f"objectType=2&objectId={self.contentid}&acfun.midground.api_st={self.st}&"
            f"extParams%5BisPlaying%5D=false&extParams%5BshowCount%5D=1&extParams%5B"
            f"otherBtnClickedCount%5D=10&extParams%5BplayBtnClickedCount%5D=0"
        )
        res = session.post(url=like_url, data=body).json()
        session.post(url=unlike_url, data=body)
        msg = "点赞成功" if res.get("result") == 1 else "点赞失败"
        return {"name": "点赞任务", "value": msg}

    def share(self, session, cookies):
        url = "https://api-ipv6.acfunchina.com/rest/app/task/reportTaskAction?taskType=1&market=tencent&product=ACFUN_APP&appMode=0"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        response = session.get(url=url, headers=headers, cookies=cookies, verify=False)
        if response.json().get("result") == 0:
            msg = "分享成功"
        else:
            msg = "分享失败"
        return {"name": "分享任务", "value": msg}

    @staticmethod
    def fetch_info(session):
        """查一次用户信息：既确认登录态，也顺手拿到昵称。

        ``result != 0`` 就是没登录（Cookie 过期时最常见），返回 None 让上层报错 ——
        不能静默往下走，否则签到失败只会得到一句莫名其妙的「签到失败」。
        """
        try:
            res = session.get(url=INFO_URL).json()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(res, dict) or res.get("result") != 0:
            return None
        info = res.get("info")
        return info if isinstance(info, dict) else {}

    def info_name(self) -> str:
        """用户信息里的昵称。字段名各端不一，按常见几种依次取。"""
        for key in ("userName", "username", "nickName", "nickname", "name"):
            value = str((self.info or {}).get(key) or "").strip()
            if value:
                return value
        return ""

    def get_info(self):
        info = self.info or {}
        if not info:
            return [{"name": "当前等级", "value": "查询失败"}]
        return [
            {"name": "当前等级", "value": info.get("level")},
            {"name": "持有香蕉", "value": info.get("banana")},
        ]

    def main(self):
        if not self.cookie:
            raise ValueError("必须提供 AcFun Cookie（至少包含 acPasstoken 和 auth_key）")
        pairs = self.parse_cookie(self.cookie)
        if not pairs.get("acPasstoken"):
            raise ValueError("Cookie 里没有 acPasstoken，请重新登录后整块复制")
        if not pairs.get("auth_key"):
            raise ValueError("Cookie 里没有 auth_key，请重新登录后整块复制")

        session = requests.session()
        session.headers.update(
            {
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.70",
                "Referer": "https://www.acfun.cn/",
            }
        )
        requests.utils.add_dict_to_cookiejar(session.cookies, pairs)
        # ac_username 是网址编码过的昵称，离线就能读出来
        self.username = unquote(pairs.get("ac_username") or "").strip()
        self.info = self.fetch_info(session) or {}
        if not self.info:
            raise ValueError("Cookie 已失效（查不到用户信息），请重新登录后复制新的 Cookie")
        self.username = self.username or self.info_name()

        self.get_video(session=session)
        self.get_token(session=session)
        sign_msg = self.sign(session=session)
        like_msg = self.like(session=session)
        danmu_msg = self.danmu(session=session)
        throwbanana_msg = self.throwbanana(session=session)
        info_msg = self.get_info()
        msg = [
            {"name": "帐号信息", "value": self.account_label},
            sign_msg,
            like_msg,
            danmu_msg,
            throwbanana_msg,
            *info_msg,
        ]
        return "\n".join(f"{one.get('name')}: {one.get('value')}" for one in msg)


if __name__ == "__main__":
    with open(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json"),
        encoding="utf-8",
    ) as f:
        datas = json.loads(f.read())
    _check_item = datas.get("ACFUN", [])[0]
    print(AcFun(check_item=_check_item).main())
