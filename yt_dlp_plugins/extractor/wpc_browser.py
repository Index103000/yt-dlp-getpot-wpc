# yt_dlp_plugins/extractor/wpc_browser.py
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import time
import uuid
from dataclasses import dataclass

import nodriver
import nodriver.core.config
from nodriver import cdp, start

from yt_dlp_plugins.extractor.common import (
    generate_common_fingerprint_key,
    convert_string_to_uint32,
)
from yt_dlp_plugins.extractor.proxy_auth_ext_v3 import (
    parse_proxy_url,
    proxy_has_auth,
    write_proxy_auth_extension,
    maybe_activate_proxy_extension,
    proxy_requires_http_wrapper,
    proxy_server_without_auth,
)
from yt_dlp_plugins.extractor.resource_gate import ResourceGate
from yt_dlp_plugins.extractor.wpc_exceptions import WPCError, WPCRejectedRequest
from yt_dlp_plugins.extractor.wpc_launch_guard import WPCLaunchGuard
from yt_dlp_plugins.extractor.wpc_paths import (
    get_wpc_browser_runtime_root_dir,
    get_wpc_resource_gate_dir,
)
from yt_dlp_plugins.extractor.wpc_proxy_fail_guard import should_skip_proxy, mark_bad_proxy
from yt_dlp_plugins.extractor.wpc_runtime_config import WPC_RUNTIME_CONFIG


# =============================================================================
# 基础常量
# =============================================================================

# WebPoClient 轮询等待间隔。
WEB_PO_BACKOFF_SECONDS = 1.0

# YouTube WPC 初始化页。
#
# 说明：
# - themeRefresh=1 是当前测试中表现比较稳定的入口；
# - 若后续启用 open_youtube_and_wait_ytcfg() 的重试逻辑，
#   会追加 wpc_attempt / wpc_ts，避免浏览器复用异常页或缓存状态。
WPC_YOUTUBE_BOOT_URL = "https://www.youtube.com?themeRefresh=1"

# 浏览器运行期目录中的子目录名称。
#
# 注意：
# - 这些是 browser 模块自己的内部目录结构；
# - 不应由 wpc_paths.py 维护。
BROWSER_PROFILE_DIRNAME = "profile"
BROWSER_EXT_DIRNAME = "ext"


# =============================================================================
# 数据结构：BrowserRuntimeDirs
# =============================================================================

@dataclass
class BrowserRuntimeDirs:
    """
    表示一次浏览器运行对应的临时目录集合。

    字段说明：
    - run_id:
        当前运行目录的唯一标识。
    - root_dir:
        本次浏览器运行目录根路径。
    - profile_dir:
        浏览器 profile 目录。
    - ext_dir:
        代理认证扩展目录。

    当前目录结构示例：
    ~/.cache/yt-dlp-getpot-wpc/browser_runtime/<run_id>/
    ├── profile/
    └── ext/

    说明：
    - 这里的数据结构属于 browser 模块内部实现；
    - wpc_paths.py 只负责提供 browser_runtime 根目录；
    - profile/ext 的具体组织方式由本模块维护。
    """

    run_id: str
    root_dir: str
    profile_dir: str
    ext_dir: str


# =============================================================================
# 日志工具
# =============================================================================

def _logger_warning(logger, message: str, *, once: bool = False) -> None:
    """
    兼容 yt-dlp provider logger 的 warning 调用。

    背景：
    - 有些 yt-dlp logger 支持 warning(message, once=True)；
    - 有些包装 logger 只支持 warning(message)；
    - 为避免参数不兼容，这里统一做兼容封装。

    注意：
    - 该函数只用于内部兜底日志；
    - 对于高频路径，优先使用 logger.debug()，减少线上 warning 噪声。
    """
    try:
        logger.warning(message, once=once)
    except TypeError:
        logger.warning(message)


def _logger_error(logger, message: str) -> None:
    """
    记录 WPC 内部错误日志。

    注意：
    - 不直接调用 logger.error()；
    - yt-dlp 的 provider logger.error() 可能会走 downloader.report_error()；
    - report_error() 可能打印完整 traceback；
    - 对于 WPC 内部的可预期失败，应该只记录 warning/debug；
    - 真正失败通过 WPCError / WPCRejectedRequest 抛给 getpot_wpc.py。
    """
    _logger_warning(logger, message, once=False)


# =============================================================================
# 通用 async timeout 工具
# =============================================================================

async def _await_with_timeout(
    awaitable,
    *,
    timeout_seconds: float,
    timeout_message: str,
):
    """
    对 awaitable 增加 asyncio timeout。

    参数：
    - awaitable:
        需要等待的协程。
    - timeout_seconds:
        超时时间，单位秒。
    - timeout_message:
        超时后抛出的业务错误信息。

    异常：
    - 超时后抛 WPCError；
    - 不直接抛 yt-dlp POT 框架异常。

    说明：
    - WPC 是浏览器方案，比 bgutil 更容易卡在启动、页面加载、JS ready 等阶段；
    - 这里用 asyncio.wait_for 给关键阶段加边界；
    - 超时后统一转成 WPCError，由 getpot_wpc.py 再映射到 yt-dlp 框架异常。
    """
    try:
        return await asyncio.wait_for(
            awaitable,
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as e:
        raise WPCError(timeout_message) from e


async def _safe_evaluate(tab, js: str):
    """
    安全执行 tab.evaluate。

    返回：
    - (True, value)
    - (False, error_repr)

    为什么需要：
    - 页面加载早期 evaluate 可能失败；
    - 等待 document.readyState / ytcfg / WebPoClient 时，不应该因为一次 evaluate
      失败就立即中断；
    - 应该记录并继续轮询，直到达到 timeout。
    """
    try:
        value = await tab.evaluate(js)
        return True, value
    except Exception as e:
        return False, repr(e)


# =============================================================================
# 页面诊断工具
# =============================================================================

async def collect_page_diagnostics(tab) -> dict[str, object]:
    """
    收集当前页面诊断信息。

    用途：
    - 判断是否真的打开了 YouTube；
    - 判断是否进入 chrome-error://chromewebdata/；
    - 判断是否是 ERR_SSL_PROTOCOL_ERROR / ERR_TIMED_OUT 等浏览器错误页；
    - 判断 ytcfg / WebPoClient 为什么不可用。

    注意：
    - 该函数只做 best-effort；
    - evaluate 失败不会抛异常，而是写入诊断字段。
    """

    async def _eval(js: str) -> object:
        ok, value = await _safe_evaluate(tab, js)
        if ok:
            return value
        return f"<evaluate failed: {value}>"

    return {
        "href": await _eval("location.href"),
        "title": await _eval("document.title"),
        "readyState": await _eval("document.readyState"),
        "ytcfg_exists": await _eval(
            "!!window.top['ytcfg'] && typeof window.top['ytcfg'].get === 'function'"
        ),
        "webpo_exists": await _eval(
            "!!window.top['havuokmhhs-0']?.bevasrs?.wpc"
        ),
        "bg_st_hr_enabled": await _eval(
            "!window.top['ytcfg']?.get('EXPERIMENT_FLAGS') "
            "|| !!ytcfg.get('EXPERIMENT_FLAGS')?.bg_st_hr"
        ),
        "body_preview": await _eval(
            "document.body ? document.body.innerText.slice(0, 1000) : ''"
        ),
    }


def _is_chrome_error_page(diagnostics: dict[str, object]) -> bool:
    """
    判断当前页面是否是 Chromium 错误页。

    典型错误：
    - chrome-error://chromewebdata/
    - ERR_SSL_PROTOCOL_ERROR
    - ERR_SSL_VERSION_OR_CIPHER_MISMATCH
    - ERR_TIMED_OUT
    - ERR_PROXY_CONNECTION_FAILED
    - ERR_TUNNEL_CONNECTION_FAILED
    - ERR_SOCKS_CONNECTION_FAILED
    """
    href = str(diagnostics.get("href") or "")
    body = str(diagnostics.get("body_preview") or "")

    if href.startswith("chrome-error://"):
        return True

    chrome_error_markers = (
        "ERR_SSL_PROTOCOL_ERROR",
        "ERR_SSL_VERSION_OR_CIPHER_MISMATCH",
        "ERR_TIMED_OUT",
        "ERR_PROXY_CONNECTION_FAILED",
        "ERR_TUNNEL_CONNECTION_FAILED",
        "ERR_SOCKS_CONNECTION_FAILED",
        "ERR_CONNECTION_CLOSED",
        "ERR_CONNECTION_RESET",
        "ERR_EMPTY_RESPONSE",
        "This site can’t be reached",
        "This site can't be reached",
        "This site can’t provide a secure connection",
        "This site can't provide a secure connection",
        "此网站无法提供安全连接",
        "无法访问此网站",
    )

    return any(marker in body for marker in chrome_error_markers)


def _format_page_diagnostics(diagnostics: dict[str, object]) -> str:
    """
    将页面诊断信息压缩成适合日志和异常的信息。

    注意：
    - body_preview 最多 1000 字符；
    - 不包含代理密码；
    - 用于 WPCError，方便线上 systemd 日志定位。
    """
    return (
        f"href={diagnostics.get('href')!r}; "
        f"title={diagnostics.get('title')!r}; "
        f"readyState={diagnostics.get('readyState')!r}; "
        f"ytcfg_exists={diagnostics.get('ytcfg_exists')!r}; "
        f"webpo_exists={diagnostics.get('webpo_exists')!r}; "
        f"bg_st_hr_enabled={diagnostics.get('bg_st_hr_enabled')!r}; "
        f"body_preview={diagnostics.get('body_preview')!r}"
    )


# =============================================================================
# 浏览器 ready 检查
# =============================================================================

async def wait_browser_ready(
    browser,
    *,
    logger,
    timeout_seconds: float = 10.0,
    interval_seconds: float = 0.2,
) -> None:
    """
    等待 nodriver 浏览器对象进入最低可用状态。

    重要说明：
    - 不要求 browser.connection 非空；
    - Linux/headless/nodriver 环境下 browser.connection 可能一直是 None；
    - 但 browser.main_tab 存在时，browser.get() 和 main_tab.evaluate 仍可能正常工作；
    - 因此这里的最低可用条件是：
        browser is not None
        browser.main_tab is not None

    为什么不能强依赖 browser.connection：
    - 之前线上报错：
        'NoneType' object has no attribute 'send'
      就是因为直接执行 browser.connection.send(...)；
    - 但独立测试脚本证明：
        browser.connection is None
        browser.get() success
        WebPoClient available
      因此 connection=None 不应该作为失败条件。

    超时后：
    - 如果 main_tab 仍然没有 ready，则抛 WPCError。
    """
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds

    last_state: dict[str, object] = {}

    while time.monotonic() < deadline:
        connection = getattr(browser, "connection", None)
        main_tab = getattr(browser, "main_tab", None)
        tabs = getattr(browser, "tabs", None)

        last_state = {
            "browser_is_none": browser is None,
            "connection_is_none": connection is None,
            "main_tab_is_none": main_tab is None,
            "tabs_type": type(tabs).__name__,
            "tabs_len": len(tabs) if isinstance(tabs, list) else None,
        }

        if browser is not None and main_tab is not None:
            if connection is None:
                logger.debug(
                    "[wpc-browser-ready] browser.main_tab is ready but "
                    "browser.connection is None; continue without browser-level CDP connection"
                )
            return

        logger.debug(
            "[wpc-browser-ready] waiting browser ready. "
            f"state={last_state}; "
            f"elapsed={time.monotonic() - started_at:.2f}s; "
            f"timeout={timeout_seconds}s"
        )

        await asyncio.sleep(interval_seconds)

    raise WPCError(
        "browser started but main_tab is not ready; "
        f"timeout_seconds={timeout_seconds}; "
        f"last_state={last_state}"
    )


async def best_effort_clear_cookies(browser, *, logger) -> None:
    """
    尽力清理 cookies。

    重要说明：
    - 清 cookie 不是 WPC 生成 POT 的硬前置；
    - 当前 WPC 每次都会创建独立 profile；
    - 正常情况下 profile 本来就是空的；
    - browser.connection 为 None 时直接跳过；
    - clear cookies 失败也只记录 debug，不中断 WPC。

    为什么保留该步骤：
    - 如果未来 profile 被复用，或者临时目录清理失败，清 cookie 可以降低状态污染；
    - 如果 browser.connection 可用，清理一下仍然有意义。
    """
    connection = getattr(browser, "connection", None)

    if connection is None:
        logger.debug(
            "[wpc-browser-ready] skip clearing cookies because browser.connection is None"
        )
        return

    try:
        await connection.send(cdp.storage.clear_cookies())
    except Exception as e:
        logger.debug(
            f"failed to clear browser cookies via CDP, continue anyway: {e!r}"
        )


# =============================================================================
# 页面等待逻辑
# =============================================================================

async def wait_document_ready(
    tab,
    *,
    logger,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.5,
) -> None:
    """
    等待 document.readyState 进入 interactive 或 complete。

    为什么需要：
    - nodriver browser.get() 返回时，页面可能仍然是 loading；
    - 独立测试中已经看到：
        browser.get() success
        document.readyState = loading
        约 1 秒后 document.readyState = complete
        WebPoClient 随后出现；
    - 如果 browser.get() 后立刻检查 WebPoClient，容易误判 WPC 不可用。

    超时：
    - 抛 WPCError；
    - 不直接抛 yt-dlp 框架异常。
    """
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds

    last_ready_state = None
    last_error = None

    while time.monotonic() < deadline:
        ok, value = await _safe_evaluate(tab, "document.readyState")

        if ok:
            last_ready_state = value
            last_error = None
        else:
            last_error = value

        logger.debug(
            "[document-ready] "
            f"ok={ok}; readyState={value!r}; "
            f"elapsed={time.monotonic() - started_at:.2f}s; "
            f"timeout={timeout_seconds}s"
        )

        if ok and value in ("interactive", "complete"):
            return

        await asyncio.sleep(interval_seconds)

    raise WPCError(
        "Timed out waiting for document.readyState to become interactive/complete; "
        f"timeout_seconds={timeout_seconds}; "
        f"last_ready_state={last_ready_state!r}; "
        f"last_error={last_error!r}"
    )


async def wait_ytcfg_available(
    tab,
    *,
    logger,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.5,
) -> None:
    """
    等待 YouTube 页面中的 ytcfg 可用。

    为什么需要：
    - WPC 判断实验开关时会访问 ytcfg；
    - 如果 ytcfg 尚未注入，WebPoClient 也大概率还没有准备好；
    - 等 ytcfg 可以减少过早检查 WebPoClient 导致的误判。

    超时：
    - 抛 WPCError。
    """
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds

    js = "!!window.top['ytcfg'] && typeof window.top['ytcfg'].get === 'function'"

    last_value = None
    last_error = None

    while time.monotonic() < deadline:
        ok, value = await _safe_evaluate(tab, js)

        if ok:
            last_value = value
            last_error = None
        else:
            last_error = value

        logger.debug(
            "[ytcfg-ready] "
            f"ok={ok}; exists={value!r}; "
            f"elapsed={time.monotonic() - started_at:.2f}s; "
            f"timeout={timeout_seconds}s"
        )

        if ok and value is True:
            return

        await asyncio.sleep(interval_seconds)

    raise WPCError(
        "Timed out waiting for ytcfg to become available; "
        f"timeout_seconds={timeout_seconds}; "
        f"last_value={last_value!r}; "
        f"last_error={last_error!r}"
    )


def _build_youtube_boot_url(attempt: int) -> str:
    """
    构造 YouTube 启动 URL。

    为什么追加 wpc_attempt / wpc_ts：
    - 避免 Chromium 复用上一次 chrome-error 或空白 loading 状态；
    - 让每次重试更像一次新的页面请求；
    - 线上代理偶发错误时，重新打开页面更容易恢复。
    """
    ts = int(time.time() * 1000)

    if "?" in WPC_YOUTUBE_BOOT_URL:
        return f"{WPC_YOUTUBE_BOOT_URL}&wpc_attempt={attempt}&wpc_ts={ts}"

    return f"{WPC_YOUTUBE_BOOT_URL}?wpc_attempt={attempt}&wpc_ts={ts}"


async def open_youtube_and_wait_ytcfg(
    browser,
    *,
    logger,
    page_load_seconds: float,
    document_ready_seconds: float,
    ytcfg_wait_seconds: float,
    max_attempts: int,
    retry_wait_seconds: float,
    proxy_extension_activation_id: str,
) -> object:
    """
    打开 YouTube，并等待 ytcfg 可用。

    返回：
    - 成功时返回 browser.main_tab。

    失败：
    - 多次尝试后仍无法拿到 ytcfg，则抛 WPCError。

    为什么需要这个函数：
    - 线上日志显示代理扩展已经激活，但 ytcfg 等待超时；
    - 这通常不是“扩展未加载”，而是 YouTube 页面没有正常加载完成；
    - 可能表现为：
        1. chrome-error://chromewebdata/
        2. ERR_SSL_PROTOCOL_ERROR
        3. ERR_TIMED_OUT
        4. 页面 readyState 长期 loading
        5. 页面可打开但 JS 资源迟迟没注入 ytcfg
    - 因此要把 browser.get + document.ready + ytcfg wait 作为一个整体重试。
    - 当前 launch_browser() 默认仍然只做一次页面打开；
    - 如果你后续希望启用页面级重试，可以将 launch_browser() 中的单次打开逻辑替换为调用本函数。
    """
    last_error: Exception | None = None
    last_diagnostics: dict[str, object] = {}

    attempts = max(1, int(max_attempts))

    for attempt in range(1, attempts + 1):
        url = _build_youtube_boot_url(attempt)

        logger.debug(
            "[youtube-load] "
            f"attempt={attempt}/{attempts}; "
            f"url={url!r}; "
            f"page_load_seconds={page_load_seconds}; "
            f"document_ready_seconds={document_ready_seconds}; "
            f"ytcfg_wait_seconds={ytcfg_wait_seconds}; "
            f"proxy_extension_activation_id={proxy_extension_activation_id!r}"
        )

        try:
            await _await_with_timeout(
                browser.get(url),
                timeout_seconds=page_load_seconds,
                timeout_message=(
                    "failed to load YouTube page in browser: "
                    f"timeout after {page_load_seconds}s; "
                    f"attempt={attempt}/{attempts}"
                ),
            )

            main_tab = getattr(browser, "main_tab", None)
            if main_tab is None:
                raise WPCError(
                    "browser.main_tab is None after browser.get; "
                    f"attempt={attempt}/{attempts}"
                )

            # browser.get() 成功后先记录一次页面状态。
            diagnostics = await collect_page_diagnostics(main_tab)
            last_diagnostics = diagnostics

            logger.debug(
                "[youtube-load] after browser.get | "
                f"attempt={attempt}/{attempts}; "
                f"{_format_page_diagnostics(diagnostics)}"
            )

            # 如果已经进入 Chromium 错误页，不要浪费 60 秒等 ytcfg，直接进入下一轮。
            if _is_chrome_error_page(diagnostics):
                raise WPCError(
                    "YouTube opened as Chromium error page; "
                    f"attempt={attempt}/{attempts}; "
                    f"{_format_page_diagnostics(diagnostics)}"
                )

            await wait_document_ready(
                main_tab,
                logger=logger,
                timeout_seconds=document_ready_seconds,
                interval_seconds=0.5,
            )

            diagnostics = await collect_page_diagnostics(main_tab)
            last_diagnostics = diagnostics

            logger.debug(
                "[youtube-load] after document ready | "
                f"attempt={attempt}/{attempts}; "
                f"{_format_page_diagnostics(diagnostics)}"
            )

            if _is_chrome_error_page(diagnostics):
                raise WPCError(
                    "YouTube became Chromium error page after document ready; "
                    f"attempt={attempt}/{attempts}; "
                    f"{_format_page_diagnostics(diagnostics)}"
                )

            await wait_ytcfg_available(
                main_tab,
                logger=logger,
                timeout_seconds=ytcfg_wait_seconds,
                interval_seconds=0.5,
            )

            diagnostics = await collect_page_diagnostics(main_tab)
            last_diagnostics = diagnostics

            logger.debug(
                "[youtube-load] ytcfg ready | "
                f"attempt={attempt}/{attempts}; "
                f"{_format_page_diagnostics(diagnostics)}"
            )

            return main_tab

        except Exception as e:
            last_error = e

            logger.debug(
                "[youtube-load] attempt failed | "
                f"attempt={attempt}/{attempts}; "
                f"err={e!r}; "
                f"last_diagnostics={_format_page_diagnostics(last_diagnostics)}"
            )

            if attempt < attempts and retry_wait_seconds > 0:
                await asyncio.sleep(retry_wait_seconds)

    raise WPCError(
        "failed to load YouTube with ytcfg after retries | "
        f"attempts={attempts}; "
        f"proxy_extension_activation_id={proxy_extension_activation_id!r}; "
        f"last_error={last_error!r}; "
        f"last_diagnostics={_format_page_diagnostics(last_diagnostics)}"
    )


# =============================================================================
# 浏览器运行期目录：由 browser 模块自己维护
# =============================================================================

def generate_browser_runtime_run_id() -> str:
    """
    生成一次浏览器运行使用的唯一 run_id。

    返回：
    - 32 位十六进制 UUID 字符串。

    说明：
    - 每次浏览器启动都应使用独立 run_id；
    - profile/ext 目录天然隔离，避免并发冲突。
    """
    return uuid.uuid4().hex


def make_browser_runtime_dirs(run_id: str | None = None) -> BrowserRuntimeDirs:
    """
    创建一次浏览器运行所需的临时目录，并返回目录信息。

    参数：
    - run_id:
        可选运行 ID；若不传，则自动生成新的唯一 run_id。

    返回：
    - BrowserRuntimeDirs 对象。

    当前目录结构：
    <browser_runtime_root>/<run_id>/
    ├── profile/
    └── ext/

    说明：
    - browser_runtime_root 只从 wpc_paths.py 获取根目录；
    - 具体如何组织 run_id/profile/ext，由 browser 模块自己决定。
    """
    runtime_root = get_wpc_browser_runtime_root_dir()
    pathlib.Path(runtime_root).mkdir(parents=True, exist_ok=True)

    actual_run_id = run_id or generate_browser_runtime_run_id()

    root_dir = pathlib.Path(runtime_root) / actual_run_id
    profile_dir = root_dir / BROWSER_PROFILE_DIRNAME
    ext_dir = root_dir / BROWSER_EXT_DIRNAME

    profile_dir.mkdir(parents=True, exist_ok=True)
    ext_dir.mkdir(parents=True, exist_ok=True)

    return BrowserRuntimeDirs(
        run_id=actual_run_id,
        root_dir=str(root_dir),
        profile_dir=str(profile_dir),
        ext_dir=str(ext_dir),
    )


def cleanup_browser_runtime_dirs(
    runtime_dirs: BrowserRuntimeDirs | str,
    *,
    initial_delay_seconds: float | None = None,
    max_retries: int | None = None,
    retry_interval_seconds: float | None = None,
) -> None:
    """
    清理一次浏览器运行对应的临时目录。

    参数：
    - runtime_dirs:
        BrowserRuntimeDirs 对象或运行根目录路径。
    - initial_delay_seconds:
        首次删除前等待多久。
    - max_retries:
        最大重试次数。
    - retry_interval_seconds:
        每次失败后的等待时间。

    说明：
    - 在 WSL + Windows 浏览器 + NTFS 挂载目录场景下，
      Chromium 的 profile 文件句柄可能不会在 browser.stop() 返回后立刻释放；
    - Linux 高并发下也可能出现 Chrome 退出后短时间内仍持有文件句柄；
    - 因此这里采用“短暂等待 + 多次重试”的方式提高清理成功率；
    - 默认值从 WPC_RUNTIME_CONFIG 读取。
    """
    cfg = WPC_RUNTIME_CONFIG

    if initial_delay_seconds is None:
        initial_delay_seconds = cfg.runtime_cleanup_initial_delay_seconds

    if max_retries is None:
        max_retries = cfg.runtime_cleanup_max_retries

    if retry_interval_seconds is None:
        retry_interval_seconds = cfg.runtime_cleanup_retry_interval_seconds

    if isinstance(runtime_dirs, BrowserRuntimeDirs):
        root_dir = runtime_dirs.root_dir
    else:
        root_dir = str(runtime_dirs)

    if not root_dir:
        return

    if not os.path.exists(root_dir):
        return

    last_error = None

    if initial_delay_seconds > 0:
        time.sleep(initial_delay_seconds)

    for attempt in range(1, max_retries + 1):
        try:
            shutil.rmtree(root_dir)
            return
        except FileNotFoundError:
            return
        except Exception as e:
            last_error = e

        if attempt < max_retries:
            time.sleep(retry_interval_seconds)

    if last_error:
        raise last_error


# =============================================================================
# 浏览器可用性检查
# =============================================================================

def is_browser_available(
    *,
    logger,
    browser_executable_path: str | None,
) -> bool:
    """
    检查浏览器是否可用。

    参数：
    - logger:
        日志对象。
    - browser_executable_path:
        浏览器可执行文件路径。

    返回：
    - True:
        可用。
    - False:
        不可用。

    当前规则：
    1. 必须显式提供 browser_path；
    2. 指定路径必须存在；
    3. 指定路径必须是文件；
    4. 指定路径必须具备执行权限。

    注意：
    - 这里只返回 bool；
    - 不抛 WPCError；
    - provider.is_available() 调用它时更适合保持安静。
    """
    if not browser_executable_path:
        logger.debug(
            "WPC PO Token Provider requires browser_path to be configured. "
            'Please pass it with --extractor-args "youtubepot-wpc:browser_path=XYZ".'
        )
        return False

    path = pathlib.Path(browser_executable_path)

    if not path.exists():
        logger.debug(f"Browser executable path does not exist: {browser_executable_path}")
        return False

    if not path.is_file():
        logger.debug(f"Browser executable path is not a file: {browser_executable_path}")
        return False

    if not os.access(str(path), os.X_OK):
        logger.debug(f"Browser executable path is not executable: {browser_executable_path}")
        return False

    return True


# =============================================================================
# WebPoClient 可用性检查与 mint
# =============================================================================

async def get_webpo_client_path(tab, logger):
    """
    等待页面中的 WebPoClient 可用，并返回对应的 JS 访问路径。

    当前 WPC 使用的路径：
        window.top['havuokmhhs-0']?.bevasrs?.wpc

    参数：
    - tab:
        nodriver 页面对象。
    - logger:
        日志对象。

    返回：
    - str:
        WebPoClient 的 JS 路径表达式。
    - False:
        在超时时间内未找到。

    注意：
    - 这里不直接抛异常；
    - mint_po_token() 会根据返回值抛 WPCError。
    """
    # todo: dynamically extract
    # note: this assumes "bg_st_hr" experiment is enabled
    webpo_client_path = "window.top['havuokmhhs-0']?.bevasrs?.wpc"

    timeout_seconds = WPC_RUNTIME_CONFIG.browser_timeout.webpo_client_wait_seconds
    deadline = time.monotonic() + timeout_seconds
    started_at = time.monotonic()

    bg_st_hr_js = (
        "!window.top['ytcfg']?.get('EXPERIMENT_FLAGS') "
        "|| !!ytcfg.get('EXPERIMENT_FLAGS')?.bg_st_hr"
    )

    last_webpo_value = None
    last_experiment_value = None
    last_ready_state = None
    last_href = None

    while time.monotonic() < deadline:
        ok_webpo, webpo_exists = await _safe_evaluate(
            tab,
            f"!!{webpo_client_path}",
        )

        ok_exp, experiment_enabled = await _safe_evaluate(
            tab,
            bg_st_hr_js,
        )

        ok_ready, ready_state = await _safe_evaluate(
            tab,
            "document.readyState",
        )

        ok_href, href = await _safe_evaluate(
            tab,
            "location.href",
        )

        if ok_webpo:
            last_webpo_value = webpo_exists

        if ok_exp:
            last_experiment_value = experiment_enabled

        if ok_ready:
            last_ready_state = ready_state

        if ok_href:
            last_href = href

        logger.debug(
            "[webpo-ready] "
            f"webpo_ok={ok_webpo}; webpo_exists={webpo_exists!r}; "
            f"bg_st_hr_ok={ok_exp}; bg_st_hr_enabled={experiment_enabled!r}; "
            f"ready_ok={ok_ready}; readyState={ready_state!r}; "
            f"href_ok={ok_href}; href={href!r}; "
            f"elapsed={time.monotonic() - started_at:.2f}s; "
            f"timeout={timeout_seconds}s"
        )

        if ok_webpo and webpo_exists is True:
            return webpo_client_path

        if ok_exp and experiment_enabled is False:
            _logger_warning(
                logger,
                "bg_st_hr experiment is not enabled, WebPoClient may not be available.",
                once=True,
            )

        await asyncio.sleep(WEB_PO_BACKOFF_SECONDS)

    logger.debug(
        "[webpo-ready] "
        "Timed out waiting for WebPoClient to be available in browser. "
        f"timeout_seconds={timeout_seconds}; "
        f"last_webpo_value={last_webpo_value!r}; "
        f"last_bg_st_hr_enabled={last_experiment_value!r}; "
        f"last_ready_state={last_ready_state!r}; "
        f"last_href={last_href!r}"
    )

    return False


async def mint_po_token(
    tab,
    logger,
    content_binding,
    mint_cold_start_token=False,
    mint_error_token=False,
):
    """
    在已准备好的浏览器页面中，通过 WebPoClient 生成一个 PO Token。

    参数：
    - tab:
        nodriver 页面对象。
    - logger:
        日志对象。
    - content_binding:
        本次 mint 的 content binding。
    - mint_cold_start_token:
        是否请求 cold-start token。
    - mint_error_token:
        是否请求 error token。

    返回：
    - 成功生成的 PO Token 字符串。

    异常：
    - WebPoClient 不可用时抛 WPCError；
    - 长时间处于 notready 状态时抛 WPCError；
    - 单次 mws 执行超过 WPC_WEBPO_MINT_TIMEOUT_SECONDS 时抛 WPCError。
    """
    webpo_client_path = await get_webpo_client_path(tab, logger)

    if not webpo_client_path:
        raise WPCError("Could not find WebPoClient in browser")

    mws_params = {
        "c": content_binding,
        "mc": mint_cold_start_token,
        "me": mint_error_token,
    }

    # 通过浏览器页面上下文执行 WebPoClient().mws(...)。
    # 若返回 SDF:notready，则走退避重试。
    mint_po_token_code = f"""
        {webpo_client_path}().then((client) => client.mws({json.dumps(mws_params)})).catch(
            (e) => {{
                if (String(e).includes('SDF:notready')) {{
                    return 'backoff';
                }} else {{
                    throw e;
                }}
            }}
        )
    """

    timeout_seconds = WPC_RUNTIME_CONFIG.browser_timeout.webpo_mint_seconds
    deadline = time.monotonic() + timeout_seconds

    tries = 0

    while time.monotonic() < deadline:
        remaining_seconds = max(1.0, deadline - time.monotonic())

        po_token = await _await_with_timeout(
            tab.evaluate(mint_po_token_code, await_promise=True),
            timeout_seconds=min(remaining_seconds, timeout_seconds),
            timeout_message=(
                "Timed out executing WebPoClient.mws in browser. "
                f"timeout_seconds={timeout_seconds}, content_binding={content_binding}"
            ),
        )

        if po_token != "backoff":
            return po_token

        logger.debug("Waiting for WebPoClient to be ready in browser...")
        await asyncio.sleep(WEB_PO_BACKOFF_SECONDS)
        tries += 1

    raise WPCError(
        "Timed out waiting for WebPoClient to be ready in browser. "
        f"timeout_seconds={timeout_seconds}, tries={tries}"
    )


# =============================================================================
# 浏览器启动与配置
# =============================================================================

async def launch_browser(config, *, logger):
    """
    启动浏览器并完成页面初始化，强制代理扩展激活成功。

    返回：
    - 已初始化完成的浏览器实例。

    初始化流程：
    1. 启动浏览器；
    2. 等待 browser/main_tab ready；
    3. 尽力清理 cookies；
    4. 强制激活 MV3 代理扩展；
    5. 打开 YouTube；
    6. 等 document.readyState；
    7. 等 ytcfg 可用；
    8. 返回 browser。

    关键生命周期规则：
    - browser 启动成功后，如果本函数后续任一步失败，必须在本函数内部关闭 browser；
    - 只有完整初始化成功后，才 return browser；
    - return 之后由外层 mint_po_token_once() 的 finally 负责关闭 browser；
    - 这样可以避免 ytcfg 超时、页面加载超时、代理异常时残留 Chromium 进程。
    """
    browser_timeout = WPC_RUNTIME_CONFIG.browser_timeout
    browser = None

    try:
        # ------------------- 启动浏览器 -------------------
        try:
            browser = await _await_with_timeout(
                start(config=config),
                timeout_seconds=browser_timeout.browser_launch_seconds,
                timeout_message=(
                    "failed to start browser: timeout after "
                    f"{browser_timeout.browser_launch_seconds}s"
                ),
            )
        except WPCError:
            raise
        except Exception as e:
            raise WPCError(f"failed to start browser: {e!r}") from e

        # ------------------- 等待 browser ready -------------------
        await wait_browser_ready(
            browser,
            logger=logger,
            timeout_seconds=min(10.0, browser_timeout.browser_launch_seconds),
            interval_seconds=0.2,
        )

        # ------------------- 尝试清理 cookies（best-effort） -------------------
        # 清理 cookie 只作为 best-effort。
        # 某些 Linux/nodriver 场景下 browser.connection 可能为 None，
        # 此时不能因为清理 cookie 失败就终止 WPC。
        await best_effort_clear_cookies(browser, logger=logger)

        # ------------------- 强制激活代理扩展 -------------------
        # 激活 MV3 代理扩展
        # - 测试已经证明：只传 --load-extension 时，浏览器可能不走代理；
        # - 调用 health 激活后，浏览器代理才会生效；
        # - 所以该步骤虽然不一定要立刻抛异常，但必须记录状态，并影响后续诊断。
        #
        # 注意：
        # - 生产链路通常不要传 proxy_url；
        # - proxy_url 非空会触发 health 页面动态切换代理，可能引入 need_restart 语义；
        # - WPC 一次性浏览器中，代理配置已经写入 wpc_sw.js，通常只需要“激活”，不需要“切换”。
        # -------------------------------------------------------------------------
        proxy_extension_activation_id = ""
        proxy_extension_activation_error = ""

        try:
            proxy_extension_activation_id = await maybe_activate_proxy_extension(
                browser,
                timeout=getattr(
                    WPC_RUNTIME_CONFIG,
                    "proxy_extension_activation_seconds",
                    3.0,
                ),
                visible_probe=False,
                proxy_url="",  # 生产环境只唤醒，不切换代理
            )

            if not proxy_extension_activation_id:
                proxy_extension_activation_error = (
                    "proxy extension activation returned empty extension_id"
                )
                raise WPCError(
                    "MV3 proxy extension failed to activate | no extension_id returned"
                )

            logger.debug(
                "[wpc-proxy-auth] proxy extension successfully activated "
                f"| extension_id={proxy_extension_activation_id}"
            )

        except WPCError:
            raise
        except Exception as e:
            proxy_extension_activation_error = repr(e)
            raise WPCError(
                "MV3 proxy extension activation failed, cannot proceed with browser launch | "
                f"error={proxy_extension_activation_error}"
            ) from e

        # ------------------- 打开 YouTube 页面 -------------------
        try:
            await _await_with_timeout(
                browser.get(WPC_YOUTUBE_BOOT_URL),
                timeout_seconds=browser_timeout.page_load_seconds,
                timeout_message=(
                    "failed to load YouTube page in browser: timeout after "
                    f"{browser_timeout.page_load_seconds}s"
                ),
            )
        except Exception as e:
            raise WPCError(
                "failed to load YouTube page in browser "
                f"| proxy_extension_activation_id={proxy_extension_activation_id!r} "
                f"| proxy_extension_activation_error={proxy_extension_activation_error!r} "
                f"| caused_by={e!r}"
            ) from e

        main_tab = getattr(browser, "main_tab", None)
        if main_tab is None:
            raise WPCError(
                "browser.main_tab is None after browser.get "
                f"| proxy_extension_activation_id={proxy_extension_activation_id!r} "
                f"| proxy_extension_activation_error={proxy_extension_activation_error!r}"
            )

        diagnostics = await collect_page_diagnostics(main_tab)
        logger.debug(
            "[youtube-load] after browser.get | "
            f"{_format_page_diagnostics(diagnostics)}"
        )

        if _is_chrome_error_page(diagnostics):
            raise WPCError(
                "YouTube opened as Chromium error page "
                f"| proxy_extension_activation_id={proxy_extension_activation_id!r} "
                f"| proxy_extension_activation_error={proxy_extension_activation_error!r} "
                f"| diagnostics={_format_page_diagnostics(diagnostics)}"
            )

        # ------------------- 等 document ready -------------------
        try:
            await wait_document_ready(
                main_tab,
                logger=logger,
                timeout_seconds=min(30.0, browser_timeout.page_load_seconds),
                interval_seconds=0.5,
            )
        except Exception as e:
            raise WPCError(
                "failed while waiting document ready "
                f"| proxy_extension_activation_id={proxy_extension_activation_id!r} "
                f"| proxy_extension_activation_error={proxy_extension_activation_error!r} "
                f"| caused_by={e!r}"
            ) from e

        diagnostics = await collect_page_diagnostics(main_tab)
        logger.debug(
            "[youtube-load] after document ready | "
            f"{_format_page_diagnostics(diagnostics)}"
        )

        if _is_chrome_error_page(diagnostics):
            raise WPCError(
                "YouTube became Chromium error page after document ready "
                f"| proxy_extension_activation_id={proxy_extension_activation_id!r} "
                f"| proxy_extension_activation_error={proxy_extension_activation_error!r} "
                f"| diagnostics={_format_page_diagnostics(diagnostics)}"
            )

        # ------------------- 等 ytcfg 可用 -------------------
        try:
            await wait_ytcfg_available(
                main_tab,
                logger=logger,
                timeout_seconds=min(30.0, browser_timeout.page_load_seconds),
                interval_seconds=0.5,
            )
        except Exception as e:
            raise WPCError(
                "failed while waiting ytcfg "
                f"| proxy_extension_activation_id={proxy_extension_activation_id!r} "
                f"| proxy_extension_activation_error={proxy_extension_activation_error!r} "
                f"| caused_by={e!r}"
            ) from e

        # 只有完整初始化成功后，才把 browser 交给外层。
        return browser

    except Exception:
        # 关键修复：
        # 如果 browser 已经启动，但初始化过程失败，
        # 必须在这里关闭，否则外层拿不到 browser 引用，会残留 Chromium。
        if browser is not None:
            try:
                _logger_warning(
                    logger,
                    "[wpc-browser] launch_browser failed after browser started; stopping browser now",
                    once=True,
                )
                browser.stop()
            except Exception as stop_error:
                _logger_error(
                    logger,
                    f"[wpc-browser] failed to stop browser after launch failure: {stop_error!r}",
                )

        raise


def build_nodriver_config(
    *,
    browser_executable_path: str,
    runtime_dirs: BrowserRuntimeDirs,
    proxy: str | None = None,
):
    """
    为一次性浏览器运行构造 nodriver 配置。

    参数：
    - browser_executable_path:
        浏览器可执行文件路径。
    - runtime_dirs:
        由 browser 模块创建的一次运行期目录。
    - proxy:
        可选代理地址。

    返回：
    - nodriver.core.config.Config 对象。

    当前职责：
    1. 使用独立 profile 运行期目录；
    2. 根据代理情况写入认证扩展或设置 --proxy-server；
    3. 生成随机 fingerprint；
    4. 返回完整浏览器配置。

    代理策略：
    - 带认证 HTTP/HTTPS 代理：
        只加载 MV3 扩展，由扩展负责 proxy.settings 和 onAuthRequired；
        不再同时传 --proxy-server，避免两套代理机制竞争。
    - 带认证 SOCKS 代理：
        直接 WPCRejectedRequest；
        原因是 Chrome 对 SOCKS 认证不稳定，容易 ERR_SOCKS_CONNECTION_FAILED；
        建议上游先转成本地 HTTP 代理。
    - 不带认证代理：
        直接使用 --proxy-server；
        但会通过 proxy_server_without_auth() 规整 scheme，例如 socks5h -> socks5。
    """
    browser_args: list[str] = []

    if proxy:
        # yt-dlp 外层已将 socks5h/socks4a 规整后再传入，这里按已规整的 scheme 处理。
        if proxy_has_auth(proxy):
            # Chrome 对“带认证 SOCKS”支持不稳定。
            #
            # 之前线上已经复现：
            #   ERR_SOCKS_CONNECTION_FAILED
            #
            # 这种错误继续启动浏览器只会浪费资源，因此直接 rejected，
            # 由 getpot_wpc.py 映射成 PoTokenProviderRejectedRequest，
            # POT 框架会尝试下一个 provider，不打印 warning。
            if proxy_requires_http_wrapper(proxy):
                raise WPCRejectedRequest(
                    "authenticated SOCKS proxy is not supported reliably by Chrome MV3 proxy auth extension; "
                    "please wrap it as a local HTTP proxy first, for example: "
                    "gost -L=http://127.0.0.1:18080 -F=socks5://user:pass@host:port"
                )

            # 带认证 HTTP/HTTPS 代理：
            # - 生成临时扩展到 ext 目录；
            # - 由扩展处理 proxy.settings 和 407 认证；
            # - 不再传 --proxy-server，避免命令行代理和扩展代理双来源竞争。
            proxy_cfg = parse_proxy_url(proxy)
            write_proxy_auth_extension(pathlib.Path(runtime_dirs.ext_dir), proxy_cfg)

            # 不再传 --proxy-server，避免和扩展 proxy.settings 竞争，也就是 避免双来源代理导致竞态/弹窗
            # 令行代理 vs 扩展代理：两套机制同时存在时，谁生效、何时生效、是否覆盖，会因版本/启动时序/策略不同而出现不一致，最常见表现就是：
            # * 某些请求走了命令行代理但扩展还没挂好监听 → 弹窗
            # * 或者代理配置在运行中被覆盖/切换导致偶发现象
            # browser_args.append(f"--proxy-server={proxy_server_without_auth(proxy)}")

            # 加载扩展，扩展负责 407 onAuthRequired 注入凭据
            browser_args.append(f"--load-extension={runtime_dirs.ext_dir}")
            # 只允许该扩展（减少其他插件干扰，通常更稳，但是对应 官方 chrome 浏览器，会在手动导入插件时，仍然看不到，非必要不加）
            # browser_args.append(f"--disable-extensions-except={self.runtime_dirs.ext_dir}")
        else:
            # 不带认证代理：
            # - 直接使用 Chrome --proxy-server；
            # - 但仍然要规整 scheme；
            # - 例如 socks5h://host:port 不能直接传给 Chrome，应转成 socks5://host:port。
            browser_args.append(f"--proxy-server={proxy_server_without_auth(proxy)}")

    # 专属 fingerprint-chromium 的 自定义 参数，用于配置 浏览器 指纹信息
    # 这款 指纹浏览器 可以支持 无头状态下，获取可用于下载的 po token ，其他方案提供的 po token 只能列出，不能下载
    # https://github.com/adryfish/fingerprint-chromium
    # 优化了自动化使用场景，提供了以下特性：
    # * 封闭 Shadow DOM 的伪造支持 新增fakeShadowRoot属性，与shadowRoot属性相同，但是实现对Closed Shadow Root访问，方便自动化工具处理。
    # * 避免 CDP 检测 调用 Runtime.enable 时不会触发 CDP（Chrome DevTools Protocol）检测，进一步增强自动化的隐蔽性。
    # * Webdriver navigator.webdriver设定为false，避免自动化工具设置为true。
    # * Headless 只是将User-Agent的HeadlessChrome的改成Chrome，其他Headless特征没有更改，谨慎使用。

    # 生成随机 fingerprint。
    # 这里统一生成一个带时间信息的字符串。
    # 当前设计就是每次运行随机，不与缓存做任何绑定或复用关系。
    fingerprint_key = generate_common_fingerprint_key()
    # 将传入的 fingerprint_key 转为 uint32 格式，因为当前 fingerprint-chromium 指纹浏览器的指纹 seed 只能接收 uint32
    fingerprint = convert_string_to_uint32(fingerprint_key)
    browser_args.append(f"--fingerprint={fingerprint}")

    # Linux / systemd / Docker-like 环境下建议加入这些参数，提升 headless 稳定性。
    #
    # --disable-dev-shm-usage:
    #   避免 /dev/shm 太小导致 Chromium 崩溃。
    #
    # --no-sandbox:
    #   某些 root/systemd/container 场景下 Chromium sandbox 可能启动失败。
    #
    # 注意：
    # - 如果你明确不希望禁用 sandbox，可以把 --no-sandbox 移除；
    # - 线上以 root 运行 Chromium 时通常需要它。
    browser_args.extend([
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ])

    config = nodriver.core.config.Config(
        # 使用独立 profile 目录，避免并发运行时 profile 锁冲突。
        user_data_dir=runtime_dirs.profile_dir,

        # 生产环境是 Linux 环境，需要无头运行。
        headless=True,

        browser_executable_path=browser_executable_path,
        browser_args=browser_args,
    )

    return config


def build_wpc_browser_resource_gate() -> ResourceGate:
    """
    构造 WPC 浏览器启动使用的 ResourceGate。

    当前 resource_gate 的文件统一放在：
    ~/.cache/yt-dlp-getpot-wpc/resource_gate

    配置来源：
    - WPC_RUNTIME_CONFIG.resource_gate

    为什么保留 ResourceGate：
    - WPC 是浏览器方案，启动成本较高；
    - 默认仍然建议通过资源门禁避免同时启动过多浏览器；
    - 如果你确认机器资源足够，可以通过 WPC_DISABLE_RESOURCE_GATE=1 跳过。

    注意：
    - ResourceGate 是可等待的资源门禁；
    - WPCLaunchGuard 是 fail-fast 的硬保护；
    - 即使禁用 ResourceGate，也建议保留 WPCLaunchGuard。
    """
    gate_cfg = WPC_RUNTIME_CONFIG.resource_gate

    return ResourceGate(
        gate_name="wpc_browser_launch",
        base_dir=get_wpc_resource_gate_dir(),
        reserved_mb=gate_cfg.reserved_mb,
        min_free_after_launch_mb=gate_cfg.min_free_after_launch_mb,
        max_memory_percent=gate_cfg.max_memory_percent,
        reservation_ttl_seconds=gate_cfg.reservation_ttl_seconds,
        sample_count=gate_cfg.sample_count,
        sample_interval_seconds=gate_cfg.sample_interval_seconds,
        retry_interval_seconds=gate_cfg.retry_interval_seconds,
    )


def _log_runtime_config(logger) -> None:
    """
    打印当前 WPC 运行时配置。

    说明：
    - 默认每次 mint 都打印一次；
    - 如果觉得日志过多，可设置 WPC_LOG_CONFIG_ON_EACH_MINT=0；
    - 该日志用于确认线上环境变量是否真的生效。
    """
    if not WPC_RUNTIME_CONFIG.log_config_on_each_mint:
        return

    gate_cfg = WPC_RUNTIME_CONFIG.resource_gate
    timeout_cfg = WPC_RUNTIME_CONFIG.browser_timeout
    launch_guard_cfg = WPC_RUNTIME_CONFIG.launch_guard

    logger.info(
        "[wpc-runtime-config] "
        f"disable_cache_lock={WPC_RUNTIME_CONFIG.disable_cache_lock}, "
        f"disable_resource_gate={gate_cfg.disabled}, "
        f"reserved_mb={gate_cfg.reserved_mb}, "
        f"min_free_after_launch_mb={gate_cfg.min_free_after_launch_mb}, "
        f"max_memory_percent={gate_cfg.max_memory_percent}, "
        f"reservation_ttl_seconds={gate_cfg.reservation_ttl_seconds}, "
        f"sample_count={gate_cfg.sample_count}, "
        f"sample_interval_seconds={gate_cfg.sample_interval_seconds}, "
        f"retry_interval_seconds={gate_cfg.retry_interval_seconds}, "
        f"browser_launch_seconds={timeout_cfg.browser_launch_seconds}, "
        f"page_load_seconds={timeout_cfg.page_load_seconds}, "
        f"webpo_client_wait_seconds={timeout_cfg.webpo_client_wait_seconds}, "
        f"webpo_mint_seconds={timeout_cfg.webpo_mint_seconds}, "
        f"launch_guard_enabled={launch_guard_cfg.enabled}, "
        f"launch_guard_memory_check_enabled={launch_guard_cfg.memory_check_enabled}, "
        f"launch_guard_max_concurrent={launch_guard_cfg.max_concurrent}, "
        f"launch_guard_reserved_mb={launch_guard_cfg.reserved_mb}, "
        f"launch_guard_min_available_mb={launch_guard_cfg.min_available_mb}, "
        f"launch_guard_min_free_after_launch_mb={launch_guard_cfg.min_free_after_launch_mb}, "
        f"launch_guard_max_memory_percent={launch_guard_cfg.max_memory_percent}, "
        f"launch_guard_slot_stale_seconds={launch_guard_cfg.slot_stale_seconds}"
    )


# =============================================================================
# 一次性浏览器 mint
# =============================================================================

def mint_po_token_once(
    *,
    logger,
    event_loop,
    browser_executable_path: str,
    proxy: str | None,
    content_binding: str,
):
    """
    使用一次性临时浏览器生成一个 PO Token。

    参数：
    - logger: 日志对象；
    - event_loop: provider 持有的 nodriver loop；
    - browser_executable_path: 浏览器可执行文件路径；
    - proxy: 可选代理地址；
    - content_binding: 本次 mint 的 content binding。

    返回：
    - 成功生成的 po token 字符串。

    异常：
    - WPCRejectedRequest:
        当前请求应被拒绝，例如代理已标记为 bad，或者是认证 SOCKS 代理；
        不应再启动浏览器。
    - WPCError:
        浏览器启动、页面加载、WebPoClient、mint 等链路失败。

    代理失败短路：
    - 如果当前 proxy_url 已经被标记为坏代理，则直接抛 WPCRejectedRequest；
    - 不再启动 Chromium；
    - 避免 yt-dlp POT 框架用同一个代理重复触发 WPC。
    - 由 getpot_wpc.py 映射成 PoTokenProviderRejectedRequest；
    - POT 框架会 trace 后尝试下一个 provider，不会打印 warning。

    本函数现在包含多层保护：

    1. WPCLaunchGuard 启动前硬保护
       - fail-fast；
       - 跨进程并发槽位；
       - 内存检查；
       - 不等待资源；

    2. ResourceGate 可选资源门禁
       - 可等待资源；
       - 默认禁用；
       - WPC_DISABLE_RESOURCE_GATE=1 时跳过。

    3. 高成本失败次数限制
       - 在 getpot_wpc.py 中完成；
       - 针对 WebPoClientNotFound / MintTimeout / PageLoadTimeout；
       - 防止同一个 key 反复启动浏览器。

    注意：
    - LaunchGuard 和 ResourceGate 是两回事；
    - 即使禁用 ResourceGate，也建议保留 LaunchGuard；
    - LaunchGuard 是 WPC 兜底场景的最后硬保护。

    关键流程：
    1. LaunchGuard 和 ResourceGate 资源检查；
    2. 创建本次运行独立 profile/ext 目录；
    3. 构造 nodriver config；
    4. 启动浏览器；
    5. 打开 YouTube 页面；
    6. 等待 WebPoClient；
    7. 生成 po token；
    8. 关闭浏览器；
    9. 清理 runtime 目录。

    ResourceGate 说明：
    - 默认启用；
    - WPC_DISABLE_RESOURCE_GATE=1 时跳过；
    - 跳过后不会等待内存门禁，也不会创建 reservation；
    - 适合高并发兜底、且确认机器资源足够的场景。
    """
    browser = None
    runtime_dirs = None
    gate = build_wpc_browser_resource_gate()

    _log_runtime_config(logger)

    # ============================================================
    # 代理失败短路保护。
    #
    # 目的：
    # - 如果同一个 proxy_url 已经在本进程中失败过；
    # - 后续 POT 框架再次请求 WPC provider；
    # - 直接失败，不再启动浏览器。
    #
    # 这样可以避免坏代理导致 Chromium 重复启动、反复超时。
    # ============================================================
    skip_proxy, bad_proxy_record = should_skip_proxy(proxy)
    if skip_proxy and bad_proxy_record is not None:
        raise WPCRejectedRequest(
            "skip WPC browser launch because current proxy was already marked bad | "
            f"proxy={bad_proxy_record.masked_proxy} | "
            f"reason={bad_proxy_record.reason}"
        )

    try:
        # ============================================================
        # WPC 启动前硬保护。
        #
        # 放在最外层的原因：
        # - 在创建 runtime dirs 之前检查；
        # - 在启动浏览器之前检查；
        # - 在 ResourceGate disabled 时仍然生效；
        # - 拿到 slot 后，到 browser.stop()/cleanup 完成后才释放，
        #   这样限制的是“同时存在的 WPC 浏览器流程数量”，
        #   而不只是“同时启动瞬间”。
        # ============================================================
        with WPCLaunchGuard(logger):
            if WPC_RUNTIME_CONFIG.resource_gate.disabled:
                logger.debug(
                    "[wpc-resource-gate] disabled by WPC_DISABLE_RESOURCE_GATE=1; "
                    "launch browser directly"
                )

                runtime_dirs = make_browser_runtime_dirs()

                browser_config = build_nodriver_config(
                    browser_executable_path=browser_executable_path,
                    runtime_dirs=runtime_dirs,
                    proxy=proxy,
                )

                logger.info(
                    "Launching temporary browser to retrieve PO Token. "
                    "The browser will be closed immediately after token generation."
                )

                browser = event_loop.run_until_complete(
                    launch_browser(
                        browser_config,
                        logger=logger,
                    )
                )

            else:
                # 门外等待：
                # 若系统资源明显不足，则先等待，不要直接争抢启动许可。
                gate.wait_until_allowed(logger=logger)

                # 在“启动许可”上下文中执行真正的启动动作。
                #
                # 该上下文内部会：
                # 1. 获取门闩锁；
                # 2. 创建 reservation；
                # 3. 门内二次判断资源；
                # 4. 允许后再执行启动。
                with gate.acquire_launch_permission(logger=logger):
                    runtime_dirs = make_browser_runtime_dirs()

                    browser_config = build_nodriver_config(
                        browser_executable_path=browser_executable_path,
                        runtime_dirs=runtime_dirs,
                        proxy=proxy,
                    )

                    logger.info(
                        "Launching temporary browser to retrieve PO Token. "
                        "The browser will be closed immediately after token generation."
                    )

                    # 真正启动浏览器的动作放在门闩锁内。
                    browser = event_loop.run_until_complete(
                        launch_browser(
                            browser_config,
                            logger=logger,
                        )
                    )

            # 浏览器启动完成后，若走 ResourceGate，启动许可已经释放；
            # 但 LaunchGuard slot 仍然持有，直到 browser.stop()/cleanup 后释放。
            # 这样可以限制同时存在的 Chromium 数量。
            if browser is None:
                raise WPCError("browser is None after launch_browser")

            main_tab = getattr(browser, "main_tab", None)

            if main_tab is None:
                raise WPCError("browser.main_tab is None after launch_browser")

            po_token = event_loop.run_until_complete(
                mint_po_token(
                    tab=main_tab,
                    logger=logger,
                    content_binding=content_binding,
                )
            )

            return po_token

    except WPCRejectedRequest:
        # 已经是明确拒绝：
        # - 不记录坏代理；
        # - 不包一层 WPCError；
        # - 交给 getpot_wpc.py 映射为 PoTokenProviderRejectedRequest。
        raise

    except WPCError as e:
        # ============================================================
        # 代理失败记录。
        #
        # 只要本次 WPC 使用了 proxy，并且浏览器链路失败，
        # 就认为当前代理对 WPC 不可用。
        #
        # 这样下一次同 proxy_url 进入 mint_po_token_once() 时，
        # 会在启动浏览器前直接短路。
        # ============================================================
        if proxy:
            mark_bad_proxy(
                proxy,
                reason=repr(e),
            )

            logger.debug(
                "[wpc-proxy-auth] mark current proxy as bad; "
                "future WPC calls with the same proxy will skip browser launch"
            )

        raise

    except Exception as e:
        # ============================================================
        # 未预期异常统一转成 WPCError。
        #
        # 注意：
        # - 不直接抛 yt-dlp POT 框架异常；
        # - getpot_wpc.py 负责最终映射。
        # ============================================================
        wrapped = WPCError(
            f"Failed to mint PO Token via temporary browser: unexpected error: {e!r}"
        )

        if proxy:
            mark_bad_proxy(
                proxy,
                reason=repr(wrapped),
            )

            logger.debug(
                "[wpc-proxy-auth] mark current proxy as bad after unexpected error; "
                "future WPC calls with the same proxy will skip browser launch"
            )

        raise wrapped from e

    finally:
        if browser:
            try:
                browser.stop()
            except Exception as e:
                _logger_error(
                    logger,
                    f"Failed to stop browser cleanly: {e!r}",
                )

        # 无论成功还是失败，都尽量清理本次运行产生的统一临时目录。
        if runtime_dirs:
            try:
                cleanup_browser_runtime_dirs(runtime_dirs)
            except Exception as e:
                _logger_error(
                    logger,
                    f"Failed to cleanup runtime dirs. "
                    f"root_dir={runtime_dirs.root_dir}, e={e!r}",
                )