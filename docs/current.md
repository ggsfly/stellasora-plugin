# 当前：1.5.3｜WHAT 本地验收通过，待许可推送 dev

## 1.5.3 接入状态（2026-11-26）

- **运行口径**：`[what] enabled` 默认关闭。开启只开放查询，首次建库和以后更新均由操作员执行 `/st_update`；不因开启、查询或定时触发资料采集。正常答案由插件合并转发，超过单帧预算按原序分包，每包末行固定 `来源：https://www.gamekee.com/stellasora`。
- **实现位置**：[插件接线](../plugin.py)、[手动资料库](../tools/what_library.py)、[图文交付](../tools/what_delivery.py)。技术细节留日志，QQ 提示用短语；发送未获确认时不重试。HOW 的停轮行为未改，仅清除对不存在的 `stop` 工具的文案引导。
- **本地证据**：三名补丁子智能体的旧会话日志显示 `RATE_LIMIT`、各重试 5 次后在首轮工具调用前退出；原实现已落盘。新开的火山方舟 DeepSeek 会话接手完成补丁。最终本地复跑：手动库 58 项、图文交付 43 项、接线 68 项、离线 57 项、既有 HOW/榜单等 245 项，合计 471 项通过。新增测试零触网尝试；旧套件的 8 次榜单网络尝试全部被拦截。用已有快照回放首建覆盖 1 次目录、8 页清单、722 篇详情；等戳第二轮抽验 36 篇、复用 686 篇，SQLite 完整性通过、快照未变。再以临时库和模拟 QQ 验证「优叶 90 级攻击」、18 张泳装图、整篇新手手册、没查到及送达不确定；未启动真实 MaiBot 或发送 QQ。
- **独立复审**：库的发布与限流闸、原文分包与落款、开关及回执三项均通过。用只读快照库模拟发送薇洛 74 张立绘，小预算下实拆 15 包，图序、逐包体积和末行均符合约定。复审另发现临时库残留会误报「建库中」：已改为只按活跃任务判断，并用实际库与插件、模拟 QQ 验证。取消或任务已结束时，若仍无正式库则提示「请先执行 /st_update」。另用固定时钟验证了 `Retry-After` 的秒数与日期两种写法：上游要求 48 小时，则在期限内再次执行 WHAT 资料同步发起零上游请求。
- **仍待验证**：真实 Planner 是否按结构化意图发起工具、实际 QQ 每包末行/图序/大包送达、异常提示后 Planner 是否重复发话；日期筛选和逐图语义标注不在本次通过结论内。失败保留旧正式代；跨次逐篇断点复用和旧代回收还未完成。实现仍在独立 `dev` 工作区，未提交、未推送、未发布。
- **交付顺序**：开发与本地模拟完成后先汇报已验行为、测试结果与遗留项。只有收到用户明确许可后，才提交并推送到 `dev`。随后由远程 MaiBot 实例加载该 `dev` 提交并做 QQ 实机验收；本机 MaiBot 只是开发环境，不作为实机验收实例。
- **远程实机用例**：在远程实例开启 WHAT；先问「优叶 90 级攻击」确认无库提示，再由操作员执行 `/st_update`。建库后重问并请求「发一下优叶泳装图」，检查无重复回复、图序和每包落款；再按[实机验收清单](plan-1.5.3-what.md#103-实机验收清单qq)检查未命中及故障。如有异常，保留脱敏日志及实际收到的消息顺序。

## 1.5.3 历史里程碑：离线基线（2026-11-26）

- **范围**：只有离线闭环（原文 → 解析 → SQLite+FTS5 → 结构化查询结果）。未接 QQ / Planner /
  `send.forward` / `/st_update`；无联网、无新爬取；`plugin.py`、`_manifest.json`、宿主与发布配置未动；
  HEAD 仍为 `2c49462`，未 commit / 未 push。
- **工作区**：`tools/what_parse.py`、`what_db.py`、`what_query.py`、`what_offline.py`、
  `tests/test_what_offline.py`（本轮新增回归 11 个，合计 57）。
- **本轮实测**：722/722 篇入库（680 节点数组 + 42 `__tjMeta`；645 ok / 77 media_only），新测试 57 项通过、
  既有回归 245 项通过。完整库 `integrity_check=ok`、外键问题 0；独立核对 100,513 条事实、片段、媒体及术语的来源指针，
  回读问题 0。旧回归中榜单分支的 8 次外部 DNS 尝试均被拦截；新离线测试和全量验收没有外部调用。
- **可查产物**：[构建报告](../.zcode/what-research/20261125-offline-build-volc4/build-report.json)、
  [演示结果](../.zcode/what-research/20261125-offline-build-volc4/demo-results.json)、
  [原文验收样例](../.zcode/what-research/20261125-offline-eval/REPORT.md)。已有库禁止覆盖；每次复跑选新输出目录。

从插件目录用 PowerShell 复跑（读取现有本地快照，不回源）：

```powershell
$py = 'F:/MaiBot/.venv/Scripts/python.exe'
$out = '.zcode/what-research/offline-recheck-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
& $py -X utf8 -B tests/test_what_offline.py
& $py -X utf8 -B tools/what_offline.py --build-dir $out build
& $py -X utf8 -B tools/what_offline.py --build-dir $out demo
```

- **语义约定**：分区取真实顶层（怪物 / NPC / 秘纹 / 旅人图鉴 / 攻略区 / 剧情区 / 二创区 / 游戏鉴赏，
  无目录 → null，提示语只留在 `tree_path`）；`chunk.scope` 只用 wiki / article（无 announcement 来源，
  不虚构）；媒体候选要求 kind ∈ image/album、role ∈ sendable_roles、judged，mp4 记 `video_asset`、
  视频封面进待复核；等级有效但无该指标时单独说明，不谎报「等级无效」。
- **定向独立复审：PASS（限离线）**。不把视频或待复核封面当图片，合法等级缺少指标与非法等级分流，
  目录分类不混入提示语，发布前出现同名库时保留旧文件和本次 staging；泳装 18 张保持原文顺序。
  G08 的「体力之音」辅位效果精确命中 24 篇；「水之音」是另一个检索条件，精确命中 5 篇。
- **离线验收边界**：当时尚未接入 QQ、`/st_update` 和 Planner。接入进度见上节。日期两列
  （`event_valid_range` / `observed_at`）和逐图视觉标注均不在这次离线复审的通过结论内。

# 历史：1.5.1｜猫猫球攻略源已验收（按用户确认）

## 已实现

- **猫猫球（B 站）攻略源**：`[how] source`（`gamekee` / `bili` / `all`）热切换；
  `bili` 单源不启动 GameKee 后台探查，`/st_update` 仍显式探查。
- **B 站链路**：`opus/detail?id=` 取详情 → 图片直链 → Pillow 转 webp →
  永久落 `runtime_dir/cache/bili_guides/<opus_id>/`（staging 目录 + 目录级原子切换）；
  二次查询零网络。
- **出站安全**：新增 `net_common.read_http_bytes`（统一代理/状态码/限长/受限跳转），
  GameKee 与猫猫球共用；图片主机白名单含跳转目标校验。
- **清单**：`assets/bili_guides/manifest.json`，42 个官方角色（官方旅人图鉴 pid=211502，
  半角括号写法）+ 20 条已审核动态。像素深渊（其他游戏）不入库。
- **维护脚本** `tools/bili_update.py`：`--ids` 核验 / 默认增量扫描（`opus/feed/space`），
  只出报告，边界未确认或存在待确认候选时不推进 checkpoint。
- **发送层**：两源对称收集（`GuideResult`）+ 公共发送体；成功 `success=True`
  且 `stop_after_execution=True`，真实失败 `success=False` + `error`；
  猫猫球同一相册一节点、多图保序、单段署名。
- **榜单时间与代理**：图片榜"更新于"恒为 `MM-DD HH:MM`（跨午夜可辨日期，
  刷新失败降级时显示缓存数据的实际抓取时间）；新增 `[leaderboard] proxy`
  仅作用于 ssleaderboard，留空强制直连（不读环境变量），GameKee/B 站不受影响。

## 历史验收范围

按用户确认，1.5.1 视为已验收。以下保留原验收范围，不再列为待办。

- QQ 实测：`source=bili` 问「艾蕾攻略」「冬香攻略」收图并带来源署名；
  问未收录角色提示「猫猫球暂未收录」而非换源。
- 热切换：WebUI 改 `[how] source` 保存 → 日志「攻略来源已切换为 X」，
  切到 bili 后无「GameKee 探查排程」日志。
- `all` 双源：一句问出两源图，各自署名；单侧故障时另一侧照发并标注。
- 榜单相关（1.5.0 遗留）：`render_images=false` 文字榜观感、`/st_bb` 渲染复用日志。
- 攻略图 note 不再出现「左右滑动可以查看下一张图！！」（解析位置闸 + 存量索引窄兼容）。

## 已知限制

- 内置清单是静态快照：新动态需人工用 `bili_update.py --ids` 核验后改清单提交；
  列表接口只给 opus_id 与摘要，没有标题/图片数。
- 攻略图缓存永久且不复验上游：作者改图或删动态后需人工清
  `cache/bili_guides/<opus_id>/`。
- Pillow（含 WebP）依赖宿主 Python；缺失时 `bili` 查询明确报错，不降级发原图。
- `to_thread` 里已发出的同步请求无法真正中止；来源切换只保证不再排新探查，
  在飞批次由 single-flight 终态纪律自愈。
- **升级注意**：`[leaderboard] proxy` 留空=强制直连（不读环境变量）。原先靠
  环境变量代理拉榜的实例需显式填写，否则会拉榜失败。
