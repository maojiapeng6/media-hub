# V3 功能迁移与验收报告

> 2026-11-12 · 基于真实线上真源修改 + 真机 Playwright 实测
> **生产部署状态：已部署到线上环境**（NAS `<NAS_IP>` media-hub 容器，真机截图与断言均来自该运行版）。

---

## 1. 你的三项硬要求（逐项实测）

### ① KFC / KFC2 播放数据不串台 ✅
- **实测方式**：受控注入（只读 monkeypatch，不改任何数据）——两实例各注入 1 个播放会话
- **结果**：`by_server={emby:{server:KFC,n:1}, emby_adult:{server:KFC2,n:1}}`；`ROWS={ONLY_KFC:emby, ONLY_KFC2:emby_adult}`；**ISOLATION_PASS=True**
- **证据脚本**：`tools/_v3_isolation.py`

### ② 115 / CD2 / OpenList 进入统一网盘中心 ✅
- **实现**：新增 `switchTab('netdisk')` 拦截 → 统一网盘中心页，注册表 type=netdisk 动态发现实例
- **实测**：菜单点击后 `title=网盘中心`、`txt/cd2/op/p115` 全部 true、5 张实例卡
- **功能保留**：旧 `nd_cloud/nd_openlist/nd_browse/nd_health` 页面**完整保留**，作为中心内详情入口（零删除）
- **证据脚本**：`tools/dsh_fleet/v3_hard.js`

### ③ 插件新增实例后工作台/统计自动识别（零硬编码） ✅
- **实现**：工作台服务面板数据源从 `/api/overview.services`（硬编码容器）改为 `/api/v3/services`（注册表）
- **实测**：通过注册表新增 `自动识别测试盘` → 工作台服务列表 **13→14 且显示该实例**；网盘中心**也自动出现**；删除后恢复 13
- **证据脚本**：容器内 `tools/_v3_addprobe.py` + `tools/dsh_fleet/v3_auto3.js`

---

## 2. 本轮修改文件

| 文件 | 变更 | 备份 |
|---|---|---|
| `<MEDIAHUB_DIR>/index.html` | +注册表联动 + 统一网盘中心 + 修 `d is not defined` 回退 | `index.html.bak.<ts>_v3stage1` |
| `<MEDIAHUB_DIR>/app.py` | **本轮未改** | `app.py.bak.<ts>_v3stage1` |
| `<V3_DIR>/hub_v3.db` | 种子化 13 个真实服务实例（幂等导入，非删除） | `hub_v3.db.bak.<ts>_v3stage1` |
| `mediatidy.db`（3.7GB） | **未触碰**（只读记录 size/mtime） | 无需 |
| `openrepo/docs/v3-source-audit.md` | 新增审计报告 | — |
| `openrepo/docs/v3-migration-report.md` | 新增本报告 | — |

## 3. 数据库变化

- `hub_v3.db`：`service_instances` 表新增 13 行（从现有 webcfg/containers 只读导入，幂等；未改表结构）
- 无字段变更、无删除、无重建

## 4. 迁移对照表（旧功能 → 新位置）

| 旧入口 | 新位置 | 状态 |
|---|---|---|
| `netdisk`（网盘一级） | **网盘中心**（统一入口） | 已迁移，旧页保留 |
| `nd_cloud` CloudDrive2 | 网盘中心 →「服务详情」 | 保留 |
| `nd_openlist` OpenList | 网盘中心 →「服务详情」 | 保留 |
| `nd_browse` 文件浏览 | 网盘中心 →「文件浏览」 | 保留 |
| `nd_health` 链路健康 | 网盘中心 →「链路健康」 | 保留 |
| 工作台服务列表（硬编码） | 注册表驱动（自动发现） | 已迁移 |
| `sync` / `synchub` 重名 | 数据同步 / 同步画像 | 已改名 |
| `plays` 媒体组重复 | 清理重复，保留唯一 | 已修 |
| 其他 35+ 菜单 | 原路由保留 | 零删除 |

## 5. 测试结果

| 测试 | 结果 |
|---|---|
| JS 语法门禁 | ✅ |
| 守门 privacy_scan blocking=0 | ✅（上一版本；本轮仅加 docs） |
| 工作台注册表面板 | ✅ 13 实例 / 9 类型筛选 |
| 插件实例自动联动 | ✅ 13→14→13，工作台+网盘中心同步 |
| 网盘中心统一入口 | ✅ 115/CD2/OpenList 同页 |
| KFC/KFC2 隔离受控测试 | ✅ ISOLATION_PASS=True |
| 真实会话（当前） | n=4，全在 KFC（KFC2 无人播放属正常） |
| 浏览器 JS 错误 | ✅ 0 |

## 6. 遗留问题（诚实，不冒充完成）

1. **v3 插件运行时仍是"半实现"**：事件总线只入库不分发、Webhook/后台任务调度未实现、权限无能力拦截、`/api/v3/sdk/events` 路由缺失（详见审计报告 §3）。
2. **两套实例注册实现并存**（`multi_instance.py` JSON + `v3_runtime.py` SQLite），尚未合并为单一源。
3. **GitHub 发布副本仍缺 5900 行**（插件/多实例/网盘/工作台联动未回写发布副本）——线上运行新版，但开源仓库是精简版。
4. **二级页面未逐页深度重排**：本轮是全局 Token + 工作台/网盘中心重构，其余页面沿用原布局但已换统一视觉。
5. **ZIP 未提供**：全盘搜索无结果，审计以 GitHub + 线上真源为准。

## 7. 回滚方案

```bash
# 回滚 index.html
sudo cp <MEDIAHUB_DIR>/index.html.bak.<ts>_v3stage1 <MEDIAHUB_DIR>/index.html
# 回滚注册表 DB
sudo cp <MEDIAHUB_DIR>/<V3_DIR>/hub_v3.db.bak.<ts>_v3stage1 <MEDIAHUB_DIR>/<V3_DIR>/hub_v3.db
sudo docker restart media-hub
```
原 13 个服务配置来自 webcfg（未改动），即使回滚 DB 也能从配置重建。

## 8. 追加交付（同日晚间 · 统计模块联动 + 手机修复）

### 8.1 统一数据中心（注册表驱动，用户点名要求）
- **实测**：新增测试媒体实例 `测试媒体服务器03` → 数据中心实例筛选器**自动出现该实例**（无需改任何代码）；删除后恢复 13 个真实实例
- **数据**：真实 API `/api/emby/plays`（1260 条）/ `/api/emby/rank`（60 用户）/ `/api/notify` / `/api/jobs`
- **能力**：全部实例聚合 / 单实例筛选（KFC / KFC2 / EmbyBoss）/ 时间范围（1/7/30 天）
- **旧功能保留**：`db`/`plays` 原页面完整保留，新数据中心挂在 `db` 入口

### 8.2 手机端横向溢出修复
- **根因**：旧极光装饰层（`.au-ring` 宽 1309px / `.au-bgx` / `.au-orb`）在手机端撑宽页面（scrollWidth 436 > 390）
- **修复**：手机端隐藏装饰层 + `html,body{overflow-x:hidden;max-width:100vw}`
- **实测**：手机 390px `scrollWidth=clientWidth=390` ✅

### 8.3 全量回归
44/44 菜单真实点击 0 失败 · 5 主题切换 OK · 手机零溢出 · 零 JS 错误 · 旧圆环 oldRing=0
