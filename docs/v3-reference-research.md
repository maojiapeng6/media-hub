# 优秀项目研究与架构借鉴报告

> KFC Media Hub V3.0 产品级重构 · 阶段 2 交付
> 2026-11-12 · 结论全部来自真实联网访问，**严禁编造**。证据分级：
> **[文档]** 官方公开文档明确写出 · **[源码]** 官方 GitHub 源码实际看到（带路径） · **[推断]** 根据产品行为推断（非事实）

---

## 0. 访问成功/失败记录（诚实清单）

| 项目 | 成功访问 | 失败 / 无法访问 | 源码可得性 |
|---|---|---|---|
| **MoviePilot** | 官网 `movie-pilot.org`、文档 `wiki.movie-pilot.org/plugin`、GitHub `jxxghp/MoviePilot`（默认分支 v3，12k+ stars，GPL-3.0）、官方插件库 `jxxghp/MoviePilot-Plugins`（`package.v3.json`、`docs/Plugin_Development.md`）、v2/v3 多个 raw 源码 | `package.v3.json` 响应截断（可见 49 条）；v3 动态路由挂载点、APScheduler 注册文件未定位 | **完全可得（GPL-3.0）** |
| **MediaTidy** | 官方文档站 11 个页面（rules/organize/strm/ff-cache/dashboard/drive/plugins/automation/capabilities/manual 等）、官方开源插件模板 `MuRongYun8/mediatidy-plugin-template`（`plugin.py`/`plugin.json`）、宿主契约 `methods.json`（86 方法） | **`MuRongYun8/MediaTidy` 主程序仓库 404（闭源）**，需 `LICENSE_CODE` 授权；调度器实现、缓存后端、Webhook 接收端源码均无 | **仅插件 SDK 可得，主程序闭源** |
| **ResourceTool** | 官方 Wiki 11 个页面（`/features/emby-instances/emby-users/emby-requests/telegram/pan115/monitor/missing-fill` 等）、`shutu777/resourcetool-wiki`（VuePress 文档站） | **主程序源码不存在或不公开**（作者 GitHub 仅 fork、GitHub 全局搜索无结果、Docker Hub 抓取失败）；`/features/emby-user-mgmt/` 404 | **零源码，纯文档** |

> ⚠️ 用户任务书猜测的 MoviePilot 仓库 `imsyy/MoviePilot` **不存在**；官方为 `jxxghp/MoviePilot`（官网安装脚本与 GitHub API 双重确认）。
> ⚠️ ResourceTool 为付费闭源（¥129/299/399，授权码绑设备），其「数据模型/适配器抽象/权限模型」**只能标 [文档] 或 [推断]，不得当作源码事实**。

---

## 1. MoviePilot：插件系统真实机制（源码可验证）

### 1.1 插件目录与入口 [源码]
- v2：`app/plugins/<id>/__init__.py`；加载器 `app/core/plugin.py:_load_selective_plugins` 用 `importlib.import_module("app.plugins.<id>")`，卸载清 `sys.modules`（`_clear_plugin_modules`）。
- v3：插件**移出主仓**，独立仓库 `plugins.v3/<id>/__init__.py`；宿主运行目录仍是 `ROOT_PATH/"app"/"plugins"`（`app/adapters/system/plugin/dependency.py`）。

### 1.2 插件契约 [源码] `app/sdk/plugin/base.py`
`_PluginBase(metaclass=ABCMeta)` 抽象方法：`init_plugin(config)` / `get_state()` / `get_api()` / `get_form()` / `get_page()` / `stop_service()`；可选 `get_service()`（定时）/ `get_command()`（远程命令）/ `get_dashboard()` / `get_agent_tools()` / `get_actions()` / `get_database_models()`。
→ **关键借鉴**：稳定的生命周期契约让启停可预测，插件贡献能力靠「返回声明」而非修改宿主。

### 1.3 元数据/清单 [源码]
- 市场索引 `package.v3.json`（键=插件 ID；字段 `name/description/labels/version/icon/author/level/system_version/history/release`），`app/adapters/system/plugin/package.py` 按代际拼 `package.{v3|v2}.json`。
- 依赖 `app/adapters/system/plugin/manifest.py:select_dependency_manifest()`：优先 `pyproject.toml [project].dependencies`，回退 `requirements.txt`。

### 1.4 路由/页面/导航 [源码]
`get_api()` 返回 `[{path,endpoint,methods,auth,summary}]`；v2 `get_plugin_apis()` 自动加前缀 `/{plugin_id}`、默认 `auth="apikey"`；`get_form()/get_page()` 返回 Vuetify JSON；`get_render_mode()→("vue","dist/assets")` 走 Vue 联邦。静态插件管理路由在 `app/api/routers.py`（`RouterSpec(plugin.router,"/plugin")`）。

### 1.5 定时任务与事件 [源码]
- `get_service()` 返回 `[{id,name,trigger,func,kwargs}]`，trigger 支持 cron/interval/date；`get_plugin_services()` 仅收集 `get_state()==True` 者。
- 事件：装饰器 `@eventmanager.register(EventType.PluginAction)`（`app/runtime/events.py`）。
- **事件总线真实实现** [源码] `app/core/event.py`(v2) / `app/runtime/events.py`(v3)：`EventManager(metaclass=Singleton)` 全局单例；**双模式**——`EventType` 广播（fire-and-forget → `PriorityQueue.put((priority,event))` → 后台守护消费者 → 同步 handler 交 `ThreadHelper` 线程池、异步 handler `asyncio.run_coroutine_threadsafe`）、`ChainEventType` 链式（同步顺序执行并可返回值）。

### 1.6 安装/卸载/启停/配置 [源码]
- v2 无独立 uninstall/enable 接口，靠 `start()/stop()/init_plugin()/reload_plugin()` + `get_state()` 联动事件处理器开关；配置走 `SystemConfigOper`，键 `plugin.<pid>`，安装列表键 `SystemConfigKey.UserInstalledPlugins`。
- v3 用 `app/db/models/plugininstance.py` 的 **`plugininstance` 表**：`instance_id / source_plugin_id / config_data(JSON) / is_enabled / is_default_target`；`set_enabled()` 注释明确「置假即卸载，配置保留」；支持**虚拟分身**（一源码多实例）。
- `PluginPackageManager`（`app/adapters/system/plugin/package.py`）含 Release ZIP 优先 + 路径穿越校验；`PluginDependencyInstaller`（`dependency.py`）做版本约束求交集、批量失败逐组恢复。

---

## 2. MediaTidy：自动化引擎理念（主程序闭源，仅 SDK 契约可验证）

> 主程序 Go 闭源（仓库 404）。以下 [源码] 仅指官方开源插件模板 `MuRongYun8/mediatidy-plugin-template/plugin.py` 与宿主契约 `methods.json`（官方称对应主仓 `backend/internal/plugins/contract/methods.json`）；主程序内部以 [文档]/[推断] 标注。

### 2.1 调度与队列 [文档]/[源码]
- [文档] 全局队列统一调度、**同类型任务顺序执行**、SSE 实时推送；定时任务 `type ∈ organize/strm/clean_dirs/daily_summary`，5 位 cronExpr，由「MT 统一调度器」保存（非插件自建）。
- [源码] `methods.json` 的 `RunResult.status`：`submitting/queued/running/waiting/done/failed/cancelled/interrupted/outcome_unknown`。
- [推断] Go 主程序自带调度器+队列，非 APScheduler（具体源码不可得）。

### 2.2 规则与数据模型 [文档]/[源码]
- [文档] 规则为三类 YAML（分类/命名/质量评分），来源优先级 **本地 > 订阅源 > 内置**；分类按 `genre_ids/original_language/origin_country` 顺序匹配命中即停；评分 `Σ(权重×位置分)`，含 `required/min_score_diff/unknown_policy`。
- [文档] 方案字段 `sourceDir/destDir/accountId/ruleId/qualityRuleId/realtimeMode/cronExpr`。[源码] `Profile{id,name,kind:"organize"|"strm",revision,enabled,source,destination,settings}`、`PathEndpoint`。

### 2.3 执行流程与状态机 [源码]（强借鉴价值）
`plugin.py` 类 `Plugin(BasePlugin)`：
- 流程 `flow["stage"]`：`scan → scan_failed/waiting/failed/review/done`
- 逐文件 `item["status"]`：`pending → unrecognized/group_pending/recognized → verifying → verification_failed/outcome_unknown → organize_failed`，含 `identity_conflict`
- 长任务续接：`ctx.mt.tasks.wait(refs, completion={"action":...})` 配 `@continuation("scan-complete")/@continuation("organize-complete")`，`_event_flow` 校验 `parentRunId`
- 分批 `BATCH_SIZE=8`、`MAX_FILES=500`；幂等键 `idempotencyKey`

### 2.4 多网盘统一抽象 [文档]/[源码]（强借鉴）
- [文档] 虚拟路径（`/我的115/...`）映射 `cd2MountPath→accountId`；通道 `fileManagement: cd2|fuse`、`download: direct|cd2`；CD2 gRPC 操作，来源键 `cloudName+userName` 分账号；`providerType: aliyun/quark/local/unknown`。
- [源码] `methods.json` `files.roots` 返回 `storageType:"local"|cloud` + `read/write/delete` 权限；`FileMeta{rootId,path,ref,version,sha1}` —— **「根 + 引用 + 版本 + 权限」的统一文件抽象**，隔离多网盘差异。

### 2.5 缓存 [源码]/[文档]
- [源码] 契约暴露 `ffprobe.cacheExists(sha1:40位)`。[文档] 整理 FF（ffprobe 结果）与神医 FF（Emby MediaSource/MediaStreams）均以 **SHA1 为键**，本地 JSON + 远程节点 + `strm_assistant_ff` 缓存域；`ProbeRevision>=2`(音轨)/`>=4`(字幕) 决定旧缓存补探。
- [推断] 缓存后端与远程 API 未公开。

### 2.6 Webhook 与插件扩展 [文档]/[源码]
- [文档] Emby 回调 `http://<IP>:2019/api/webhook/emby`（JSON）；CD2 Webhook 对应 `realtimeMode=webhook`；GitHub push 触发规则订阅同步。
- [源码] 契约无 `webhook.*` 方法，仅 `http.request`、`notification.send`、`triggers.events.*`；接收端实现源码未找到。
- [源码] 插件 SDK v2：`plugin.json` 声明 `id/version/sdkVersion:"2"/permissions/actions/queries/triggers`，`ui.version:"2"`；`@action/@query/@continuation`；`methods.json` 共 **86 方法**（tasks/triggers.schedules/triggers.events/organize/quality/rules/strm/tmdb/ai/storage/ffprobe 等）。[文档] 受信任代码、**非安全沙箱**，受能力授权约束。

---

## 3. ResourceTool：综合运营理念（纯文档，零源码）

> 付费闭源。以下全部 [文档] 或 [推断]，**不得当作源码事实**。

- **多实例** [文档]：「开服→实例管理」以可折叠卡片罗列所有 Emby 实例，每张卡独立持有 Bot Token/管理员 TG/群组、运营模式（标准/签到）、权限模板、媒体库管控、生命周期任务（到期扫描/活跃度检测，各实例单独 Cron）。官方场景「一台国产剧、一台欧美剧」。字段（配置名/内网地址/API Key/反代端口/默认 115 账号）见 `emby302`。[推断] 一服务类型挂 N 实例的实体关系；DB 表结构未公开。
- **媒体服务** [文档]：Emby 302 播放网关；按 **STRM 类型**（路径类/Pickcode/分享类/原生）× **网盘模式**（共享自动均衡/源号直出/定向分流/自备/原生）二维分派出链。Jellyfin 仅经「飞牛 OS 助手（FnOS 302）」体现；**Plex 全站未提及**。
- **能力检测** [文档]：115 Cookie 健康检查、账号信息（VIP/容量/到期）、Bot Token 测试、代理测试、MTProto Session 状态（在线/离线/重连中）、容器监控（挂 `docker.sock`）。
- **机器人/路由/权限** [文档]：三层 Bot——主 Bot + 每实例独立 Bot（可单独启停）+ 每 115 账号副 Bot（通知隔离）；MTProto Session 监听频道（**Bot 无法读非自有频道**，与我们 V3 用 userbot 的判断一致）；消息监控转发（源频道→多目标，白黑名单/模板替换/智能去重）与监控转存（→多 115 账号+转存/离线目录）；系统侧 `admin/user` + JWT + 菜单/插件 key；运营侧「权限模板」封装网盘模式/设备上限/Emby Policy/媒体库权限，批量套用+范围覆盖；用户状态 `active/disabled/expired`、白名单、积分。
- **资源运营** [文档]：求片（TMDB 搜索→提交新片/缺集/异常→管理员或自动找片→115 转存→通知，7 天限 3 次，每实例独立 MoviePilot v2 订阅 Token）；缺集补漏（TMDB 已播集数 vs Emby 已有集数→索引匹配→全部/缺集转存）。
- **播报/监控** [文档]：Bot 转存/离线通知、备份完成 TG 通知、生命周期汇总；`/monitor/<64位Token>` 免登录大屏（Emby API 经后端代理）、使用统计、日志按天轮转 7 天。[推断] Go 单体 + SQLite + Caddy + JWT。

---

## 4. 对 KFC Media Hub 的借鉴决策（可实现 / 不照搬）

| 借鉴点 | 来源与证据 | 决策 |
|---|---|---|
| **稳定插件生命周期契约**（init/get_state/stop_service + 能力声明式返回） | MoviePilot [源码] `app/sdk/plugin/base.py` | ✅ **实现**（V3 首包已落地，继续对齐契约风格） |
| **事件双模式**（广播异步 + 链式同步返回值）、优先级队列、后台消费 | MoviePilot [源码] `app/runtime/events.py` | ✅ **实现**（V3 事件总线补「链式事件」与消费线程池） |
| **配置与启停分离**（`is_enabled` 一位切换「卸载保留配置」） | MoviePilot [源码] `plugininstance.py` | ✅ **实现**（V3 卸载已保留独立数据，补 `is_enabled` 语义） |
| **依赖清单严格校验 + 冲突保护** | MoviePilot [源码] `dependency.py` | ✅ **实现**（V3 manifest dependencies 已校验，补版本约束求交） |
| **清单代际兼容**（v2/v3 平滑迁移） | MoviePilot [源码] `package.{v2\|v3}.json` | ✅ **实现**（V3 manifest 加 `sdk_version` 字段） |
| **虚拟路径 + `rootId/ref/version` 统一文件抽象** | MediaTidy [源码] `methods.json FileMeta` | ✅ **实现**（V3 Storage API 统一本地/115/CD2/OpenList） |
| **全局队列 + 同类型串行 + SSE 进度 + BUSY 去重** | MediaTidy [文档]+[源码] RunResult 状态机 | ✅ **实现**（阶段⑤任务中心核心） |
| **长任务续接 `tasks.wait` + `@continuation` + 幂等键** | MediaTidy [源码] `plugin.py` | ✅ **实现**（阶段⑤，解决插件进程重启后任务续跑） |
| **SHA1 为缓存键 + 能力版本号决定旧缓存失效** | MediaTidy [源码]+[文档] ffprobe.cacheExists / ProbeRevision | ✅ **实现**（V3 缓存层统一以内容哈希+schema 版本为键） |
| **规则与代码解耦（YAML，本地>订阅>内置）** | MediaTidy [文档] features/rules | ✅ **借鉴**（阶段⑤规则引擎） |
| **「网关配置即实例」+ 权限模板批量套用** | ResourceTool [文档] 实例管理/权限模板 | ✅ **借鉴**（V3 实例+权限模板，但数据模型自主设计） |
| **Session 监听 + Bot 交互双轨**（Bot 读不了非自有频道） | ResourceTool [文档] + 我们 V3 userbot 实测 | ✅ **已验证**（V3 TG userbot 走 telethon 账号登录） |
| **本地消息索引复用于搜索/求片/补漏** | ResourceTool [文档] | ✅ **借鉴**（V3 tg_messages 表已是此设计） |
| **Token 化免登录监控大屏** | ResourceTool [文档] `/monitor/<token>` | ⚠️ **谨慎**（安全审计后才做，避免未授权暴露） |
| ❌ 插件与宿主**同进程同依赖**（MoviePilot [源码] importlib） | MoviePilot v2 | ❌ **不照搬**——有宿主污染/依赖冲突风险；V3 用**独立子进程**更稳（任务书 §4.5） |
| ❌ Vuetify JSON 拼页面 / Vue 联邦 | MoviePilot [源码] get_form/get_page | ❌ **不照搬**——强耦合前端框架；V3 用自研 Design Token + 原生组件 |
| ❌ 虚拟分身 / 500+ 市场 + GitHub Token 重分发链 | MoviePilot [源码] plugininstance / package.v3 | ❌ **不照搬**——无多租户需求即过度设计 |
| ❌ MediaTidy 闭源授权 / 受信任插件非沙箱 / 115 风控单线程 | MediaTidy [文档] | ❌ **不照搬** |
| ❌ ResourceTool 付费授权/设备绑定/深度绑定 115 中心网关/Go 单体上限 | ResourceTool [文档] | ❌ **不照搬**（且**无源码，不做任何底层架构断言**） |

## 5. 最终架构选择（融合而非复制）

```
自研 V3 插件平台（吸收三家优点，规避其缺陷）
├─ 插件运行时：独立子进程隔离（优于 MoviePilot 同进程）+ 稳定契约（学 MoviePilot）+ 代际清单
├─ SDK：Service/Instance/Event/Dashboard/Navigation/Task/Storage/Config/Permission/Log/Notification
├─ 服务注册中心：一插件类型挂 N 实例（学 ResourceTool 理念，数据模型自研）
├─ 文件抽象：虚拟路径 + rootId/ref/version（学 MediaTidy 契约）
├─ 事件总线：广播 + 链式双模式、优先级队列、去重/重试/死信（学 MoviePilot，加持久化）
├─ 任务中心：全局队列 + 同类型串行 + 长任务续接 + 幂等键（学 MediaTidy）
├─ 缓存层：内容哈希键 + schema 版本失效（学 MediaTidy）
└─ 设计系统：自研 Design Token + Liquid Glass（不复制任何一家的 UI）
```

## 6. 未找到 / 无法访问（诚实记录）

- MoviePilot：v3 动态 `add_api_route` 挂载点、APScheduler 实际注册文件、`health.py`（52KB pip/uv 命令）未逐行读、`package.v3.json` 完整条目数（响应截断，≥49）、插件市场前端交互渲染未研究。
- MediaTidy：**主程序全部源码（404 闭源）**、调度器技术栈、缓存后端与远程 API、Webhook 接收端、alist 支持（未确认）、`methods.json` 后半段（截断于 `records.list`）。
- ResourceTool：**主程序源码零可得**（作者 GitHub 仅 fork、全局搜索无结果、Docker Hub 抓取失败）、Plex 支持未找到、`/features/emby-user-mgmt/` 404。
