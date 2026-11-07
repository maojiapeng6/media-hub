# 安全部署清单（务必阅读）

## 当前已知缺口（发布前必修）

| # | 缺口 | 状态 | 影响 |
|---|---|---|---|
| G1 | 业务写端点未强制登录（`/api/action`、`/api/container` 等仅授权闸，无会话闸） | 🔴 未修 | 任何能访问 5678 的人可启停容器/改配置 |
| G2 | 授权闸默认 30 天试用放行全部写操作 | 设计如此 | 决定商业模式（试用/发码） |
| G3 | `/api/config` 在授权闸放行清单（叠加 G1 = 无鉴权改配置） | 随 G1 一并修 | 同上 |

> **G1 未修前：切勿把 5678 端口暴露公网**。置于内网 / 反代加认证（Basic Auth / Authelia / OAuth2 Proxy）。

## 已内置的防线（本仓库现状）

- **管理员账号体系**：首次启动引导创建（pbkdf2 200k 哈希）、HttpOnly 会话 Cookie、双提交 CSRF、登录限流。
- **授权码体系**：HMAC-SHA256（低 30bit 有效签名）+ 厂商密钥（`KFC_VENDOR_SECRET`）跨机发码 + 双密钥链 + 设备指纹绑定 + 节点数上限 + 吊销。
- **危险操作**：容器 stop/rm 前端二次确认；`/api/repair` 动作白名单（未登记动作一律拒绝）；面板禁止对自身容器做 stop/restart。
- **隐私**：源码零硬编码凭据（CI 守门 `privacy_scan` 强制）；`config.json`/`*.db`/`secrets.json`/哈希清单被 `.gitignore` 拦截；错误信息经 `sanitize_err` 脱敏（不回显配置内容）。
- **审计与限流**：`audit_log`（actor/ip/action/args_hash）+ `rate_limit` 表。

## 部署安全清单（上线前逐项勾选）

- [ ] 设置页创建管理员（强密码），或 compose 设 `KFC_MASTER_KEY`
- [ ] 面板端口仅绑定内网 / 防火墙限制来源 IP
- [ ] 若公网可达：反代 + HTTPS + 额外认证层
- [ ] `docker.sock` 挂载是高危能力：不依赖容器管理功能时移除该挂载
- [ ] `KFC_VENDOR_SECRET` 只存在发码方环境；客户实例**不配**该变量（他们只能试用/激活你发的码）
- [ ] 定期 `git pull` 更新；关注 CHANGELOG 的 Security 段
- [ ] PT 站点巡检只做首页 GET——**任何情况下不要自动化登录/试密**（部分站试密即封 IP）
