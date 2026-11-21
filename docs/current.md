# 当前：1.5.0 重写｜代码完成，待 QQ 亲测

## 状态

- **六个实施步骤全部完成**，全量回归 EXIT=0（107 断言，9 节 G/I/J/K/R/S/T/U/V）。
- 工作区：dev 分支，修改未提交（用户规则：不经允许不 commit/push）。
- 改动清单：plugin.py 重写、tests/test_all.py 重写、README/CHANGELOG/manifest 更新、
  新增 tools/{gamekee,leaderboard,leaderboard_render,access}.py 与 docs/current.md、
  删除 11 个旧模块 + overrides.json + docs/prompts_*.md。

## 待用户亲测（发布门槛，桩结果不算通过）

1. 目标实例 `git pull` + 重启（manifest 变更需重启）→ QQ 实测：
   - 问「艾蕾攻略」「春科直伤篇」「水系攻略」→ 收图（开关 webp 各试一次）
   - `/st_bb` `/st_fe` → 图片榜单放大看清晰度/对齐
   - `/st_update` → 三路报告文本
2. 宿主渲染依赖：确认实例机 `plugin_runtime.render` 开启且有 Chrome/Edge。

## 关键决定速查（详见 plans/plan-1.5.0-gamekee.md）

图片一律 send.forward；攻略图 webp（prefer_webp 可关）、榜单渲染 PNG；
懒刷新/计时绑定 last_verified；探查失败 30min 重试；手动探查失败不重置计时；
Hook+执行层双平台校验；manifest=[send.text,send.forward,render.html2png,chat.get_all_streams]；
config_version 1.4.0（[query] 删、[gamekee] 增）；host min 1.3.15 / sdk min 2.16.0（逐项核实放宽，与 manifest 一致）。

## 已验（E1）

- 全量 107 断言全绿（G1-G12 how 契约 / I 非阻塞 / J 描述契约 / K10-K25 st_update 三路独立
  / R1-R8e 榜单数据层+HTML 纯函数 / S1-S10 图片链路 / T1-T8 Hook / U1-U14 解析匹配 /
  V1-V14+pre 调度与图片（含无变化不重建路径））。
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

