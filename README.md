# 星塔旅人攻略查询 (Stella Sora Guide)

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

MaiBot 的星塔旅人（Stella Sora）游戏攻略查询插件。在 QQ 群里直接用中文提问，
机器人自动抓取 [stelladb](https://stelladb.pages.dev/) 攻略并返回**官方中文译名**的自然语言回答。

## 功能

| 工具 | 回答的问题 | 示例 |
|------|-----------|------|
| `stellasora_what` | "是什么"：角色属性/技能/培养素材/礼物、秘纹、首领弱点与机制、当期讨伐、卡池资讯 | 猫眼的培养素材是什么？ |
| `stellasora_how` | "怎么玩"：配队、纹章词条、秘纹推荐、技能升级优先度（用户要求时附预设码） | 夏花的纹章优先级？ |
| `lookup_game_term` | 游戏术语中英对照与游戏内 ID（供 planner 内部调用） | — |

### 特性

| 特性 | 说明 |
|------|------|
| **离线优先** | 优先读取本地持久化数据（`data/offline/`），无实时外部网络依赖 |
| **双通道更新** | 每日 17:00 自动定时更新，支持管理员在聊天端发送 `/st_update` 手动触发 |
| **直接发送模式** | 内部 LLM 加工后直发聊天，支持人格与表达风格注入（默认开启） |
| **官方中文输出** | 4.8 万余条中英对照字典，术语与技能描述对齐官方译名 |
| **模块化选段** | 按问题意图只提供相关模块给 LLM，资料不截断 |
| **表驱动查询** | 基于统一队伍-槽位表（`team_table.json`）按成员交集抽取区块，降低 token 消耗 |
| **分组与详略策略** | 同区块多队伍合并展示；问询角色详述，其余成员作为队友并集简列 |
| **纹章网格对齐** | 基于绝对列索引解析，精确对齐 70/80/90 级纹章词条 |
| **权限控制** | 支持群聊/私聊的白名单与黑名单模式 |
| **零第三方依赖** | 纯 Python 标准库实现 |

## 安装

### 方式一：插件市场（推荐）

MaiBot WebUI → 插件市场 → 搜索"星塔旅人" → 安装

### 方式二：手动安装

```bash
# 克隆到 MaiBot 的 plugins 目录
cd MaiBot/plugins
git clone https://github.com/ggsfly/stellasora-plugin.git stellasora
```

重启 MaiBot 后插件自动加载。

### 数据初始化（必须）

数据文件（字典 `data/dict.json`、离线攻略 `data/offline/` 等）**不随仓库分发**，安装后需运行一次更新脚本在本地生成。

**最简方式：双击插件目录下的 `update_dictionary.bat`**（自动使用 MaiBot 根目录 `.venv` 的 Python，自动完成字典构建与离线数据同步，详见下方「数据维护」）。

命令行方式（在插件目录下，用 MaiBot 根目录 `.venv` 的 Python 执行）：

```bash
# Windows（MaiBot 根目录的 .venv 含 maibot_sdk，系统 python 通常没有）
..\..\.venv\Scripts\python.exe tools/update_dict.py --mode remote
..\..\.venv\Scripts\python.exe tools/sync_data.py --all
```

初始化完成后重启 MaiBot，插件即可离线运行。

## 配置

插件配置位于 `config.toml`（也可在 WebUI 插件配置页修改，热更新即时生效）：

```toml
[plugin]
# 配置版本。变更会使直发成品缓存 key 变化（旧答案自动失效）；若 config.toml 钉死旧版本号，请手动同步以免命中旧缓存
config_version = "1.2.0"

[access_control]
# 鉴权模式：
#   off       = 不限制（默认，所有聊天可用）
#   whitelist = 仅白名单内的群/用户可用
#   blacklist = 黑名单内的群/用户禁用
mode = "off"

# 白名单（群号或用户号，每行一个）。群聊按群号判断，私聊按用户号判断
whitelist = []
# 例：
# whitelist = [
#   "123456789",    # 某个群
#   "987654321",    # 某个用户（私聊）
# ]

# 黑名单（格式同上）
blacklist = []

[query]
# 直接发送模式：插件内部用 LLM 加工攻略成品后直接发送到聊天，工具只向 planner 返回"已发送"
# 关闭（false）则退回旧行为：攻略原文交给 planner 翻译（回复质量取决于 planner 转述）
direct_send = true
# 同流同主题直发去重窗口（秒）：同一 stream_id + query 在此时间内重复调用直接拦截，
# 防止 what+how 双直发刷屏
dedup_window = 60
# 直发成品缓存时长（秒），0=禁用（24 小时内重复提问秒回）
answer_cache_ttl = 86400
# 直接发送使用的模型任务名（如 utils/replyer/planner，对应主程序模型配置里的任务）；
# 留空使用默认模型。推荐 utils（快速响应 2-4s，术语翻译已在代码中完成）
# 查询失败时工具统一返回"未找到相关攻略"，不会回传原文
llm_model = "utils"
# 直接发送时注入 bot 人格与表达风格（读取主程序人格配置，成品回答与 bot 口吻一致）；
# 关闭则使用无人格的攻略助手口吻
inject_persona = true

[overrides]
# 中文别名/俗称 → 官方中文名映射
# 用户在群里用简称提问时，插件自动映射到官方角色/术语名再查攻略
# 每条别名为一个 [[overrides.aliases]] 条目，WebUI 会渲染为可增删的列表编辑器

[[overrides.aliases]]
alias = "春科"
official = "科洛妮丝（新春）"

[[overrides.aliases]]
alias = "土"
official = "地"
```

> **提示**：修改主程序全局人格配置（如 bot_config 的 personality/nickname/reply_style 等）不会触发插件配置热重载，已缓存的直发答案口吻最长 24 小时内保持原口吻。如需立即刷新口吻，可手动清空 data/webcache/answers/ 目录，或将 answer_cache_ttl 设置为 0 禁用后再改回。

### 建议配置

公开部署时建议改为白名单模式，只允许自己的群使用：

```toml
[access_control]
mode = "whitelist"
whitelist = ["你的群号"]
```

## 使用示例

在 QQ 群里直接提问即可：

```
千都世先升级什么技能？
猫眼的培养素材是什么？
夏花的纹章优先级？
猫眼火队的秘纹推荐顺序？
土印记队的预设码是什么？
```

机器人会自动完成：术语查词（"土"→官方"地"）→ 抓取对应攻略 → 官方中文输出。

**回答示例（夏花的纹章优先级）：**

> 夏花是风属性角色，以下是她的纹章推荐词条（按出现顺序列举）：
>
> **70级纹章（三角形）**：风系穿透 110 / 技能伤害 20% / 暴击率 15%
> **80级纹章（圆形）**：支援技能等级 +3 / 风系穿透 110 / 技能伤害 20%
> **90级纹章（六边形）**：花海·侵蚀 +3 / 全能领导 +3 / 自我提升 +3

## 数据维护

插件采用**本地离线优先**架构，平时查询无需联网。三类数据及其更新方式：

| 数据 | 生成脚本 | 内容 |
|------|---------|------|
| 字典 `data/dict.json` + `names.json` | `tools/update_dict.py`（`update_dictionary.bat` 自动调用） | 官方中英对照译名（4.8 万余条） |
| 离线攻略/预设码 `data/offline/` | `tools/sync_data.py` | 六元素 infodoc、索引、预设码、ss-data 数据集、榜单数据 |
| 更新报告 `data/_update_report.json` | 上述脚本产出 | 新增/更新/保留条目统计 |

### 1. 一键初始化 / 更新

双击插件目录下的 `update_dictionary.bat`：

- 自动使用 MaiBot 根目录 `.venv` 的 Python（`maibot_sdk` 只在其内）；
- 自动检测本地 ss-data 克隆（亦兼容旧 StellaSoraData 布局）：有则 `git pull` 增量更新 + local 模式，无则 remote 模式直拉 GitHub；`dict.json` 不存在时自动首次构建；
- 随后执行 `sync_data.py --all` 全量同步离线数据，并运行一致性测试。

代理：默认 `http://127.0.0.1:7890`；追加参数 `--direct` 强制直连；亦可用 `HTTPS_PROXY` 环境变量。

### 2. 每日定时自动更新

插件后台协程每日 **17:00** 静默拉取全量离线数据，成功后原子覆写本地数据并清除直发成品缓存；网络异常时保留本地数据降级，不影响查询。

### 3. 聊天端手动更新

管理员在聊天中发送 `/st_update`，插件鉴权后异步执行全量同步，并重建统一队伍-槽位表 `team_table.json`。

### 4. 命令行独立同步

```bash
# 全量同步（元素 infodoc + 索引 + 预设码 + ss-data 数据集 + 榜单数据）
python tools/sync_data.py --all

# 仅同步单项（元素名 / index / presets / character / disc / gacha / raid / blitz / duel / meta / blitz_season）
python tools/sync_data.py --element ignis

# 字典更新（local 需 --source 指向 ss-data 克隆；remote 仅下载语言目录）
python tools/update_dict.py --mode local --source /path/to/ss-data
python tools/update_dict.py --mode remote

# 代理控制：优先级 --proxy 参数 > 环境变量 HTTPS_PROXY/HTTP_PROXY > 默认代理
python tools/sync_data.py --all --proxy ""   # 强制直连
```

### 提示词文档

直发模式的 LLM 提示词为两份单一事实源，`plugin.py` 启动时读取并缓存，修改后需**重启插件**；文档缺失或读取失败时直发查询返回「未找到相关攻略。」并记录 error：

- `docs/prompts_how.md`（`stellasora_how`）：内联游戏机制知识与回答规则
- `docs/prompts_what.md`（`stellasora_what`）：模块化材料说明与回答规则

> 提示词只约束 LLM 的**输出表达**；是否触发工具由 planner（工具 `description`）决定，材料范围由 `service.py` 路由决定。

## 关于与致谢

- **本项目由 AI 辅助开发制作**
- [stelladb](https://stelladb.pages.dev/) — 攻略数据
- [ss-data](https://github.com/AutumnVN/ss-data) — 游戏解包字典数据（原 StellaSoraData）
- [Mistique's Field Reports 社区](https://docs.google.com/spreadsheets/d/1otsS2C1RkXLaFSvp2SMOS-vtRBaEBpZlcgR361_fdAE) — 元素队攻略与预设码

## 许可证

[GPL-3.0-or-later](LICENSE)

