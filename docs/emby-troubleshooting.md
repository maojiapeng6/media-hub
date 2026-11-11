# Emby 入库故障排查与修复（Troubleshooting）

> 适用：CloudDrive2 / 网盘挂载 → MediaTidy 生成 `.strm` → Emby 入库的自动化链路。
> 本文不含任何真实地址、凭据或用户标识，路径均为示例逻辑路径。

## 0. 三边核对法

**"看不到某部剧"绝大多数不是文件丢了，而是识别 / 索引出错。** 按顺序核对：

| 边 | 检查 | 正常标准 |
|---|---|---|
| ① 网盘 | 源视频实体存在且可读 | `stat` 成功、size > 0 |
| ② 本地 STRM | `.strm` 数量与内容 | 指向 ① 的真实路径，数量与预期集数一致 |
| ③ Emby | Series / Episode | 独立 Series 存在，`strm 数 == Emby Episode 数` |

算出 `gap = strm 数 − Emby 集数`，再按下面案例对症修复。**不要一上来就删库、重建库或反复点扫描。**

---

## EF-001 · 分组层被误写 `tvshow.nfo`（critical）

**现象**：网盘与 STRM 都有某剧，Emby 里搜不到；库中只出现一部以"年代"命名的假剧。

**根因**：年代/分类分组目录（例如 `<STRM_ROOT>/电视剧/欧美剧/2010-2019/`）里出现 `tvshow.nfo`，Emby 把整个分组层识别成一部 Series，其下几百个剧目录全变成 Season。

**责任链**：
- 直接原因：分组层的 nfo 让 Emby 误判文件夹类型
- 写入者：Emby 自身 `SaveLocalMetadata=True` 会把识别结果回写 nfo（格式含 `displayorder` / `lockdata` / `dateadded` / `season -1`，且因不是真剧而缺 `tmdbid` / `originaltitle`）——与其它正常剧的 nfo 格式一致
- 固化：nfo 落地后每次扫描都读它，不会自动降级
- 放大：Emby 已定型的 Series 不会自动降级；全库扫描慢时难自愈

**为什么只有一层坏**：全库 130 个分组层普查，只有 1 层带 `tvshow.nfo`，其余层与库全部正常。属于偶发误判被 nfo 固化，**不是配置差异**。

**修复方案**

| 方案 | 做法 | 风险 |
|---|---|---|
| A′ PathExclusions（推荐） | 备份库配置 → 临时排除该路径 → 扫描清索引 → 取消排除 → 再扫描重建 → **关 SaveLocalMetadata** | 需先验证排除会清已有索引；扫描慢；库级变更须备份 |
| A 目录改名（兜底） | 临时改名分组层 → 扫描清索引 → 改回 → 再扫描 | 改名期间整理任务可能重建目录 |
| B 单剧补录 | 该剧实体化放独立路径，建只含该剧的小库 | 不根治、有重复、库变多 |
| C 删库重建 | 删除并重建媒体库 | **不推荐**：观看记录/收藏按 item id 存，可能全丢 |

**⛔ 绝对禁止**
- `DELETE /Items/{Id}` —— OpenAPI 原文 `Deletes an item from the library and file system`，**连物理文件一起删**
- 反复 `POST /Library/Refresh` —— 每次都会取消正在跑的扫描再重启，扫描永远跑不完

**复发防护**：关闭 `SaveLocalMetadata`；持续普查所有分组层（含 ≥10 个子目录）是否混入 nfo；分组层内不放任何 nfo，剧目录 nfo 必须是含 `tmdbid` 的实体文件。

---

## EF-002 · 第二实例刷新路径前缀错误（high）

`[STRM-Realtime] Emby refresh path failed {"server":"<SECOND>","path":"/app/strm/..."}`，而主实例用 `/strm/...` 成功 → 每次 `success:1 / failed:1`。
**修复**：把该实例 `embyRefresh` 路径前缀改为 `/strm`，保存后**重启整理服务加载配置**，并实测日志 `failed=0`。不要自动改任何凭据。

---

## EF-003 · 增量通知 Created 不导入新文件（high）

**现象**：网盘与 STRM 都有最后一集，Emby 少最后一集，前面集数一集不缺。
**根因**：`Library/Media/Updated` 增量通知只更新已存在 item，**不会把全新 `.strm` 导入库**；必须靠 `Scan media library` 任务或重启 Emby。
**实测**：连发 140 个 Created 通知，库条目仍为 1；重启 Emby 后 30 秒内 140 集全部入库。
**"当时有任务所以失败"？** 任务冲突只加剧排队，不是根因；根因是 EF-002 / EF-003 / EF-004 叠加。
**修复**：对目标剧 `POST /emby/Items/{SeriesId}/Refresh?Recursive=true`，或启动一次扫描并让它跑完；启停用 `POST|DELETE /ScheduledTasks/Running/{Id}`。

---

## EF-004 · 全库扫描极慢 / 被反复取消（medium）

每个 `.strm` 都要经挂载连网盘探测媒体信息，几万个文件耗时数小时。**只启动一次并让它跑完**；紧急单剧用独立小库或重启 Emby 触发快速增量扫描。

---

## EF-005 · 剧目录 nfo 悬空 / 缺封面（medium）

某年代层 665 个剧目录：367 个实体 nfo、**232 个悬空符号链接**、66 个完全没有。悬空链接指向网盘上不存在的 nfo → 元数据/海报时有时无，新剧易缺封面。
**修复**：换成实体 `tvshow.nfo`（`title / year / tmdbid / uniqueid`），写前备份原链接；封面按 TMDB ID 抓官方海报写回 Emby `Primary`，**禁止 movie/tv 跨类型错配**。

---

## 高频坑清单

| 坑 | 说明 |
|---|---|
| `DELETE /Items/{Id}` | 删物理文件，不能当"清索引"用 |
| 反复 `/Library/Refresh` | 取消并重启扫描任务 |
| 目录 symlink | Emby **不跟随**，新目录必须实体文件 |
| 新建库字段 | 用 `Paths`（不是 `Locations`）；补路径用 `POST /Library/VirtualFolders/Paths {Id,Path,RefreshLibrary}` |
| `SearchTerm` 带年份括号 | 假阴性，Series 名可能不带年份；用宽松词或按 TMDBID / Path 查 |
| `GET /Items/{Id}` 不带 UserId | 404，改用 `/Items?Ids=...` 或 `ParentId` |
| `glob` 与 `[tmdbid-xxxxx]` | 方括号被当字符类 → "0 个文件"是假阴性，用 `glob.escape()` 或 `os.walk` |
| 网盘全盘 `find` | 超时；限定目录或从 `.strm` 内容反推精确路径再 `stat` |
| 新 CSS 插入位置 | 独立 `<style>` 放 `</head>` 前，别插进未闭合 `@media` |
| 新仪表盘卡 | 必须登记布局注册表，否则会排到顶部区之上 |

---

## Web 监控模块映射

面板 `📺 Emby → 🩺 Emby 故障检修` 提供七节点监控：
`网盘 → 整理轮询 → 整理落位 → STRM 生成 → 通知刷新 → Emby 入库 → 元数据/封面`。

每节点采集真值并标注 正常/警告/失败，可单独诊断与修复；剧集明细逐行给出 **TMDBID、海报、季/集数、strm 与 Emby 集数差、115 实体路径、NFO 状态、最大集号、七节点状态**，并提供「刷新入库 / 重发通知 / 补 NFO / 补封面」真实动作；页面另含错误案例库与刷新日志尾（失败标红）。
