# 缺失封面扫描与补齐（媒体库模块）

> 版本：v0.1.6 · 线上 media-hub V1.47c · 零凭据参考实现：`app/cover_repair.py`
> 触发场景（用户原话）：「最近入库的新片会有几个没有封面的，还有个别其他影片也没有封面，加一个扫描缺失封面并补齐的功能」。

## 1. 它解决什么

strm 媒体库里常见「入库成功但没封面」：Emby 条目的 `ImageTags.Primary` 为空，
前端只能显示文字占位图。传统「缺图重读」工具只做一件事——让 Emby 重新读取
**条目目录里已经存在**的图片文件；而 strm 新片的目录里往往根本没有图片，
所以这类工具会把条目写进「补刮单」后一直等着，封面永远不会出现。

本模块直接把 **TMDB 官方海报写进 Emby 媒体库本体**
（`POST /Items/{Id}/Images/Primary`），因此：

| 效果 | 说明 |
|---|---|
| Emby 原生 UI | 立即有封面，不需要刷新元数据 |
| 所有播放器 / 客户端 | 立即有封面（读的是 Emby 图片） |
| 面板「最近入库」海报墙 | 由占位 SVG 变为真实海报 |
| 面板既有 9kg 缺图模块 | 互不干扰，职责互补 |

## 2. 处理链路

```
扫描 Emby（分页，Movie/Series，ImageTags 判定）
   ↓ 缺 Primary 的条目（带 Id / 片名 / 年份 / Tmdb ID / 实例）
匹配海报
   ① TMDB ID 精确取（严格同类型：Movie→/movie/{id}，Series→/tv/{id}）
   ② 同作品多语言海报集 /{kind}/{id}/images
   ③ 片名 + 年份严格搜索（标题必须一致；宽松匹配需显式开启）
   ↓ poster_path
下载图床（服务端直连，失败回退代理隧道）
   ↓ 二进制
上传 Emby（base64 文本 + application/octet-stream）
   ↓
回读 /emby/Items?Ids=... 校验 ImageTags.Primary 确实出现
```

## 3. 🔴 匹配红线（实测踩过的坑）

**TMDB 的 movie 与 tv 是两套独立 ID 空间。** 同一个数字 ID 在两端通常是
两部完全不同的作品。早期实现里「movie 详情无海报 → 尝试 tv/{同 ID}」，
把一部 2011 年电影配上了同名 ID 下某纪录片的海报。

现规则：

- 只按条目类型对应的端点查，**绝不跨类型回退**；
- 该端点返回 200 但无海报 → 查同一作品多语言海报集 → 片名严格搜索；
- 仍无 → **标注原因、不写库**（宁缺勿错），前端状态列直接显示原因；
- 「宽松匹配」默认关闭，开启后结果明确标记「需人工核对」。

Emby 4.10 的图片上传端点同样有坑：Swagger 写明
`must be base64 encoded`。直接 POST 二进制会返回 204 但 `ImageTags` 仍为空，
必须提交 **base64 文本** + `Content-Type: application/octet-stream`，
并在写入后回读校验。

## 4. 线上 API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cover/servers` | 派生可用 Emby 实例（新增媒体服务器自动进入，无需改代码） |
| GET | `/api/cover/scan?server=&scope=&limit=&lib=` | 同步扫描；`scope=recent` 按入库倒序，`all` 全库；`lib` 可限定媒体库 ID |
| POST | `/api/cover/scan_async` | 全库扫描走异步 job，避免请求超时 |
| POST | `/api/cover/fix` | 异步 job；`{items, server, dry, size, loose}`；单批上限 300 |
| GET | `/api/cover/status` | 扫描/补齐进度与最近一次结果摘要 |

- 写端点统一经过面板既有的授权写闸 `sec_write_gate`。
- `dry=true` 只查 TMDB、绝不写库，用于先核对匹配质量。
- 前端入口：**媒体库 → 🖼 缺失封面扫描与补齐**，支持实例下拉、
  范围（最近 1000 / 5000 / 全库）、全选、单张补、试运行、进度显示。

## 5. 参考实现（本仓库）

`app/cover_repair.py` 是零凭据、纯标准库的可复用版本，所有外部调用通过回调注入：

```python
from cover_repair import scan_missing, find_poster, repair, encode_for_emby

scan   = scan_missing(fetch_items, server="emby", scope="recent", limit=1000)
result = repair(scan["items"], tmdb_get, image_fetch, image_upload,
                api_key="<你的 TMDB Key>", dry=False)
```

| 函数 | 注入的回调 | 职责 |
|---|---|---|
| `scan_missing` | `fetch_items(start, count, scope, lib_id)` | 分页扫描 + `ImageTags` 判定 |
| `find_poster` | `tmdb_get(url)` | 严格同类型匹配海报 |
| `repair` | `image_fetch(path, size)` / `image_upload(id, b64, ctype)` | 下载 + 写入 + 回读校验 |
| `encode_for_emby` | — | base64 封装（Emby 4.10 要求） |

`tests/test_cover_repair.py` 16 项单测覆盖分页、Primary 判定、
**不跨类型回退**、多语言海报集、严格/宽松片名匹配、试运行不写库、
上传编码与失败计数。运行：

```bash
python -m unittest discover -s tests -p "test_cover_repair.py"
```

## 6. 配置要求

| 项 | 说明 |
|---|---|
| TMDB API Key | 面板「设置」里填写，未配置时扫描可用但补齐会明确报「未配置 TMDB Key」 |
| TMDB 代理 | `api.themoviedb.org` 常需代理；图床既能服务端直连，也能回退代理隧道 |
| Emby API Key | 每个实例各自配置；新增实例在注册表保存后自动出现在下拉里 |

## 7. 已知边界

- **TMDB 本身没有该片海报**（多为演唱会、古典音乐现场、冷门片）：不会补，
  状态列显示「TMDB 无该片海报（不跨类型/不模糊匹配）」。可在 Emby 原生 UI
  手动上传，或等 TMDB 补图后重跑扫描。
- **单批 300 张**：超大批量请分批或反复扫描补齐；异步 job 有进度可查。
- **只处理 Movie / Series**：Season / Episode 的封面不在本模块范围。
- **成人库**：TMDB 覆盖率低，扫描能跑通但补齐成功率会明显低于主库。
- **全库扫描** 7 万+ 条目需数十秒到数分钟，务必走异步接口。

## 8. 验收记录（线上实机）

| 项目 | 结果 |
|---|---|
| 部署 | 增量补丁、保 inode 回传、容器语法校验、重启后 HTTP 200 |
| 前端 | 整页内联 JS `node --check` 通过；媒体库页面板渲染 |
| 补齐闭环 | TMDB 抓图 → base64 上传 → 204 → 回读 `ImageTags.Primary` 出现 |
| 规模实测 | 最近 5000 条共 73 条缺封面（含扫描期间新入库 1 条）→ **63 张成功写入**，10 条 TMDB 无图；连同前序批次 **共补 79 张** |
| 正确性 | 复扫归零到仅剩「TMDB 无图」条目；误补 0；已补条目跨重启保持 |
| 多实例 | 主库 / 第二实例 / 双实例扫描均通过；全库异步 job 正常返回 |
| 试运行 | `dry=true` 逐条返回匹配来源且**不写库** |
