# V2.0.0 全量源码审计报告

> 2026-11-12 · 基于**真实拉取的 GitHub 仓库**（`maojiapeng6/media-hub`，223 文件，commit `a2c0843`）与**线上真源**（`192.168.10.110:/vol1/1000/docker/media-hub/`）逐文件审计。
> **ZIP 说明**：工作区与 Downloads 全盘搜索 `*media*hub*.zip` / `*kfc*.zip` 均无结果——**ZIP 未提供**，故以 GitHub 仓库 + 线上真源为准（二者均非 README 推断）。

---

## 1. 架构真值（源码级）

| 维度 | 事实 | 证据 |
|---|---|---|
| 后端 | **标准库 `http.server.ThreadingHTTPServer` 单体**，非 Flask/Django | `app/app.py:13-18` |
| 前端 | **原生 JS 无框架**，单 HTML | `app/index.html` 仅 2 个 `<script>`，无 `src=` 引入框架 |
| 容器 | `python:3.11-slim`，仅 COPY app.py + index.html | `app/Dockerfile:3,10,16` |
| CI | privacy_scan → py_compile → 冒烟 curl /api/health → compose 校验；**不跑 tests/** | `.github/workflows/ci.yml:16-46` |
| 路由 | GitHub 副本 `do_GET` 行 24156 / `do_POST` 行 25133；线上 327 个 API | 源码 grep |

## 2. 三份源码差异（关键事实）

| 来源 | app.py | index.html | 说明 |
|---|---|---|---|
| GitHub v2.0.0（已推） | 28455 行 / 1.38MB | 50516 行 / 1.67MB | **发布副本（零凭据精简版）** |
| 本地 openrepo tag v2.0.0 | 28454 行 | 50515 行 | 与 GitHub 一致 |
| 线上真源（运行版） | **34351 行 / 1.69MB** | **55621 行 / 2.07MB** | 多约 5900 行 |

**差异构成**（源码证据）：线上多出 ① v3 插件运行时内联段（`v3_runtime.py:4` 自述）、② 多实例注册表内联（`multi_instance.py:16-17` 自述）、③ v3 前端（`v3_frontend.js` 独立文件，未内联进 GitHub 的 index.html）、④ nd_* 网盘页面/API（GitHub 全仓 0 命中）。
→ **发布副本 ≠ 线上运行版**，用户要求"以实际运行为准"是正确的。

## 3. 插件运行时真相（app/v3_runtime.py）

**真实实现**：DB 9 表（`v3_db_init` 70-121）、Manifest 校验（188-221）、安装/能力注册（254/286）、卸载（322）、生命周期启停（350-451，真实 `subprocess.Popen` 369）、服务注册 CRUD（465-571，含真实 HTTP 探测 536-564）、事件发布/确认（575/608）、能力聚合（625）、路由（663-734）。

**仅声明未落地**（必须诚实）：
- `data_schema` / `lifecycle_hooks` 只进 schema，无校验/调用点（150-154）
- 权限是字符串白名单比对（698-704），**无实际能力拦截**
- `v3_event_publish` 只 INSERT 返回，**无分发**；webhook 未实现；`/api/v3/sdk/events` 路由缺失
- `background_tasks` 只落库（303-307），**无调度器**
- `restart_count` 恒为 0（435）
- 依赖 `_mf_now`/`_mf_wan_json` 等宿主函数（本目录无定义）→ **发布副本不能独立运行**

## 4. 服务注册 vs 硬编码（用户硬要求相关）

- 硬编码 `_CONTAINERS_DEF` 12 项（`app.py:135-148`），`containers()` 读 cfg 覆盖（219-243）
- `multi_instance.py` 用 **JSON 文件**管 Emby 双实例（`emby`/`emby_adult`，29/75-104）
- `v3_runtime.py service_instances` 用 **SQLite** 管通用多类型实例（93-97）
- → **两套重复实例注册实现**；且原前端工作台读 `/api/overview.services`（硬编码容器），**不读注册表**（本轮已修）

## 5. 网盘相关（用户硬要求相关）

GitHub 副本**没有** nd_cloud/nd_openlist/nd_browse/nd_health 页面/API（全仓 0 命中），只有 cloudcookie 相关（35657/36542）。线上真源有完整网盘页面，但**四个独立一级入口** → 本轮已统一为「网盘中心」单入口，115/CD2/OpenList 归属其下（旧页保留为详情页）。

## 6. KFC / KFC2 播放数据

后端 `emby_sessions()`（线上 L18410+）已双实例并发拉取（`emby`/`emby_adult`），每条打 `server_key`/`_sname`，`by_server` 聚合正确。**受控测试**（双实例各注入 1 会话）结果：`by_server={emby:KFC n=1, emby_adult:KFC2 n=1}`，用户/设备/影片零串台 → **隔离 PASS**。

## 7. 测试代码（真实可执行，CI 未运行）

- `tests/test_multi_instance.py`：10 例（JSON 注册表/脱敏/容错）
- `tests/test_cover_repair.py`：ScanTests/PosterTests/RepairTests
- `tests/test_memguard.py`：8 组测试（meminfo/psi/config/推送/合并）
- 均为标准库 unittest + fake 注入，**可 `unittest discover` 执行**

## 8. 安全与隐私

- 明文凭据：**无真实值**，均为占位符（`config/.env.example:11-63`、`deploy/docker-compose.yml:19,43-60`）
- IP：均为占位/注释（app.py:177/20014 等）
- 发布链路有 `privacy_scan` 守门且 blocking=0

## 9. Bug / 技术债清单（真实）

| 级别 | 问题 | 状态 |
|---|---|---|
| 高 | 发布副本 ≠ 线上运行版（5900 行差异） | 已记录，需持续同步 |
| 高 | 工作台读硬编码容器列表，插件新增实例不自动出现 | **本轮已修**（改读注册表） |
| 高 | 网盘四个重复一级入口 | **本轮已修**（统一网盘中心） |
| 中 | 两套实例注册实现（JSON + SQLite）并存 | 待合并 |
| 中 | 事件总线无分发/Webhook 未实现/后台任务无调度 | 待实现 |
| 中 | 插件权限仅白名单比对，无能力拦截 | 待实现 |
| 低 | CI 不跑 tests/ | 待补 |
| 低 | `restart_count` 恒 0 | 待修 |
