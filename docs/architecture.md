# 架构图

```
                        ┌─────────────────────────────────────┐
                        │        MediaHub Web 总控台 (5678)     │
                        │   app.py (Flask) + index.html (SPA)  │
                        │  ┌───────────────────────────────┐  │
                        │  │  服务实例注册表 services.json   │  │  ← 多实例：每类可 ➕ 添加
                        │  │  emby[] moviepilot[] qb[] mt[] │  │     凭据经 Fernet 加密落盘
                        │  │  cd2[] telegram_bots[] tmdb[]  │  │
                        │  └───────────────────────────────┘  │
                        └───────┬─────────────────┬───────────┘
                                │ Docker API      │ HTTP API（按实例路由）
                    ┌───────────┴──┐   ┌──────────┴───────────────────────────┐
                    │ 容器启停/日志 │   │ Emby  MP  qB  MediaTidy  CD2  TMDB  TG │
                    └──────────────┘   └──────────────────────────────────────┘

  Telegram Bots（可选，独立容器，共用 MySQL 不同库）：
   ├─ kfcgame    积分经济 / PT 任务 / 求片   → 库 kfcgame
   ├─ embyboss   Emby 用户管理（Sakura 衍生，GPL-3.0）→ 库 embyboss
   └─ pt2cloud   PT 上云 / strm 链路          → 库 pt2cloud

  数据面：qB 下载 → MediaTidy 整理 → CD2 挂载网盘 → Emby 入库 →（可选）TG 通知
```

## 多实例路由约定

- 前端每个服务页：实例下拉/Tab + ➕ 新增 + ✏️ 编辑 + 🗑 删除（二次确认）。
- 后端所有外部调用改为 `get_service(type, id)` 从注册表取连接信息。
- API 增加 `?instance=<id>` 参数指定实例；缺省用该类型第一个 `enabled` 实例。
