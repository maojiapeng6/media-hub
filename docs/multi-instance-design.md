# 多实例服务注册表 · 架构设计（P3 实施蓝图）

> 目标：把所有"外部服务连接"从写死的单实例改造成**用户自助添加、可 N 个实例、带 ➕ 加号**的注册表模式。参考 MediaTidy 方案列表 / RT 添加 Emby 的交互。

## 1. 现状盘点（代码真值）

| 连接 | 现状 | 位置 | 改造难度 |
|---|---|---|---|
| `RT_HTTP` | env 外置单实例 | app.py:18459 | 低（已是外置，只差多实例化） |
| Emby | `webcfg.json` emby_key/emby_key2（=2个写死位） | app.py `_cfg` | 中（键位固定→列表） |
| MP/qB/MT/CD2 | webcfg 单实例键 | app.py | 中 |
| TG Bot | cfg.tg_token 单个 | app.py `_tg_owner` | 中 |
| MySQL | PTDB_* env 单实例 | app.py `_pt_db()` | 低 |
| TMDB | 单 key | app.py | 低 |

> **关键利好**：面板已有 `webcfg.json` + `cfg()` 统一配置入口 + SEC_ROOT 加密 + `SEC_FEATURES` 含 `multibot` 功能位——多实例是"水到渠成"，不是推倒重来。

## 2. 数据模型

```jsonc
// webcfg.json 新增段（向后兼容：旧单实例键继续可用，读取时自动折算为 instances[0]）
"services": {
  "emby": {
    "instances": [
      {"id": "sv-emby-1", "name": "主服", "url": "http://…:8096",
       "api_key": "enc1:…", "enabled": true, "primary": true}
    ]
  },
  "moviepilot": {"instances": [...]},
  "qbittorrent": {"instances": [...]},
  "mediatidy":   {"instances": [...]},
  "clouddrive2": {"instances": [...]},
  "rt":          {"instances": [...]},
  "tgbot":       {"instances": [...]}   // 对齐 SEC_FEATURES multibot
}
```

- `id` 服务端生成 `sv-<type>-<seq>`；凭据字段复用现有 **enc1: Fernet** 加密。
- **兼容铁律**（商用化零变更）：升级检测 `install.lock` → 旧键自动折算注册表，**不改任何用户已存值**；读路径 `get_service(type,id=None)`：id 空时返回 primary → 旧键折算 → 报错引导 ➕。

## 3. 后端 API（新增 4 个端点）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/services` | GET | 全部类型+实例（凭据打码 `****`，返回 `has_secret:true`） |
| `/api/services/upsert` | POST | `{type, id?, name, url, ...凭据, enabled}` 新增/编辑（小眼睛查看走既有 sec 通道） |
| `/api/services/delete` | POST | `{type,id}`，被引用中的实例拒删并提示 |
| `/api/services/test` | POST | `{type,id?}` 单实例连通测试（复用各 svc 已有 health 逻辑） |

- 所有既有动作接口（`/api/action`、`/api/container`、各 svc 探测）加可选参 `?sv=<instance_id>`；`get_service(type, sv)` 统一取数，**旧调用零改动**（缺省走 primary）。
- 路由层：`svc+act` 现有分发不动，只在 `_xxx_api()` 取 base/url 处改为经 `get_service()`。

## 4. 前端交互（对标 MT/RT）

1. **设置页 → 服务实例卡片区**：每类一张卡，卡片内实例列表（名称/地址/状态点/primary 星标），右上角 **➕ 添加实例**。
2. ➕ 弹窗：名称、URL、凭据字段（按 type 动态：emby→api_key；mp/qb/mt→user+pass；tgbot→token+api_id+api_hash）、`测试连通` 按钮（绿✓才允许保存，可跳过）。
3. 行内操作：✏️ 编辑（凭据小眼睛）｜⭐ 设为 primary｜⏸ enabled 开关｜🗑 删除（`uxConfirm` 二次确认，引用中拒删）。
4. **功能页实例下拉**：Emby/MP/qB/MT/CD2 各 Tab 顶部加实例选择器（只有 1 实例时隐藏，多实例时记忆选择到 localStorage）。
5. 空态：无任何实例 → `emptyState('➕ 添加你的第一个 Emby 实例')`，点击直达 ➕ 弹窗。

## 5. 分期实施（每步可独立验收回滚）

| 步骤 | 内容 | 验收 |
|---|---|---|
| P3-1 | 后端注册表读写 + `get_service()` + 4 API + 旧键折算 | API 自测：增删改查/test |
| P3-2 | Emby 类型接入（面板 Emby 页下拉 + 加号） | 两实例并行操作库互不串 |
| P3-3 | MP / qB / MT 接入 | 各页下拉生效 |
| P3-4 | RT / TG Bot（multibot 功能位）接入 | 机器人管理页多 token |
| P3-5 | 设置页服务实例总览卡片收尾 | 全流程截图验收 |

## 6. 风险与红线

- **绝不整文件覆盖线上**：改 media-hub 必走 paramiko 拉 NAS 真源 → 改 → 回传（MEMORY §0 红区）。
- **升级零变更**：老用户 webcfg 旧键必须继续生效（折算而非迁移删除）。
- 每步 `py_compile` + API 实测；前端每步浏览器真机验证。
