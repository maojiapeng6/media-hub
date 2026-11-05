# 快速开始

## 前置

- 一台 Linux/NAS（x86_64 或 ARM64），装好 Docker 与 docker compose
- 至少一个已运行的 Emby/Jellyfin，以及你要接入的服务（MoviePilot / qB / MediaTidy / CD2）

## 1. 配置

```bash
mkdir -p app/config
cp config/services.json.example app/config/services.json
cp config/.env.example          app/.env
```

编辑 `.env` 与 `services.json`，填入你自己的服务地址与凭据（全部占位符替换）。

## 2. 部署

```bash
cd deploy
docker compose up -d
# 面板：http://<本机IP>:5678
```

首次进入会跳 **配置向导**：逐服务填写 → 点「测试连通」→ 全绿保存。

## 3. 添加 Bot（可选）

```bash
cp bots/kfcgame/config.example.json bots/kfcgame/config.json   # 填你的 bot_token/api_id/api_hash
cd bots/kfcgame && docker compose up -d
```

> 注意：`config.json` 已被 .gitignore 拦截，不会被提交。

## 常见问题

- **面板打不开**：确认 `MH_PORT` 与防火墙端口。
- **各服务「离线」**：多为凭据/地址未填或填错 → 打开设置页逐服务「测试连通」。
- **跨机器部署**：`services.json` 填服务真实 IP 或域名，无需在同一台机器。

更多见 [faq.md](faq.md) · [architecture.md](architecture.md)。
