# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号采用 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Planned
- 业务写端点强制登录（G1）
- 首次配置向导对齐 services.json
- 发布副本与线上（v0.1.3 多实例已上线）的代码级全量同步评估

## [0.1.8] - 2026-11-09 · 夸克 → 115 自动转存（OpenList + CD2）

> 线上 media-hub V1.49 已实机验收；发布副本仅提供**零凭据参考实现**，
> 不整文件覆盖线上真源（ROADMAP §2/§5 隐私红线）。

### Added
- **自动监控转存链路**：OpenList 列「夸克中转」（`refresh:true` 强制刷新目录缓存）
  → `/api/fs/get` 取直链 → 流式下载 → 写入 CD2 的 115 挂载目录，由 CD2 上传 115。
  后台线程定轮扫描，按 `相对路径|大小` 去重，状态落盘、可重入、可启停。
- **Web 面板**：容器控制页新增「夸克 → 115 自动转存」卡（状态 / 立即扫描 /
  启停 / 最近记录 + 秒传说明）；`GET /api/q2f/status`、`POST /api/q2f/scan`、
  `POST /api/q2f/config`（写端点走登录闸）。
- **容器控制注册 OpenList**：容器注册表与动作总线增加 `openlist`，
  服务目录与容器列表均可管理其启停 / 重启 / 日志 / 状态。
- **`app/quark2five.py`**：零凭据、纯标准库、依赖注入的参考实现
  （服务地址 / 令牌 / 路径全部由调用方传入，无任何内网地址或密钥硬编码）。

### Notes（秒传真相）
- **不存在「夸克秒传到 115」**：115 秒传只在 115 云端已存在相同 SHA1 时成立；
  夸克与 115 哈希库不共享，跨盘不保证秒传，未命中即完整实传（夸克侧限速）。
- 实机：OpenList 跨存储 `copy` 到 115 Cloud 被 115 返回「校验文件失败 code 10002」，
  故采用 CD2 落地链路；CD2 上传时若命中 115 云端已有文件则由 CD2 自行秒传。
- 坑：OpenList 目录缓存会漏掉新建目录，列目录必须 `refresh:true`；线上真源部署
  必须增量补丁 + 保 inode 回写（本轮遭遇并发会话整文件覆盖，已重打恢复）。

## [0.1.7] - 2026-11-09 · 内存预警（面板 + TG）+ 排行榜默认头像 / 观影清单

> 线上 media-hub V1.48 已实机验收；发布副本提供**零凭据参考实现 + 单测 + 文档**，
> 不整文件覆盖线上真源（ROADMAP §2/§5 隐私红线）。

### Added
- **🛡 仪表盘「内存预警」独立卡片**：内存占用 / 可用 / Swap / PSI IO full / OOM 累计 /
  load / 容器占用 Top / 处置建议；阈值与开关可在面板调整，支持「测试 TG 推送」。
  后台 60s 轮询，等级跃迁、冷却到期、新增 OOM 才推，**绝不刷屏**；只读，不自动关任何进程。
- **排行榜每人都有头像**：18px 圆形、行更紧凑；无官方头像时显示**系统默认人形头像**
  （按用户名稳定哈希配色），真图 `200 0 bytes` 也会回落，绝不出现破图。
- **点头像看「他都看了哪些影片」**：新增 `GET /api/emby/userplays`，跨多台媒体服务器
  合并播放明细并去重；弹层按 7/30/90/365 天切换，展示海报 / 片名 / 时间 / 时长 /
  来源 / 客户端 / TMDB 直达，保留整行跳「播放数据」页的既有行为。
- **`app/memguard.py`**：零凭据、纯标准库、回调注入的参考实现
  （`snapshot` / `level_of` / `should_push` / `format_message` / `merge_userplays`）。
- **`tests/test_memguard.py`**：25 项单测；全量 `unittest discover` **51 项全绿**。
- **`docs/memguard.md`**：数据流、API 契约、推送节流表、前端三处改动、部署坑与验收记录。

### Fixed
- 🔴 **恢复 v1.47c 封面模块**：旧并发会话用 13:43 的过期快照回写，把 `/api/cover/*` 全抹掉
  （status/scan 404）。本轮从**最新线上真源**重新锚点注入 v1.47c 代码块，并补回
  提取时漏掉的模块级缓存 `COVER_LAST` / `_TMDB_POSTER_CACHE`（否则 `cover_status` 500）。
- 🔴 **新模块排到 EMBY 上面**：未登记自定义布局 `OVM_DEF` 的卡片会落兜底 key 并被排到
  「实时仪表盘」标题栏之前。已登记 `memguard` key + `ovmKeyOf` + 轮询槽，EMBY 实时观看区恢复置顶。
- 🔴 **CSS 全部失效**：新样式曾插进 `@media(max-width:640px)` 未闭合的块里，桌面端
  KPI 竖排、配置面板默认展开。改为在 `</head>` 前开独立 `<style>`。
- MediaTidy 面板代理恢复（`/api/mt` 200；容器与 2019/8091 直连均正常）。

### Changed
- **A（用户选定）**：禁用 `framebase` 每 30 分钟的全库动态封面任务（只改该任务的
  `enabled`，其余 3 个任务 / 预设 / 凭据零变更，双备份、保 inode）；启动日志不再
  「恢复 1 个定时任务」、不再触发多线程 GIF 渲染，掐掉每半小时打爆 1 GiB cgroup 的放大器。

### Notes
- 实机验收：`memguard/status` 200 且 Swap 100% 正确判 `crit`；手动测试推送与后台自动推送
  均 `ok=true`；EMBY 区 y=464 / 预警卡 y=3634（顶部不受影响）；12 行排行榜 = 12 个头像；
  真实点击头像弹出 65 条观影记录；封面扫描 50 缺 3 → 试运行命中 3 张官方海报 →
  真实补齐 3 张 → 回读 `ImageTags.Primary` 有值 → 复扫缺 0。
- 隐私守门 `privacy_scan --paths . --baseline`：168 文件、blocking **0**（50 项 MED 全白名单）。
- push 仍待 GitHub 授权（本机无 origin / token / SSH / 连接器），存档 commit + tag 已完成。

## [0.1.6] - 2026-11-08 · 媒体库「缺失封面扫描 + 一键补齐」

> 线上 media-hub V1.47c 已实机验收；发布副本提供零凭据参考实现与文档，
> 不整文件覆盖线上真源（ROADMAP §2/§5 隐私红线）。

### Added
- **媒体库页新模块「🖼 缺失封面扫描与补齐」**：扫描 Emby `ImageTags.Primary` 为空的 Movie/Series → 按 TMDB ID 抓官方海报 → **写进 Emby 媒体库本体**（`POST /Items/{Id}/Images/Primary`）→ 回读校验。Emby 原生 UI 与全部客户端立即生效。
- 线上新增 6 个端点：`GET /api/cover/servers|scan|status`、`POST /api/cover/scan_async|fix`（写端点走既有授权闸 `sec_write_gate`），补齐走异步 job + 进度查询。
- 前端支持：实例下拉（**自动派生，新增媒体服务器无需改代码**）、最近 1000 / 5000 / 全库、试运行（dry，绝不写库）、全选 / 单张补、匹配来源与失败原因展示、进度轮询。
- **`app/cover_repair.py`**：零凭据、纯标准库、回调注入的参考实现（`scan_missing` / `find_poster` / `repair` / `encode_for_emby`）。
- **`tests/test_cover_repair.py`**：16 项单测全绿（分页、Primary 判定、不跨类型回退、多语言海报集、严格/宽松片名匹配、试运行不写库、上传编码与失败计数）。
- **`docs/cover-repair.md`**：处理链路、匹配红线、API 契约、配置要求、已知边界、实机验收记录。

### Fixed
- 🔴 **误补海报**：TMDB `movie` / `tv` 是独立 ID 空间，旧逻辑「movie 无海报 → 回退 tv/{同 ID}」会把别的作品海报写进媒体库。改为严格同类型；无图则标注原因不写库（宁缺勿错），误补图片已从线上删除。
- Emby 4.10 图片上传端点须提交 **base64 文本** + `application/octet-stream`；直接 POST 二进制会返回 204 但 `ImageTags` 仍为空 → 现按 Swagger 定义封装并在写入后回读校验。
- 宽松匹配被采纳时正确标注「需人工核对」（单测驱动修复，参考实现与线上一致）。

### Notes
- 实机：最近 5000 条共 73 条缺封面 → 63 张成功写入 → 10 条 TMDB 本身无图（演唱会 / 古典现场等，系统标明原因）；连同前序批次 **共补 79 张**，复扫误补 0、已补条目跨重启保持。
- 与 9kg 域「缺图重读」职责互补：后者只重读本地已有图片，本模块负责「本地根本没图」的 strm 新片。
- push 仍待 GitHub 授权（本机无 token / SSH / 连接器），存档 commit + tag 已完成。

## [0.1.5] - 2026-11-08 · 面板 GitHub 发布通道 + 巡检第 7 段

### Added
- **🚀 同步 GitHub（面板按钮）**：版本公告页一键把 NAS 发布源（`/dockroot/openrepo`，本仓库的 NAS 侧副本）提交到 GitHub——Git Data API 纯 HTTP 单 commit（无容器内 git 依赖）、blob sha1 本地比对跳过未变更、`sha:null` 显式删除远端多余文件、走既有代理。
  与 `scripts/bootstrap_github.py`（本地建仓+首推）互补：建仓一次，日常同步靠面板。
- **🏷 发布 Release（面板按钮）**：版本号打 tag + 创建 Release（tag 缺省取版本公告版本）。
- **「🚀 GitHub 发布与同步」配置卡**：Token（保存后不回显）/仓库/分支/PT 站点清单/AI 服务地址，走 `/api/config`（`gh_*`/`pt_sites`/`ai_probe` 进 SEC_ALLOW_NEW）。
- **深度巡检第 7 段「商业化服务栈」**：机器人容器运行态（kfcgame/embyboss/pt2cloud，健康检查+重启计数）、MySQL 连通+表数、PT 站点首页连通（**只 GET 首页绝不试密**，403/429/503 判 CF 挑战而非宕机）、AI 服务 /v1/models、GitHub 通道可达性。
- `GET /api/gh/status`、`POST /api/gh/push`（登录闸+异步 job）、`POST /api/gh/release`（登录闸）。
- `docs/security.md` 安全部署清单；`AGENTS.md` AI 协作规范（随仓库分发，任何 AI 助手开工必读）。

### Fixed
- 面板 `apiPost` 无超时问题同步上线（线上原为裸 fetch，后端挂起时按钮永久转圈）。

## [0.1.4] - 2026-11-06 · GitHub 仓库分类规范 + 一键建仓发布

> 用户授权「GitHub 上要建什么库自己建好，分类做好、整齐排列」；本轮把仓库分类与自动发布链路全部落地。

### Added
- **`.github/ISSUE_TEMPLATE/`**（`config.yml` 分流 / `bug_report.yml` / `feature_request.yml`）+ `PULL_REQUEST_TEMPLATE.md`：按模块分类、强制隐私确认、GPL 提示（补齐 ROADMAP P5）。
- **`docs/repo-layout.md`**：一级目录职责表、命名约定、文档与 GitHub 元数据分类、仓库设置（名称/可见性/topics/许可证）、新增文件三条检查。
- **`scripts/bootstrap_github.py`**：零凭据一键建仓 → 设置简介 / topics / 10 个 issue 分类标签 → 配置 remote → push master + 全部 tag → 建 Release；全幂等，支持 `--dry-run` / `--public` / `--owner` / `--no-push`；**推送前强制 `privacy_scan` 守门**。
- `config/.env.example` 补齐 `deploy/docker-compose.yml` 实际引用的全部变量键（`PTDB_*` / `KFC_MASTER_KEY` / `RT_*` / `PT2CLOUD_DB_PASS`），消除 compose 未定义变量告警。

### Changed
- README 快速开始改为与 compose 真实路径一致（`deploy/.env` + 首次配置向导），补完整目录树与组件表。
- `docs/publish-checklist.md`：由「用户手工三步」升级为「**授权一次后 AI 全自动**」，补齐分类设置与验收清单。
- `deploy/docker-compose.yml` 注释中的复制命令笔误修正。

### Notes
- **不拆仓**：保持单主仓 + 分段许可证声明（根 Apache-2.0 / `bots/embyboss/` GPL-3.0），避免破坏既有隐私基线与 CI；按许可证拆仓留作后续可选评估。
- 本轮**仍未 push**：实机核实本机无 `gh`、无 `GH_TOKEN/GITHUB_TOKEN`、无 SSH key、无凭据管理器条目、GitHub 连接器未绑定。授权后脚本自动完成建仓与发布，无需人工新建仓库。

## [0.1.3] - 2026-11-06 · N 实例注册表 + 实时观看布局/MT 完整名

> 线上 media-hub 已按锚点增量热修并实机验收；发布副本为**零凭据参考实现 + 运行时文档**，
> 不整文件覆盖线上真源（ROADMAP §2/§5 隐私红线）。

### Added
- **动态 N 实例注册表** `app/multi_instance.py`（纯标准库、零凭据、`tests/` 10 项单测全过）。
  - 老配置成对内置键自动折算为前两实例，**零变更升级**；新增实例存独立 JSON，保存即生效（3s 缓存），无需重启。
  - `public()` 永不下发明文 api_key；表单打码值回存时保留原 Key，编辑不破坏凭据。
- **`docs/multi-instance-runtime.md`**：数据模型、API 契约、自动派生矩阵、发布副本 6 步接入清单、已知边界。
- 线上新增 `/api/emby/instances`（GET）与 `/api/emby/instances/save` / `test`（POST），前端「➕ 媒体服务器」弹窗。
- 实时观看会话新增 `mt_full_name`（整理后完整文件名），卡片右侧单行展示、悬停看全文。

### Changed（线上热修，语义与本仓库参考实现一致）
- 实时观看 / 总览统计 / 播放记录 / 排行榜 / 来源筛选 / 来源徽标 / Emby 管理页按钮 /
  图片扫描 / 测速 / 动作分发 **全部改为遍历注册表**，新增第 3、4 台自动各创建一份数据。
- 单会话实时观看卡限宽 `minmax(380px,620px)`、右侧顶部对齐，消除封面右侧大片空白；
  片名 + 用户名 + TG 头像/ID 明确排在封面右侧。
- 相关标题文案去「两台」字样，改为「全部实例」。

### Fixed
- 🔴 模块加载期求值 `EMBY_INSTANCES = emby_instances()` 会调用尚未定义的注册表函数 → 容器崩溃循环；改惰性求值。
- 新增实例动作分发误走通用容器动作 → 运行时惰性注册到 `ACTIONS`。

### Verification（线上实机）
- 33 处锚点补丁全命中且幂等；宿主 + 容器 `py_compile` OK；整页 JS 语法 OK；HTTP 200。
- E2E：新增第 3 台 → `instances` 2→3、总览出第 3 张卡、`rank`/`plays` 的 `parts` 自动多出该实例、
  动作端点返回业务错误而非 500；凭据打码无泄漏；删除后 1:1 还原为 2 台。
- ⚠️ 发布副本仍是 v0.1.1 单实例旧基线，代码级全量同步需单独评估（禁止整文件覆盖）。

## [0.1.2] - 2026-11-06 · 实时观看/排行榜/动态范围体验修复

> 本次改动同时已热修到线上 media-hub；发布副本为**等效逻辑同步**（单实例基线适配，零凭据注入）。

### Added
- **实时观看详细动态范围徽章**：卡片标题旁新增醒目 chip，直接展示完整动态范围串。
- **文件名驱动的动态范围解析**（`_norm_dr_full`）：Emby 对 strm/115 库 `DvProfile` 恒为 `None`，
  真实 Profile/格式写在整理后的文件名里。新增「文件名优先 + Emby 字段兜底」解析，
  支持 `Dolby Vision P4/P5/P7/P8`、`HDR10+`、`HDR10`、`HLG`、`SDR` 及组合（如 `Dolby Vision P8 + HDR10+`）。
- **实时观看用户头像**：会话数据新增 `user_id` / `user_avatar`（走 `/embyimg/.../users/.../Primary` 后端代理，
  API Key 不下发前端），圆形头像可点击跳转 TG 主页。

### Fixed
- 🔴 **实时观看头像空白**：图片代理对「无头像用户」返回 `HTTP 200 + 空 body`，`onerror` 不触发导致空白。
  改为「首字母色块垫底 + 真图覆盖」，`onerror` 与 `onload naturalWidth===0` 双路回落，绝不空白。
- 🔴 **动态范围只显示「Dolby Vision」无 P 号 / 前端丢弃 P8**：`dr` 正则用 `$` 精确锚定把 `P8` 后缀丢掉，
  现放宽匹配并优先采用后端全串 `dr` 字段。
- 🔴 **「扫描媒体库」指令无效**：刷新 URL 误带 `RefreshChildren=true` 使本版 Emby 把库扫描变成 10ms
  no-op（HTTP 204 但不扫文件）。移除该参数，恢复真实递归扫盘。
- **排行榜头像**：补齐「200 空图」回落，压缩行高（padding 3px 10px、头像 20px），每行必有头像且不撑高。

### Notes
- 排行/会话头像、动态范围均为纯前端展示 + 只读 Emby 字段，不涉及任何写操作或凭据变更。

## [0.1.1] - 2026-11-05 · 授权码商用闭环

### Added
- **厂商发码模式（G4）**：`KFC_VENDOR_SECRET` 环境变量启用跨机发码——厂商密钥签发的授权码可在任何配置同密钥的部署实例上激活；未配置时回退本机随机密钥（个人部署兼容）。校验为双密钥链（厂商优先、本机兜底）。
- **授权码签发器（G5）**：`tools/license_issuer.py` 离线 CLI（issue/list），与面板算法同构，含本地台账（`.license-ledger.json`，不入库）。

### Fixed
- 🔴 **授权码签名形同虚设**：`license_sign` 原实现 `sec_b32(n,13)[:6]` 因 32bit 截断+左侧补 `A`，签名恒为 `AAAAAA`——任何伪造码均可通过校验。现取 HMAC 低 30bit 编 6 字符，签名真实生效。**旧签名码全部失效，需重签。**
- 🔴 **合法授权码自签自验失败**：`_norm_code` 的易混映射 `I→J/L→K/O→Q` 会改写合法码（字母表实际含 I/L/O）。现只映射不在字母表中的 `0→O、1→I`。

### Security
- 吊销 3 个旧签名联调残留码；旧签名码在新体系下验签直接拒绝。

## [0.1.0-rc] - 2026-11-05

### Added
- 首次干净发布提交：media-hub 总控台 + bots（kfcgame / embyboss / pt2cloud）+ 配置模板 + 全栈 compose + 文档 + CI。
- 隐私守门：`tools/privacy_scan.py`（规则 + 白名单 baseline + 个人秘密哈希清单机制）+ pre-commit 钩子 + GitHub Actions。
- 首次配置向导骨架（`sec_init` / `install.lock` 双模式）。

### Changed
- 源码零硬编码凭据：外部连接全部环境变量 / `webcfg.json` / `services.json` 外置。
- `apiPost` 增加 AbortController 超时兜底（60s），失败返回 `{ok:false}` 与 `apiGet` 对齐——修复后端挂起时按钮永久转圈。
