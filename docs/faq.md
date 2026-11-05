# 常见问题 FAQ

**Q：面板需要装什么依赖吗？**
不需要。`app/app.py` 只用 Python 标准库（`http.server`），有 Python 3.11+ 就能跑，容器镜像也仅基于 `python:3.11-slim`。

**Q：首次打开没有配置，界面会报错吗？**
不会。未配置的服务在页面上显示为空状态/「未配置」，点击 ➕ 添加实例即可；不影响其它已配置模块。

**Q：我有多个 Emby（比如主服 + 成人服），能都接进来吗？**
能。所有服务类型（Emby / MoviePilot / qBittorrent / MediaTidy / CD2 / RT / TG Bot）都支持多实例，面板内 ➕ 添加、按实例路由操作。

**Q：我的凭据存在哪？会明文落盘吗？**
配置存 `webcfg.json`（挂载在 `/data`），敏感字段以 `enc1:`（Fernet）加密；主密钥优先取环境变量 `KFC_MASTER_KEY`，否则生成于 `/data/.kfcsec/master.key`。

**Q：升级会覆盖我已有配置吗？**
不会。程序检测 `install.lock` 判定为升级模式，只做只读确认，**绝不重置/覆盖**已有凭据。这也是本项目的发布红线。

**Q：容器管理是怎么实现的？**
挂载 `/var/run/docker.sock`，面板通过 Docker Engine API 做启停/日志/资源查看。若不需要该能力，删除 compose 里那一行挂载即可（对应功能会提示不可用）。

**Q：面板默认有登录鉴权吗？**
有完整的账号体系（`/api/sec/setup` 首次创建管理员 → 写 `install.lock`；`/api/sec/login` 登录；pbkdf2 密码哈希；HttpOnly 会话 Cookie + 双提交 CSRF）。但**当前版本的「未登录禁止写操作」尚未全量强制**（授权闸只拦「授权过期」），因此**切勿把 5678 端口直接暴露公网**，部署在内网或加反代认证。此项已列为发布前必修缺口（见 ROADMAP）。

**Q：PT 站点、115、Emby 这些第三方服务条款谁负责？**
使用者自行遵守。本仓库不含任何站点账号、不提供影视资源，仅为自托管编排工具。

**Q：为什么 `bots/embyboss/` 是 GPL-3.0 而其它是 Apache-2.0？**
该目录源自 Sakura_embyboss（上游 GPL-3.0），衍生修改必须同协议开源。其余为自研，采用 Apache-2.0。详见根目录 `NOTICE`。
