# AGENTS.md —— AI 助手工作规范（MediaHub 开源仓库）

> 本文件是给任何 AI 编程助手（WorkBuddy / Claude / Codex / Copilot 等）的强制规范。
> **开工先读 §1，再动手。**

## 1. 开工必读（顺序固定）

1. `README.md` —— 组件构成 + 快速开始（各服务连接全靠你填 `config/` 模板）
2. `docs/architecture.md` —— 架构与服务注册表
3. `docs/security.md` —— 已知缺口（G1 写端点未强制登录等）+ 部署安全清单
4. `docs/publish-checklist.md` —— 发布流程与验收清单
5. `CHANGELOG.md` —— 改了什么、有什么坑

## 2. 铁律

- **隐私**：不得引入真实凭据 / 内网 IP / 私人域名 / 用户 ID；示例一律 `your_xxx_here`、`127.0.0.1`、`host.docker.internal`。提交前必须过守门：
  ```bash
  python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt   # 需 blocking=0
  ```
- **配置外置**：所有外部连接信息走 `config/.env` / `app/config/services.json` / 环境变量，**不得写死在源码**。
- **凭据零变更**：升级/迁移不得重置用户已有凭据；新增账号走引导流程。
- **授权码**：`tools/license_issuer.py` 是厂商侧发码器，密钥只进 `.vendor-secret.txt`（已 gitignore）。普通用户用面板「📥 激活授权码」；无码可 30 天试用，过期自动只读。
- **先问后改**：破坏性/永久改动先说明（改什么/效果/风险），获确认再动手；改完必须实测。

## 3. 改动纪律

- 后端：`app/app.py`（标准库 http.server，无第三方依赖）；前端：`app/index.html`（单文件 SPA）。
- 改完：`python -m py_compile app/app.py` + `node --check` 校验改过的前端函数 + 冒烟（本地起服务 curl `/healthz`）。
- 提交：`CHANGELOG.md` → 守门 → `git commit`（Conventional Commits）→ 语义化 tag。
- `bots/embyboss/` 是 **GPL-3.0** 衍生（Sakura EmbyBoss），改动须同协议开源并保留上游版权；其余目录 Apache-2.0。

## 4. 布局与文档

目录职责见 `docs/repo-layout.md`；多实例见 `docs/multi-instance-design.md`（设计）与 `docs/multi-instance-runtime.md`（运行时）。
