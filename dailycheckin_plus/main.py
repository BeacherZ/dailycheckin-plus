import argparse
import json
import os
import time
from datetime import datetime, timedelta

from dailycheckin_plus.__version__ import __version__
from dailycheckin_plus.configs import checkin_map, get_checkin_info, get_notice_info
from dailycheckin_plus.utils.message import PROJECT_NAME, PROJECT_URL, account_hint, push_message, render_site_block


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--include", nargs="+", help="任务执行包含的任务列表")
    parser.add_argument("--exclude", nargs="+", help="任务执行排除的任务列表")
    return parser.parse_args()


# 找到的 config.json 路径。V2EX 这类「cookie 会滑动续期」的站点签完要把新 cookie
# 写回去，所以得知道配置文件在哪。找不到就留空，写回逻辑自己会安静跳过。
CONFIG_PATH = ""


def check_config(task_list):
    global CONFIG_PATH
    config_path = None
    config_path_list = []
    for one_path in [
        "/ql/scripts/config.json",
        "config.json",
        "../config.json",
        "./config/config.json",
        "../config/config.json",
        "/config.json",
    ]:
        _config_path = os.path.join(os.getcwd(), one_path)
        if os.path.exists(_config_path):
            config_path = os.path.normpath(_config_path)
            break
        config_path_list.append(os.path.normpath(os.path.dirname(_config_path)))
    if config_path:
        CONFIG_PATH = config_path
        print("使用配置文件路径:", config_path)
        with open(config_path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception:
                print("Json 格式错误，请检查 config.json 文件格式是否正确！")
                return False, False
        try:
            notice_info = get_notice_info(data=data)
            _check_info = get_checkin_info(data=data)
            check_info = {}
            for one_check in checkin_map.keys():
                if one_check in task_list:
                    if _check_info.get(one_check.lower()):
                        for _index, check_item in enumerate(_check_info.get(one_check.lower(), [])):
                            if "xxxxxx" not in str(check_item) and "多账号" not in str(check_item):
                                if one_check.lower() not in check_info.keys():
                                    check_info[one_check.lower()] = []
                                # 带上配置出处与账号序号：站点模块签完后要能把续期后的
                                # cookie 写回 config.json 里对应的那一项（目前只有 V2EX 用）。
                                check_item = dict(check_item)
                                check_item["_config_path"] = config_path
                                check_item["_site_key"] = one_check.upper()
                                check_item["_account_index"] = _index
                                check_info[one_check.lower()].append(check_item)
            return notice_info, check_info
        except Exception as e:
            print(e)
            return False, False
    else:
        print("未找到 config.json 配置文件\n请在下方任意目录中添加「config.json」文件:\n" + "\n".join(config_path_list))
        return False, False


def checkin():
    start_time = time.time()
    utc_time = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"当前时间: {utc_time}\n当前版本: {__version__}")
    args = parse_arguments()
    include = args.include
    exclude = args.exclude
    if not include:
        include = list(checkin_map.keys())
    else:
        include = [one for one in include if one in checkin_map.keys()]
    if not exclude:
        exclude = []
    else:
        exclude = [one for one in exclude if one in checkin_map.keys()]
    task_list = list(set(include) - set(exclude))
    notice_info, check_info = check_config(task_list)
    if check_info:
        task_name_str = "\n".join(
            [f"「{checkin_map.get(one.upper())[0]}」账号数 : {len(value)}" for one, value in check_info.items()]
        )
        print(f"\n---------- 本次执行签到任务如下 ----------\n\n{task_name_str}\n\n")
        content_list = []
        for one_check, check_list in check_info.items():
            check_name, check_func = checkin_map.get(one_check.upper())
            print(f"----------开始执行「{check_name}」签到----------")
            accounts = []
            for index, check_item in enumerate(check_list):
                # 有些站点的输出里没有用户名，多账号时只能靠配置里的身份字段区分
                hint = account_hint(check_item)
                try:
                    msg = check_func(check_item).main()
                    print(f"第 {index + 1} 个账号: ✅✅✅✅✅")
                except Exception as e:
                    msg = str(e)
                    print(f"第 {index + 1} 个账号: ❌❌❌❌❌\n{e}")
                accounts.append({"hint": hint, "text": msg})
            # 单账号保持原样；多账号加序号、段间用短分隔行，和站点之间区分开
            content_list.append(render_site_block(check_name, accounts))
        print("\n\n")
        # 结尾只报本项目的版本号。以前这里还会去 PyPI 查 dailycheckin 的「最新版本」，
        # 那是上游的包，脱钩后已经跟本项目无关 —— 镜像是最新的也照样显示 25.12.9，
        # 白白让人以为装了个老版本。要不要更新由面板设置页判断（它比的是镜像自带的版本标签）。
        content_list.append(
            f"开始时间: {utc_time}\n"
            f"任务用时: {int(time.time() - start_time)} 秒\n"
            f"当前版本: {__version__}\n"
            f"项目地址: {PROJECT_URL}"
        )
        # 一次任务只推一条：站点结果 + 上面的任务元信息拼在同一条消息里，
        # 由 message.push_message 负责加标题、插分隔行。
        # 标题里带项目名，方便一眼认出这条通知是谁发的；不带日期，日期正文里「开始时间」有。
        push_message(
            content_list=content_list,
            notice_info=notice_info,
            title=f"{PROJECT_NAME} 每日签到",
        )
        return


if __name__ == "__main__":
    checkin()
