# 内存预警 + 用户观影清单（V1.48 / v0.1.7）

> 本页描述**发布副本的零凭据参考实现**（`app/memguard.py`）与线上媒体中枢面板的
> 行为契约。所有地址、Token、Media Server Key 均由部署者自行在「设置」页填写，
> 文档与源码中不含任何真实凭据或内网地址。

## 1. 解决什么问题

NAS 上常驻服务（虚拟机、下载器、媒体服务器、定时任务）叠加后可能顶到物理内存上限；
Swap 打满后内核会连续触发整机 `global_oom`，`dockerd` 在文件系统同步上阻塞，
表现是「面板 / Emby / SSH 突然全部连不上」，而重启只是重新计时。

本模块提供**只读的提前预警**：

- 仪表盘新增独立卡片，显示内存、Swap、PSI IO 等待、OOM 累计、容器占用 Top 与处置建议；
- 达到「注意 / 危险」阈值时通过已配置的 Bot 推送给管理员，冷却期内不重复刷屏；
- 管理员可自行调整阈值、关闭推送、手动测试推送；
- **绝不自作主张关进程、清内存、改配置**——只给建议，处置由人决定。

## 2. 数据流

```
 /proc/meminfo ─┐
 /proc/pressure/memory ─┤
 /proc/vmstat (oom_kill) ─┼→ mg_snapshot() ─→ GET /api/memguard/status ─→ 仪表盘卡片（20s）
 /proc/loadavg ─┘              │
 Docker Engine stats ──────────┘        └→ 后台 60s 轮询 → should_push() → TG HTML 消息
                                              （等级跃迁 / 冷却到期 / 新增 OOM）
```

- 面板读 15s 缓存，不触发重采样；后台线程 `force=True` 每 60s 采一次。
- 容器占用 Top 走 Docker Engine API `/containers/{name}/stats?stream=false`，
  RSS ≈ `memory_stats.usage - memory_stats.stats.inactive_file`，并发取数、超时保护。
- ⚠ cgroup v1 环境下容器内的 `/proc/meminfo` 反映**宿主机**真值（实机与 `free` 一致）；
  cgroup v2 + 内存命名空间隔离时请改读 `/sys/fs/cgroup/memory.current` 与 `memory.max`。

## 3. API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memguard/status` | 快照 JSON（等级 / 内存 / Swap / PSI / OOM / load / top / advice / cfg / push） |
| POST | `/api/memguard/config` | 保存阈值与开关，立即生效；写端点走既有授权闸 |
| POST | `/api/memguard/test` | 手动发一条「测试推送」，返回 `ok` / `msg` / `preview` |
| GET | `/api/emby/userplays?name=&uid=&days=&limit=` | 某用户跨实例观影明细（合并去重、按时间倒序） |

配置项（落盘为部署者自管的 JSON 文件，零凭据）：

| 键 | 默认 | 含义 |
|---|---|---|
| `enabled` | `true` | 是否允许自动 TG 推送 |
| `warn_mem` / `crit_mem` | `82` / `91` | 内存占用百分比 |
| `warn_swap` / `crit_swap` | `60` / `80` | Swap 占用百分比 |
| `cooldown_min` | `15` | 同一等级下重复提醒的最小间隔（分钟） |

分级：内存 / Swap / PSI full 任一越「危险」阈值 → `crit`；任一越「注意」阈值或
`PSI full >= 2` → `warn`；否则 `ok`。PSI full ≥ 8 直接判危险。

## 4. 推送节流（防刷屏）

| 事件 | 是否推 |
|---|---|
| 等级发生变化（`ok→warn`、`warn→crit`、恢复后再度告警） | ✅ |
| 同一等级且超过冷却时间 | ✅ |
| `oom_kill` 计数新增（且基线已建立） | ✅ |
| 等级不变、冷却未到 | ❌ |
| 恢复正常 `ok` | ❌（同时复位等级，下次复发仍会推） |
| `enabled=false` | ❌ |
| 进程刚启动、OOM 基线为 0 | ❌ 仅因计数推（避免一启动就误报） |

消息为 Telegram HTML：等级、内存、Swap、PSI/OOM/load、占用 Top 4、最多 4 条建议、
面板入口提示。**不含**任何地址、Token、Key 或个人信息。

## 5. 前端三处改动

1. **仪表盘卡片**：位于「NAS 硬件」之后、「后台实时任务」之前；在自定义布局系统
   (`OVM_DEF`) 中登记为独立模块，避免未登记的兜底 key 被排到「实时仪表盘」标题栏之前，
   **EMBY 实时观看区始终保持在最上面**。卡片挂现有轮询槽，不新增定时器。
2. **排行榜头像**：18px 圆形、行高 3px 10px；有 Media Server 头像则显示真图，
   否则显示**系统默认人形头像**（按用户名稳定哈希配色，同一个人每次配色一致）。
   真图返回 `200` 但 0 字节时不触发 `onerror`，故同时在 `onload` 检查
   `naturalWidth === 0`，失败一律回落默认头像，绝不出现破图。
3. **点头像看「他都看了哪些影片」**：弹层按 7/30/90/365 天切换，合并多台媒体服务器
   的播放明细（去重键 = 时间前 16 位 + 片名），显示片名、海报、播放时间、时长、
   来源实例、客户端、年份与 TMDB 直达链接，并提供「在播放数据页查看」入口。
   整行点击仍保留原有的跳「播放数据」页行为，不破坏既有验收项。

## 6. 参考实现与单测

- `app/memguard.py`：`parse_meminfo` / `parse_psi` / `normalize_config` / `level_of` /
  `advice_for` / `snapshot` / `should_push` / `format_message` / `merge_userplays`。
  全部外部调用通过回调注入，纯标准库、无网络、无凭据。
- `tests/test_memguard.py`：覆盖脏 `/proc` 文本、阈值夹取、三级判定、建议排除核心服务、
  缓存/强制重采样、采集异常不抛、推送节流六种分支、消息脱敏、跨实例合并去重与排序。
  与既有 `test_cover_repair.py` / `test_multi_instance.py` 一起跑，全量 51 项通过。
- 按文件加载模块（`importlib.util.spec_from_file_location`），**不要**把 `app/`
  加进 `sys.path`，否则会与同目录的 `app.py` 冲突、破坏既有测试的导入路径。

## 7. 部署与验收要点

- 每个 30 分钟跑一次全库动态图渲染、且内存上限只有 1 GiB 的定时任务，是典型的
  「周期性放大器」：禁用该任务后容器不再每半小时被 cgroup OOM 杀掉并自动重启。
  面板模块只做观测，关不关任务由管理员决定。
- 写生产文件时用 `cat /tmp/new > 目标文件` 保留 inode（bind mount 场景），
  不要 `open(path, 'w')` 覆盖；改前先拉线上真源，防止并发任务用旧快照回写。
- 新增 CSS 必须放在独立的 `<style>` 块或已知闭合的层级里；插进别人的
  `@media` 块会导致「桌面端样式全部失效」（本轮实测踩过：KPI 竖排、配置面板默认展开）。
- 自定义布局系统里**必须登记新模块 key**，否则兜底 key 会排到标题栏之前。

### 实机验收记录（泛化）

| 项 | 结果 |
|---|---|
| `GET /api/memguard/status` | 200；Swap 逼近 100% 时正确判 `crit`，Top 容器与前 4 项建议齐全 |
| 手动测试推送 | `ok=true`，管理员 Bot 收到 HTML 消息 |
| 后台自动推送 | 启动后首个轮询周期即因 `crit` 推送成功，`last_ok=true`，冷却内不重复 |
| 仪表盘位置 | EMBY 实时观看区仍在最上方，预警卡在其后；4 列 KPI 网格、配置区默认收起 |
| 排行榜 | 12 行 = 12 个头像（无头像者默认人形图），18px、行高紧凑、无破图 |
| 点头像弹层 | 真实点击 → 合并出 65 条观影记录，海报 / 时间 / 时长 / 来源正常渲染 |
| 恢复封面模块 | 扫描 50 条缺 3 → 试运行匹配 3 张官方海报 → 真实补齐 3 张 → 回读 `ImageTags.Primary` 有值 → 复扫缺 0 |

## 8. 已知边界

- `oom_kill` 是**开机以来累计值**，重启会清零；判断「新增 OOM」以本次进程生命周期内的基线为准。
- PSI 在部分内核未启用 `CONFIG_PSI` 时读不到，按 0 处理，仅靠内存/Swap 阈值判定。
- 观影清单依赖各媒体服务器的播放报表库 / 活动日志；某台实例缺库时该实例跳过，不影响其它实例。
- 合并去重按「分钟 + 片名」，同一分钟内同一片的多次播放只显示一次（用于「看过哪些」而非精确计数）。
