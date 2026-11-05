# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号采用 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- 首次配置向导：无配置时逐项引导填写外部服务凭据 + 连通性自检。
- 多实例服务注册表（`services.json`）：Emby / MoviePilot / qBittorrent / MediaTidy / CD2 / TG Bot 支持添加多实例，面板内 ➕ 添加。
- 凭据 Fernet 对称加密落盘（用户主密钥），运行时解密到内存，不落明文。
- 隐私守门扫描器 `tools/privacy_scan.py` + pre-commit 钩子 + CI 强制。
- docker-compose 全栈一键部署（`deploy/`）。

### Changed
- 外部连接全部改为环境变量 / `services.json` 外置，源码零硬编码凭据。
- 面板默认无鉴权 → 增加登录口令项（`MH_ADMIN_USER` / `MH_ADMIN_PASS`）。
- 危险容器操作（stop / rm）统一二次确认 + 后端白名单。

### Removed
- 移除源码中所有作者真实凭据（密码 / Token / API Key / 内网地址 / 私人域名 / TG UID）。
