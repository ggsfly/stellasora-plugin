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

- **永久本地持久化（离线优先）**：日常查询优先且直接读取本地文件（`data/offline/` 六大元素详细页、索引与预设码），完全脱离外部网络实时依赖，毫秒级响应；数据文件由更新脚本在本地生成，不随仓库分发
- **双通道数据更新**：支持每日 17:00 自动后台定时更新，同时支持管理员在聊天端发送 `/st_update` 手动触发更新并即刻刷新直发缓存；同步流程同时产出统一队伍-槽位表 `data/offline/presets/team_table.json`（how 查询的数据底座），更新后自动重载
- **直接发送模式**（默认开启）：攻略正文由插件内部 LLM 加工成中文成品后**直接发送到聊天**，
  不经过 MaiBot 的 reply 生成器——避免回复者看不到工具数据导致的答非所问；
  planner 只收到"已发送"，回复一句简短确认
- **精简回复**：直发 LLM 只回答用户问的问题（不列无关条目），回复一般不超过 300 字；
  只在用户明确要求「全部/所有/完整」时才全量列举
- **全程无英文输出**：直发 LLM 提示词强制不留英文——资料残余的英文单词/句子（地名、
  专有名词、描述句）一律意译为自然简体中文；预设码与参数占位符（&Param1&、##术语#ID#）除外
- **官方中文输出**：47,504+ 条全字段中英对照字典（来自游戏解包数据，由更新脚本本地生成），技能/潜能/秘纹/素材名
  全部替换为官方中文译名，如 泷闪（Torrent Flash）、花海·侵蚀（Flower Formation: Erosion）；
  与攻略站逐字一致的技能/潜能**描述文本**也整段译为官方中文
- **多步智能路由**：planner 自动查词→路由工具→组织回答；未收录的俗称自动拆词重试
  （如"土"→官方"地"）
- **how 表驱动查询（统一队伍-槽位表）**：how 查询基于 `data/offline/presets/team_table.json` 运行——问句中的角色成员与表交集命中队伍行，只抓取命中行对应攻略页区块交给 LLM，不再整页下发 infodoc（实测 token 消耗约为整页的 14-18%）；多角色查询与单角色同链路，未命中直接返回"未找到相关攻略"
- **分组输出与联合查询详略**：资料按「N. 队伍名 → 详述成员 → 队友并集行」分块输出——同攻略区块命中多队伍时合并为"1. 队伍名"编号条目，队友以并集行列示（不再逐行重复刷屏）；联合查询问句含 1-2 个角色时仅首个角色详述、其余简列，≥3 个角色或问句含「完整/详细/全部/所有」时全部详述；输出装配层清除攻略页表格行号碎片（纯数字行）并折叠多余空行
- **纹章网格解析与精准对齐**：源头清洗采用 rowspan/colspan 网格列位感知模型，解析层基于绝对列索引映射与横向列带延续，精确对齐 70/80/90 级词条，彻底杜绝跨档错位与伪重复词条
- **预设码按需附码**：预设码内置于统一表中，只有用户明确要求"预设码"时才在结果尾行附加（码原文保真）
- **黑白名单鉴权**：白名单/黑名单模式可切换，支持群号与用户号
- **本地缓存**：攻略页 1 小时、预设码 24 小时缓存，重复提问秒回
- **零第三方依赖**：全部使用 Python 标准库

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

### 数据源与离线持久化说明

- **离线持久化机制**：日常查询 100% 优先读取本地持久化文件（位于 `data/offline/` 目录），彻底摆脱网络波动与站点不可用影响。**该目录与字典数据不随仓库分发**，安装/拉取后需运行一次更新脚本生成（见下方「数据更新」），首次运行前请完成数据初始化；
- 攻略数据源：[stelladb](https://stelladb.pages.dev/)（社区维护的英文攻略站）；
- 预设码数据源：社区维护的公开 Google Docs 文档；
- 字典数据：[StellaSoraData](https://github.com/AutumnVN/StellaSoraData)（游戏解包中英文数据，47,504+ 条官方译名映射，由更新脚本本地生成，查词完全离线）。

## 配置

插件配置位于 `config.toml`（也可在 WebUI 插件配置页修改，热更新即时生效）：

```toml
[plugin]
# 升级到 1.1.0 后旧格式直发缓存自动失效；若 config.toml 中钉死 1.0.0，请手动改为 1.1.0 以立即失效旧缓存（或等待 24h TTL 自然过期）
config_version = "1.1.0"

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
# 直发模式注入 docs/game_knowledge.md 游戏机制知识（纹章推荐输出格式等）；关闭则不注入
inject_knowledge = true

[overrides]
# 别名/俗称/上游笔误映射：将别名、俗称、变体写法映射到官方中文名、英文名或条目 ID
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

## 预设码与统一队伍-槽位表

预设码（Preset Code）是社区用于分享队伍配置的字符串，解码后即为"1 名主控+2 名援护"的
角色 ID 组合。插件将预设码与 stelladb 攻略页队伍区块整合为统一队伍-槽位表
`data/offline/presets/team_table.json`，作为 `stellasora_how` 的唯一数据底座：

- **统一表结构**：每行 = 恰好 3 个槽位（1 主控+2 援护，全部可解析为官方角色）+ 队名
  （预设文档队名与攻略页区块名并集）+ 所属元素 + 攻略区块引用 + 构建期固化的 Rotation 数据；
  槽位不满或无法解析的行不入表，仅记录在构建报告中；
- **预设码解码 slot 入口**：预设码按 base64 解码出 3×32bit 大端角色 ID，直接得到主控/援护
  槽位（60 码全量验证），无需解析队名文本；
- **固化关联**：码行与攻略页队伍区块在表构建期按"主控一致+成员包含+名字相似度决胜"一次
  关联，运行时零模糊匹配；
- **无码行攻略可达**：未关联预设码的队伍区块按共享前缀拆行入表，攻略查询同样覆盖；
- **码原文保真**：表中存码原文，仅当用户明确要求"预设码"时在结果尾行附加
  `预设码：<code>（主控X、援护Y、Z）`，替换引擎不改动码本身；
- **省 token**：how 查询按命中行的 `guide_ref` 只提取对应攻略页区块（数百~数千字符），
  不再整页下发 infodoc（数万字符），实测 material 体积约为整页的 14-18%；
- **表更新同源**：统一表由数据同步流程（每日 17:00 定时 / `/st_update` / CLI 同一入口）的
  presets 项产出，同步完成后运行时缓存自动重载，无需重启插件。

## 字典更新

> ⚠️ **流量提醒**：字典更新需要从 GitHub 拉取 [StellaSoraData](https://github.com/AutumnVN/StellaSoraData) 仓库数据。
> 该仓库包含完整的游戏解包数据（含 `_Lua` 脚本等大目录，完整克隆可达数百 MB）：
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

### 字典构成（全字段）

`dict.json` 为**全字段字典**（47,504 条，8.8 MB）：

| 字段 | 内容 | 消费方 |
|------|------|--------|
| `.1`（29,158 条） | 角色/技能/物品等**名字** | names.json 查词索引 + term_replace（名字字段优先） |
| `.2/.3/...`（18,346 条） | 技能/潜能**描述**、剧情、语音、UI 文本 | term_replace 增量替换（与攻略站文本逐字一致时整段译为官方中文） |

- `names.json`（27,173 键）仅索引 `.1` 名字字段，描述长文本不入索引
- term_replace 译名决胜规则：`.1` 名字字段优先于 `.2+` 描述字段，同为 `.1` 按 CAT_PRIORITY——
  名字译名稳定，`.2+` 仅做增量贡献
- `DictLookup.get_full(id)` / `service.lookup_full(id)` 按 ID 查询完整文本（现即主字典）

### 中文别名与人工修正

插件支持两种维度的自定义覆盖：

1. **运行时中文别名（推荐）**：在 `config.toml` 中配置 `[overrides.aliases]`（可在 WebUI 直接填写，热更新即时生效）。
   用户在群里用简称或俗称提问时，插件自动映射到官方中文名再查攻略。每条别名为一个 `[[overrides.aliases]]` 条目：
   ```toml
   [overrides]
   # 中文别名/俗称 → 官方中文名映射
   # WebUI 会渲染为可增删的列表编辑器

   [[overrides.aliases]]
   alias = "春科"
   official = "科洛妮丝（新春）"

   [[overrides.aliases]]
   alias = "土"
   official = "地"
   ```

2. **底层数据修正（构建时）**：上游解包数据偶有笔误（如 `CharacterDes.157.1` 的「花玲」应为官方「花铃」）。
   `data/overrides.json` 是人工维护的底层修正层，构建/更新字典时自动应用：

   ```json
   {
     "entries": { "CharacterDes.157.1": { "cn": "花铃" } },
     "aliases": { "花玲": "Character.157.1" }
   }
   ```

   - `entries`：按 ID 覆盖条目字段（en/cn/cat），修正笔误
   - `aliases`：向名字索引追加别名（俗称/变体写法 → 主表 ID），目标 ID 必须存在
   - 另有查询侧兜底：查词命中非 Character 条目但存在同英文名的 Character 条目时，
     自动改路由到角色条目，避免攻略抓取被静默跳过

## 开发

```bash
# 全量回归测试（单文件，支持按节运行：python tests/test_all.py B G J）
python tests/test_all.py

# 网络连通性自检
python tools/probe_google_doc.py
```

### 提示词文档

直接发送模式的 LLM 提示词以 `docs/prompts.md` 为单一事实源：插件启动时由 `plugin.py` 的
`_load_prompt_doc()` 读取并缓存于模块级变量，修改该文件后需**重启插件**才能生效。
文档含 4 个占位符（`persona_block`/`knowledge_block`/`question`/`material`）与回答规则 1-9；
若文档缺失或读取失败，直发相关查询将返回"未找到相关攻略。"并记录 error 日志。

- 游戏机制知识文档（`docs/game_knowledge.md`，注入 `{knowledge_block}` 占位符）修改后同样需重启生效。

## 关于与致谢

- **本项目由 AI 辅助开发制作**
- [stelladb](https://stelladb.pages.dev/) — 攻略数据
- [StellaSoraData](https://github.com/AutumnVN/StellaSoraData) — 游戏解包字典数据
- [Mistique's Field Reports 社区](https://docs.google.com/spreadsheets/d/1otsS2C1RkXLaFSvp2SMOS-vtRBaEBpZlcgR361_fdAE) — 元素队攻略与预设码

## 许可证

[GPL-3.0-or-later](LICENSE)

