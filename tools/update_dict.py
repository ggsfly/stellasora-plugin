#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全字段字典更新（CLI 与插件内 /st_update 共用的核心库）

入口：
  update_dict_via_preferred_path()  插件 /st_update 自动调用：本地 ss-data 克隆优先
                                    （git pull + local），否则 remote
  main()                            CLI 薄壳：python tools/update_dict.py --mode ...

两种模式：
  --mode local   从本地 ss-data 仓库读取（需 --source 参数；建议先对本地仓库 git pull）
  --mode remote  直接从 GitHub 拉取最新语言文件（git sparse clone，只下载 EN/language 与
                 CN/language 两个目录，需要本机安装 Git 并加入 PATH）

运行于 MaiBot runner 子进程时 stdout 是黑洞：所有输出必须走 logger，
失败必须以异常上抛（禁止 sys.exit——SystemExit 不是 Exception 子类，
会击穿调用方后台任务的 except 分支造成静默死亡）。

合并策略（保守）：
  - 首次构建：dict.json 不存在时直接从零生成（首装/数据文件未随仓库分发场景）
  - 新数据覆盖旧条目（全字段：.1 名字 + .2/.3 描述/效果/剧情文本）
  - 旧条目独有 ID 保留（历史角色/物品不下线）
  - names.json 全量重建（仅索引 .1 名字字段）
  - 应用 overrides.json 人工修正层
  - 更新报告写入 data/_update_report.json
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_dict import (  # noqa: E402
    _normalize_game_text,
    apply_alias_overrides,
    apply_entry_overrides,
    apply_replacement_overrides,
    build_name_index,
    collect_language_subdirs,
    load_all_entries,
    load_overrides,
    write_json,
)

from net_common import host_data_dir, resolve_proxy as _resolve_proxy

logger = logging.getLogger("stellasora.update_dict")

REPO_URL = "https://github.com/AutumnVN/ss-data.git"

# 本地 ss-data 克隆的约定位置：plugins/ss-data（与 MaiBot/plugins 同级目录）
_LOCAL_CLONE_ROOT = Path(__file__).resolve().parents[2] / "ss-data"


def load_current(dict_path: Path) -> Dict[str, Dict[str, str]]:
    """读取现有字典；不存在时返回空表（支持首次构建，数据文件不再随仓库分发）。"""
    if not dict_path.is_file():
        logger.info("%s 不存在，将执行首次构建（从零生成字典）", dict_path)
        return {}
    with dict_path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def _git_proxy_env(proxy: Optional[str]) -> Dict[str, str]:
    """解析代理并构造 git 子进程环境变量（与 fetch 走同一套 resolve_proxy 语义）。"""
    resolved = _resolve_proxy(proxy)
    env = os.environ.copy()
    if resolved:
        logger.info("git 使用代理: %s", resolved)
        # git 通过 HTTPS_PROXY/HTTP_PROXY 环境变量识别代理
        env["HTTPS_PROXY"] = resolved
        env["HTTP_PROXY"] = resolved
    else:
        logger.info("git 直连模式（不使用代理）")
        # 清除可能残留的系统代理环境变量
        env.pop("HTTPS_PROXY", None)
        env.pop("HTTP_PROXY", None)
    return env


def _run_git(args: list[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> str:
    """执行 git 子进程：捕获输出，失败抛 RuntimeError（含 stderr 尾段）。"""
    git = shutil.which("git")
    if not git:
        raise RuntimeError("未找到 git（需要安装 Git 并加入 PATH）")
    try:
        proc = subprocess.run(
            [git, *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args[:2])} 超时（600s）") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"git {' '.join(args[:2])} 失败 (rc={proc.returncode}): {tail}")
    return proc.stdout


def fetch_from_root(data_root: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """从 ss-data 根目录加载 EN/CN 全字段文本。"""
    _en_bin, en_lang = collect_language_subdirs(data_root, "en")
    _cn_bin, cn_lang = collect_language_subdirs(data_root, "cn")
    if not en_lang.is_dir() or not cn_lang.is_dir():
        raise FileNotFoundError(f"数据源不完整（缺少 language 目录）: {data_root}")
    logger.info("loading EN: %s", en_lang)
    en_data = load_all_entries(en_lang)
    logger.info("loading CN: %s", cn_lang)
    cn_data = load_all_entries(cn_lang)
    return en_data, cn_data


def fetch_remote(tmp_root: Path, proxy: Optional[str] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """git sparse clone 仓库的语言目录到临时目录，再按本地源加载。

    Args:
        tmp_root: 临时目录根路径。
        proxy: HTTP 代理地址，如 "http://127.0.0.1:7890"。
               传入空字符串 "" 表示强制直连；
               传入 None 则自动读取环境变量 HTTPS_PROXY/HTTP_PROXY，否则使用默认代理 127.0.0.1:7890。
    """
    git_env = _git_proxy_env(proxy)
    repo_dir = tmp_root / "ss-data"
    logger.info("sparse clone %s", REPO_URL)
    _run_git(
        ["clone", "--depth", "1", "--filter=blob:none", "--sparse", REPO_URL, str(repo_dir)],
        env=git_env,
    )
    _run_git(["sparse-checkout", "set", "EN/language", "CN/language"], cwd=repo_dir, env=git_env)
    return fetch_from_root(repo_dir)


def build_new_dict(en_data: Dict[str, str], cn_data: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """EN/CN 同 ID 对齐生成新主表（仅保留两侧都存在的条目）。"""
    common = sorted(set(en_data) & set(cn_data))
    out: Dict[str, Dict[str, str]] = {}
    for key in common:
        en_v = _normalize_game_text(en_data[key].strip())
        cn_v = _normalize_game_text(cn_data[key].strip())
        if not en_v or not cn_v:
            continue
        if en_v.lower() == "null" or cn_v == "Null":
            continue
        out[key] = {"en": en_v, "cn": cn_v, "cat": key.split(".", 1)[0]}
    return out


def run_dict_update(
    mode: str,
    source: Optional[str],
    output: Path,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """字典更新核心（可 import）：按模式取源 → 合并 → 修正层 → 原子落盘。返回统计。

    失败以异常上抛（调用方决定降级语义）；dict.json/names.json/_update_report.json
    全部经 write_json 原子写，运行中进程的并发查询不会读到半截文件。
    """
    output_dir = Path(output)
    dict_path = output_dir / "dict.json"
    old_dict = load_current(dict_path)
    logger.info("现有字典: %s 条", f"{len(old_dict):,}")

    # 1. 按模式获取最新语言数据
    if mode == "local":
        if not source:
            raise ValueError("local 模式需要 source 参数（ss-data 根目录）")
        data_root = Path(source)
        if not data_root.is_dir():
            raise FileNotFoundError(f"数据源不存在: {data_root}")
        en_data, cn_data = fetch_from_root(data_root)
    elif mode == "remote":
        tmp_root = Path(tempfile.mkdtemp(prefix="stellasora_dict_"))
        try:
            en_data, cn_data = fetch_remote(tmp_root, proxy=proxy)
        finally:
            # 数据已载入内存，临时目录可立即清理
            shutil.rmtree(tmp_root, ignore_errors=True)
    else:
        raise ValueError(f"未知模式: {mode}（须为 local/remote）")

    logger.info("EN 词条: %s | CN 词条: %s", f"{len(en_data):,}", f"{len(cn_data):,}")

    # 2. 生成新主表并与旧字典合并
    new_dict = build_new_dict(en_data, cn_data)
    merged: Dict[str, Dict[str, str]] = dict(new_dict)
    for key, entry in old_dict.items():
        if key not in merged:
            merged[key] = entry

    added = sorted(set(new_dict) - set(old_dict))
    updated = sorted(
        k for k in set(new_dict) & set(old_dict) if new_dict[k] != old_dict[k]
    )
    stale = sorted(set(old_dict) - set(new_dict))

    # 3. 应用人工修正层（overrides.json，位置固定于插件根目录）后写主表 + 重建名字索引 + 写报告
    overrides = load_overrides(Path(__file__).resolve().parents[1])
    merged = apply_entry_overrides(merged, overrides["entries"])
    merged = apply_replacement_overrides(merged, overrides["replacements"])
    name_index = build_name_index(merged)
    name_index = apply_alias_overrides(name_index, overrides["aliases"])
    write_json(dict_path, merged)
    write_json(output_dir / "names.json", name_index)
    report = {
        "mode": mode,
        "old_count": len(old_dict),
        "merged_count": len(merged),
        "added": len(added),
        "added_sample": added[:50],
        "updated": len(updated),
        "updated_sample": updated[:50],
        "stale_kept": len(stale),
        "stale_sample": stale[:50],
    }
    write_json(output_dir / "_update_report.json", report)

    stats: Dict[str, Any] = {
        "mode": mode,
        "old_count": len(old_dict),
        "total": len(merged),
        "added": len(added),
        "updated": len(updated),
        "stale_kept": len(stale),
    }
    logger.info(
        "字典更新完成: 新增 %d | 更新 %d | 数据源已消失但保留 %d | 合计 %s",
        len(added), len(updated), len(stale), f"{len(merged):,}",
    )
    return stats


def update_dict_via_preferred_path(
    output: Path,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """按优先路径更新字典：本地 ss-data 克隆（git pull + local）优先，否则 remote。

    原 update_dictionary.bat 的等效入口，由插件 /st_update 后台任务自动调用。
    本地克隆 git pull 失败仅告警、以克隆现状继续（克隆本身就是有效数据源）。
    """
    if (_LOCAL_CLONE_ROOT / ".git").is_dir() and (_LOCAL_CLONE_ROOT / "EN" / "language" / "en_US").is_dir():
        logger.info("发现本地 ss-data 克隆: %s，git pull 更新", _LOCAL_CLONE_ROOT)
        try:
            _run_git(["pull", "--ff-only"], cwd=_LOCAL_CLONE_ROOT, env=_git_proxy_env(proxy))
        except RuntimeError as exc:
            logger.warning("本地克隆 git pull 失败，以现状继续: %s", exc)
        return run_dict_update("local", str(_LOCAL_CLONE_ROOT), output, proxy)
    logger.info("本地克隆不存在，使用 remote 模式（GitHub sparse clone）")
    return run_dict_update("remote", None, output, proxy)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update StellaSora CN-EN dictionary.")
    parser.add_argument("--mode", choices=["local", "remote"], default="local",
                        help="local=本地仓库（需 --source）；remote=GitHub 直拉（需 Git）")
    parser.add_argument("--source", default=None,
                        help="ss-data 根目录（仅 local 模式需要）")
    parser.add_argument("--output",
                        default=str(host_data_dir()),
                        help="字典输出目录（含 dict.json / names.json，默认 <宿主>/data/plugins/ggsfly.stellasora-plugin）")
    parser.add_argument(
        "--proxy",
        type=str,
        default=None,
        help=(
            "HTTP 代理地址，如 http://127.0.0.1:7890（默认使用该地址）。"
            "传入空字符串 \"\" 表示强制直连；"
            "不传则自动读取环境变量 HTTPS_PROXY/HTTP_PROXY，否则使用默认代理。"
            "仅 remote 模式（git clone）时生效。"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    try:
        stats = run_dict_update(args.mode, args.source, Path(args.output), proxy=args.proxy)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(
        f"[done] 新增 {stats['added']} | 更新 {stats['updated']} | "
        f"数据源已消失但保留 {stats['stale_kept']} | 合计 {stats['total']:,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
