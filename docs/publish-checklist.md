# 发布上线手册（P6 · 自动建仓与推送）

> 🔴 **最高指令（2026-11-05，永久生效）**：**每次版本更新都自动走本手册的「常规发布流程」并存档，每一次都要，用户不提醒也必须执行。**
> **每次新任务开工先读三文档**：①根目录 `AI接力_开源发布ROADMAP.md` ②本文件 ③`multi-instance-design.md`。
> **用户 2026-11-06 授权**：「GitHub 上如果要建什么库的，你自己建好就行，分类做好，整齐排列」→ 仓库名/可见性/许可证/topics/目录分类由 `docs/repo-layout.md` 固定，**不需要人工去 GitHub 点 New repository**。

## 常规发布流程（每次版本更新固定走，不许跳步）

```bash
cd openrepo
# 1. 更新 CHANGELOG.md（Keep a Changelog 格式，写清 Added/Changed/Fixed）
# 2. 守门（非 0 禁止发布）
python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt --exclude audit .githooks
# 3. 存档
git add -A && git commit -m "release vX.Y.Z: <一句话>"
git tag vX.Y.Z
# 4. 发布（remote 配好后，AI 每次自动执行）
git push origin master --tags
# 5. 回写锚点：更新根目录 ROADMAP §8 阶段进度 + .workbuddy/memory 当日日志
```

## 🚀 首次发布：授权一次，之后全自动

### 授权（唯一需要人工的一次动作，三选一）

| 方式 | 操作 | 效果 |
|---|---|---|
| **A（推荐）** | 在 WorkBuddy 启用 **GitHub 连接器** 并完成 OAuth | 之后 AI 可直接建仓、推送、管 Release |
| B | 设置环境变量 `GITHUB_TOKEN=<PAT>`（scope: `repo`） | 脚本可读 |
| C | 安装 `gh` CLI 并 `gh auth login` | 脚本通过 `gh auth token` 读取 |

> 🔴 任何 Token 都**不写入仓库任何文件**；`scripts/bootstrap_github.py` 只在进程内存中读取，日志不打印值。

### 一键执行

```bash
cd openrepo
python scripts/bootstrap_github.py --dry-run     # 先只看计划，不联网、不改动
python scripts/bootstrap_github.py               # 建 Private 仓 + 分类 + push + Release
```

脚本依次做 6 件事（**每步幂等，重跑安全**）：

1. `privacy_scan` 守门（`blocking != 0` 立即中止）
2. 取当前登录 owner → 查仓库，不存在则建**空仓**（不勾任何初始化，避免 `non-fast-forward`）
3. 写简介 + PUT topics + 建 10 个 issue 分类标签（性质/模块/红线，见 `docs/repo-layout.md` §4-§5）
4. `git remote add|set-url origin`
5. `git push -u origin HEAD:master --tags`（含全部历史 tag）
6. 按最新 tag 建 GitHub Release（正文自动取 `CHANGELOG.md` 对应段落）

常用参数：`--public` 直接公开 · `--owner <org>` 建到组织 · `--no-push` 只建仓 · `--skip-release` · `--tag <tag>`。

### 发布前自查（脚本会自动跑，人工可复核）

```bash
cd openrepo
git log --oneline                 # 应看到多次干净提交，无隐私字样
git ls-files | grep -E "config\.json$|\.env$|privacy-hashes"   # 应无输出
python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt \
    --exclude audit .githooks     # 应输出 "blocking": 0，退出码 0
```

### 转 Public 前验收清单

- [ ] GitHub 页面逐目录点开：`app/` `bots/` `config/` `deploy/` `docs/` `tests/` `tools/` `scripts/` `.github/`
- [ ] 仓库 About 简介与 10 个 topics 已自动填好；`ISSUE_TEMPLATE` 三个入口可用；issue labels 分类齐全
- [ ] 用 GitHub 搜索框搜 `.privacy-hashes.txt` 里登记的每一个**原始值**（QQ 邮箱、密码、内网 IP、UID、私人域名）—— **都应零结果**。⚠️ 原始值只存本地，绝不写进本仓库任何文件（守门扫描器会拦截）
- [ ] Actions 三个 job 全绿：privacy / compile / compose-config
- [ ] Settings → General → Danger Zone → **Change visibility → Public**
- [ ] （可选）Settings → Branches 给 `master` 加保护规则，禁止 force push

### 2026-11-06 实机状态

`git remote -v` 空、`gh` 未安装、无 `GITHUB_TOKEN/GH_TOKEN/GITHUB_PAT`、无 SSH key、
Windows 凭据管理器无 GitHub 条目、GitHub 连接器未绑定 → **push 客观不可执行**。
存档（commit + tag）已全部完成；**授权一到位即可自动建仓推送，无需再提醒**。

## 之后交给 AI 做的部分（说一声即可）

| 想做什么 | 对 AI 说 |
|---|---|
| 修发布缺口 G1（写操作强制登录） | 「修 ROADMAP §11 G1」 |
| 多实例 ➕ 实施 | 「按 docs/multi-instance-design.md 做 P3-1」 |
| 首次建仓 + 推送 | 「同步 openrepo 到 GitHub」（授权后 AI 直接跑一键脚本） |
| 加 Docker 镜像发布 | 「配 GitHub Actions 推 ghcr.io」 |

## 常见问题

- **push 被拒 `non-fast-forward`**：说明建仓库时勾了初始化。解决：`git pull origin master --allow-unrelated-histories` 再 push；或删掉远端仓库重建（脚本建仓不会触发此问题）。
- **想以后换仓库名**：GitHub 仓库 Settings 改名即可，本地 `git remote set-url origin <新地址>`。
- **忘了推 tags**：`git push origin --tags` 单独补。
- **PUT topics 返回 403**：Token 缺 repo 权限，不影响代码推送；稍后在仓库 About 手动补齐，或换有权限的 Token 重跑脚本（幂等）。
- **想拆成多个仓库**：按许可证隔离（根 Apache-2.0 / `bots/embyboss/` GPL-3.0）是唯一有工程理由的拆法，但会改动隐私基线与 CI，需单独评估后再执行，本轮保持单主仓。
