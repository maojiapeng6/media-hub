## 改动说明

<!-- 一句话说清改了什么、为什么改 -->

## 关联

- 关联 issue：#
- 涉及模块：`app/` / `bots/` / `deploy/` / `docs/` / `config/` / `tools/`

## 验证

- [ ] 本地 `python tools/privacy_scan.py --paths . --baseline .privacy-baseline.txt` → `blocking: 0`
- [ ] 相关 Python 文件 `py_compile` 通过
- [ ] 涉及线上行为时已实机验证（贴脱敏后的结果）
- [ ] 未改动、未重置任何已有凭据；升级路径向后兼容

## 隐私确认

- [ ] 本 PR 不含真实 Token / API Key / 密码 / 内网 IP / 私人域名 / 浏览器 profile
- [ ] 新增示例值只用 `your_xxx_here`、`changeme`、`127.0.0.1`、`host.docker.internal`

> 修改 `bots/embyboss/` 时须遵守 GPL-3.0 并保留上游版权声明，见 `NOTICE`。
