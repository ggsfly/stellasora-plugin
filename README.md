# 星塔旅人攻略查询 (Stella Sora Guide)

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

MaiBot 的星塔旅人（Stella Sora）游戏攻略查询插件。在 QQ 群里直接用中文提问，
机器人自动抓取 [stelladb](https://stelladb.pages.dev/) 攻略并返回**官方中文译名**的自然语言回答。

## 功能

| 工具 | 回答的问题 | 示例 |
|------|-----------|------|
| `stellasora_what` | "是什么"：角色属性、技能描述、培养素材、礼物偏好 | 猫眼的培养素材是什么？ |
| `stellasora_how` | "怎么玩"：配队、纹章词条、秘纹推荐、技能升级优先度 | 夏花的纹章优先级？ |
| `stellasora_how` + 预设码 | 队伍预设码（仅用户明确要求时查询） | 土印记队的预设码是什么？ |
| `lookup_game_term` | 游戏术语中英对照与游戏内 ID（供 planner 内部调用） | — |

### 特性

- **直接发送模式**（默认开启）：攻略正文由插件内部 LLM 加工成中文成品后**直接发送到聊天**，
  不经过 MaiBot 的 reply 生成器——避免回复者看不到工具数据导致的答非所问；
  planner 只收到"已发送"，回复一句简短确认
- **精简回复**：直发 LLM 只回答用户问的问题（不列无关条目），回复一般不超过 300 字；
  只在用户明确要求「全部/所有/完整」时才全量列举
- **全程无英文输出**：直发 LLM 提示词强制不留英文——资料残余的英文单词/句子（地名、
  专有名词、描述句）一律意译为自然简体中文；预设码与参数占位符（&Param1&、##术语#ID#）除外
- **官方中文输出**：内置 47,504 条全字段中英对照字典（来自游戏解包数据），技能/潜能/秘纹/素材名
  全部替换为官方中文译名，如 泷闪（Torrent Flash）、花海·侵蚀（Flower Formation: Erosion）；
  与攻略站逐字一致的技能/潜能**描述文本**也整段译为官方中文
- **多步智能路由**：planner 自动查词→路由工具→组织回答；未收录的俗称自动拆词重试
  （如"土"→官方"地"）
- **预设码按需查询**：只有用户明确要求"预设码"时才访问预设码文档
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

### 数据源说明

- 攻略数据：[stelladb](https://stelladb.pages.dev/)（社区维护的英文攻略站）
- 字典数据：[StellaSoraData](https://github.com/AutumnVN/StellaSoraData)（游戏解包中英文数据，已随插件打包）
- 预设码：社区维护的公开 Google Docs 文档（官方允许下载）
- 查询攻略/预设码时需要联网访问上述站点；查词功能完全离线

## 配置

插件配置位于 `config.toml`（也可在 WebUI 插件配置页修改，热更新即时生效）：

```toml
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
# 直接发送使用的模型：任务名（utils/replyer/planner 等主程序模型配置里的任务）、
# 模型名（model_config.toml models[].name）或模型标识（model_identifier）均可；
# 推荐使用 utils（快速响应 2-4s，术语翻译已在代码中完成）；留空使用默认模型；无法识别的值回落默认模型
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

## 字典更新

> ⚠️ **流量提醒**：字典更新需要从 GitHub 拉取 [StellaSoraData](https://github.com/AutumnVN/StellaSoraData) 仓库数据。
> 该仓库包含完整的游戏解包数据（含 `_Lua` 脚本等大目录，完整克隆可达数百 MB）：
> - **本地仓库模式**（`update_dict.bat` / `--mode local`）：要求本机已有 StellaSoraData 克隆，每次更新只拉取**增量**变更（通常仅几 MB）；但若你还没有本地克隆，首次 `git clone` 会拉取**完整仓库**，请留意流量。
> - **remote 模式**（`--mode remote`）：不会克隆完整仓库，仅下载 `EN/language` 与 `CN/language` 两个语言目录（约 10 MB），但需要可直连 GitHub 的网络。

游戏版本更新后（新角色/新技能），更新内置字典：

- **一键更新**：双击插件目录下的 `update_dict.bat`
  （自动 `git pull` 本地 StellaSoraData 仓库 → 增量更新 → 一致性校验）
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

### 人工修正与自定义覆盖

插件支持两种维度的自定义覆盖：

1. **运行时配置覆盖（推荐）**：在 `config.toml` 中配置 `[overrides]`（可在 WebUI 直接填写，热更新即时生效）：
   - `aliases`：别名/俗称/输入变体映射（如 `"土" = "地"`, `"花玲" = "花铃"`），查询词自动解析到官方术语。
   - `replacements`：攻略资料文本替换规则（如 `"Finale Echoing" = "终焉绝响"`），优先于内置字典执行，支持中英文短语直接替换。

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
# 字典一致性测试
python tests/test_dict.py

# 术语覆盖率抽查
python tests/check_term_coverage.py

# 网络连通性自检
python tools/probe_google_doc.py
```

## 关于与致谢

- **本项目由 AI 辅助开发制作**
- [stelladb](https://stelladb.pages.dev/) — 攻略数据
- [StellaSoraData](https://github.com/AutumnVN/StellaSoraData) — 游戏解包字典数据
- [Mistique's Field Reports 社区](https://docs.google.com/spreadsheets/d/1otsS2C1RkXLaFSvp2SMOS-vtRBaEBpZlcgR361_fdAE) — 元素队攻略与预设码

## 许可证

[GPL-3.0-or-later](LICENSE)

