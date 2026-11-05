# 贡献指南

感谢你有兴趣让 MediaHub 更好！

## 开发环境

```bash
# 本地跑面板（无需 Docker 也可）
cd app
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install flask requests pymysql cryptography psutil
cp ../config/.env.example .env                        # 填入你本地的服务地址
python app.py                                         # 默认 5678
```

## 提交规范

- Commit message 采用 [Conventional Commits](https://www.conventionalcommits.org/)：`feat: ...` / `fix: ...` / `docs: ...`。
- 一个 PR 聚焦一件事；行为变更请配对应说明。
- 提 PR 前本地务必过隐私守门：

```bash
git config core.hooksPath .githooks   # 启用钩子
python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt
```

> **CI 会强制跑隐私扫描**，未通过不允许合并。

## 隐私红线

- 任何 PR **不得**引入内网 IP、真实 Token / API Key / 密码 / 私人域名 / 群组 UID。
- 示例值只能用 `your_xxx_here`、`changeme`、`127.0.0.1`、`host.docker.internal`。

## 许可证相关

`bots/embyboss/` 源自 Sakura EmbyBoss（GPL-3.0）。对该目录的修改须遵循 GPL-3.0 条款并保留原作者版权。
