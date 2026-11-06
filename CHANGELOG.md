# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号采用 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Planned
- 多实例服务注册表实施（P3-1~P3-5，见 docs/multi-instance-design.md）
- 业务写端点强制登录（G1）
- 首次配置向导对齐 services.json

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
