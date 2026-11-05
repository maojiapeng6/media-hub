# 发布上线手册（P6 · 自助推送指南）

> 🔴 **最高指令（2026-11-05，永久生效）**：**每次版本更新都自动走本手册的「常规发布流程」并存档，每一次都要，用户不提醒也必须执行。**
> **每次新任务开工先读三文档**：①根目录 `AI接力_开源发布ROADMAP.md` ②本文件 ③`multi-instance-design.md`。

## 常规发布流程（每次版本更新固定走，不许跳步）

```bash
cd openrepo
# 1. 更新 CHANGELOG.md（Keep a Changelog 格式，写清 Added/Changed/Fixed）
# 2. 守门（非 0 禁止发布）
python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt --exclude audit .githooks
# 3. 存档
git add -A && git commit -m "release vX.Y.Z: <一句话>"
git tag vX.Y.Z
# 4. 发布（remote 配好后）
git push origin master --tags
# 5. 回写锚点：更新根目录 ROADMAP §8 阶段进度 + .workbuddy/memory 当日日志
```

## 首次发布（三步，2 分钟）

> 本仓库在本地已 100% 就绪（隐私守门全绿、CI 配齐、文档齐全）。
> 推送到 GitHub **不需要把任何凭据交给任何工具**——按下面三步自己就能完成，全程 2 分钟。

## 第 0 步：发布前自查（30 秒）

```bash
cd openrepo
git log --oneline                 # 应看到 3+ 次提交，无隐私字样
git ls-files | grep -E "config\.json$|\.env$|privacy-hashes"   # 应无输出
python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt \
    --exclude audit .githooks     # 应输出 "blocking": 0，退出码 0
```

## 第 1 步：在 GitHub 建空仓库

1. 登录 GitHub → 右上角 **+** → **New repository**
2. 仓库名建议：`media-hub`（或你拍板的名字）
3. **Private**（先私有，验收后再转 Public）
4. ⚠️ **不要**勾选任何初始化选项（README/.gitignore/license 都不要——本地已有，勾了会冲突）
5. Create repository

## 第 2 步：本地推送（两条命令）

```bash
cd openrepo
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin master --tags
```

- 推送时 GitHub 会弹浏览器授权（或要求 PAT 粘贴一次），之后记住凭据。
- `--tags` 会把 `v0.1.0-rc` 一起推上去，GitHub 仓库页自动出现 Releases 入口。

## 第 3 步：验收清单（转 Public 前）

- [ ] GitHub 页面逐目录点开看一遍：`app/`、`bots/`、`config/`、`deploy/`、`docs/`
- [ ] 用 GitHub 页面右上角搜索框搜：你本地 `.privacy-hashes.txt` 里登记的每一个**原始值**（QQ 邮箱、密码、内网 IP、UID、私人域名）—— **都应零结果**。⚠️ 原始值只存你本地，绝不写进本仓库任何文件（守门扫描器会拦截）。
- [ ] CI（Actions 页）三个 job 全绿：privacy / compile / compose-config
- [ ] Settings → General → Danger Zone → **Change visibility → Public**
- [ ] About 栏填简介 + topics（如 `emby` `mediatidy` `moviepilot` `docker` `self-hosted`）
- [ ] （可选）Settings → Branches 给 `master` 加保护规则，禁止 force push

## 之后交给 AI 做的部分（说一声即可）

| 想做什么 | 对 AI 说 |
|---|---|
| 修发布缺口 G1（写操作强制登录） | 「修 ROADMAP §11 G1」 |
| 多实例 ➕ 实施 | 「按 docs/multi-instance-design.md 做 P3-1」 |
| 每次更新后推送 | 「同步 openrepo 到 GitHub」（有 remote 后我可直接 push，需你首次授权过） |
| 加 Docker 镜像发布 | 「配 GitHub Actions 推 ghcr.io」 |

## 常见问题

- **push 被拒 `non-fast-forward`**：说明建仓库时勾了初始化。解决：`git pull origin master --allow-unrelated-histories` 再 push；或删掉远端仓库重建。
- **想以后换仓库名**：GitHub 仓库 Settings 改名即可，本地 `git remote set-url origin <新地址>`。
- **忘了推 tags**：`git push origin --tags` 单独补。
