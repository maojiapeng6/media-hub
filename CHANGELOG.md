# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号采用 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Planned
- 多实例服务注册表实施（P3-1~P3-5，见 docs/multi-instance-design.md）
- 业务写端点强制登录（G1）
- 首次配置向导对齐 services.json

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
