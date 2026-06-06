# yt_dlp_plugins/extractor/wpc_runtime_config.py
from __future__ import annotations

import os
from dataclasses import dataclass


def _read_bool_env(name: str, default: bool = False) -> bool:
    """
    读取 bool 类型环境变量。

    支持的 true 值：
    - 1
    - true
    - yes
    - on

    支持的 false 值：
    - 0
    - false
    - no
    - off

    设计说明：
    - 未配置时返回 default；
    - 配置了无法识别的值时也返回 default；
    - 避免因为线上误配置导致 provider 直接崩溃。
    """
    raw = os.environ.get(name)

    if raw is None:
        return default

    value = raw.strip().lower()

    if not value:
        return default

    if value in {'1', 'true', 'yes', 'on'}:
        return True

    if value in {'0', 'false', 'no', 'off'}:
        return False

    return default


def _read_int_env(
    name: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """
    读取 int 类型环境变量。

    参数：
    - name: 环境变量名
    - default: 默认值
    - min_value: 可选最小值
    - max_value: 可选最大值

    设计说明：
    - 未配置时返回 default；
    - 配置了非法整数时返回 default；
    - 配置值小于 min_value 时钳制到 min_value；
    - 配置值大于 max_value 时钳制到 max_value。
    """
    raw = os.environ.get(name)

    if raw is None or not raw.strip():
        return default

    try:
        value = int(raw.strip())
    except ValueError:
        return default

    if min_value is not None and value < min_value:
        value = min_value

    if max_value is not None and value > max_value:
        value = max_value

    return value


def _read_float_env(
    name: str,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    """
    读取 float 类型环境变量。

    主要用于：
    - ResourceGate 内存百分比；
    - 采样间隔；
    - 浏览器阶段超时；
    - 高成本失败窗口和冷却时间。
    """
    raw = os.environ.get(name)

    if raw is None or not raw.strip():
        return default

    try:
        value = float(raw.strip())
    except ValueError:
        return default

    if min_value is not None and value < min_value:
        value = min_value

    if max_value is not None and value > max_value:
        value = max_value

    return value


@dataclass(frozen=True)
class WPCResourceGateConfig:
    """
    WPC 浏览器启动资源门禁配置。

    注意：
    - WPC 是浏览器方案，启动成本明显高于 bgutil 的 Node/Deno 脚本；
    - 默认仍然启用 ResourceGate；
    - 高并发兜底场景下，可以通过 WPC_DISABLE_RESOURCE_GATE=1 跳过。
    """

    disabled: bool

    reserved_mb: int
    min_free_after_launch_mb: int
    max_memory_percent: float
    reservation_ttl_seconds: int
    sample_count: int
    sample_interval_seconds: float
    retry_interval_seconds: float


@dataclass(frozen=True)
class WPCBrowserTimeoutConfig:
    """
    WPC 浏览器流程超时配置。

    字段说明：
    - browser_launch_seconds:
        启动浏览器的超时时间。

    - page_load_seconds:
        单次 browser.get(url) 的超时时间。

    - ytcfg_wait_seconds:
        每次打开 YouTube 后，等待 ytcfg 注入的超时时间。

        说明：
        - 线上 headless + 代理环境下，YouTube 首屏 JS 加载可能明显慢于本地；
        - 如果该值过小，容易出现页面其实还能继续加载，但 WPC 已经判定失败；
        - 建议线上设置为 60~90 秒。

    - youtube_load_attempts:
        YouTube 页面加载与 ytcfg 等待的最大尝试次数。

        说明：
        - 每次尝试都会重新 browser.get()；
        - 如果页面进入 chrome-error://chromewebdata/、SSL 错误页、ytcfg 超时，会进行下一次尝试；
        - 该值不宜太大，避免代理不可用时拖太久。

    - youtube_retry_wait_seconds:
        两次 YouTube 加载尝试之间的等待时间。

    - webpo_client_wait_seconds:
        ytcfg 已可用之后，等待 WebPoClient 出现的超时时间。

    - webpo_mint_seconds:
        调用 WebPoClient.mws() mint token 的超时时间。
    """
    browser_launch_seconds: float
    page_load_seconds: float
    ytcfg_wait_seconds: float
    youtube_load_attempts: int
    youtube_retry_wait_seconds: float
    webpo_client_wait_seconds: float
    webpo_mint_seconds: float


@dataclass(frozen=True)
class WPCRuntimeConfig:
    """
    WPC provider 运行时配置。

    主要能力：
    1. 可配置禁用 content_binding 单 key 锁；
    2. 可配置禁用 ResourceGate；
    3. 可配置浏览器关键阶段 timeout；
    4. 可配置 runtime 目录清理重试；
    5. 可配置高成本失败次数限制。

    高成本失败次数限制说明：
    - WPC 失败后，yt-dlp 可能在同一个视频解析过程中重复请求 POT；
    - 对 bgutil 来说，失败成本较低；
    - 对 WPC 来说，每次失败都可能启动一个浏览器，成本非常高；
    - 所以对 page_load_timeout / WebPoClientNotFound / mint_timeout 这类高成本失败做次数限制。
    """

    disable_cache_lock: bool
    resource_gate: WPCResourceGateConfig
    browser_timeout: WPCBrowserTimeoutConfig

    runtime_cleanup_initial_delay_seconds: float
    runtime_cleanup_max_retries: int
    runtime_cleanup_retry_interval_seconds: float

    """
    高成本失败统计窗口，单位秒。

    示例：
    WPC_EXPENSIVE_FAILURE_WINDOW_SECONDS=120

    含义：
    - 在 120 秒窗口内统计同一个 key 的高成本失败次数；
    - 超过窗口后重新计数。
    """
    expensive_failure_window_seconds: float

    """
    高成本失败最大真实尝试次数。

    示例：
    WPC_EXPENSIVE_FAILURE_MAX_ATTEMPTS=2

    含义：
    - 同一个 key 在窗口内最多允许真实启动浏览器失败 2 次；
    - 第 2 次失败后进入冷却；
    - 冷却期间后续请求直接 fail fast，不再启动浏览器。

    特殊值：
    - 0 表示禁用高成本失败次数限制。
    """
    expensive_failure_max_attempts: int

    """
    高成本失败冷却时间，单位秒。

    示例：
    WPC_EXPENSIVE_FAILURE_COOLDOWN_SECONDS=300

    含义：
    - 超过最大失败次数后，当前 key 冷却 300 秒；
    - 300 秒内直接 fail fast；
    - 冷却结束后允许重新尝试。
    """
    expensive_failure_cooldown_seconds: float

    launch_guard: WPCLaunchGuardConfig

    log_config_on_each_mint: bool


@dataclass(frozen=True)
class WPCLaunchGuardConfig:
    """
    WPC 浏览器启动前硬保护配置。

    它和 ResourceGate 的区别：
    - ResourceGate 可以等待资源；
    - LaunchGuard 是 fail-fast；
    - 资源不足或并发槽位满时，直接拒绝启动浏览器。

    适用场景：
    - bgutil 大面积异常；
    - WPC 作为兜底 provider 被高并发触发；
    - 为了避免瞬间启动大量 Chromium 把服务器打死。
    """

    enabled: bool

    """
    是否启用启动前内存检查。
    """
    memory_check_enabled: bool

    """
    跨进程最大 WPC 浏览器启动/运行并发。

    注意：
    - 这是跨进程限制；
    - 用文件槽位实现；
    - 不是 asyncio.Semaphore。
    """
    max_concurrent: int

    """
    预估单个 WPC 浏览器实例内存占用，单位 MB。
    """
    reserved_mb: int

    """
    启动 WPC 前，系统至少需要有多少 available memory，单位 MB。
    """
    min_available_mb: int

    """
    假设启动 WPC 后，系统至少需要剩余多少 available memory，单位 MB。
    """
    min_free_after_launch_mb: int

    """
    当前内存使用率超过该百分比时，拒绝启动。
    """
    max_memory_percent: float

    """
    槽位文件 stale 时间，单位秒。

    如果进程被 kill，没有正常释放 slot 文件，超过该时间后会自动清理。
    """
    slot_stale_seconds: float


def load_wpc_runtime_config() -> WPCRuntimeConfig:
    """
    从环境变量加载 WPC 运行时配置。

    当前支持的环境变量：

    目录锁：
    - WPC_DISABLE_CACHE_LOCK

    资源门禁：
    - WPC_DISABLE_RESOURCE_GATE
    - WPC_RESOURCE_GATE_RESERVED_MB
    - WPC_RESOURCE_GATE_MIN_FREE_AFTER_LAUNCH_MB
    - WPC_RESOURCE_GATE_MAX_MEMORY_PERCENT
    - WPC_RESOURCE_GATE_RESERVATION_TTL_SECONDS
    - WPC_RESOURCE_GATE_SAMPLE_COUNT
    - WPC_RESOURCE_GATE_SAMPLE_INTERVAL_SECONDS
    - WPC_RESOURCE_GATE_RETRY_INTERVAL_SECONDS

    浏览器超时：
    - WPC_BROWSER_LAUNCH_TIMEOUT_SECONDS
    - WPC_BROWSER_PAGE_LOAD_TIMEOUT_SECONDS
    - WPC_WEBPO_CLIENT_WAIT_SECONDS
    - WPC_WEBPO_MINT_TIMEOUT_SECONDS

    runtime 目录清理：
    - WPC_RUNTIME_CLEANUP_INITIAL_DELAY_SECONDS
    - WPC_RUNTIME_CLEANUP_MAX_RETRIES
    - WPC_RUNTIME_CLEANUP_RETRY_INTERVAL_SECONDS

    高成本失败次数限制：
    - WPC_EXPENSIVE_FAILURE_WINDOW_SECONDS
    - WPC_EXPENSIVE_FAILURE_MAX_ATTEMPTS
    - WPC_EXPENSIVE_FAILURE_COOLDOWN_SECONDS

    日志：
    - WPC_LOG_CONFIG_ON_EACH_MINT
    """
    return WPCRuntimeConfig(
        disable_cache_lock=_read_bool_env(
            'WPC_DISABLE_CACHE_LOCK',
            default=True,
        ),
        resource_gate=WPCResourceGateConfig(
            disabled=_read_bool_env(
                'WPC_DISABLE_RESOURCE_GATE',
                default=True,
            ),
            reserved_mb=_read_int_env(
                'WPC_RESOURCE_GATE_RESERVED_MB',
                500,
                min_value=0,
                max_value=1024 * 1024,
            ),
            min_free_after_launch_mb=_read_int_env(
                'WPC_RESOURCE_GATE_MIN_FREE_AFTER_LAUNCH_MB',
                2000,
                min_value=0,
                max_value=1024 * 1024,
            ),
            max_memory_percent=_read_float_env(
                'WPC_RESOURCE_GATE_MAX_MEMORY_PERCENT',
                80.0,
                min_value=1.0,
                max_value=100.0,
            ),
            reservation_ttl_seconds=_read_int_env(
                'WPC_RESOURCE_GATE_RESERVATION_TTL_SECONDS',
                120,
                min_value=1,
                max_value=24 * 60 * 60,
            ),
            sample_count=_read_int_env(
                'WPC_RESOURCE_GATE_SAMPLE_COUNT',
                5,
                min_value=1,
                max_value=100,
            ),
            sample_interval_seconds=_read_float_env(
                'WPC_RESOURCE_GATE_SAMPLE_INTERVAL_SECONDS',
                0.5,
                min_value=0.0,
                max_value=60.0,
            ),
            retry_interval_seconds=_read_float_env(
                'WPC_RESOURCE_GATE_RETRY_INTERVAL_SECONDS',
                2.0,
                min_value=0.1,
                max_value=10 * 60.0,
            ),
        ),
        browser_timeout=WPCBrowserTimeoutConfig(
            browser_launch_seconds=_read_float_env(
                'WPC_BROWSER_LAUNCH_TIMEOUT_SECONDS',
                60.0,
                min_value=1.0,
                max_value=10 * 60.0,
            ),
            page_load_seconds=_read_float_env(
                'WPC_BROWSER_PAGE_LOAD_TIMEOUT_SECONDS',
                60.0,
                min_value=1.0,
                max_value=10 * 60.0,
            ),
            ytcfg_wait_seconds=_read_float_env(
                'WPC_YTCFG_WAIT_SECONDS',
                60.0,
                min_value=1.0,
                max_value=10 * 60.0,
            ),
            youtube_load_attempts=_read_int_env(
                'WPC_YOUTUBE_LOAD_ATTEMPTS',
                2,
                min_value=1,
                max_value=10,
            ),
            youtube_retry_wait_seconds=_read_float_env(
                'WPC_YOUTUBE_RETRY_WAIT_SECONDS',
                2.0,
                min_value=0.0,
                max_value=60.0,
            ),
            webpo_client_wait_seconds=_read_float_env(
                'WPC_WEBPO_CLIENT_WAIT_SECONDS',
                60.0,
                min_value=1.0,
                max_value=10 * 60.0,
            ),
            webpo_mint_seconds=_read_float_env(
                'WPC_WEBPO_MINT_TIMEOUT_SECONDS',
                60.0,
                min_value=1.0,
                max_value=10 * 60.0,
            ),
        ),
        runtime_cleanup_initial_delay_seconds=_read_float_env(
            'WPC_RUNTIME_CLEANUP_INITIAL_DELAY_SECONDS',
            0.5,
            min_value=0.0,
            max_value=60.0,
        ),
        runtime_cleanup_max_retries=_read_int_env(
            'WPC_RUNTIME_CLEANUP_MAX_RETRIES',
            5,
            min_value=1,
            max_value=100,
        ),
        runtime_cleanup_retry_interval_seconds=_read_float_env(
            'WPC_RUNTIME_CLEANUP_RETRY_INTERVAL_SECONDS',
            0.5,
            min_value=0.0,
            max_value=60.0,
        ),
        expensive_failure_window_seconds=_read_float_env(
            'WPC_EXPENSIVE_FAILURE_WINDOW_SECONDS',
            120.0,
            min_value=1.0,
            max_value=60 * 60.0,
        ),
        expensive_failure_max_attempts=_read_int_env(
            'WPC_EXPENSIVE_FAILURE_MAX_ATTEMPTS',
            2,
            min_value=0,
            max_value=100,
        ),
        expensive_failure_cooldown_seconds=_read_float_env(
            'WPC_EXPENSIVE_FAILURE_COOLDOWN_SECONDS',
            300.0,
            min_value=0.0,
            max_value=60 * 60.0,
        ),
        launch_guard=WPCLaunchGuardConfig(
            # 保留 WPC 启动前硬保护
            enabled=_read_bool_env(
                'WPC_LAUNCH_GUARD_ENABLED',
                default=True,
            ),
            # 跨进程最多同时跑 1~2 个浏览器
            memory_check_enabled=_read_bool_env(
                'WPC_LAUNCH_GUARD_MEMORY_CHECK_ENABLED',
                default=True,
            ),
            # 启动前内存检查
            max_concurrent=_read_int_env(
                'WPC_LAUNCH_GUARD_MAX_CONCURRENT',
                100,
                min_value=0,
                max_value=100,
            ),
            # 预估单个浏览器占用
            reserved_mb=_read_int_env(
                'WPC_LAUNCH_GUARD_RESERVED_MB',
                700,
                min_value=0,
                max_value=1024 * 1024,
            ),
            # 启动 WPC 前至少要有 2GB available
            min_available_mb=_read_int_env(
                'WPC_LAUNCH_GUARD_MIN_AVAILABLE_MB',
                2048,
                min_value=0,
                max_value=1024 * 1024,
            ),
            # 启动后至少保留 1GB
            min_free_after_launch_mb=_read_int_env(
                'WPC_LAUNCH_GUARD_MIN_FREE_AFTER_LAUNCH_MB',
                1024,
                min_value=0,
                max_value=1024 * 1024,
            ),
            # 内存使用率超过 85% 拒绝启动
            max_memory_percent=_read_float_env(
                'WPC_LAUNCH_GUARD_MAX_MEMORY_PERCENT',
                85.0,
                min_value=1.0,
                max_value=100.0,
            ),
            # slot 残留 2 分钟视为 stale，会强制移除
            slot_stale_seconds=_read_float_env(
                'WPC_LAUNCH_GUARD_SLOT_STALE_SECONDS',
                2 * 60.0,
                min_value=30.0,
                max_value=24 * 60 * 60.0,
            ),
        ),
        log_config_on_each_mint=_read_bool_env(
            'WPC_LOG_CONFIG_ON_EACH_MINT',
            default=True,
        ),
    )


# 模块级配置。
#
# 说明：
# - yt-dlp provider 进程启动后，环境变量通常不会再变化；
# - 因此模块加载时读取一次即可；
# - 如果修改 systemd Environment，需要重启 yt-downloader 服务。
WPC_RUNTIME_CONFIG = load_wpc_runtime_config()