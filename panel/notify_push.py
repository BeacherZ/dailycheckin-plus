"""把一条通知按 config.json 里配置的渠道推出去。

**由面板以子进程方式调用**，不要 import 进面板进程：面板是零依赖的（纯标准库），
而引擎要 requests / pycryptodome。引擎装不上时面板还得能起来，所以推送这一步
必须隔离在子进程里。

用法（stdin 是 JSON）：
    echo '{"title":"...","blocks":["..."],"..."}' | python panel/notify_push.py config.json

blocks 是「消息区块」列表，标题、分隔行、结尾项目地址都由引擎的
`message.push_message` 统一处理 —— 和签到通知走同一条渲染路径，样式不会跑偏。
"""

import json
import sys


def main() -> int:
    payload = json.load(sys.stdin)
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/config.json"
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    from dailycheckin_plus.configs import get_notice_info
    from dailycheckin_plus.utils.message import push_message
    import dailycheckin_plus

    # 打一行模块路径：万一 sys.path 让 site-packages 里的旧引擎抢先，日志里一眼能看出来
    print("引擎模块:", dailycheckin_plus.__file__)

    blocks = [str(one) for one in (payload.get("blocks") or []) if str(one or "").strip()]
    if not blocks:
        print("没有内容可推送")
        return 2
    push_message(
        content_list=blocks,
        notice_info=get_notice_info(data),
        title=str(payload.get("title") or ""),
    )
    print("已提交推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())
