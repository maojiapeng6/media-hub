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

## EF-006 · 分组层复发：新剧又被吞进假「2010」（critical，2026-11-19）

**背景**：EF-001 的根因 nfo 虽在 11-11 已移走（`.bak.20261111_narcos`），但 Emby 索引**已固化不自动降级**；只要假 Series「2010」(Id `3066062`) 还在，MT 新整理进来的剧**仍会被吞进去**。用户更新「风骚律师 (2015)」后搜索，结果全是 `2010 / S1:E2 / SeriesName=2010`。

**现象 / 证据**：
- 假「2010」仍在、下属 **600 个 Season**；风骚律师全部集 `SeriesId=3066062`
- 分组层只剩 `.bak`，根因文件确实没了 → 纯索引层问题，磁盘文件一个不少
- 风骚律师目录 **6 季 236 个 `.strm`** 完整，指向 115 实体（156 个 mkv）

**修复（单剧立等可见，零文件改动）**：独立补录法（毒枭同款）
1. 备份整目录到 `_backup_bcs_2015/`
2. 在 `/strm/补录区/<剧目录>` 实体复制全部 strm（**symlink 不跟随**、去掉悬空海报链接）、写实体 `tvshow.nfo`（含 `tmdbid`）、复制实体海报
3. 建独立媒体库（正确 body，见高频坑清单）→ 停卡死的 Scan 任务 → 重启 Emby → 对库根 Folder 做 `POST /Items/{FolderId}/Refresh?Recursive=true&FullRefresh`
4. 实测：Series「风骚律师」`3403618`、**6 季 236 集**、TMDB `60059`、Primary 封面有、MediaSource 指向 115 真文件

**这次踩的坑（已并入高频坑清单）**：先用 `?name=&paths=` query 建库 → 空壳；再用 body 建库但旧空壳没删 → 同时存在两个同名库，扫描器混乱、Folder 始终不生成、Scan 任务卡在 2.53% 数小时。**删掉空壳、只留一个 + 重启 + 定向 Folder Refresh 后 15 秒入库**。

**根治（2026-11-19 用户点头后执行，"整层独立库法"）**：单剧补录不治本，假「2010」索引顽固清不掉。**最终实测成功方案 = 把整层搬出原库路径 + 建独立库重建**，666 部剧一次性恢复为独立 Series：
1. 关 `SaveLocalMetadata=False`（`POST /Library/VirtualFolders/LibraryOptions {Id,LibraryOptions}`）——**防复发关键**，Emby 不再往磁盘写层 nfo
2. 把 `2010-2019` 目录**改名再移出**欧美剧库路径 → `/strm/欧美剧2010年代`（`mv`，不动任何文件；32849 个 strm 全程完好、已备份）
3. 建库：`POST /Library/VirtualFolders {Name,CollectionType:tvshows}`（**不要带 Locations**，会建空壳库；旧空壳没删+新库并存会让扫描器混乱、Scan 卡 2.53%）→ 再 `POST /Library/VirtualFolders/Paths {Id,Path,RefreshLibrary:true}` 补路径
4. 重启 Emby → `POST /Items/{FolderId}/Refresh?Recursive=true&FullRefresh` → 等约 5 分钟
5. 实测 **666 部独立 Series**（= 基线剧目录数），TMDBID 全对、抽样剧名全是独立剧，季/集后台持续填充

**为何 DELETE /Items/{Id} 清不掉假索引**：Emby 删除 item 时逐个 `FetchShortcutPath` 读 strm 指向的媒体，旧路径不存在 → 大量 `DirectoryNotFoundException` → 删除卡死。所以根治靠**重建新库**而非清旧索引；假「2010」旧 item 成无物理依托的历史残留（不影响新库）。**严禁对有物理依托的 item 用 DELETE**（连文件一起删）。

**Web 支撑（V1.58）**：`📺 Emby → 🩺 故障检修 → 🕳️ 吞噬检修` 新增：① 分组层吞噬体检（列出假 Series，如 `2010 / 600 季`）；② 查某剧是否被吞（显示 `SeriesName/SeriesId`，被吞标红，附单剧刷新 + 补录按钮）；③ 补录区状态。端点 `/api/embyfix/v2/{layers,swallow,rescue,rescue/status}`。

### 最终治本 v2：DB 改 `type`（2026-11-19，用户要求"回原位置"后）

上法（整层独立库）虽恢复剧集，但**剧不在原库路径**、假「2010」仍作为残留存在，用户不接受。深挖 Emby `library.db`（4.27GB，SQLite）找到**真正根因**：

- Emby 4.10 媒体项表是 **`MediaItems`**（不是 `Items`），类型字段 `type`：`3=Folder`、`6=Series`、`7=Season`、`8=Episode`
- 所有正常年代层（2020-2030/2000-2009…）Path 都是 `type=3` Folder；**唯独 `2010-2019` 是 `type=6` Series**——其他字段（ParentId/PresentationUniqueKey/ProviderIds）完全一样。Emby 就是靠这一个 `type` 值区分层是"文件夹"还是"一部剧"
- 只要 `type=6`，无论怎么删 nfo、删子树、重扫，Emby 都会把它重建为假剧；`DELETE /Items/{Id}` 又会因逐集 FetchShortcutPath 读 strm 而卡死

**决定性修复（保留原路径，剧回到正确位置）**：
1. **在线备份**：`sqlite3.connect(src).backup(dst)`（9.6s 一致快照，不停服）
2. **停 Emby** → 打开 `library.db`（`PRAGMA foreign_keys=OFF`）：
   - 递归 CTE 找出假 Series（如 `Id=3441055`）整棵子树，`MediaItems` + `MediaStreams2`/`ItemLinks2`/`ItemPeople2`/`UserItemShares`/`ItemExtradata`/`SyncJobItems2` 按 `ItemId IN (...)` 分批删（800/批，避开变量上限）
   - **关键一句**：`UPDATE MediaItems SET type=3, Name='2010-2019', PresentationUniqueKey=NULL WHERE Id=<假SeriesId>` —— 把根行从 Series 改回 Folder
   - `commit()` + `PRAGMA wal_checkpoint(TRUNCATE)` + 回读验证
3. **启 Emby** → 库根已显示 `2010-2019` 为 **Folder** ✅
4. 对 Folder 发 **666 个剧目录的 `POST /Library/Media/Updated {Updates:[{Path,UpdateType:Created}]}`**（分批 40/批）→ **45 秒内 666 部独立 Series 全部入库**
5. 季/集由全库扫描异步填充（每集连 115 探测，固有慢）；期间**不要反复停启动扫描**（会从头排队）

**实测**：假剧 `2010`=0、`2010-2019`=Folder、层内独立剧 **666**、32849 strm 完好、风骚律师在原路径 `2010-2019/风骚律师 (2015) [tmdbid-60059]`、TMDB `60059`。

**为何用 Created 通知而非扫描建 Series**：Emby 增量通知不导入新文件（Season/Episode 级无效），但对**已存在的目录项**（剧目录）发 Created 能让实时监控秒级建 Series——这正是 666 剧秒级入库的关键。

---

## 高频坑清单

| 坑 | 说明 |
|---|---|
| `DELETE /Items/{Id}` | 删物理文件，不能当"清索引"用 |
| 反复 `/Library/Refresh` | 取消并重启扫描任务 |
| 目录 symlink | Emby **不跟随**，新目录必须实体文件 |
| 新建库字段 | **建库用 `POST /Library/VirtualFolders` + JSON body：`{Name,CollectionType,Locations:[path],RefreshLibrary:true}`**（`Locations` 是正确字段）。用 `?name=&paths=` query 参数会建出**空壳库**（显示路径但不扫描、Folder 不生成）；补路径才用 `POST /Library/VirtualFolders/Paths {Id,Path,RefreshLibrary}`。重建前先删空壳库，且**一次只能有一个同名库**，否则扫描器混乱 |
| `SearchTerm` 带年份括号 | 假阴性，Series 名可能不带年份；用宽松词或按 TMDBID / Path 查 |
| `GET /Items/{Id}` 不带 UserId | 404，改用 `/Items?Ids=...` 或 `ParentId` |
| `glob` 与 `[tmdbid-xxxxx]` | 方括号被当字符类 → "0 个文件"是假阴性，用 `glob.escape()` 或 `os.walk` |
| 网盘全盘 `find` | 超时；限定目录或从 `.strm` 内容反推精确路径再 `stat` |
| 新 CSS 插入位置 | 独立 `<style>` 放 `</head>` 前，别插进未闭合 `@media` |
| 新仪表盘卡 | 必须登记布局注册表，否则会排到顶部区之上 |
| Emby 4.10 媒体表 | 是 `MediaItems`（不是 `Items`）；`type` 字段 `3=Folder / 6=Series / 7=Season / 8=Episode` |
| 假剧清不掉（type=6） | 删 nfo/删子树/重扫都无效，`DELETE /Items` 又卡在逐集读 strm；**正解 = 停服改 DB `type` 6→3 + 清子树 + checkpoint**，再对剧目录发 Created 通知秒级重建 |
| 季集不导入 | Created 通知只对已存在目录项（剧目录）建 Series 有效；Season/Episode 级无效，须靠全库扫描异步填（且别反复停启） |

---

## Web 监控模块映射

面板 `📺 Emby → 🩺 Emby 故障检修` 提供七节点监控：
`网盘 → 整理轮询 → 整理落位 → STRM 生成 → 通知刷新 → Emby 入库 → 元数据/封面`。

每节点采集真值并标注 正常/警告/失败，可单独诊断与修复；剧集明细逐行给出 **TMDBID、海报、季/集数、strm 与 Emby 集数差、115 实体路径、NFO 状态、最大集号、七节点状态**，并提供「刷新入库 / 重发通知 / 补 NFO / 补封面」真实动作；页面另含错误案例库与刷新日志尾（失败标红）。
