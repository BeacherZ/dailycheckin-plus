#!/usr/bin/env python3
"""通知模块自测：一次签到**恒定只发一条**消息，带标题、带分隔行、结尾是项目地址。

引擎依赖 requests，这里塞一个假的进 sys.modules，只关心「发了几条、内容长什么样」。
纯标准库，CI 里直接跑：python3 tools/test_notice.py
"""
import importlib.util
import json
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "dailycheckin_plus", "utils", "message.py")
MAIN = os.path.join(ROOT, "dailycheckin_plus", "main.py")

# --------------------------------------------------------------------------- #
# 假 requests：把所有出网调用记下来
# --------------------------------------------------------------------------- #
CALLS = []


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {}


def _record(name):
    def fn(*args, **kwargs):
        url = kwargs.get("url") or (args[0] if args else "")
        CALLS.append({"fn": name, "url": url, **kwargs})
        return _Resp()

    return fn


fake = types.ModuleType("requests")
fake.post = _record("post")
fake.get = _record("get")
sys.modules["requests"] = fake

spec = importlib.util.spec_from_file_location("dc_message", MOD)
message = importlib.util.module_from_spec(spec)
spec.loader.exec_module(message)

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name if cond else f"{name} :: {detail}")
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else " :: " + str(detail)))


# --------------------------------------------------------------------------- #
# 与 dailycheckin_plus/main.py 里 content_list 的构造方式保持一致
# --------------------------------------------------------------------------- #
TITLE = f"{message.PROJECT_NAME} 每日签到"
# 结尾那行的版本号直接读引擎里的，别在这儿写死 —— 一涨版本这条自测就假红
with open(os.path.join(ROOT, "dailycheckin_plus", "__version__.py"), encoding="utf-8") as _f:
    VERSION = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", _f.read()).group(1)
CONTENT = [
    "「V2EX 论坛」\n帐号信息: ABC\n今日签到: 20261016 的每日登录奖励 13 铜币\n帐号余额: 38259.34",
    "「哔哩哔哩」\n第 1 个账号: ✅✅✅✅✅",
    f"开始时间: 2026-10-16 09:00:01\n任务用时: 0 秒\n当前版本: {VERSION}\n项目地址: {message.PROJECT_URL}",
]

TG = {"tg_bot_token": "BOT", "tg_user_id": "123"}


def run(notice_info=None, content=None, title=TITLE):
    CALLS.clear()
    message.push_message(
        content_list=list(content if content is not None else CONTENT),
        notice_info=dict(notice_info or TG),
        title=title,
    )
    return [c for c in CALLS if "telegram" in str(c.get("url"))]


print("=" * 66)
print("通知模块：一条消息、带标题、结尾是项目地址")

# 1) 只发一条，且带标题
tg = run()
check("TG 只发一条", len(tg) == 1, f"实际 {len(tg)} 条")
body = tg[0]["data"]["text"] if tg else ""
check("正文首行是标题", body.startswith(f"【{TITLE}】"), repr(body[:60]))
check("标题与正文之间空一行", f"【{TITLE}】\n\n「V2EX 论坛」" in body, repr(body[:80]))
check("站点之间用分隔行隔开", body.count(message.SEPARATOR.strip()) == 2, body.count(message.SEPARATOR.strip()))
check("三个区块都在", all(k in body for k in ("V2EX 论坛", "哔哩哔哩", "开始时间")), body[:200])

# 2) 结尾的项目地址指向本项目，而不是上游
check("项目地址指向本项目", message.PROJECT_URL in body, message.PROJECT_URL)
check("正文里不再出现上游仓库地址", "Sitoi/dailycheckin" not in body, body[-120:])

# 2.1) 项目名：常量、通知标题、企业微信卡片署名都得是新名字
check("PROJECT_NAME 是 dailycheckin-plus", message.PROJECT_NAME == "dailycheckin-plus", message.PROJECT_NAME)
check("项目地址是新仓库", message.PROJECT_URL.endswith("/dailycheckin-plus"), message.PROJECT_URL)
check("兜底标题带项目名", message.NOTICE_TITLE == "dailycheckin-plus 每日签到", message.NOTICE_TITLE)
check("标题里有项目名", message.PROJECT_NAME in TITLE, TITLE)
check("正文首行的标题带项目名", body.startswith(f"【{message.PROJECT_NAME}"), repr(body[:60]))
check("通知里不再出现旧项目名", "dailycheckin-compose" not in body, body[:120])

# 3) 老配置里的 MERGE_PUSH=false 也必须被忽略 —— 它正是「还是两条」的元凶之一
for legacy in ("false", "0", "no", ""):
    tg = run({**TG, "merge_push": legacy})
    check(f"忽略旧配置 MERGE_PUSH={legacy!r}，仍是一条", len(tg) == 1, f"实际 {len(tg)} 条")

# 4) 没传标题时不该凭空冒出一对空括号
CALLS.clear()
message.push_message(content_list=["「A」\nok"], notice_info=dict(TG), title="")
plain = [c for c in CALLS if "telegram" in str(c.get("url"))][0]["data"]["text"]
check("不传标题就没有标题行", plain == "「A」\nok", repr(plain))

# 5) 空区块直接丢掉，不会留下空行
tg = run(content=["「A」\nok", "", None, "  ", "「B」\nok"])
text = tg[0]["data"]["text"]
check("空区块被丢弃，分隔行只剩一个", text.count(message.SEPARATOR.strip()) == 1, repr(text))
check("空区块不产生悬空标题", text.startswith(f"【{TITLE}】\n\n「A」"), repr(text[:60]))

# 6) 自带标题能力的渠道要收到标题
CALLS.clear()
message.push_message(
    content_list=list(CONTENT),
    notice_info={"pushplus_token": "T", "gotify_url": "https://g", "gotify_token": "gt", "ntfy_topic": "topic", "bark_url": "https://bark"},
    title=TITLE,
)
pushplus = [c for c in CALLS if "pushplus" in str(c.get("url"))]
gotify = [c for c in CALLS if "/message?token=" in str(c.get("url"))]
ntfy = [c for c in CALLS if "/topic" in str(c.get("url", ""))]
pp_title = json.loads(pushplus[0]["data"])["title"] if pushplus else None
check("PushPlus 用上了标题", pp_title == TITLE, f"实际 {pp_title!r}")
check("Gotify 用上了标题", gotify and gotify[0]["data"]["title"] == TITLE, gotify[0]["data"] if gotify else "没有调用")
check("ntfy 用上了标题", ntfy and "Title" in ntfy[0].get("headers", {}), ntfy[0].get("headers") if ntfy else "没有调用")

# 7) 企业微信应用消息里的回跳地址也必须是本项目
CALLS.clear()
message.push_message(
    content_list=list(CONTENT),
    notice_info={"qywx_corpid": "c", "qywx_agentid": "1", "qywx_corpsecret": "s", "qywx_touser": "u"},
    title=TITLE,
)
qywx = [c for c in CALLS if "qyapi.weixin.qq.com" in str(c.get("url"))]
check("企业微信卡片回跳本项目", qywx and message.PROJECT_URL in str(qywx[-1].get("data")), str(qywx[-1].get("data"))[:120] if qywx else "没有调用")
check("企业微信卡片署名也是新项目名", qywx and message.PROJECT_NAME in str(qywx[-1].get("data")), str(qywx[-1].get("data"))[:120] if qywx else "没有调用")

# 8) main.py 里的元信息块必须用同一个常量，别再写死上游地址
main_src = open(MAIN, encoding="utf-8").read()
check("main.py 结尾用 PROJECT_URL", "项目地址: {PROJECT_URL}" in main_src, main_src[-300:])
check("main.py 不再硬编码上游地址", "Sitoi/dailycheckin\"" not in main_src, "main.py 里还有写死的上游链接")
check("main.py 的标题拼上了项目名", 'title=f"{PROJECT_NAME} 每日签到' in main_src, main_src[-400:])
# 标题里带日期的话，同一天跑两次会看到两个不同标题；日期正文「开始时间」里已经有了
check("标题不带日期（正文里有开始时间）", "每日签到 ·" not in main_src and "utc_time[:10]" not in main_src, "main.py 的标题还在拼日期")

# 8.1) 结尾只报本项目的版本 —— 上游那个「最新版本」是 PyPI 上的 dailycheckin 包，
#      脱钩后已经无关：镜像明明是最新的，通知里却显示 25.12.9，看着像装了个老版本。
check("main.py 不再去 PyPI 查上游版本", "pypi.org" not in main_src, "main.py 里还有 PyPI 请求")
check("通知结尾没有「最新版本」这一行", "最新版本: {" not in main_src, "main.py 还在拼最新版本")
check("通知结尾只报本项目版本", "当前版本: {__version__}" in main_src, main_src[-300:])
check("main.py 不为一个版本号去连网（requests 已摘掉）", "import requests" not in main_src, "main.py 还 import 了 requests")
body = tg[0]["data"]["text"] if tg else ""
check("正文里没有上游的版本号", "25.12.9" not in body, body[-120:])

# 9) 「校验提醒」是另一条通知：标题固定，不带日期也不带站点数
notify_src = open(os.path.join(ROOT, "panel", "autoverify.py"), encoding="utf-8").read()
check("失效通知标题是「<项目名> 校验提醒」", '} 校验提醒"' in notify_src, "autoverify.py 里找不到标题格式")
check("失效通知也复用引擎渲染（分隔行/标题不会跑偏）",
      '"blocks"' in notify_src and "notify_push.py" in notify_src, "推送没走子进程 + 引擎渲染")

# 10) 两级分隔行 + 多账号编号：站点之间 16 个「—」，同站多账号之间 8 个
check("站点分隔行是 16 个破折号", message.SITE_SEPARATOR.strip() == "—" * 16, len(message.SITE_SEPARATOR.strip()))
check("账号分隔行是 8 个破折号", message.ACCOUNT_SEPARATOR.strip() == "—" * 8, len(message.ACCOUNT_SEPARATOR.strip()))
check("SEPARATOR 仍指向站点分隔行（旧调用不跑偏）", message.SEPARATOR == message.SITE_SEPARATOR, "别名断了")

single = message.render_site_block("V2EX 论坛", [{"hint": "ABC", "text": "帐号信息: ABC\n签到成功"}])
check("单账号：标题一行 + 账号信息一行", single == "「V2EX 论坛」\n账号信息: ABC\n签到成功", repr(single))
check("模块写的「帐号信息」被统一成「账号信息」", "帐号信息" not in single, repr(single))
check("单账号没身份时用面板备注", message.render_site_block("NodeSeek", [{"hint": "家里", "text": "签到结果: ✅"}])
      == "「NodeSeek」\n账号信息: 家里\n签到结果: ✅",
      repr(message.render_site_block("NodeSeek", [{"hint": "家里", "text": "签到结果: ✅"}])))
check("模块给了身份就优先用模块的", message.render_site_block("B", [{"hint": "家里", "text": "账号信息: 真名\nok"}])
      == "「B」\n账号信息: 真名\nok", "备注不该盖掉真实用户名")
check("「未知用户」这种废话不算身份，退回备注",
      message.render_site_block("百度贴吧", [{"hint": "大号", "text": "账号信息: 未知用户\n签到成功"}])
      == "「百度贴吧」\n账号信息: 大号\n签到成功", "未知用户应被忽略")

multi = message.render_site_block(
    "NodeSeek",
    [{"hint": "", "text": "签到结果: ✅"}, {"hint": "", "text": "签到结果: ✅"}],
)
check("多账号每段标题都是同一行「站名」", multi.count("「NodeSeek」") == 2, repr(multi))
check("多账号都写账号信息行", multi.count("账号信息: ") == 2, repr(multi))
check("没有身份时账号信息退回序号", "账号信息: 第 1 个账号" in multi and "账号信息: 第 2 个账号" in multi, repr(multi))
check("多账号之间用短的 8 个破折号", message.ACCOUNT_SEPARATOR in multi and message.SITE_SEPARATOR not in multi, repr(multi))

with_hint = message.render_site_block(
    "AcFun",
    [{"hint": "13800000001", "text": "签到信息: 今日已签到"}, {"hint": "13800000002", "text": "签到信息: 今日已签到"}],
)
check("多账号的备注写在账号信息行", "账号信息: 13800000001" in with_hint and "账号信息: 13800000002" in with_hint, repr(with_hint[:80]))
check("标题行不夹带括号", "「AcFun」（" not in with_hint, repr(with_hint[:40]))

check("空输出时账号信息照写、正文写（无输出）",
      message.render_site_block("A", [{"hint": "", "text": " "}, {"hint": "", "text": "ok"}])
      == "「A」\n账号信息: 第 1 个账号\n（无输出）" + message.ACCOUNT_SEPARATOR + "「A」\n账号信息: 第 2 个账号\nok",
      repr(message.render_site_block("A", [{"hint": "", "text": " "}, {"hint": "", "text": "ok"}])))
check("没配账号时返回空串", message.render_site_block("A", []) == "", "空站点应整体丢弃")

check("account_hint 优先用面板备注", message.account_hint({"remark": "公司号", "username": "ABC"}) == "公司号")
check("account_hint 读 username", message.account_hint({"username": "ABC", "password": "x"}) == "ABC")
check("account_hint 读 phone", message.account_hint({"phone": "13800000001"}) == "13800000001")
check("account_hint 不看 cookie/token/password",
      message.account_hint({"cookie": "session=abc", "token": "t", "password": "p"}) == "",
      message.account_hint({"cookie": "session=abc", "token": "t", "password": "p"}))
check("account_hint 对非 dict 不炸", message.account_hint(None) == "" and message.account_hint("x") == "")

# 同一站点多账号不能被 16 个的站点分隔行拆开
check("main.py 按站点聚合后再渲染", "render_site_block(check_name, accounts)" in main_src, "main.py 没接上")
check("main.py 从配置取身份字段", "account_hint(check_item)" in main_src, "main.py 没取 hint")

print("=" * 66)
print(f"通过 {len(passed)}  失败 {len(failed)}")
for one in failed:
    print("  ✗", one)
sys.exit(1 if failed else 0)
