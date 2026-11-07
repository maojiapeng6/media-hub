# AGENTS.md —— AI 助手工作规范（本工作区）

> 本文件是给任何 AI 编程助手（WorkBuddy / Claude / Codex / Copilot 等）的强制工作规范。
> **开工第一件事：读 §1 的文档，然后才能动手。**

## 1. 开工必读（顺序固定，不可跳过）

1. `AI接力_共享记忆入口.md` —— 入口规则 + 最高指令 + 勘误速查
2. `AI接力_开源发布ROADMAP.md` —— 开源发布总锚点（进度 §8 / 缺口 §11 / 决策 §10）
3. `openrepo/docs/multi-instance-design.md` —— 多实例实施蓝图
4. `openrepo/docs/publish-checklist.md` —— 发布流程与验收清单
5. `.workbuddy/memory/` 最新日期日志 + `MEMORY.md` —— 上一轮做了什么

## 2. 铁律

- **每次版本更新自动存档并发布**（CHANGELOG + commit + tag；push 待 remote；用户不提醒也执行）。
- **隐私红线**：任何改动不得引入内网 IP、真实凭据、私人域名、TG UID；`openrepo/tools/privacy_scan.py` 守门 blocking=0 才允许提交。
- **NAS 线上真源红区**：改 media-hub 必须 paramiko 拉线上真源 → 增量补丁（锚点断言唯一）→ py_compile → SFTP 保 inode 回传（`cat /tmp/x > dst`）→ `docker restart` → 实测。**严禁拿本地副本整文件覆盖线上**（线上可能有其它会话上线的新功能，已踩坑）。
- **凭据零变更**：严禁改/重置线上任何现有密码；仅允许新增账号。
- **必须实测不许猜**；测试后 1:1 还原；永久改动先问。
- `docker restart` **不重读 compose env**——改环境变量必须 `docker compose up -d` 重建。
- Git Bash 的 `/tmp` ≠ Windows Python 的 `/tmp`——paramiko 传文件用工作区绝对路径。

## 3. 每轮收尾

- 更新 `AI接力_开源发布ROADMAP.md` §8 进度勾选 + §10 决策日志
- 追加 `.workbuddy/memory/YYYY-MM-DD.md` 当日日志（append-only，带 [任务名]）
- 若有版本变更：按 §2 第一条存档，并同步 `openrepo/` 到 NAS `/vol1/1000/docker/openrepo`

## 4. 关键入口速查（占位符——部署者按自己环境填写）

| 项 | 值 |
|---|---|
| NAS | `<你的NAS_IP>`（SSH 账号自定） |
| 面板 | `http://<NAS_IP>:5678`（管理员账号在首次启动向导创建） |
| 发布源（NAS） | `/vol1/1000/docker/openrepo`（面板容器内 `/dockroot/openrepo`） |
| 隐私守门 | `python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt` |
| 发码器 | `tools/license_issuer.py`（厂商密钥 `.vendor-secret.txt`，仅发码方持有） |
