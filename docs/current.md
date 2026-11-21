# 当前：1.5.0｜已提交推送 dev，待 QQ 亲测

## 状态

- **三项任务完成**：① 版本下限核实放宽（host 1.3.15 / sdk 2.16.0，逐项证据见 CHANGELOG）；
  ② gpt-6.2-sol 验收十二轮至 PASS；③ commit `15fd649`（25 文件 +3150/-9943）已推送 origin/dev。
- 验收修复记录（第 5–12 轮）：Hook 平台剔除、轻量探查路径（probe_updates 无变化不重建）、
  后台失败重试/手动接管 phase 语义、提交线程互斥 + 双重代际核验、single-flight 等待者
  shield/终态职责收口、正常等待期手动成功/失败对排程的精确语义、空唤醒抖动消除。
- 工作区干净（仅 .zcode/ 未跟踪，不入库）。
- **验收后精简轮**（commit `6c38c67`）：修复 resolve_proxy 无代理环境 NameError
  （默认直连语义）；删旧文本榜渲染层/无调用缓存方法；GameKee 六页请求收口
  `_request_article`；测试桩清理。净 -145 行（代码 2389 + 测试 1429），全量回归
  两连跑全绿、真机探查/解析一致性验证通过。SDK 指南合规核对通过（主体接口符合，
  版本口径说明已入 CHANGELOG）。
- **未发布改动（工作区未提交）**：榜单文字开关（config_version 1.4.1 +
  `leaderboard.render_images`，关闭走 `render_text_board_nodes` 1.4.x 文字榜）；
  how 多图小节按相册原顺序全发（单张失败跳图保其余）；队伍码随图发送（用户裁决：
  查攻略即发图也发码，无关键词分流），索引 schema v2 + 旧 v1 索引自动迁移重建
  （`ensure_gamekee_index` 把 schema 判定纳入 needs_refresh）。

## 待用户亲测（发布门槛，桩结果不算通过）

1. 目标实例 `git pull` + 重启（manifest 变更需重启）→ QQ 实测：
   - 问「艾蕾攻略」「春科直伤篇」「水系攻略」→ 收图（开关 webp 各试一次）
   - 问「地系攻略」「水系攻略」→ 图后是否附对应页队伍码（地页无码应不附）
   - `/st_bb` `/st_fe` → 图片榜单放大看清晰度/对齐
   - `/st_update` → 三路报告文本
2. 宿主渲染依赖：确认实例机 `plugin_runtime.render` 开启且有 Chrome/Edge。
3. 本轮新增未验证项：
   - `render_images=false` 下文字榜实机（表头+每 25 名一节点的排版观感）
   - 多图角色实机收图（薇洛等纯多图相册：张数/顺序是否与 GameKee 页一致）
   - 队伍码随图发送实机（文本节点位置、码是否可直接复制进游戏）
   - v1 索引自动迁移：老实例升级后首次查询是否重建并出现码节点

## 关键决定速查（详见 plans/plan-1.5.0-gamekee.md）

图片一律 send.forward；攻略图 webp（prefer_webp 可关）、榜单渲染 PNG（render_images=false 退文字榜）；
多图小节按相册原顺序全发（"末张=长图"只对部分角色成立）；队伍码为页级公共段，随命中页发送；
懒刷新/计时绑定 last_verified，schema 版本偏低也触发刷新；探查失败 30min 重试；手动探查失败不重置计时；
Hook+执行层双平台校验；manifest=[send.text,send.forward,render.html2png,chat.get_all_streams]；
config_version 1.4.1（[query] 删、[gamekee] 增、[leaderboard] 增 render_images）；
host min 1.3.15 / sdk min 2.16.0（逐项核实放宽，与 manifest 一致）。

## 已验（E1）

- 全量 128 断言全绿（G1-G12 how 契约（含队伍码随图/无码页不附/裸"队伍码"未命中）
  / I 非阻塞 / J 描述契约 / K10-K25 st_update 三路独立
  / R1-R8e 榜单数据层+HTML 纯函数 / S1-S12 图片链路+文字榜开关
  / T1-T8 Hook / U1-U14 解析匹配+队伍码解析 / V1-V14+pre 调度与图片，
  含 V6a-b 成功路径迁移重建、V6a-c 失败路径降级）。
- 真机：六页 24 小节解析、艾蕾真图 220KB webp+缓存命中、match_guides 18 用例。
- 回退：dev@b51a858 + .zcode/config-backup-1.3.0.toml。

## 启动与最小验证

```powershell
F:/MaiBot/.venv/Scripts/python.exe tests/test_all.py    # 必须全绿
git -C F:\MaiBot\plugins\ggsfly_stellasora-plugin status --short
```

## 路由分工

Leader/整合：GLM-5.3-Flash；计划/审查：gpt-6.2-sol（已五轮 PASS）；
干活/搜索：workbuddy/hy4flash-free。共享文件由 Leader 统一写入。

