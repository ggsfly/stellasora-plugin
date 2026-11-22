from pathlib import Path
from typing import Iterable, Optional
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

_PLUGIN_ID = "ggsfly.stellasora-plugin"

# 宿主分配目录
# （见 host_data_dir / host_runtime_dir 注释）
_MAX_REDIRECTS = 3

# 直连用的占位代理键：ProxyHandler 的 proxies 为空时会被 build_opener 丢弃
# （其 skip 逻辑），opener 随后回落到系统/环境代理。用一个永不匹配真实主机的
# 键把它留住，就得到真正的"不做代理"。
_DIRECT_SENTINEL = "http+direct-sentinel.invalid"


class HttpBytesError(Exception):
    """read_http_bytes 的失败基类（各数据源映射成自己的异常类型）。"""


def host_data_dir() -> Path:
    """宿主分配给插件的持久数据目录（CLI 运行兜底；插件运行时由 ctx.paths.data_dir 覆盖）。"""
    return Path(__file__).resolve().parents[2] / "data" / "plugins" / _PLUGIN_ID


def resolve_proxy(proxy: Optional[str]) -> Optional[str]:
    """解析最终代理地址（None=直连）。

    优先级：显式 proxy 参数 → 环境变量 HTTPS_PROXY/HTTP_PROXY → 直连。
    传入空字符串 "" 表示强制直连。不再内置默认代理地址——宿主部署环境
    千差万别，写死本地端口会在无代理机器上让全部上游请求失败；需要代理时
    由部署环境经环境变量提供（与宿主自身行为一致）。
    """
    if proxy is not None:
        # 显式传入空串 → 直连
        return proxy.strip() or None
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    return env_proxy.strip() if env_proxy else None


def build_opener(proxy_url: Optional[str]) -> urllib.request.OpenerDirector:
    """根据代理参数创建一个 urllib opener——代理语义的**唯一解析点**。

    三种取值（调用方直接传原始配置，不要预先 resolve）：
    - `None`：按环境变量链（HTTPS_PROXY/HTTP_PROXY），无则直连——GameKee/B 站用；
    - 空串/空白：强制直连，**不读环境变量**——榜单 `[leaderboard] proxy` 留空用；
    - 非空：用该代理。

    预先 resolve 再传给本函数会造成双解析：空串先被 resolve 成 None，再在这里被
    解释成"读环境变量"，于是"留空强制直连"失效。

    `ProxyHandler({})` 是"不做代理"的标准写法；传 None 会被 urllib 当成待解析的
    代理串而抛 TypeError。
    """
    if proxy_url is None:
        resolved = resolve_proxy(None)
    else:
        resolved = proxy_url.strip() or None
    if resolved:
        proxy_handler = urllib.request.ProxyHandler({"http": resolved, "https": resolved})
        return urllib.request.build_opener(proxy_handler)
    # 直连：build_opener 会把 proxies 为空的 ProxyHandler **丢掉**（其 skip 判定），
    # opener 于是没有代理 handler、回落到系统/环境代理——"强制直连"就失效了。
    # 用带一个必然不匹配占位键的 handler 保证它被保留，且不会命中任何真实主机。
    proxy_handler = urllib.request.ProxyHandler({_DIRECT_SENTINEL: "http://127.0.0.1:1"})
    return urllib.request.build_opener(proxy_handler)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用 urllib 自动跟随跳转：30x 原样返回，由调用方逐跳校验后再决定。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _host_allowed(url: str, allowed_hosts: Iterable[str]) -> bool:
    """主机是否在白名单内（大小写不敏感；只认精确主机名）。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return bool(host) and host in {h.lower() for h in allowed_hosts}


def read_http_bytes(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    headers: Optional[dict] = None,
    proxy_url: Optional[str] = None,
    timeout: int = 30,
    max_bytes: int,
) -> bytes:
    """受限 HTTP GET → 字节（统一代理/状态码/长度上限/跳转校验）。

    出站主机白名单是硬约束：初始 URL 与每一次 `Location` 跳转的目标都必须落在
    `allowed_hosts` 内，否则拒绝——上游响应里的 URL 不可信，不校验等于允许
    第三方把请求引到任意主机。跳转手动跟随（urllib 的自动跳转已禁用），这样
    每一跳都能先校验主机再发请求。

    失败一律抛 HttpBytesError，由调用方映射成自己的异常类型。
    """
    if not _host_allowed(url, allowed_hosts):
        raise HttpBytesError(f"请求主机不在白名单: {urllib.parse.urlparse(url).hostname or url[:80]}")

    opener = build_opener(proxy_url)
    # 用自建 handler 覆盖默认跳转处理（同名类后者优先）
    opener.add_handler(_NoRedirect())

    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        req = urllib.request.Request(current, headers=dict(headers or {}))
        try:
            with opener.open(req, timeout=timeout) as response:
                code = response.getcode()
                location = response.headers.get("Location") if response.headers else None
                if code in (301, 302, 303, 307, 308) and location:
                    target = urllib.parse.urljoin(current, location)
                    if not _host_allowed(target, allowed_hosts):
                        raise HttpBytesError(
                            f"跳转目标主机不在白名单: {urllib.parse.urlparse(target).hostname}"
                        )
                    current = target
                    continue
                if code != 200:
                    raise HttpBytesError(f"HTTP {code}: {current[:120]}")
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise HttpBytesError(f"HTTP {exc.code}: {current[:120]}") from exc
        except HttpBytesError:
            raise
        except Exception as exc:
            raise HttpBytesError(
                f"请求失败: {current[:120]}: {type(exc).__name__}: {exc}"
            ) from exc
        if len(data) > max_bytes:
            raise HttpBytesError(f"响应超过上限（>{max_bytes} 字节）: {current[:120]}")
        return data
    raise HttpBytesError(f"跳转次数超过上限（{_MAX_REDIRECTS}）: {url[:120]}")


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


