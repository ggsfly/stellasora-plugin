# what 直发提示词（单一事实源）

本文件是星塔旅人插件 `stellasora_what` 工具「直接发送模式」LLM 提示词的单一事实源，与 how 工具的 `docs/prompts.md` 相互独立、互不共用。

## 当前状态

**本文件尚未接线。** `plugin.py` 目前 what/how 共用 `docs/prompts.md`；后续接线时需：
1. 为 what 增加模块级文档路径与加载函数（参照 `_PROMPT_DOC_PATH` / `_load_prompt_doc()` 模式）；
2. `handle_what` → `_direct_send` 增加 what 专用模板选择逻辑。

## 占位符说明

- `persona_block`：bot 人格与表达风格块（直发模式按 `inject_persona` 注入；回传模式注入客观体输出要求）——**与 how 完全同源，复用 `_build_persona_block()`**
- `question`：用户的原始问题
- `material`：字典/攻略站查询并已替换为官方中文译名的原始资料

## 已定稿：人格注入模块

人格注入行为与 how 保持一致（`_direct_send` 现有逻辑，无需改动）：

- `inject_persona = true` 且直发：注入 `{persona_block}`（bot 昵称/别名/人格/表达风格/情感特质，与 replyer 同源）
- 回传模式（`direct = false`）：注入客观体输出要求（禁止人格语气，中间产物保持客观攻略体）
- `inject_persona = false`：注入空串（无人格的攻略助手口吻）

## 提示词正文

下方围栏内为提示词正文骨架。**除人格注入（`{persona_block}`）外，其余模块（角色资料结构约定、回答规则、输出格式约束、知识块）均留空，待后续命令补充。**

```
{persona_block}你是星塔旅人（Stella Sora）游戏资料查询助手。用户在 QQ 群里问了下面这个问题，下面还附有一份已替换为官方中文译名的角色/装备资料。

【用户问题】
{question}

【资料】
{material}
```
