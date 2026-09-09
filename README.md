# 星塔旅人攻略查询 (Stella Sora Guide)

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

MaiBot 的星塔旅人（Stella Sora）游戏攻略查询插件。在 QQ 群里直接用中文提问，
机器人自动抓取 [stelladb](https://stelladb.pages.dev/) 攻略并返回**官方中文译名**的自然语言回答。

## 功能

| 工具 | 回答的问题 | 示例 |
|------|-----------|------|
| `stellasora_what` | "是什么"：角色属性、技能描述、培养素材、礼物偏好 | 猫眼的培养素材是什么？ |
| `stellasora_how` | "怎么玩"：配队、纹章词条、秘纹推荐、技能升级优先度 | 夏花的纹章优先级？ |
| `stellasora_how` + 预设码 | 队伍预设码（查询统一队伍-槽位表，仅用户明确要求时附码） | 土印记队的预设码是什么？ |
| `lookup_game_term` | 游戏术语中英对照与游戏内 ID（供 planner 内部调用） | — |

### 特性

| 特性 | 说明 |
|------|------|
| **离线优先** | 优先读取本地持久化数据（`data/offline/`），无实时外部网络依赖 |
| **双通道更新** | 每日 17:00 自动定时更新，支持管理员在聊天端发送 `/st_update` 手动触发更新 |
| **直接发送模式** | 内部 LLM 加工后直发聊天，支持人格与表达风格注入（默认开启） |
| **官方中文输出** | 47,500+ 条中英对照字典，术语与技能描述对齐官方译名 |
| **表驱动查询** | 基于统一队伍-槽位表（`team_table.json`）按交集抽取区块，减少 token 消耗 |
| **分组与详略策略** | 同区块多队伍合并展示；问询角色详述，其余成员作为队友并集简列 |
| **纹章网格对齐** | 基于绝对列索引解析，精确对齐 70/80/90 级纹章词条 |
| **预设码支持** | 内置 60 组队伍预设码，用户明确索取时按需附加 |
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

数据文件（离线攻略 `data/offline/`、字典 `data/dict.json` 等）**不随仓库分发**，安装后需运行一次更新脚本在本地生成。

**最简方式：双击插件目录下的 `update_dictionary.bat`**（自动使用 MaiBot 根目录 `.venv` 的 Python，
自动检测本地 StellaSoraData 克隆：有则增量更新，无则 remote 模式直拉 GitHub，
首次运行时自动从零构建字典，最后运行一致性测试）。

命令行方式（在插件目录下，用 MaiBot 根目录 `.venv` 的 Python 执行）：

```bash
# Windows（MaiBot 根目录的 .venv 含 maibot_sdk，系统 python 通常没有）
..\..\.venv\Scripts\python.exe tools/sync_data.py --all
..\..\.venv\Scripts\python.exe tools/update_dict.py --mode remote

# 无法使用代理时加 --proxy "" 强制直连
# ..\..\.venv\Scripts\python.exe tools/update_dict.py --mode remote --proxy ""
```

初始化完成后重启 MaiBot，插件即可离线运行。

## 配置

插件配置位于 `config.toml`（也可在 WebUI 插件配置页修改，热更新即时生效）：

```toml
[plugin]
# 升级到 1.1.1 后直发成品缓存 key 变更自动失效；若 config.toml 中钉死旧版本号，请手动改为 1.1.1 以立即失效旧缓存（或等待 24h TTL 自然过期）
config_version = "1.1.1"

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
# 工具返回文本最大长度（字符），超出按行边界截断并标注，防止撑爆 LLM 上下文；
# how 路径含元素队 infodoc 全文，完整攻略需较大预算；预设码区块不会被截断
default_max_length = 40000
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
# 别名/俗称映射：将别名、俗称、变体写法映射到官方中文名、英文名或条目 ID
# 示例：aliases = { "土" = "地", "花玲" = "花铃" }
aliases = { "土" = "地", "花玲" = "花铃" }

# 文本替换规则：直接将抓取的攻略文本中的英文短语、笔误或旧称替换为指定中文
# 优先于内置字典执行，支持中英文子串替换
# 示例：replacements = { "Finale Echoing" = "终焉绝响" }
replacements = { "Finale Echoing" = "终焉绝响" }
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

## 攻略与预设码数据维护

插件采用**本地离线优先**持久化架构，平时查询完全无需联网。数据更新提供自动化与手动模式：

### 1. 每日定时自动更新
- 插件后台守护协程在每日 **17:00** 自动静默拉取最新攻略与预设码；
- 同步成功后自动原子覆写本地持久化数据，并清除直发成品缓存；
- 如遇网络波动自动保留本地完好数据降级，不影响日常查询服务。

### 2. 聊天端指令手动更新
- 管理员可在配置了白名单的聊天中发送：
  ```
  /st_update
  ```
  （或输入包含 `st_update` 的指令）
- 插件验证白名单权限后异步执行全量数据拉取，同时重建统一队伍-槽位表 `team_table.json`，
  完成后向聊天流反馈更新报告并刷新直发缓存与表运行时缓存。

### 3. 命令行独立同步脚本
在服务器终端或本地维护时，可直接运行 `tools/sync_data.py`：

```bash
# 全量同步所有元素攻略、索引与预设码
python tools/sync_data.py --all

# 仅同步指定元素（如火队 ignis / 水队 aqua / 地队 terra 等）
python tools/sync_data.py --element ignis

# 代理控制：默认使用 http://127.0.0.1:7890；
# 优先级：--proxy 参数 > 环境变量 HTTPS_PROXY/HTTP_PROXY > 默认代理
python tools/sync_data.py --all --proxy http://127.0.0.1:7890  # 指定代理
python tools/sync_data.py --all --proxy ""                     # 强制直连
```

## 字典更新

> - **本地仓库模式**（`update_dictionary.bat` / `--mode local`）：要求本机已有 StellaSoraData 克隆，每次更新只拉取**增量**变更（通常仅几 MB）；但若你还没有本地克隆，首次 `git clone` 会拉取**完整仓库**，请留意流量。
> - **remote 模式**（`--mode remote`）：不会克隆完整仓库，仅下载 `EN/language` 与 `CN/language` 两个语言目录（约 10 MB）。

游戏版本更新后（新角色/新技能），更新字典：

- **一键更新**：双击插件目录下的 `update_dictionary.bat`
  （自动使用 MaiBot 根目录 `.venv` 的 Python；自动检测本地 StellaSoraData 克隆：有则 `git pull` 增量更新 + 本地模式，
  无则 remote 模式直拉 GitHub；`dict.json` 不存在时自动首次构建；
  最后运行字典一致性测试 `test_all.py` A-D 节。
  代理控制：默认走 `http://127.0.0.1:7890`；追加参数 `--direct` 强制直连；也可通过 `HTTPS_PROXY` 环境变量指定其他代理）
- **手动更新**：

```bash
git -C /path/to/StellaSoraData pull --ff-only
python tools/update_dict.py --mode local --source /path/to/StellaSoraData
```

- 更新报告见 `data/_update_report.json`（新增/更新/保留条目统计）

### 提示词文档

直接发送模式的 LLM 提示词按工具分为两份单一事实源，插件启动时由 `plugin.py` 读取并缓存于
模块级变量，修改后需**重启插件**才能生效；若文档缺失或读取失败，直发相关查询将返回
"未找到相关攻略。"并记录 error 日志：

- `docs/prompts_how.md`（`stellasora_how` 工具）：含 `persona_block`/`question`/`material`
  占位符、内联游戏机制知识与回答规则 1-9
- `docs/prompts_what.md`（`stellasora_what` 工具）：含 `persona_block`/`question`/`material`
  占位符（当前仅人格注入模块+骨架，回答规则待补充）

## 关于与致谢

- **本项目由 AI 辅助开发制作**
- [stelladb](https://stelladb.pages.dev/) — 攻略数据
- [StellaSoraData](https://github.com/AutumnVN/StellaSoraData) — 游戏解包字典数据
- [Mistique's Field Reports 社区](https://docs.google.com/spreadsheets/d/1otsS2C1RkXLaFSvp2SMOS-vtRBaEBpZlcgR361_fdAE) — 元素队攻略与预设码

## 许可证

[GPL-3.0-or-later](LICENSE)

