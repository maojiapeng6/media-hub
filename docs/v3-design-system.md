# KFC Media Hub V3 · Design System 规范

> 阶段 3 交付 · 2026-11-12
> **可运行原型**：`design/v3-design-system.html`（浏览器直接打开，含深色/浅色主题切换、8 组信息架构、驾驶舱、插件市场、服务实例、全组件展示、手机/桌面响应式）
> 原原则则：**先确认完整设计效果，再进入批量实现**。本文件是唯一设计真源，禁止各页面自定义颜色/圆角/间距。

---

## 1. 视觉定位

Apple Liquid Glass + 顶级 SaaS Dashboard + 专业媒体运营控制台。深色高级玻璃质感、多层背景模糊、边缘高光、空间纵深、真实浮层关系。

**禁止**：廉价霓虹风 / 大面积高饱和渐变 / 每卡不同色 / 无意义发光边框 / 过度圆角 / 装饰性图标堆砌 / 统计数字密集堆砌 / 所有页面同一套卡片排列 / 无意义动画 / 文字对比度不足。

---

## 2. Design Token（CSS 变量唯一真源，定义于 `:root`，浅色主题覆写 `[data-theme="light"]`）

### 2.1 颜色 Token
| Token | 深色值 | 用途 |
|---|---|---|
| `--primary` / `--primary-strong` | `#7ed957` / `#52b54b` | 主色（品牌绿）、主按钮、激活态 |
| `--secondary` | `#8b5cf6` | 辅助色（强调/图表第二系列） |
| `--bg` / `--bg-elevated` / `--surface` | `#0b0e14` / `#11151f` / `#151a26` | 页面 / 抬升 / 卡片底 |
| `--border` / `--border-strong` | `rgba(255,255,255,.09)` / `.16` | 分割线 / 输入框边 |
| `--text` / `--text-2` / `--text-3` | `#e8edf6` / `#9aa7bd` / `#6b7891` | 主 / 次 / 弱文字（对比度均 ≥ WCAG AA） |
| `--success/warning/error/info` | `#4ade80 / #fbbf24 / #f87171 / #60a5fa` | 状态色 |
| `--*-bg` | 对应 13% 透明度 | 状态标签底色 |

### 2.2 玻璃材质 Token（**真实 Liquid Glass，不是加 backdrop-filter 就算**）
| Token | 值 | 作用 |
|---|---|---|
| `--glass-bg` | `rgba(28,34,48,.55)` | 半透玻璃底 |
| `--glass-blur` | `24px` | 多层背景模糊（配合 `saturate(1.5)`） |
| `--glass-border` | `rgba(255,255,255,.13)` | 玻璃边缘 |
| `--glass-hi` | `rgba(255,255,255,.22)` | **边缘高光**（`inset 0 1px 0`） |
| `--glass-shadow` | `0 18px 50px -12px rgba(0,0,0,.6)` | 空间纵深外阴影 |

实现要点（`.glass` 类）：
```
background: var(--glass-bg);
backdrop-filter: blur(24px) saturate(1.5);
border: 1px solid var(--glass-border);
box-shadow: var(--glass-shadow), inset 0 1px 0 var(--glass-hi), inset 0 -1px 0 rgba(0,0,0,.12);
::before { 135° 高光渐变覆盖（opacity .5） }
```
**环境光**：3 个 blur(90px) 柔和光晕（主绿/辅紫/信息蓝，opacity .1~.24），非高饱和渐变块。

### 2.3 布局 Token
| Token | 值 |
|---|---|
| `--sidebar-w` / `--header-h` / `--content-max` | `236px` / `64px` / `1440px` |
| `--card-r` / `--btn-r` / `--input-r` | `16px` / `10px` / `10px` |
| `--grid-gap` / `--section` | `16px` / `28px` |
| `--ease` / `--dur` | `cubic-bezier(.22,.8,.28,1)` / `.24s` |

### 2.4 组件 Token
Button（3 态：primary/ghost/danger，hover 抬升 -2px）、Input/Select（focus 主色描边+3px 柔光）、Table（斑马 hover，无重边框）、Card（= .glass）、Modal/Drawer（.glass + 遮罩）、Dropdown、Tabs（分段控）、Tooltip、Badge（pill，`--badge-r:999px`）、Chart（统一配色）。

---

## 3. 信息架构（8 个一级分组，替代当前 43 个平铺菜单）

```
A 工作台       综合总览 / 实时动态 / 服务健康
B 服务中心     服务实例 / 媒体服务器 / 网盘 / 消息机器人 / AI 服务
C 媒体运营     实时观看 / 播放统计 / 排行榜 / 用户管理
D 机器人中心   机器人列表 / 消息路由 / AI 管理 / 游戏管理
E 自动化中心   任务 / 定时 / 规则 / Webhook / 事件
F 插件中心     插件市场 / 已安装 / 插件开发 / 插件权限
G 数据中心     数据统计 / 数据聚合 / 历史 / 导出
H 系统管理     用户 / 角色权限 / API / 日志 / 监控 / 备份 / 设置
```
- 侧栏支持**折叠 / 搜索 / 收藏 / 自定义排序**；插件注册导航自动进入对应分组。
- 禁止打开网站面对几十个无优先级入口。
- 原型实测：36 个导航项，桌面/手机均正确渲染。

---

## 4. 总览驾驶舱（不是数字堆积）

五层视觉优先级：① 系统整体状态（进度环 + 状态标签）② 关键业务数据（KPI 卡）③ 实时动态 ④ 服务实例状态 ⑤ 近期任务与异常。
- **组件注册表驱动**：`WIDGETS = [{id,title,span,render}]`，总览不写死组件列表；插件可动态 `registerWidget()` 贡献。
- 支持自定义布局（拖拽排序 / 添加 / 删除 / 隐藏 / 按用户保存）。
- 原型实测：6 个内置组件，注册表动态生成。

---

## 5. 图标方案（**重要修正**）

**原型中的 emoji 仅为占位示意，不能作为最终 UI 图标。**
- 服务品牌图标：优先使用各项目**官方公开 Logo / SVG**（Emby / Jellyfin / Plex / Telegram / 115 / CloudDrive2 / MoviePilot / qBittorrent / MediaTidy），同一品牌全站共用同一份资源，支持深色/浅色背景。
- **无法取得官方 Logo 时用统一中性图标（线性 stroke 风格，如 Lucide），明确标注，禁止伪造官方标识。**
-  funcional 图标：统一线性图标集（1.8px stroke，currentColor），禁止 emoji 混排（emoji 是彩色贴图，在浅色主题对比度不稳——原型浅色截图已暴露此问题）。
- 官方 Logo 资源建立 `design/brands/` 目录，带来源 URL 与许可证说明。

---

## 6. 响应式（原型实测）

| 断点 | 规则 |
|---|---|
| `>1100px` | 4 列 KPI / 2 列内容 |
| `760–1100px` | 2 列 |
| `<760px` | 1 列；侧栏变抽屉（☰ 开关）；搜索隐藏；**顶栏标题 nowrap、只显示时钟时间（隐藏日期）、隐藏通知/头像**（实测修复过标题竖排 bug）；零横向溢出（DOM 实测 scrollWidth=clientWidth=390） |

---

## 7. 动效（克制、有意义）

- 统一 `--ease cubic-bezier(.22,.8,.28,1)` / `--dur .24s`。
- 允许：hover 抬升 -2px + 柔和阴影、内容 fadeIn-up、进度条/环平滑过渡、卡片 stagger 入场、主题切换渐变。
- 禁止：无意义循环发光、霓虹流动边框、大幅弹跳、页面级闪屏。
- **实测过的坑**：进度卡每 2s 全量重建 DOM 会重触发入场动画导致「一直闪」→ 必须**骨架只建一次 + 增量更新 textContent/style**（线上 V3 进度卡已按此实现）。

---

## 8. 组件清单（原型已包含，可运行验证）

✅ Glass Card / KPI 卡 / 进度环 / 状态标签 / 分段 Tabs / 按钮（primary/ghost/sm/danger）/ 输入框 / 下拉 / 开关 / 表格 / 列表项 / 条形图 / Toast / 环境光背景 / 深浅主题切换 / 侧栏抽屉 / 时钟。

---

## 9. 批量实现纪律（进入阶段 6 前必须遵守）

1. 所有页面引用 `:root` Token，**禁止页面内自定义颜色值**。
2. 所有卡片用 `.glass`，禁止各页重复写 backdrop-filter。
3. 图标按第 5 节方案，**禁止 emoji 进最终 UI**。
4. 总览组件从注册表读，插件可动态贡献。
5. 每批改造后跑：桌面深色 / 浅色 / 手机三套截图 + JS 错误 + 横向溢出断言。
6. 不删除任何现有功能（线上 43 菜单 / 327 API 全部保留，逐步迁移）。
