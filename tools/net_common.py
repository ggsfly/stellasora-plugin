from pathlib import Path
from typing import Optional
import json
import os
import time
import urllib.request

_PLUGIN_ID = "ggsfly.stellasora-plugin"


def host_data_dir() -> Path:
    """宿主分配给插件的持久数据目录（CLI 运行兜底；插件运行时由 ctx.paths.data_dir 覆盖）。"""
    return Path(__file__).resolve().parents[2] / "data" / "plugins" / _PLUGIN_ID


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


