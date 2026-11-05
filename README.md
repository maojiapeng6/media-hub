# MediaHub · 影音总控台 + 机器人套件

> 一个自托管的家庭影音**统一总控台**：把 Emby / MoviePilot / qBittorrent / MediaTidy / CloudDrive2 以及 Telegram 机器人整合进一个 Web 面板，覆盖 PT 链路、订阅求片、整理入库、积分经济与多端同步。
> 零内置凭据 —— 所有服务实例、Token、API Key 由**你自己填写**，配置后即点亮对应功能板块。

中文 · English (coming soon)

---

## ✨ 特性

- **统一总控台**：一个 Web 页面管理全部媒体组件，容器启停/日志/资源占用可视化（基于 Docker Engine API）。
- **多实例**：Emby、MoviePilot、qBittorrent、MediaTidy、CD2、TG 机器人均支持 **➕ 添加多个实例**，各自填 URL/Token，按实例路由。与 MediaTidy 新建整理方案、RT 添加 Emby 的多实例心智一致。
- **PT 全链路**：订阅 / 求片 / 保号 / 辅种 / 上云 strm 的自动化编排。
- **积分经济机器人**：Emby 观看时长 → 积分、签到、兑换阁、等级段位、跨端台账同步。
- **首次配置向导**：无配置时引导逐项填写 + 连通性自检；`install.lock` 区分首装/升级，**升级绝不覆盖已有凭据**。
- **隐私友好**：源码零硬编码密钥（CI 强制 `privacy_scan` 守门）。

## 🚀 快速开始

```bash
# 1. 克隆
git clone <your-repo> && cd mediahub

# 2. 生成配置（模板全为占位符）
cp config/.env.example        app/.env
cp config/services.json.example app/config/services.json

# 3. 编辑 .env / services.json，填入你自己的 Emby API Key、MP/qB/MT 账号、TG Bot Token…

# 4. 起面板
docker compose -f deploy/docker-compose.yml up -d mediahub
# 浏览器打开 http://<你的NAS_IP>:5678 → 首次进入配置向导
```

> 详细部署见 [`docs/`](docs/)：[快速开始](docs/quickstart.md) · [架构图](docs/architecture.md) · [常见问题](docs/faq.md)

## 📦 组件

| 目录 | 说明 | 是否必装 |
|---|---|---|
| `app/` | MediaHub Web 总控台（Python 标准库 `http.server`，单文件 `app.py` + `index.html`，**无第三方依赖**） | ✅ 核心 |
| `bots/kfcgame/` | 积分经济 + PT 任务 + 求片 Telegram Bot | 可选 |
| `bots/pt2cloud/` | PT 上云 / strm 链路服务 | 可选 |
| `bots/embyboss/` | Emby 用户管理 Bot（**Sakura EmbyBoss 衍生**） | 可选 |
| `config/` | 配置模板（`.env.example` / `services.json.example`） | ✅ |
| `deploy/` | docker-compose 全栈编排 | ✅ |
| `tools/` | 隐私守门扫描器等仓库工具 | 开发 |

## 🔐 安全与隐私

- 本仓库**不包含任何作者真实凭据**；所有连接信息经 `.env` / `services.json` 外置，运行时读取。
- `config.json`、`*.db`、`secrets.json`、浏览器 profile 等已被 `.gitignore` 全量拦截。
- 提交前守门：`python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt`（退出码非 0 禁止提交）。启用钩子：`git config core.hooksPath .githooks`。
- 面板默认**无鉴权**，请务必置于可信网络或自行加反代认证；危险容器操作有前端二次确认。

## ⚖️ 许可证（分段声明）

- 本仓库自研部分（`app/`、`bots/kfcgame/`、`bots/pt2cloud/`、`tools/` 等）：**Apache-2.0**（根 `LICENSE`）。
- `bots/embyboss/`：源自 **Sakura_embyboss**（上游 berry8838，**GPL-3.0**）。该目录整体继续以 GPL-3.0 发布（全文见 `bots/embyboss/LICENSE`），修改须同许可证开源并保留上游版权声明。
- 第三方组件归属详见 [`NOTICE`](NOTICE)。

> ⚠️ **合规声明**：本项目仅提供自托管工具与自动化编排，不内置任何站点账号、不提供影视资源。使用者须自行遵守所连接服务（PT 站、网盘、流媒体）的服务条款及所在地法律法规。

## 🤝 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。提 PR 前请确保 `privacy_scan` 守门通过。

## 🙏 致谢

MediaTidy · MoviePilot · qBittorrent · CloudDrive2 · Emby · Sakura EmbyBoss。
