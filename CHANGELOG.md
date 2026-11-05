# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号采用 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Planned
- 多实例服务注册表实施（P3-1~P3-5，见 docs/multi-instance-design.md）
- 业务写端点强制登录（G1）
- 首次配置向导对齐 services.json

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
