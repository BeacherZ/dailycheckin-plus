"""「这个容器要不要更新」—— 只比**版本号**，而且版本号取自镜像本身。

版本号全项目只有一处：``panel/version.py`` 的 ``VERSION``。构建时 CI 把它烤进镜像的
OCI 标签 ``cn.dailycheckin-plus.version``，所以这里读到的「最新版本」就是
``docker compose pull`` 真会拿到的那一份，不用另外去问 GitHub 上的仓库文件。

于是三件事同时成立：

* 改 README / 改测试 / 改 compose 注释：不重建镜像，版本号不变 → 不提示；
* 只把 ``VERSION`` 改了、镜像还没出：镜像标签仍是旧号 → 也不提示（拉了也一样）；
* 真发了新版镜像：标签变成新版本号 → 提示「有新版本 vX.Y.Z」。

代价是**重建镜像必须顺手涨版本号**，否则用户不会被通知到 —— 但这比反过来
（提示有新版本、拉下来一模一样）好得多，页面上也只需要「当前版本 / 最新版本」两行数字。

三条克制的原则保持不变：

* **缓存 3 小时**：镜像仓库要两次匿名令牌 + 三次清单请求，别当心跳打。
* **网络不通就如实说**：显示「连不上镜像仓库」，绝不假装「已是最新」。
* **不认识就说不认识**：镜像里没记录构建提交时，结论是「判断不了」，不猜。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import paths

REPO = paths.PROJECT_REPO
# ghcr 的路径就是仓库名的小写形式，所以换仓库/换 fork 时这里自动跟着走。
IMAGE = REPO.lower()
REGISTRY = "https://ghcr.io"
TAG = "latest"
CACHE_TTL = 3 * 3600
TIMEOUT = 6

_LOCK = threading.Lock()
_CACHE: dict = {"ts": 0.0, "data": None}

# 构建时由 CI 写进镜像的 OCI 标签：面板用它显示「远端是什么版本」。
# （metadata-action 自带的 org.opencontainers.image.version 在 main 构建里是
#  字面量 "latest"，当版本号用不了，所以另起一个。）
VERSION_LABEL = "cn.dailycheckin-plus.version"
# 改名之前（项目还叫 dailycheckin 的时候）构建的镜像只有这个老标签。
# 认它只是为了让那些容器能正确看到「远端是新版本」，不算历史包袱 —— 保留了也删不掉。
LEGACY_VERSION_LABEL = "cn.dailycheckin.version"
REVISION_LABEL = "org.opencontainers.image.revision"
CREATED_LABEL = "org.opencontainers.image.created"
BUILTIN_VERSION_LABEL = "org.opencontainers.image.version"

_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
# 多架构索引挑哪个平台：本项目只发 linux/amd64，挑不到就看第一个。
WANT_ARCH = "amd64"


def _short(sha: str) -> str:
    return (sha or "")[:7]


def parse_version(text: str):
    """``"2.2.0"`` / ``"v2.3.0-beta1"`` -> ``(2, 2, 0)`` / ``(2, 3, 0)``。

    只取前三段数字，后缀（-beta / -rc）不参与比较 —— 这个项目发版不过 beta。
    解析不出来返回 ``None``，调用方据此说「判断不了」而不是猜。
    """
    nums = re.findall(r"\d+", str(text or ""))
    if not nums:
        return None
    parts = [int(one) for one in nums[:3]]
    parts += [0] * (3 - len(parts))
    return tuple(parts)


def _git_stamp(root: str) -> dict:
    """从 git 工作区取 HEAD。没有 git / 不是仓库都直接认输，不报错。"""
    try:
        sha = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if sha.returncode != 0:
            return {"commit": "", "date": "", "source": ""}
        when = subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%cI"],
            capture_output=True, text=True, timeout=5,
        )
        return {
            "commit": sha.stdout.strip(),
            "date": when.stdout.strip() if when.returncode == 0 else "",
            "source": "git",
        }
    except Exception:  # noqa: BLE001
        return {"commit": "", "date": "", "source": ""}


def build_stamp() -> dict:
    """这份代码是从哪个提交来的。

    优先级：容器里烤好的 `PANEL_BUILD.json` → 源码根目录的 `PANEL_BUILD.json` → git HEAD。
    全都拿不到就返回空 commit，页面会显示「未记录构建信息」。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path, source in (
        (os.path.join(paths.HOME, "PANEL_BUILD.json"), "image"),
        (os.path.join(root, "PANEL_BUILD.json"), "source"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and str(raw.get("commit") or "").strip():
            return {
                "commit": str(raw["commit"]).strip(),
                "date": str(raw.get("date") or ""),
                "source": source,
            }
    return _git_stamp(root)


def local_info(version: str) -> dict:
    """给页面首屏用的本地信息：不发网络请求，所以不拖慢 /api/state。"""
    stamp = build_stamp()
    return {
        "repo": REPO,
        "url": paths.PROJECT_URL,
        "version": version,
        "commit": stamp["commit"],
        "short": _short(stamp["commit"]),
        "date": stamp["date"],
        "source": stamp["source"],
    }


def _friendly(exc: Exception, where: str) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "%s 拒绝了匿名读取（HTTP %s）—— 镜像可能是私有的" % (where, exc.code)
        if exc.code == 429:
            return "%s 限流了，过一会儿再试" % where
        if exc.code == 404:
            return "%s 上找不到这个镜像（%s）—— 仓库名或标签不对" % (where, IMAGE)
        return "%s 返回 HTTP %s" % (where, exc.code)
    if isinstance(exc, urllib.error.URLError):
        return "连不上 %s —— 容器网络可能访问不了外网" % where
    if isinstance(exc, TimeoutError):
        return "%s 响应超时" % where
    return "检查更新失败：%s" % exc


def _fetch(url: str, accept: str, token: str = ""):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "dailycheckin-plus-panel")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode() or "{}"), resp.headers.get("Docker-Content-Digest")


def registry_token() -> str:
    """匿名拉取令牌。ghcr 的清单接口必须带它，但不需要账号。"""
    scope = urllib.parse.quote("repository:%s:pull" % IMAGE, safe=":/")
    raw, _ = _fetch("%s/token?scope=%s&service=ghcr.io" % (REGISTRY, scope), "application/json")
    token = str(raw.get("token") or raw.get("access_token") or "").strip()
    if not token:
        raise ValueError("镜像仓库没有返回令牌")
    return token


def _pick_platform(manifest: dict) -> dict:
    """多架构索引里挑 linux/amd64；只有一份清单时返回 {}（说明它本身就是镜像清单）。"""
    entries = manifest.get("manifests") or []
    if not entries:
        return {}
    for one in entries:
        if (one.get("platform") or {}).get("architecture") == WANT_ARCH:
            return one
    return entries[0]


def labels_to_info(labels: dict, digest: str = "", tag: str = TAG) -> dict:
    """把镜像的 OCI 标签翻译成面板要的字段。抽出来是为了能离线测（不用连仓库）。"""
    labels = labels or {}
    revision = str(labels.get(REVISION_LABEL) or "").strip()
    custom = str(labels.get(VERSION_LABEL) or labels.get(LEGACY_VERSION_LABEL) or "").strip()
    if not parse_version(custom):
        # 老镜像没有自定义标签，退回 OCI 标准标签：main 构建是 "latest"，
        # tag 构建才是 "2.2.0"。认不出就当没有，页面上只显示提交。
        fallback = str(labels.get(BUILTIN_VERSION_LABEL) or "").strip()
        custom = fallback if parse_version(fallback) else ""
    return {
        "tag": tag,
        "commit": revision,
        "short": _short(revision),
        "date": str(labels.get(CREATED_LABEL) or "").strip(),
        "version": custom,
        "digest": digest or "",
    }


def latest_image(tag: str = TAG) -> dict:
    """问镜像仓库：``latest`` 那份镜像是由哪个提交构建的、什么时候建的。

    返回 ``{"commit", "short", "date", "version", "digest"}``；拿不到关键信息就抛异常，
    由 :func:`check` 转成「判断不了」或「检查失败」，绝不编一个结果出来。
    """
    token = registry_token()
    base = "%s/v2/%s" % (REGISTRY, IMAGE)
    manifest, digest = _fetch("%s/manifests/%s" % (base, tag), _ACCEPT, token)

    picked = _pick_platform(manifest)
    if picked:
        digest = picked.get("digest") or digest
        manifest, _ = _fetch("%s/manifests/%s" % (base, digest), _ACCEPT, token)

    config_digest = (manifest.get("config") or {}).get("digest")
    if not config_digest:
        raise ValueError("镜像清单里没有 config 摘要")
    blob, _ = _fetch("%s/blobs/%s" % (base, config_digest), "application/json", token)
    labels = (blob.get("config") or {}).get("Labels") or {}
    info = labels_to_info(labels, digest, tag)
    if not info["date"]:
        info["date"] = str(blob.get("created") or "").strip()
    return info


def check(version: str, force: bool = False) -> dict:
    """拿本机版本号跟仓库里 ``latest`` 那份镜像自带的版本号比。

    ``needs_update`` 只有三种取值：``True``（远端更高）/ ``False``（一样或本机更高）/
    ``None``（判断不了，理由在 ``hint`` / ``error`` 里）。结果缓存，`force=True` 强制重查。
    """
    now = time.time()
    with _LOCK:
        cached = _CACHE["data"]
        if cached and not force and now - _CACHE["ts"] < CACHE_TTL:
            return cached

    out = dict(local_info(version), checked_at=now, ok=None, error="", hint="",
               remote=None, needs_update=None)
    mine = parse_version(version)
    try:
        remote = latest_image()
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = _friendly(exc, "镜像仓库 ghcr.io")
    else:
        out["ok"] = True
        out["remote"] = remote
        theirs = parse_version(remote.get("version"))
        if mine and theirs:
            out["needs_update"] = theirs > mine
        elif remote.get("commit") and out["commit"]:
            # 2.2.0 之前构建的镜像没有自定义版本标签：退回比提交，
            # 免得老镜像用户永远停在「判断不了」。
            out["needs_update"] = remote["commit"] != out["commit"]
        else:
            out["hint"] = "镜像里既没有版本号也没有构建提交，没法比。"

    with _LOCK:
        _CACHE["ts"] = now
        _CACHE["data"] = out
    return out
