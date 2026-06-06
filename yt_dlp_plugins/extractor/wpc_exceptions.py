# yt_dlp_plugins/extractor/wpc_exceptions.py
from __future__ import annotations


class WPCError(Exception):
    """
    WPC 自定义基础异常。

    - 仅用于内部业务逻辑。
    - 不依赖 yt-dlp POT 框架。
    """
    pass


class WPCRejectedRequest(WPCError):
    """
    表示该请求被 WPC 拒绝，不应该启动浏览器。

    用于：
    - 代理已被标记为坏
    - 高成本失败冷却期中
    """
    pass