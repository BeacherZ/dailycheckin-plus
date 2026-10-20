"""项目版本号 —— **全项目唯一来源**。

以前同时存在三个"版本"：面板 2.1.0、引擎 25.12.9（从上游继承的，脱钩后早就
名不副实）、以及构建提交 sha。页面上三行数字并排显示，谁也说不清哪个算数。

现在只有这一个：改这里 = 发版。检查更新看的是**镜像自带**的版本标签
（``panel/update.py`` 读 ghcr 上 ``latest`` 的 OCI 标签），所以「最新版本」等于
你 ``docker compose pull`` 会拿到的那份；改 README、调样式这种提交连镜像都不重建，
自然也不会提示"有新版本"。

发版步骤：
1. 改这里的 VERSION（``dailycheckin_plus/__version__.py`` 与 ``pyproject.toml`` 跟着改，
   ``tools/test_version.py`` 卡着一不一致就红）；
2. 提交、打 tag ``vX.Y.Z`` 推上去 —— 镜像会同时打 ``latest``、``X.Y.Z`` 和短 sha；
3. （可选）在 GitHub 建个 Release，把那三个镜像标签写进说明里。
"""

VERSION = "1.0.0"
