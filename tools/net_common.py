from pathlib import Path
from typing import Optional
import json
import os
import time
import urllib.request

_PLUGIN_ID = "ggsfly.stellasora-plugin"


def host_root() -> Path:
    """宿主（MaiBot）根目录。

    以插件目录标准布局推导：tools/net_common.py 的上三级即 MaiBot 根目录。
    Cookbook 数据一律写宿主 data/temp，不再落入插件源码目录。
    """
    return Path(__file__).resolve().parents[3]


def host_data_dir() -> Path:
    """宿主分配给插件的持久数据目录（data/plugins/<plugin_id>/）。"""
    return host_root() / "data" / "plugins" / _PLUGIN_ID


def host_cache_dir() -> Path:
    """宿主分配给插件的网络缓存目录（temp/plugins/<plugin_id>/cache/）。"""
    return host_root() / "temp" / "plugins" / _PLUGIN_ID / "cache"


_OFFLINE_DIR = host_data_dir() / "offline"

# 默认代理地址；可通过环境变量 HTTPS_PROXY / HTTP_PROXY 或构造函数 proxy 参数覆盖
_DEFAULT_PROXY = "http://127.0.0.1:7890"


def resolve_proxy(proxy: Optional[str]) -> Optional[str]:
    """解析最终代理地址。

    优先级（高→低）：
      1. 显式传入的 proxy 参数
      2. 环境变量 HTTPS_PROXY / HTTP_PROXY
      3. 默认值 http://127.0.0.1:7890

    传入空字符串 "" 表示强制直连（跳过代理）。
    """
    if proxy is not None:
        # 显式传入空串 → 直连
        return proxy.strip() or None
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if env_proxy:
        return env_proxy.strip()
    return _DEFAULT_PROXY


def build_opener(proxy_url: Optional[str]) -> urllib.request.OpenerDirector:
    """根据代理地址创建 urllib opener。代理为 None 时透明直连。"""
    if proxy_url:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    else:
        # 显式禁用系统代理，保证直连
        proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler)


def atomic_write(file_path: Path, content: str) -> None:
    """原子写入文件：先写临时文件再 rename/replace 覆盖。"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = file_path.with_name(f".{file_path.name}.{time.time_ns()}.tmp")
    try:
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(file_path)
    except Exception:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass
        raise


def read_offline_file(file_path: Path) -> Optional[str]:
    """读取离线持久化文件，若为 json 则提取 data 字段，若为纯文本则直接返回。"""
    if not file_path.is_file():
        return None
    try:
        text = file_path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        if file_path.suffix == ".json":
            data = json.loads(text)
            if isinstance(data, dict):
                content = data.get("data") or data.get("content")
                return content if isinstance(content, str) and content else None
            elif isinstance(data, str) and data:
                return data
        return text
    except Exception:
        return None


def resolve_offline_dir(cache_dir: Path, offline_dir: Optional[Path] = None) -> Optional[Path]:
    """解析离线数据目录。"""
    if offline_dir is not None:
        return Path(offline_dir)
    elif (cache_dir / "offline").is_dir():
        return cache_dir / "offline"
    elif (cache_dir.parent / "offline").is_dir():
        return cache_dir.parent / "offline"
    elif cache_dir.name in ("webcache", ".cache", ".cache_test") or "stellasora" in str(cache_dir).lower():
        return _OFFLINE_DIR
    else:
        return None
