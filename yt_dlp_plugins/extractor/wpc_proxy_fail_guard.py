# -*- coding: utf-8 -*-
"""
yt_dlp_plugins/extractor/wpc_proxy_fail_guard.py

WPC 代理失败短路保护。

设计目标：
1. 当某个 proxy_url 已经被证明无法完成 WPC 浏览器链路时，记录该代理失败；
2. 后续 yt-dlp POT 框架再次请求 WPC provider 时，如果 proxy_url 没变，直接短路；
3. 避免同一个坏代理反复启动 Chromium，浪费 CPU / 内存 / 时间；
4. 不把代理账号密码明文写入日志或 key；
5. 默认进程内有效，适合当前 yt-dlp / consumer 单任务进程内的重试场景。

为什么只做进程内缓存：
- 当前目标是拦截同一轮 yt-dlp POT 框架重复调用；
- 这种重复通常发生在同一进程、同一 proxy_url 内；
- 如果做跨进程文件缓存，可能误伤后续新任务或新代理会话；
- 代理 URL 一般带 session，URL 变化后自然会重新尝试。

如后续需要跨进程缓存，可以再扩展为文件缓存。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class BadProxyRecord:
    """
    单个坏代理记录。

    字段：
    - proxy_key:
        对 proxy_url 做 sha256 后得到的 key，不保存明文代理；
    - masked_proxy:
        脱敏后的代理 URL，仅用于日志；
    - reason:
        首次判定失败的原因；
    - created_at:
        记录创建时间，单位 time.time() 秒；
    """

    proxy_key: str
    masked_proxy: str
    reason: str
    created_at: float


# 进程内坏代理表。
_BAD_PROXY_BY_KEY: dict[str, BadProxyRecord] = {}


def mask_proxy_url(proxy_url: str | None) -> str:
    """
    脱敏代理 URL。

    示例：
        http://user:pass@host:port
    转为：
        http://***:***@host:port

    注意：
    - 该函数只用于日志；
    - 不应把原始 proxy_url 打进日志。
    """
    if not proxy_url:
        return ""

    try:
        u = urlsplit(proxy_url)

        if not u.username and not u.password:
            return proxy_url

        scheme = u.scheme or "http"
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""

        return f"{scheme}://***:***@{host}{port}"
    except Exception:
        return "<invalid-proxy-url>"


def proxy_cache_key(proxy_url: str | None) -> str:
    """
    计算代理缓存 key。

    说明：
    - 使用 sha256(proxy_url)；
    - 不把明文代理账号密码暴露到日志或异常；
    - proxy_url 为空时返回空字符串。
    """
    if not proxy_url:
        return ""

    return hashlib.sha256(proxy_url.encode("utf-8", errors="ignore")).hexdigest()


def get_bad_proxy_record(proxy_url: str | None) -> BadProxyRecord | None:
    """
    查询 proxy_url 是否已经被标记为坏代理。

    返回：
    - BadProxyRecord：已失败，应短路；
    - None：未失败，可以继续尝试。
    """
    key = proxy_cache_key(proxy_url)
    if not key:
        return None

    return _BAD_PROXY_BY_KEY.get(key)


def mark_bad_proxy(proxy_url: str | None, reason: str) -> None:
    """
    标记某个 proxy_url 为坏代理。

    说明：
    - proxy_url 为空时不记录；
    - 同一个 proxy_url 只记录第一次失败原因；
    - 后续重复失败不覆盖，便于保留最初失败现场。
    """
    key = proxy_cache_key(proxy_url)
    if not key:
        return

    if key in _BAD_PROXY_BY_KEY:
        return

    _BAD_PROXY_BY_KEY[key] = BadProxyRecord(
        proxy_key=key,
        masked_proxy=mask_proxy_url(proxy_url),
        reason=reason,
        created_at=time.time(),
    )


def clear_bad_proxy(proxy_url: str | None) -> None:
    """
    清除某个代理的坏代理记录。

    用途：
    - 手动测试；
    - 未来如果想在代理 URL 不变但确认代理恢复后重新尝试，可以调用该函数。
    """
    key = proxy_cache_key(proxy_url)
    if not key:
        return

    _BAD_PROXY_BY_KEY.pop(key, None)


def should_skip_proxy(proxy_url: str | None) -> tuple[bool, BadProxyRecord | None]:
    """
    判断当前 proxy_url 是否应直接跳过。

    返回：
    - (True, record)：该代理已失败，应跳过；
    - (False, None)：未失败，可以启动浏览器。
    """
    record = get_bad_proxy_record(proxy_url)
    return record is not None, record