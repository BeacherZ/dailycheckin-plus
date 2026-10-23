#!/usr/bin/env python3
"""版本号与「检查更新」的自测（离线，不联网）。

这个项目曾经同时挂着三个"版本"：面板 2.1.0、引擎 25.12.9（上游遗产）、构建 sha。
页面上三行数字并排，谁也说不清哪个算数；更糟的是「检查更新」比的是**提交**，
改一行 README 就提示「有新版本」，提示很快就没人信了。之后改成比版本号，又走到
另一个极端：版本号是**仓库里的字**，加了却没出镜像时，用户点了更新拉下来一模一样。

这里锁四件事：

1. 全项目只有一个版本号：``panel/version.py``，别的文件都跟它对齐；
2. 要不要更新只比**版本号**，而版本号是从仓库里 latest 那份镜像的标签读出来的
   —— 所以「最新版本」天然等于 ``docker compose pull`` 会拿到的那份；改 README、
   改测试不重建镜像也就不会涨号，不会提示；
3. 页面上只剩「当前版本 / 最新版本」两行，不显示「引擎版本」，也不放更新命令；
4. **只改文档不重建镜像**：main 分支的推送按 paths-ignore 过滤掉 README / LICENSE /
   截图；同时 tag 推送必须照常构建（路径过滤对 tag 不生效），否则发版会静默失败。

跑法：python3 tools/test_version.py
"""
import io
import os
import re
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f"  :: {detail}" if not cond and detail else ""))


def section(title):
    print("\n" + "=" * 70)
    print(title)


def read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


import panel  # noqa: E402
from panel import update  # noqa: E402

# --------------------------------------------------------------------------- #
section("1) 只有一个版本号，三处必须一致")

ver_src = read("panel", "version.py")
m = re.search(r"""^VERSION\s*=\s*["']([^"']+)["']""", ver_src, re.M)
check("panel/version.py 里有 VERSION", bool(m), "没找到 VERSION 定义")
VERSION = m.group(1) if m else ""
check("版本号是 x.y.z 形式", bool(re.fullmatch(r"\d+\.\d+\.\d+", VERSION)), VERSION)
check("panel.VERSION 与 version.py 一致", panel.VERSION == VERSION, f"{panel.VERSION} vs {VERSION}")

engine_ver = read("dailycheckin_plus", "__version__.py")
m2 = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", engine_ver)
check("引擎 __version__ 与项目版本一致", bool(m2) and m2.group(1) == VERSION,
      m2.group(1) if m2 else "没找到 __version__")

pyproject = read("pyproject.toml")
m3 = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject, re.M)
check("pyproject.toml 的 version 与项目版本一致", bool(m3) and m3.group(1) == VERSION,
      m3.group(1) if m3 else "没找到 version")

check("version.py 里只定义了一次 VERSION", len(re.findall(r"^VERSION\s*=", ver_src, re.M)) == 1)

# --------------------------------------------------------------------------- #
section("2) 判断要不要更新：只比版本号，版本号取自镜像")

check("parse_version 认得 2.2.0", update.parse_version("2.2.0") == (2, 2, 0))
check("parse_version 认得 v 前缀", update.parse_version("v2.3.1") == (2, 3, 1))
check("parse_version 忽略后缀", update.parse_version("2.4.0-beta1") == (2, 4, 0))
check("parse_version 补零到三段", update.parse_version("3") == (3, 0, 0))
check("parse_version 解析不了返回 None", update.parse_version("unknown") is None)

# 版本号还是要能被 CI 读出来（构建时烤进 OCI 标签 cn.dailycheckin-plus.version）
import subprocess  # noqa: E402
printed = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "version.py")],
                         capture_output=True, text=True, timeout=30)
check("tools/version.py 打印的就是 VERSION", printed.stdout.strip() == VERSION, printed.stdout.strip())

# --- 镜像标签 -> 面板字段 ---
info = update.labels_to_info({
    "org.opencontainers.image.revision": "a" * 40,
    "org.opencontainers.image.created": "2026-10-20T04:54:39Z",
    "cn.dailycheckin-plus.version": "2.2.0",
})
check("能从 OCI 标签里读出构建提交", info["commit"] == "a" * 40 and info["short"] == "aaaaaaa", str(info))
check("自定义版本标签优先", info["version"] == "2.2.0", str(info))
check("构建时间一并带出来", info["date"].startswith("2026-10-20"), str(info))

# 3.0.x 及更早的镜像只有老标签（项目还叫 dailycheckin 时烤的），换了新标签之后
# 那些容器的「检查更新」不能瞎 —— 老标签要认。
info = update.labels_to_info({"cn.dailycheckin.version": "3.0.1"})
check("老版本标签仍然认得出（改名前的镜像）", info["version"] == "3.0.1", str(info))
check("老标签的常量没被顺手删掉", update.LEGACY_VERSION_LABEL == "cn.dailycheckin.version", update.LEGACY_VERSION_LABEL)

info = update.labels_to_info({"org.opencontainers.image.revision": "b" * 40,
                              "org.opencontainers.image.version": "latest"})
check("老镜像没有自定义标签时，'latest' 不当版本号用", info["version"] == "", str(info))
info = update.labels_to_info({"org.opencontainers.image.version": "2.2.0"})
check("tag 构建的镜像能从标准标签认出 2.2.0", info["version"] == "2.2.0", str(info))
check("镜像里没记录提交时不编一个出来", info["commit"] == "", str(info))

# --- 多架构索引挑平台 ---
index = {"manifests": [
    {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
    {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
    {"digest": "sha256:unk", "platform": {"os": "unknown", "architecture": "unknown"}},
]}
check("多架构索引里认准 linux/amd64", update._pick_platform(index)["digest"] == "sha256:amd")
check("单份清单时不乱挑", update._pick_platform({"config": {}}) == {})

# --- 判断逻辑本身（打桩，不联网）---
def stub_local(commit):
    update.build_stamp = lambda: {"commit": commit, "date": "", "source": "image"}


def stub_image(payload):
    def _image(tag="latest"):
        if isinstance(payload, Exception):
            raise payload
        return dict(payload)
    update.latest_image = _image


def run_check(local_version=VERSION, remote_version=VERSION,
              local_commit="a" * 40, remote_commit="a" * 40):
    stub_local(local_commit)
    stub_image({"commit": remote_commit, "short": remote_commit[:7], "date": "",
                "version": remote_version, "digest": ""})
    return update.check(local_version, force=True)


out = run_check(remote_version="9.9.9", remote_commit="b" * 40)
check("远端版本更高 → 提示有新版本", out["needs_update"] is True, str(out.get("needs_update")))
check("远端版本号一起带回来（页面「最新版本」要显示）",
      out["remote"]["version"] == "9.9.9", str(out.get("remote")))

out = run_check()
check("版本一样 → 不提示", out["needs_update"] is False, str(out.get("needs_update")))

# 这条是「只比版本号」的关键：同一份镜像重建过、但没涨版本号，不该提示 ——
# 页面上两行数字一模一样却冒出一个「有新版本」，最难解释。
out = run_check(remote_commit="b" * 40)
check("只重建了镜像、版本号没涨 → 不提示", out["needs_update"] is False, str(out.get("needs_update")))

out = run_check(local_version="2.9.0", remote_version="2.10.0")
check("按数字比而不是按字符串比（2.10 > 2.9）", out["needs_update"] is True, str(out.get("needs_update")))

out = run_check(local_version="2.3.0", remote_version="2.2.2")
check("本机比远端新（本地还没发版）→ 不提示", out["needs_update"] is False, str(out.get("needs_update")))

# 2.2.0 之前构建的镜像没有 cn.dailycheckin-plus.version 标签：退回比提交，
# 别让这些用户永远停在「无法判断」。
out = run_check(remote_version="", remote_commit="b" * 40)
check("老镜像没有版本标签 → 退回比构建提交", out["needs_update"] is True, str(out))
out = run_check(remote_version="", remote_commit="a" * 40)
check("老镜像且提交一样 → 不提示", out["needs_update"] is False, str(out))

out = run_check(remote_version="", remote_commit="")
check("镜像里既没版本号也没提交 → 判断不了，并给出理由",
      out["ok"] is True and out["needs_update"] is None and "没法比" in out["hint"], str(out))

stub_local("a" * 40)
stub_image(urllib.error.URLError("no network"))
out = update.check(VERSION, force=True)
check("连不上镜像仓库 → ok=False 且如实说明", out["ok"] is False and "ghcr.io" in out["error"], str(out))
check("连不上时不给「已是最新」的假结论", out["needs_update"] is None, str(out.get("needs_update")))

stub_image(urllib.error.HTTPError("https://ghcr.io", 403, "denied", {}, None))
out = update.check(VERSION, force=True)
check("镜像私有/被拒 → 提示可能不是公开包", "私有" in out["error"], str(out))

# --- 不该再回到旧做法 ---
update_src = read("panel", "update.py")
check("不再去问 GitHub 的接口", "api.github.com" not in update_src, "又回头问仓库了")
check("版本号从镜像标签里读（所以「最新版本」= 你 pull 到的那份）",
      "cn.dailycheckin-plus.version" in update_src, "读不到版本标签")
check("判断依据是版本号本身（不是比提交）", "theirs > mine" in update_src, "判断逻辑又回到比提交了")
check("检查结果有缓存（镜像仓库也别当心跳打）", "CACHE_TTL = 3 * 3600" in update_src)

# --------------------------------------------------------------------------- #
section("3) 页面上只有「当前版本 / 最新版本」两行")

app_js = read("panel", "static", "app.js")
server_py = read("panel", "server.py")
check("诊断信息里不再有「引擎版本」", "引擎版本" not in app_js, "还在显示引擎版本")
check("诊断信息里用「版本」而不是「面板版本」", "面板版本" not in app_js, "还在写面板版本")
check("后端不再提供 engine.version", '"version": engine_version()' not in server_py)
check("后端不再有 engine_version() 实现", "def engine_version(" not in server_py)

index_html = read("panel", "static", "index.html")
check("页面上就是这两行字：当前版本 / 最新版本",
      "当前版本：" in index_html and "最新版本：" in index_html)
check("当前版本与最新版本各有落点",
      'id="aboutLocal"' in index_html and 'id="aboutUpdateText"' in index_html)
check("三种结论都有落脚点（有更新 / 已是最新 / 无法判断）",
      all(t in app_js for t in ("有新版本，拉取新镜像即可", "已是最新", "无法判断")))
check("检查失败也如实写出来，不假装已是最新", "检查失败" in app_js)

# 用户明确要求：通知里不要更新命令、不要一整段说明，简单点。
check("不再放更新命令", "docker compose pull" not in app_js and "aboutCmd" not in index_html)
check("说明段落默认隐藏（正常状态只剩两行数字）", 'id="aboutTip" hidden' in index_html)
check("首屏默认文案也不再讲「跟 main 的最新提交比」", "最新提交" not in index_html, "又回到比提交了")
check("简版的 /api/state 不会把已查到的结论抹掉", "info.checked_at === undefined" in app_js)

# --------------------------------------------------------------------------- #
section("4) 只改文档不重建镜像（CI 触发条件）")

# 版本号不再被"随便一次提交"顶上去之后，剩下的噪音是**镜像本身**：
# 只改 README 也会触发一次构建、推一个 digest 不同的 latest。用户 pull 一次
# 几百 MB，换回一份和上一版一模一样的镜像。所以 main 分支按路径过滤掉文档。
#
# 这条规则有个致命前提：paths-ignore **对 tag 推送不生效**（GitHub 官方文档：
# "Path filters are not evaluated for pushes of tags."）。要是哪天有人把它改成
# 别的写法，导致打 tag 也不构建，发版就静默失败 —— 镜像标签一直停在旧版本，
# 而 CI 全绿。所以这两件事必须一起锁。
wf = read(".github", "workflows", "build.yml")
on_block = wf.split("\njobs:", 1)[0]
push_block = on_block.split("pull_request:", 1)[0]

check("workflow 仍然在 jobs: 之前写 on: 段", bool(on_block) and "on:" in on_block)
check("main 分支推送带了 paths-ignore", "paths-ignore:" in push_block, push_block.strip()[:200])

ignored = []
if "paths-ignore:" in push_block:
    tail = push_block.split("paths-ignore:", 1)[1]
    ignored = [one.strip().strip('"\'') for one in re.findall(r"^\s+-\s+(.+?)\s*$", tail, re.M)]
check("忽略列表不是空的", bool(ignored), str(ignored))
check("markdown 文档被忽略（README 改多少次都不重建）", "**/*.md" in ignored, str(ignored))
check("LICENSE 被忽略", "LICENSE" in ignored, str(ignored))
check("README 的效果图 index.png 被忽略", "index.png" in ignored, str(ignored))
# compose 只是部署示例（容器里跑的根本不是它），改端口注释不该重建镜像。
check("compose 示例被忽略（改端口说明不重建）", "docker-compose*.yml" in ignored, str(ignored))

# 这些改了必须重建：镜像内容变了，或者必须重新验证。
for must_build in ("Dockerfile", ".dockerignore", "requirements.txt", "pyproject.toml",
                   "setup.py", "UPSTREAM.json", "panel/**", "dailycheckin_plus/**",
                   "tools/**", "docker/**"):
    check(f"不能忽略 {must_build}", must_build not in ignored, f"忽略了：{must_build}")

check("push 仍声明了 tags 触发（打 tag 一定构建）", re.search(r"^\s+tags:\s*\[?[\"']?v", push_block, re.M) is not None,
      push_block.strip()[:200])
check("注释里留了「paths 对 tag 不生效」的依据，免得后人改成静默不构建",
      "Path filters are not evaluated" in wf)
check("PR 事件不加路径过滤（否则改文档的 PR 会卡在 Pending）",
      "pull_request:" in on_block and "paths-ignore" not in on_block.split("pull_request:", 1)[1],
      on_block.split("pull_request:", 1)[1].strip()[:160])

# 运行时不许读 README：否则"改 README 不重建镜像"会让镜像里躺着一份过期文档，
# 甚至让面板显示的内容和仓库对不上。要读它就必须写出 "README.md" 这个名字，
# 所以扫字面量就够了（注释里提 README 不算）。
panel_dir = os.path.join(ROOT, "panel")
readme_users = [n for n in sorted(os.listdir(panel_dir))
                if n.endswith(".py") and "README.md" in read(os.path.join("panel", n))]
check("面板代码里没有任何地方在运行时读 README.md", readme_users == [], str(readme_users))

# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
print(f"通过 {len(passed)}  失败 {len(failed)}")
if failed:
    for name in failed:
        print("  FAIL:", name)
sys.exit(1 if failed else 0)
