# 当前：1.5.1｜猫猫球攻略源已实现，待验收

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

## 待验收

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
