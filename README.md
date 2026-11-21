# 星塔旅人攻略与排行榜 (Stella Sora Guide)

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

MaiBot 的星塔旅人（Stella Sora）攻略与排行榜插件。QQ 群里中文提问，机器人返回
[GameKee 星塔旅人 wiki](https://www.gamekee.com/stellasora)「一图流攻略」原图
（@星塔攻略组授权转载）；`/st_bb`・`/st_fe` 渲染当期国服排行榜图片。

## 功能

| 入口 | 回答的问题 | 形态 |
|------|-----------|------|
| `stellasora_how` 工具 | "XX攻略 / XX怎么玩 / 风队怎么玩"——角色/属性的一图流攻略 | GameKee 原图直发（合并转发，附所在页队伍码） |
| `/st_bb` | 当期联合讨伐（Boss Blitz）国服 Top100 | 图片榜单（合并转发） |
| `/st_fe` | 当期终焉绝响（Finale Echoing）国服 Top100 | 图片榜单（合并转发） |
| `/st_update`（操作员） | 强刷两榜数据 + 探查 GameKee 攻略源更新 | 文本报告 |

### 特性

| 特性 | 说明 |
|------|------|
| **GameKee 中文源** | 六大属性一图流（水/火/光/暗/风/地，24 个角色小节），结构化 API 解析，不爬 SPA 页面 |
| **原图直发** | 攻略图 CDN webp 转换（同尺寸约 1/9 体积，`prefer_webp` 可关）；多图小节按相册原顺序全发 |
| **队伍码随图** | 查攻略即发图也同时发该属性页的队伍码（页级公共编成码段；页无码时静默跳过） |
| **图片榜单** | HTML 表格经宿主浏览器渲染：名次不补零、分数右对齐千分位完整、长昵称截断不挤分数列；`render_images=false` 退回文字榜 |
| **懒刷新索引** | 内存索引 + TTL（默认 6h）懒刷新；后台每 24h 探查更新（`updated_at` 比对，无变化不重建） |
| **失败即暴露** | 索引不可用/图片下载失败/未收录角色均有明确用户提示，不静默降级 |
| **权限控制** | 群聊/私聊黑白名单；planner 工具清单 Hook 前置剔除（未授权零 token）；仅 QQ 平台 |
| **零第三方依赖** | 纯 Python 标准库实现 |

## 安装

### 插件市场（推荐）

MaiBot WebUI → 插件市场 → 搜索"星塔旅人" → 安装

### 手动安装

```bash
cd MaiBot/plugins
git clone https://github.com/ggsfly/stellasora-plugin.git ggsfly_stellasora-plugin
```

重启 MaiBot 后插件自动加载。**无需数据初始化**——攻略索引在首次查询时自动拉取，
`/st_update`（操作员）可立即强刷两榜与攻略源。

### 宿主要求

- MaiBot **1.4.8+**、SDK 2.23.0+（manifest 已钉死）
- 排行榜图片渲染依赖宿主 `plugin_runtime.render`（默认开启）：宿主机需有
  Chrome/Edge（自动探测）或配置 `browser_ws_endpoint`
- manifest capabilities：`send.text / send.forward / render.html2png / chat.get_all_streams`

## 配置

`config.toml`（WebUI 插件配置页可改，热更新生效）：

```toml
[plugin]
config_version = "1.4.1"

[access_control]
mode = "off"          # off / whitelist / blacklist
whitelist = []
blacklist = []

[leaderboard]
ttl_minutes = 10      # 榜单瘦身缓存有效期（分钟），0=每次触发都重新拉取
render_images = true  # 图片榜单；false 发 1.4.x 文字榜

[gamekee]
index_ttl_minutes = 360   # 攻略索引 TTL（分钟）：查询时过期先刷新
prefer_webp = true        # 攻略图 CDN webp 转换；关闭发原 PNG（约 2MB/张）

[overrides.aliases]
# 中文别名 → 索引角色名（如 春科→科洛妮丝新春、土→地）
```

### 建议配置

公开部署建议白名单模式 + 宿主 `bot_config.toml` 配置操作员：

```toml
[plugin]
permission = ["qq:你的QQ号"]  # /st_update 仅操作员可触发
```

## 使用示例

```
艾蕾攻略          → 艾蕾一图流（单节点图文+水页队伍码）
春科直伤篇        → 科洛妮丝新春·直伤篇（别名+分篇点名）
水系攻略          → 水系全部角色图（每角色一节点）+ 水页队伍码
/st_bb            → 联合讨伐图片榜单
/st_fe            → 终焉绝响图片榜单
/st_update        → 强刷两榜 + 探查攻略源（操作员）
```

攻略图与队伍码同次发送：命中角色/属性所在页有几页带队伍码就附几个文本节点，页无码时静默跳过（当前六页均有码）。

## 数据目录

| 路径 | 内容 |
|---|---|
| `<MaiBot>/data/plugins/ggsfly.stellasora-plugin/gamekee/` | state.json（探查时间戳）+ index.json（索引快照） |
| `<MaiBot>/data/plugins/ggsfly.stellasora-plugin/leaderboard/` | 榜单瘦身缓存（meta/season/{sid}_top.json） |
| `<MaiBot>/temp/plugins/ggsfly.stellasora-plugin/cache/gamekee_images/` | 攻略图片缓存（SHA-256 文件名，7 天/128MiB 回收） |

## 测试

```bash
F:/MaiBot/.venv/Scripts/python.exe tests/test_all.py           # 全量
F:/MaiBot/.venv/Scripts/python.exe tests/test_all.py G U V     # 指定节
```

## 许可

GPL-3.0-or-later。攻略图版权归 @星塔攻略组（B站），经授权转载于 GameKee。
