# 仓库分类地图（Repo Layout）

> 本页定义「什么文件放哪里」。新增文件前先看这一页，保证仓库一眼看懂、排列整齐。
> 目录职责固定，**不按版本号、不按人名、不按日期新建顶层目录**。

## 1. 一级目录总表

| 目录 | 职责 | 是否发布 | 备注 |
|---|---|---|---|
| `app/` | **核心**：MediaHub 总控台运行时（后端 `app.py`、前端 `index.html`、Dockerfile、多实例 / 封面补齐 / 内存预警零凭据参考实现） | ✅ | 唯一必装组件；零第三方依赖 |
| `bots/` | **可选模块**：按机器人/服务名分子目录 | ✅ | `kfcgame` / `pt2cloud` / `embyboss` |
| `config/` | **配置模板**：只放 `*.example`，绝不放实体配置 | ✅ | `.env.example`、`services.json.example` |
| `deploy/` | **部署编排**：`docker-compose.yml`、安装脚本 | ✅ | compose 读取 `config/.env.example` 的键 |
| `docs/` | **文档**：按主题命名，不按日期 | ✅ | 见 §3 |
| `tests/` | **测试**：`test_*.py`，纯标准库，无外部服务依赖 | ✅ | 可被 CI 直接跑 |
| `tools/` | **仓库工具**：隐私守门、发码器等离线 CLI | ✅ | 不引入运行时依赖 |
| `scripts/` | **发布/运维脚本**：`bootstrap_github.py` 一键建仓 + 分类 + push + Release | ✅ | 只读环境变量取 token，绝不落盘凭据 |
| `.github/` | **GitHub 元数据**：Actions、ISSUE/PR 模板 | ✅ | 分类见 §4 |
| `.githooks/` | **本地钩子**：pre-commit 隐私守门 | ✅ | `git config core.hooksPath .githooks` |
| `audit/` | **本地报告**：扫描结果，`.gitignore` 拦截 | ❌ | 不入库 |

根目录只保留 6 个文档型文件：`README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`LICENSE`、`NOTICE`、`.gitignore`（及其配套点文件）。**根目录不放源码、不放脚本、不放快照。**

## 2. 命名约定

| 对象 | 规则 | 示例 |
|---|---|---|
| 目录 | 小写复数名词，无版本号 | `bots/` `docs/` `tests/` |
| 文档 | 小写连字符，按主题 | `multi-instance-runtime.md` |
| 配置模板 | `<实体名>.example` | `.env.example` → `.env` |
| Python | `snake_case.py`，测试 `test_*.py` | `privacy_scan.py` |
| 版本 | 语义化 tag `vX.Y.Z`，与 `CHANGELOG` 一一对应 | `v0.1.4` |

## 3. 文档分类（`docs/`）

| 读者目标 | 文件 |
|---|---|
| 第一次使用 | `quickstart.md` |
| 理解架构 | `architecture.md`、`multi-instance-design.md` |
| 接入多实例 | `multi-instance-runtime.md` |
| 排障 | `faq.md` |
| 维护者发布 | `publish-checklist.md` |
| 维护者整理仓库 | `repo-layout.md`（本页） |

## 4. GitHub 元数据分类（`.github/`）

```
.github/
├── workflows/ci.yml              # 三个守门 job：privacy / compile / compose-config
├── ISSUE_TEMPLATE/
│   ├── config.yml                # 入口分流：安全通告 / Discussions
│   ├── bug_report.yml            # 缺陷（按模块下拉 + 隐私确认）
│   └── feature_request.yml       # 功能建议（按方向下拉）
└── PULL_REQUEST_TEMPLATE.md      # 验证清单 + 隐私确认 + GPL 提示
```

**Issue 分类标签**（由 `scripts/bootstrap_github.py` 自动创建，与模板 `labels` 对齐）：

| 类型 | 标签 |
|---|---|
| 性质 | `bug` `enhancement` `triage` |
| 模块 | `app` `bots` `deploy` `docs` `tools` |
| 红线 | `security` `privacy` |

## 5. GitHub 仓库设置（分类标签）

| 项 | 值 |
|---|---|
| 仓库名 | `media-hub` |
| 可见性 | **Private 起步**，按 `publish-checklist.md` 验收后再转 Public |
| 主分支 | `master`（禁止 force push） |
| 简介 | 自托管影音统一总控台：Emby / MoviePilot / qBittorrent / MediaTidy / CloudDrive2 多实例管理 + Telegram 机器人套件（零内置凭据） |
| Topics | `emby` `mediatidy` `moviepilot` `qbittorrent` `docker` `self-hosted` `nas` `flask` `media-center` `home-lab` |
| Issue labels | 性质 `bug`/`enhancement`/`triage` · 模块 `app`/`bots`/`deploy`/`docs`/`tools` · 红线 `security`/`privacy` |
| 许可证 | 根 `LICENSE` = Apache-2.0；`bots/embyboss/` 分段 GPL-3.0（见 `NOTICE`） |

> 以上设置由 `scripts/bootstrap_github.py` 在授权后自动完成，无需手工点选。

## 6. 新增文件前的三条检查

1. **放得对吗**？对照 §1 表；不确定就放最接近的一级目录下，别新建顶层目录。
2. **干净吗**？只写 `your_xxx_here` / `changeme` / `127.0.0.1` / `host.docker.internal`；`*.env`、`config.json`、`secrets.json`、`*.db`、浏览器 profile 已被 `.gitignore` 拦截。
3. **留痕吗**？行为变更要写 `CHANGELOG.md` 的 `[Unreleased]`，版本发布打 tag，运行 `privacy_scan` 确认 `blocking: 0`。
