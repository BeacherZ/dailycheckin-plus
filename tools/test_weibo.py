#!/usr/bin/env python3
"""微博超话站点模块自测（离线，不联网）。

锁住这几件容易在后续改动里被悄悄改坏的事：

1. **卡片解析**：``card_type`` 在微博不同接口里既有数字也有字符串，必须都认；
   名字取 ``title_sub``，containerid 从 ``scheme`` 的查询串里取；
   「已签 / 明日再来」要判定为 done 且 scheme 置 None。
2. **cookie**：``SUB`` 的值可能含 ``=``，必须用 partition 而不是 ``split("=")[1]``。
3. **超话名匹配**：剥掉【】与「超话」后缀后**全等**，不做子串 —— 「周杰伦」不能命中
   「周杰伦电影速报」。
4. **返回码**：ok=1 成功、已签到文案也算成功、402004 给验证链接、402003 标记重试。
5. **失败语义**：全部超话都失败时要抛异常（面板才会标红），部分失败只写进结果。

跑法：python3 tools/test_weibo.py（不需要 requests，也不需要网络）
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "dailycheckin_plus", "weibo", "main.py")

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f"  :: {detail}" if not cond and detail else ""))


def section(title):
    print("\n" + "=" * 70)
    print(title)


# --------------------------------------------------------------------------- #
# 假 requests + 假 CheckIn
# --------------------------------------------------------------------------- #
class FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = {}
        self.proxies = {}

    def get(self, url, **kwargs):
        raise AssertionError("自测里不该真的发请求：" + url)


fake_requests = types.ModuleType("requests")
fake_requests.Session = FakeSession
fake_requests.utils = types.SimpleNamespace(add_dict_to_cookiejar=lambda jar, data: jar.update(data))
sys.modules["requests"] = fake_requests

fake_dc = types.ModuleType("dailycheckin_plus")


class _CheckIn:
    name = "Base"


fake_dc.CheckIn = _CheckIn
sys.modules["dailycheckin_plus"] = fake_dc

spec = importlib.util.spec_from_file_location("weibo_main", MOD)
wb_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wb_mod)
Weibo = wb_mod.Weibo

# 发一条超话之间要 sleep 3~6 秒，自测里全部跳过
wb_mod.time.sleep = lambda *_a, **_k: None

COOKIE = "SUB=_2A25JqY8VDeRhGedH6FoU8CjMwj2IHXVoabc==; SUBP=003J5gHabc; _T_WM=xyz123"


def card(name, page_id, button="签到", level="等级LV.7", has_scheme=True, card_type="8"):
    scheme = (
        "/api/2/page/button?request_url=http%3A%2F%2Fi.huati.weibo.com%2Fmobile"
        f"%2Fsuper%2Factive_checkin%3Fpageid%3D{page_id}&pageid={page_id}"
        if has_scheme
        else None
    )
    btn = {"type": "default", "name": button}
    if scheme:
        btn["scheme"] = scheme
    return {
        "card_type": card_type,
        "itemid": f"follow_super_{page_id}",
        "scheme": f"sinaweibo://pageinfo?containerid=100808{page_id}&extparam=x",
        "title_sub": name,
        "desc1": level,
        "buttons": [btn],
    }


def wb_stub(topics="all", max_topics=20, pages=None, login=True, st="ST", sign=None):
    """造一个不联网的 Weibo 实例：get_json 按 URL 分派。"""
    wb = Weibo.__new__(Weibo)
    wb.topics = Weibo.parse_topics(topics)
    wb.sign_all = "all" in wb.topics
    wb.max_pages = 5
    wb.max_topics = max_topics
    wb.timeout = 1
    wb.skipped = 0
    pages = pages if pages is not None else [[card("周杰伦", "4665")]]
    state = {"page": 0, "sign": 0}

    def default_sign(url):
        state["sign"] += 1
        return {"ok": 1, "msg": "签到成功"}

    def fake(url, params=None):
        if url == wb_mod.CONFIG_URL:
            return {"data": {"login": login, "st": st}}
        if url == wb_mod.CONTAINER_URL:
            index = state["page"]
            state["page"] += 1
            group = pages[index] if index < len(pages) else []
            return {
                "ok": 1,
                "data": {
                    "cards": [{"card_group": group}],
                    "cardlistInfo": {"since_id": ("1" if index + 1 < len(pages) else "")},
                },
            }
        # 其余都是签到 URL
        return (sign or default_sign)(url)

    wb.get_json = fake
    return wb


# --------------------------------------------------------------------------- #
section("1) cookie：SUB 以 == 结尾也不能被截断")

parsed = Weibo.parse_cookie(COOKIE)
check("SUB 完整保留", parsed.get("SUB") == "_2A25JqY8VDeRhGedH6FoU8CjMwj2IHXVoabc==", repr(parsed.get("SUB")))
check("不会被截断成第一个 = 之前", parsed.get("SUB") != COOKIE.split("SUB=")[1].split(";")[0].split("=")[0])
check("其它字段也解析正常", parsed.get("SUBP") == "003J5gHabc" and parsed.get("_T_WM") == "xyz123")
check("多行粘贴也认", Weibo.parse_cookie("SUB=a\r\nSUBP=b").get("SUBP") == "b")
check("空片段被忽略", Weibo.parse_cookie("a=1;; ;b=2") == {"a": "1", "b": "2"})

for bad, expect in (({"cookie": ""}, "必须提供"), ({"cookie": "SUBP=abc"}, "未找到 SUB")):
    raised = ""
    try:
        Weibo(bad)
    except Exception as exc:  # noqa: BLE001
        raised = str(exc)
    check(f"{bad or '空配置'} → 报错含「{expect}」", expect in raised, raised)

# --------------------------------------------------------------------------- #
section("2) 配置：默认签全部，受最多签到数限制")

check("空值默认 all", Weibo.parse_topics("") == ["all"])
check("all 就是签全部", "all" in Weibo.parse_topics("all"))
check("中文逗号也认", Weibo.parse_topics("周杰伦， 林俊杰") == ["周杰伦", "林俊杰"])
check("list 也认", Weibo.parse_topics(["周杰伦", " 林俊杰 "]) == ["周杰伦", "林俊杰"])
check("不再写死某个明星当默认", Weibo.parse_topics("") != ["周杰伦"])

default_wb = Weibo({"cookie": COOKIE})
check("默认 max_topics 是 20（防跑飞）", default_wb.max_topics == 20, str(default_wb.max_topics))
check("默认就是签全部", default_wb.sign_all is True)

# --------------------------------------------------------------------------- #
section("3) 卡片解析：card_type 兼容数字与字符串")

one = Weibo.parse_topic_card(card("周杰伦", "4665"))
check("字符串 card_type=8 能解析", one is not None and one["name"] == "周杰伦", str(one))
check("containerid 从 scheme 里取", one["id"] == "1008084665", one.get("id"))
check("未签时 scheme 保留且 done=False", bool(one["scheme"]) and one["done"] is False, str(one))
check("等级被保留", one["level"] == "等级LV.7", str(one.get("level")))

num = Weibo.parse_topic_card(card("周杰伦", "4665", card_type=8))
check("数字 card_type=8 也能解析（微博有的接口给数字）", num is not None, str(num))
check("其它 card_type 忽略", Weibo.parse_topic_card(card("x", "1", card_type="9")) is None)
check("没有 title_sub 的卡片忽略", Weibo.parse_topic_card({"card_type": "8", "scheme": "sinaweibo://pageinfo?containerid=1008081"}) is None)

done_card = Weibo.parse_topic_card(card("崩坏星穹铁道", "abc", button="已签"))
check("「已签」判定为 done", done_card["done"] is True, str(done_card))
check("done 时 scheme 置 None（按钮已经换了）", done_card["scheme"] is None, str(done_card.get("scheme")))
check("「明日再来」也判定为 done", Weibo.parse_topic_card(card("x", "1", button="明日再来"))["done"] is True)

no_btn = Weibo.parse_topic_card(card("空按钮", "9", has_scheme=False))
check("没有按钮 scheme 时算 done（避免反复点）", no_btn["done"] is True and no_btn["scheme"] is None, str(no_btn))

# --------------------------------------------------------------------------- #
section("4) 超话名匹配：剥装饰，但绝不子串命中")

wb = Weibo({"cookie": COOKIE, "topics": "周杰伦"})
check("精确匹配", wb.match("周杰伦") is True)
check("带【】能匹配", wb.match("【周杰伦】") is True)
check("带「超话」后缀能匹配", wb.match("周杰伦超话") is True)
check("带空格能匹配", wb.match("周 杰 伦") is True)
check("不误匹配别人", wb.match("林俊杰") is False)
check("不误匹配「周杰伦电影」", wb.match("周杰伦电影") is False)
check("不误匹配「周杰伦电影速报」", wb.match("周杰伦电影速报") is False)
check("空名字不匹配", wb.match("") is False)

all_wb = Weibo({"cookie": COOKIE, "topics": "all"})
check("all 模式下 match 不生效（走全量分支）", all_wb.match("周杰伦") is False)

# --------------------------------------------------------------------------- #
section("5) 签到返回码")

def sign_case(payload):
    obj = Weibo.__new__(Weibo)
    obj.get_json = lambda url, params=None: payload
    return obj.sign_topic({"scheme": "/api/2/page/button?pageid=1", "name": "周杰伦"}, "ST9")


r = sign_case({"ok": 1, "msg": "签到成功"})
check("ok=1 → 成功", r["success"] is True and r["retry"] is False, str(r))
check("ok=1 无 msg 也算成功", sign_case({"ok": 1})["success"] is True)
check("「已签到」文案算成功", sign_case({"ok": 0, "msg": "你今天已经签到过了"})["success"] is True)
check("已签到的文案统一成「今日已签到」", sign_case({"ok": 0, "msg": "你今天已经签到过了"})["message"] == "今日已签到")

risk = sign_case({"result": "402004", "scheme": "https://passport.weibo.com/verify?x=1"})
check("402004 → 失败但带验证链接", risk["success"] is False and risk["verify_url"].startswith("https://"), str(risk))
check("402004 文案提示需要人工验证", "验证" in risk["message"], risk["message"])

busy = sign_case({"errno": "402003", "msg": "too frequent"})
check("402003 → 标记重试", busy["retry"] is True and busy["success"] is False, str(busy))

other = sign_case({"ok": 0, "msg": "系统繁忙"})
check("其它失败 → success=False 且带原话", other["success"] is False and "系统繁忙" in other["message"], str(other))

captured = {}


def capture(url, params=None):
    captured["url"] = url
    return {"ok": 1, "msg": "签到成功"}


obj = Weibo.__new__(Weibo)
obj.get_json = capture
obj.sign_topic({"scheme": "/api/2/page/button?pageid=1", "name": "周杰伦"}, "ST9")
check("相对 scheme 补上 m.weibo.cn 域名", captured["url"].startswith("https://m.weibo.cn/api/2/page/button"), captured["url"])
check("签到 URL 带上 st", "st=ST9" in captured["url"], captured["url"])

obj2 = Weibo.__new__(Weibo)
obj2.get_json = capture
obj2.sign_topic({"scheme": None, "name": "周杰伦"}, "ST")
check("没有 scheme 时不发请求、直接说已签", captured["url"].startswith("https://m.weibo.cn/api/2"), captured["url"])

# --------------------------------------------------------------------------- #
section("6) run()：登录态、没关注、上限、失败语义")

raised = ""
try:
    wb_stub(login=False).run()
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("login=False 抛异常", bool(raised), "没有抛异常")
check("异常文案提示 cookie 失效", "失效" in raised, raised)

raised = ""
try:
    wb_stub(pages=[[]]).run()
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("取不到任何超话时抛异常", "没有取到" in raised, raised)

raised = ""
try:
    wb_stub(topics="林俊杰", pages=[[card("周杰伦", "4665")]]).run()
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("配了没关注的超话、且一个都没签 → 抛异常", bool(raised), "没有抛异常")
check("异常里点名是哪个超话", "林俊杰" in raised, raised)
check("异常里说明当前关注了几个", "1 个" in raised, raised)

outcome = wb_stub(topics="【周杰伦】", pages=[[card("周杰伦", "4665"), card("林俊杰", "777")]]).run()
check("指定超话时只签匹配上的", [one["name"] for one in outcome["items"]] == ["周杰伦"], str(outcome["items"]))
check("指定超话不会把别人算进来", outcome["skipped"] == 0, str(outcome["skipped"]))

outcome = wb_stub(max_topics=2, pages=[[card("A", "1"), card("B", "2"), card("C", "3")]]).run()
check("all 模式按 max_topics 截断", len(outcome["items"]) == 2, str(outcome["items"]))
check("被截断的数量记在 skipped 里", outcome["skipped"] == 1, str(outcome["skipped"]))

outcome = wb_stub(pages=[[card("已签的", "1", button="已签")]]).run()
check("今天已签的超话不再发请求，但算成功", outcome["items"][0]["success"] is True and outcome["items"][0]["message"] == "今日已签到", str(outcome["items"]))

# 第一个已签（不发请求）+ 第二个失败 → 属于「部分失败」，不该抛
def only_second_fails(url):
    return {"ok": 0, "msg": "操作失败"}


outcome = wb_stub(
    pages=[[card("已签的", "1", button="已签"), card("会失败的", "2")]],
    sign=only_second_fails,
).run()
check("部分失败时整体不抛", len(outcome["items"]) == 2, str(outcome))
check("失败的那条标 False 且带原因",
      outcome["items"][1]["success"] is False and "操作失败" in outcome["items"][1]["message"], str(outcome["items"][1]))

raised = ""
try:
    wb_stub(pages=[[card("会失败的", "2")]], sign=only_second_fails).run()
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("全部失败要抛异常（否则面板会把失败当成功）", "全部超话签到失败" in raised, raised)

# --------------------------------------------------------------------------- #
section("7) main()：人有事看得懂的输出")

out = wb_stub(pages=[[card("周杰伦", "4665")]])
out.nickname = lambda: "测试用户"
text = out.main()
check("有账号信息行", "账号信息: 测试用户" in text, text)
check("有超话名", "周杰伦" in text, text)
check("成功打勾", "✅" in text, text)
check("等级写在标题里", "等级LV.7" in text, text)

out2 = wb_stub(max_topics=1, pages=[[card("A", "1"), card("B", "2")]])
out2.nickname = lambda: "u"
text2 = out2.main()
check("被 max_topics 截断时给出提示", "还有 1 个" in text2, text2)

print("\n" + "=" * 70)
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败项：")
    for one in failed:
        print("  - " + one)
sys.exit(1 if failed else 0)
