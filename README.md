# 星塔旅人攻略与排行榜 (Stella Sora Guide)

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

MaiBot 的星塔旅人（Stella Sora）攻略与排行榜插件。QQ 群里中文提问，机器人返回
[GameKee 星塔旅人 wiki](https://www.gamekee.com/stellasora)「一图流攻略」原图
（@星塔攻略组授权转载）；`/st_bb`・`/st_fe` 渲染当期国服排行榜图片。

## 功能

| 入口 | 回答的问题 | 形态 |
|------|-----------|------|
| `stellasora_how` 工具 | "XX攻略 / XX怎么玩 / 风队怎么玩"——角色/属性的一图流攻略 | 攻略图直发（合并转发；来源按 `[how] source` 选 GameKee / 猫猫球 / 双源） |
| `/st_bb` | 当期联合讨伐（Boss Blitz）国服 Top100 | 图片榜单（合并转发） |
| `/st_fe` | 当期终焉绝响（Finale Echoing）国服 Top100 | 图片榜单（合并转发） |
| `/st_update`（操作员） | 强刷两榜数据 + 探查 GameKee 攻略源更新 | 文本报告 |

### 特性

| 特性 | 说明 |
|------|------|
| **GameKee 中文源** | 六大属性一图流（水/火/光/暗/风/地，24 个角色小节），结构化 API 解析，不爬 SPA 页面 |
| **猫猫球 B 站源** | B 站 @铃儿的猫猫球 动态一图流（授权转载），按内置清单 opus_id 精确取详情，覆盖 20 条已审核动态 |
| **攻略来源可选** | `[how] source`：`gamekee`（默认，含队伍码）/ `bili` / `all` 双源同查；WebUI 保存即热切换 |
| **原尺寸 WebP** | GameKee 走 CDN webp 转换（同尺寸约 1/9 体积，`prefer_webp` 可关）；猫猫球原图本地压 webp（约 1/5.7）；多图按原顺序全发 |
| **队伍码随图** | GameKee 源查攻略即发图也同时发该属性页的队伍码（页级公共编成码段；页无码时静默跳过） |
| **图片榜单** | HTML 表格经宿主浏览器渲染：名次不补零、分数右对齐千分位完整、长昵称截断不挤分数列；节点只发图（标题/人数/抓取时间/页码在图内），同数据复用上次渲染结果；`render_images=false` 退回文字榜 |
| **懒刷新索引** | 内存索引 + TTL（默认 6h）懒刷新；后台每 24h 探查更新（`updated_at` 比对，无变化不重建）；`bili` 单源时后台任务不启动 |
| **攻略图永久缓存** | 猫猫球 webp 落 `runtime_dir/cache/bili_guides/<opus_id>/`，无 TTL/无淘汰，二次查询零网络 |
| **失败即暴露** | 索引不可用/图片下载失败/未收录角色均有明确用户提示，不静默降级；未收录与加载失败分开提示 |
| **权限控制** | 群聊/私聊黑白名单；planner 工具清单 Hook 前置剔除（未授权零 token）；仅 QQ 平台 |
| **无需额外插件依赖** | 插件逻辑纯标准库；B 站 webp 转码使用宿主 Pillow（见宿主要求） |

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
- **Pillow（含 WebP 支持）**：`bili` 源要把 B 站原图压成 webp，插件 runner 与宿主
  同一 Python 解释器，宿主 venv 自带 Pillow 即可（验证口径：`python -c
  "from PIL import features; print(features.check('webp'))"` 输出 True）。缺失时
  `bili` 查询会明确报错而不是降级发原图
- manifest capabilities：`send.text / send.forward / render.html2png / chat.get_all_streams`

## 配置

`config.toml`（WebUI 插件配置页可改，热更新生效）：

```toml
[plugin]
config_version = "1.4.2"

[access_control]
mode = "off"          # off / whitelist / blacklist
whitelist = []
blacklist = []

[leaderboard]
ttl_minutes = 10      # 榜单瘦身缓存有效期（分钟），0=每次触发都重新拉取
render_images = true  # 图片榜单；false 发 1.4.x 文字榜
proxy = ""            # 仅用于 ssleaderboard 上游；留空强制直连（不读环境变量）

[gamekee]
index_ttl_minutes = 360   # 攻略索引 TTL（分钟）：查询时过期先刷新
prefer_webp = true        # 攻略图 CDN webp 转换；关闭发原 PNG（约 2MB/张）

[how]
source = "gamekee"    # gamekee=GameKee（含队伍码）/ bili=猫猫球 B站 / all=双源同查

[overrides.aliases]
# 中文别名 → 索引角色名（如 春科→科洛妮丝新春、土→地）
```

### 建议配置

公开部署建议白名单模式 + 宿主 `bot_config.toml` 配置操作员：

```toml
[plugin]
permission = ["qq:你的QQ号"]  # /st_update 仅操作员可触发
```

### 代理（1.5.1 起）

榜单与攻略源的代理是**分开**的：

- `[leaderboard] proxy` 只管 ssleaderboard 上游拉取。**留空即强制直连，不读 `HTTPS_PROXY`/`HTTP_PROXY`**。原先靠环境变量代理拉榜的实例，升级后请显式填写（如 `proxy = "http://127.0.0.1:7890"`），否则会因直连失败而拉不到榜。
- GameKee 与 B 站攻略源不受该设置影响，仍按环境变量代理链（无则直连）。

### 资料查询（1.5.3 开发版，待 QQ 实机验收）

`[what] enabled` 默认 `false`。在测试实例开启后，由操作员执行 `/st_update` 首次建库；后续也只按这个命令更新。开启开关或提问都不会自行建库。若已经有可用旧库，更新失败仍可查询旧版资料。

试问「优叶 90 级攻击」或「发一下优叶泳装图」。Bot 直接发合并转发，一包放不下就按原顺序分包；每包最后一行是 `来源：https://www.gamekee.com/stellasora`。收不到完整资料时，只提示相应的短句。当前验证状态见[交接页](docs/current.md)。

## 使用示例

```
艾蕾攻略          → 艾蕾一图流（gamekee: 单节点图文+水页队伍码）
                  （bili:    猫猫球艾蕾图，署名 B站@铃儿的猫猫球）
水系攻略          → 水系全部角色图（每角色一节点）+ 水页队伍码
/st_bb            → 联合讨伐图片榜单
/st_fe            → 终焉绝响图片榜单
/st_update        → 强刷两榜 + 探查攻略源（操作员）
```

攻略图与队伍码同次发送：命中角色/属性所在页有几页带队伍码就附几个文本节点，页无码时静默跳过（当前六页均有码）。队伍码只属 GameKee 源；`bili` 源不发码，`all` 源两者都发。

`bili` 源的未收录与加载失败分开提示（「猫猫球暂未收录」/「加载失败」），不会悄悄换成 GameKee 的图；`all` 源在某侧失败时会照发另一侧，并在转发里标注哪一侧不可用。

## 数据目录

| 路径 | 内容 |
|---|---|
| `<MaiBot>/data/plugins/ggsfly.stellasora-plugin/gamekee/` | state.json（探查时间戳）+ index.json（索引快照） |
| `<MaiBot>/data/plugins/ggsfly.stellasora-plugin/leaderboard/` | 榜单瘦身缓存（meta/season/{sid}_top.json） |
| `<MaiBot>/temp/plugins/ggsfly.stellasora-plugin/cache/gamekee_images/` | GameKee 攻略图缓存（SHA-256 文件名，7 天/128MiB 回收） |
| `<MaiBot>/temp/plugins/ggsfly.stellasora-plugin/cache/bili_guides/` | 猫猫球攻略图缓存（`<opus_id>/*.webp` + meta.json；**无 TTL/无淘汰**，卸载不清理） |

猫猫球缓存位于宿主 `runtime_dir`（常见为 `temp/plugins/...`）。清空临时目录或重装插件后缓存自然丢失，下次查询重新抓取并压缩——这是有意为之：攻略图是授权转载的静态内容，长期缓存省流量，临时目录本身就不承诺持久。

攻略清单随包发放于 `assets/bili_guides/manifest.json`（42 个官方角色 + 20 条已审核动态）。新增动态需人工确认后改清单并提交，维护脚本 `tools/bili_update.py` 只出候选报告（不下载图、不改清单、不自动推送）：

```bash
python tools/bili_update.py --ids <opus_id1>,<opus_id2>   # 推荐：走详情接口核验，输出可粘贴片段
python tools/bili_update.py --dry-run                     # 尝试列表扫描（受 B 站匿名风控限制）
```

## 测试

```bash
F:/MaiBot/.venv/Scripts/python.exe tests/test_all.py           # 全量
F:/MaiBot/.venv/Scripts/python.exe tests/test_all.py G U V     # 指定节
```

## 许可

GPL-3.0-or-later。攻略图版权归 @星塔攻略组（GameKee 转载）与 @铃儿的猫猫球（B 站），均经授权转载。
