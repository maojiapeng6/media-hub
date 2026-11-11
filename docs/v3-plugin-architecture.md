# KFC Media Hub V3.0 · 插件化平台架构与首包交付

> 2026-11-12 第一阶段交付。**全部结论来自线上真源实机审计**（app.py 33538 行 md5 `4cccc86f…` / index.html 54432 行），非推测、非原型。

## 1. 审计结论（真实代码事实）

| 维度 | 真值 |
|---|---|
| 后端技术栈 | 标准库 `http.server.ThreadingHTTPServer`（**非 Flask**），零外部依赖（仅函数内动态 `pymysql`）；852 顶层函数 |
| 前端 | 原生 JS 无框架，单 HTML 54432 行（CSS ≈1.25 万行 / JS ≈4.08 万行） |
| 路由 | **327 个 API**（GET 167 / POST 160），全部 `if p == '/api/…'` 线性分发 |
| 数据库 | `mediatidy.db` 3.7GB / 42 表（其中 9 张 `plugin_*` 表属 **MT 自己的插件数据，media-hub 零引用**）；`sec.db` 6 表（鉴权/授权/审计） |
| 配置 | `/data/webcfg.json` 58 键，凭据全走 `cfg()`，非硬编码；`tmdb_key` 已配好 |
| 多实例 | **仅 Emby 固定双实例**（`emby_key`/`emby_key2` + `EF2_ROOTS` 硬编码 8096/8896），其它服务无通用抽象 |
| 鉴权 | **已有完整 `sec_*` 体系** + license + 全局写闸 `sec_write_gate`（L1205）——直接复用，不重建 |
| 公网入口 | `<MEDIA_DOMAIN>` → `<PUBLIC_IP>`，45678 由 Lucky 反代 → NAS 5678 |
| 部署 | Dockerfile `python:3.12-alpine`，app.py/index.html **bind-mount ro**；`/dockroot` 已挂载可写 → V3 数据独立落此 |

**关键判断**：media-hub 自身**没有插件系统**（现有「插件」只是代理 MoviePilot API）。V3 必须从零建，但**不能重写**（33538 行生产代码、327 API、43 菜单全保留），走「增量 section + 独立 DB + 子进程隔离」。

## 2. 架构决策

1. **只增不减**：V3 运行时作为新 section 增插 app.py（锚点唯一断言），老路由零改动；独立 `hub_v3.db` 放 `/dockroot/media-hub/v3/`（与 3.7GB MT 库完全隔离）。
2. **插件 = 独立子进程**：插件目录 `<id>/{manifest.json,plugin.py}`，宿主 `subprocess.Popen` 启停 + PID + 健康 + 日志，插件崩溃不影响宿主（故障隔离）。插件通过 SDK（本地 HTTP + token）回调，不进宿主进程内存。
3. **插件 ≠ 实例**：Service Registry 独立表，一个 Emby 插件可挂 N 个 Emby 实例；能力自动发现、真实连通测试（不伪造成功）。
4. **事件驱动**：唯一事件 ID + 去重键 + 重试 3 次 + 死信；插件按订阅权限拉取，看不到别人的事件。
5. **复用既有安全**：`sec_*` 鉴权/license 写闸；插件独立 token（SHA256），不碰用户现有凭据。

## 3. 首包真实能力（已部署线上 + 实测）

- **插件 Manifest 标准**：plugin_id/版本兼容/依赖/22 权限点/导航/仪表盘组件/API 路由/后台任务/事件订阅/生命周期 全字段校验
- **插件生命周期**：安装 → 权限审核 → 启用（子进程）→ 健康检查 → 重启 → 停用 → 卸载
- **插件 SDK**：log / config / 独立 KV / 事件发布 / 事件订阅轮询 / 服务实例读取
- **服务注册中心**：多实例 CRUD + 能力发现 + 真实连通测试（Emby 缺 key 自动回落 webcfg 本地凭据，用户不用重填）
- **事件总线**：发布/去重/重试/死信
- **插件中心 UI**：插件中心 / 服务实例 / 事件总线 / 平台架构 四 Tab，真实数据零模拟

## 4. 实测证据（2026-11-12 真机）

```
1 INSTALL:  ok=True（心跳演示插件）
2 ENABLE:   ok=True, pid=61（独立子进程真的起来了）
3 HEALTH@3s:  running, pid=61
4 HEALTH@12s: running, pid=61        ← 持续运行，不再崩溃
5 PLUGIN LOG: 心跳 #1 · 01:18:00 / 收到事件 plugin.heartbeat   ← 真实子进程输出
6 PLUGIN EVENTS: ['plugin.heartbeat']                          ← 事件总线真实流转
7 SVC TEST: HTTP 502（真实失败，不伪造成功）                    ← 任务书 §12 要求
8 CAPS: navigation/widget/task/event 四类自动注册               ← §5.1 自动联动
9 KV: 错误 token → 403                                         ← 权限隔离生效
10 DISABLE: ok=True
```

**过程中实测抓到并修复 2 个真 bug**（这就是"不许猜"的价值）：
- 插件子进程首次崩溃：`kv_get` 返回 None 被 `int()` → 修 SDK 空值回落默认值
- `/api/v3/sdk/schema` 被误加 token 校验 → 改为公开端点

## 5. 分阶段路线（每阶段可运行 + 实测）

| 阶段 | 内容 | 状态 |
|---|---|---|
| ① 完整审计 | 代码/DB/路由/技术栈/问题清单 | ✅ |
| ② 总体架构 | 插件运行时/SDK/注册中心/事件总线/权限 | ✅ |
| ③ 首包 | 插件运行时 + 服务注册 + 事件总线 + UI | ✅（本交付） |
| ④ 业务迁移 | 43 菜单/327 API 逐步接入插件化，**不删原功能** | 待续 |
| ⑤ 自动化 | 任务中心/Webhook/定时/重试 | 待续 |
| ⑥ UI | 液态玻璃全局重构（设计系统先行） | 待续 |
| ⑦ 测试 | 多实例/多机器人/异常/视觉全量 | 待续 |
| ⑧ 部署 | 数据迁移/回滚/交付 | 待续 |

## 6. 本目录文件（零凭据参考实现）

- `app/v3_runtime.py` — 插件运行时（Manifest/生命周期/服务注册/事件总线/能力聚合）
- `app/v3_sdk.py` — 插件开发 SDK
- `app/v3_example_plugin/` — 内置示例插件（manifest.json + plugin.py）
- `app/v3_frontend.js` — 插件中心前端
- 完整工程需宿主已有的 `cfg()/containers()/sec_*/logger` 适配层（线上 app.py 已注入）
