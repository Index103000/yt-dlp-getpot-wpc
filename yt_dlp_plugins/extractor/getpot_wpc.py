# yt_dlp_plugins/extractor/getpot_wpc.py
from __future__ import annotations

import functools
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass

from nodriver import loop

from yt_dlp.extractor.youtube.pot.provider import (
    PoTokenRequest,
    PoTokenContext,
    PoTokenProvider,
    PoTokenResponse,
    PoTokenProviderRejectedRequest,
    PoTokenProviderError,
    register_provider,
    register_preference,
    ExternalRequestFeature,
    provider_bug_report_message,
)
from yt_dlp.extractor.youtube.pot.utils import (
    get_webpo_content_binding,
    WEBPO_CLIENTS,
)

from yt_dlp_plugins.extractor.wpc_browser import (
    is_browser_available,
    mint_po_token_once,
)
from yt_dlp_plugins.extractor.wpc_cache import (
    DEFAULT_TOKEN_TTL_HOURS,
    get_cache_entry_lock,
    get_youtube_session_data_locked,
    put_youtube_session_data,
    set_youtube_session_data_locked,
)
from yt_dlp_plugins.extractor.wpc_exceptions import WPCRejectedRequest, WPCError
from yt_dlp_plugins.extractor.wpc_paths import ensure_wpc_cache_dir
from yt_dlp_plugins.extractor.wpc_runtime_config import WPC_RUNTIME_CONFIG


__version__ = '1.0.0'


# =============================================================================
# 高成本失败分类
# =============================================================================

EXPENSIVE_FAILURE_NONE = 'none'
EXPENSIVE_FAILURE_PAGE_LOAD_TIMEOUT = 'page_load_timeout'
EXPENSIVE_FAILURE_WEBPO_CLIENT_NOT_FOUND = 'webpo_client_not_found'
EXPENSIVE_FAILURE_MINT_TIMEOUT = 'mint_timeout'


@dataclass
class ExpensiveFailureState:
    """
    WPC 高成本失败状态。

    字段说明：
    - first_failed_at:
        当前统计窗口内第一次失败的时间，使用 time.monotonic()。
    - fail_count:
        当前统计窗口内已经发生的高成本失败次数。
    - cooldown_until:
        冷却截止时间。当前时间小于该值时，不再真实启动浏览器。
    - last_failure_type:
        最近一次高成本失败类型。
    - last_reason:
        最近一次高成本失败原因，已经脱敏和截断，便于日志打印。
    """

    first_failed_at: float
    fail_count: int = 0
    cooldown_until: float = 0.0
    last_failure_type: str = ''
    last_reason: str = ''


# 进程内高成本失败缓存。
#
# key 维度：
# - context
# - client
# - content_binding
# - proxy
#
# 为什么是进程内：
# - 该能力主要用于降低“同一次 yt-dlp 提取流程内重复启动浏览器”的成本；
# - 不需要跨进程持久化；
# - 跨进程持久化失败状态反而容易误伤其他任务。
_EXPENSIVE_FAILURE_CACHE: dict[str, ExpensiveFailureState] = {}


# =============================================================================
# yt-dlp POT 框架异常适配层
# =============================================================================

def to_framework_exception(exc: Exception) -> Exception:
    """
    将 WPC 自定义异常映射到 yt-dlp POT 框架异常。

    设计目标：
    - WPC 内部文件只抛 WPCError / WPCRejectedRequest；
    - 只有 getpot_wpc.py 负责感知 yt-dlp POT 框架异常；
    - 后续如果 yt-dlp POT 框架异常类型变化，只需要改这里。

    映射规则：
    - WPCRejectedRequest:
        转成 PoTokenProviderRejectedRequest。
        POT 框架会认为当前 provider 主动拒绝该请求，然后尝试下一个 provider。
        这类异常不会打印 warning，适合：
          - 坏代理短路；
          - 高成本失败冷却期；
          - 明确不应该再启动浏览器的场景。

    - WPCError:
        转成 PoTokenProviderError。
        POT 框架会 warning，然后尝试下一个 provider。
        适合真正的 WPC 执行失败。

    - 其他异常:
        原样返回，由调用方决定是否包装。
    """
    if isinstance(exc, WPCRejectedRequest):
        return PoTokenProviderRejectedRequest(str(exc))

    if isinstance(exc, WPCError):
        return PoTokenProviderError(str(exc))

    return exc


def _raise_framework_exception(exc: Exception) -> None:
    """
    将异常转换为 yt-dlp POT 框架异常并抛出。

    注意：
    - 这是一个小工具，避免每个调用点重复：
        mapped = to_framework_exception(exc)
        raise mapped from exc
    """
    mapped = to_framework_exception(exc)

    if mapped is exc:
        raise exc

    raise mapped from exc


# =============================================================================
# 日志脱敏 / 原因压缩
# =============================================================================

def _sanitize_log_text(text: str) -> str:
    """
    对日志文本做脱敏。

    当前主要脱敏代理 URL：
    - http://user:pass@host:port
    - https://user:pass@host:port
    - socks5://user:pass@host:port
    - socks5h://user:pass@host:port
    - socks4://user:pass@host:port
    - socks4a://user:pass@host:port

    为什么需要：
    - 失败原因里可能带代理地址；
    - systemd journal / yt-dlp debug 日志不应该暴露代理账号密码。
    """
    if not text:
        return ''

    return re.sub(
        r'((?:https?|socks5h?|socks4a?)://)([^/\s:@]+):([^@\s/]+)@',
        r'\1***:***@',
        text,
        flags=re.IGNORECASE,
    )


def _short_reason(reason: str, max_chars: int = 2000) -> str:
    """
    截断并脱敏失败原因。

    说明：
    - 异常 repr 可能很长；
    - 这里保留尾部，因为真正有价值的异常信息通常在后面；
    - 同时对代理账号密码脱敏。
    """
    if not reason:
        return ''

    sanitized = _sanitize_log_text(reason)

    if len(sanitized) <= max_chars:
        return sanitized

    return sanitized[-max_chars:]


# =============================================================================
# 高成本失败缓存 key / 分类 / 冷却
# =============================================================================

def _make_expensive_failure_cache_key(
    *,
    request: PoTokenRequest,
    proxy: str | None,
    content_binding: str,
) -> str:
    """
    构造 WPC 高成本失败缓存 key。

    为什么包含 proxy：
    - 同一个 content_binding 在不同代理下可能一个失败、一个成功；
    - 不应该让坏代理影响其他代理。

    为什么包含 context/client：
    - 不同 context/client 的 POT 需求可能不同；
    - 避免误伤其他请求。

    注意：
    - key 内部可以包含完整 proxy；
    - 该 key 不直接打印到日志；
    - 日志只打印脱敏后的 reason。
    """
    return '|'.join([
        request.context.value,
        request.internal_client_name or '',
        content_binding,
        proxy or '',
    ])


def _classify_expensive_failure(reason: str) -> str:
    """
    识别是否属于 WPC 高成本失败。

    只对以下几类做次数限制：
    1. page_load_timeout:
       浏览器已经启动，打开 YouTube 页面超时，或者页面 ready/ytcfg 等待超时。
    2. webpo_client_not_found:
       浏览器已经启动，页面也打开了，但没有找到 WebPoClient。
    3. mint_timeout:
       浏览器已经启动，WebPoClient 也找到了，但执行 mws / 等 ready 超时。

    注意：
    - browser.connection 为 None 不再算失败；
    - 独立测试证明 browser.connection=None 时，browser.get/main_tab.evaluate/WebPoClient 仍可成功；
    - 所以不要再因为 connection=None 做冷却或失败分类。
    """
    if not reason:
        return EXPENSIVE_FAILURE_NONE

    lower = reason.lower()

    # 浏览器主 tab 未 ready。
    #
    # 这说明 start() 后没有拿到可用页面对象，属于高成本失败。
    if (
        "browser started but main_tab is not ready" in lower
        or "browser.main_tab is none" in lower
    ):
        return EXPENSIVE_FAILURE_PAGE_LOAD_TIMEOUT

    # 页面加载 / 页面基础 ready 超时。
    #
    # 包括：
    # - browser.get YouTube 超时；
    # - document.readyState 一直不进入 interactive/complete；
    # - ytcfg 一直不可用。
    if (
        "failed to load youtube page in browser" in lower
        or "timed out waiting for document.readystate" in lower
        or "timed out waiting for ytcfg" in lower
        or "failed while waiting document ready" in lower
        or "failed while waiting ytcfg" in lower
        or "youtube opened as chromium error page" in lower
        or "youtube became chromium error page" in lower
        or "failed to load youtube with ytcfg after retries" in lower
        or "page_load_timeout" in lower
        or "page load timeout" in lower
    ):
        return EXPENSIVE_FAILURE_PAGE_LOAD_TIMEOUT

    # WebPoClient 不存在。
    #
    # 对应：
    # - Could not find WebPoClient in browser
    # - Timed out waiting for WebPoClient to be available in browser
    if (
        "could not find webpoclient" in lower
        or "timed out waiting for webpoclient to be available" in lower
        or "webpo_client_not_found" in lower
    ):
        return EXPENSIVE_FAILURE_WEBPO_CLIENT_NOT_FOUND

    # mint 超时。
    #
    # 对应：
    # - Timed out executing WebPoClient.mws in browser
    # - Timed out waiting for WebPoClient to be ready in browser
    if (
        "timed out executing webpoclient.mws" in lower
        or "timed out waiting for webpoclient to be ready" in lower
        or "mint_timeout" in lower
    ):
        return EXPENSIVE_FAILURE_MINT_TIMEOUT

    return EXPENSIVE_FAILURE_NONE


def _check_expensive_failure_gate(key: str) -> str | None:
    """
    检查当前 key 是否处于高成本失败冷却期。

    返回：
    - None:
        允许真实启动浏览器。
    - str:
        当前处于冷却期，不允许真实启动浏览器，返回可打印原因。

    行为：
    - 如果未启用次数限制，直接允许；
    - 如果没有失败状态，直接允许；
    - 如果 cooldown_until > now，直接 fail fast；
    - 如果冷却已经过期，则允许重新尝试。

    重要变化：
    - 调用方命中冷却时应抛 WPCRejectedRequest；
    - 这样最终会转成 PoTokenProviderRejectedRequest；
    - yt-dlp POT 框架不会打印 warning。
    """
    cfg = WPC_RUNTIME_CONFIG

    if cfg.expensive_failure_max_attempts <= 0:
        return None

    state = _EXPENSIVE_FAILURE_CACHE.get(key)

    if not state:
        return None

    now = time.monotonic()

    if state.cooldown_until > now:
        return (
            'WPC expensive failure cooldown active; '
            f'failure_type={state.last_failure_type}; '
            f'fail_count={state.fail_count}; '
            f'cooldown_remaining={state.cooldown_until - now:.1f}s; '
            f'last_reason={state.last_reason}'
        )

    return None


def _record_expensive_failure(
    *,
    key: str,
    failure_type: str,
    reason: str,
) -> None:
    """
    记录一次 WPC 高成本失败。

    逻辑：
    1. 只处理明确识别的高成本失败；
    2. 如果超过统计窗口，重置计数；
    3. 如果仍在窗口内，fail_count + 1；
    4. fail_count 达到上限后，进入冷却期。

    示例默认值：
    - window_seconds=120
    - max_attempts=2
    - cooldown_seconds=300

    含义：
    - 120 秒内允许同一个 key 真实启动浏览器失败 2 次；
    - 第 2 次失败后进入 300 秒冷却；
    - 冷却期间不再启动浏览器。
    """
    if failure_type == EXPENSIVE_FAILURE_NONE:
        return

    cfg = WPC_RUNTIME_CONFIG

    if cfg.expensive_failure_max_attempts <= 0:
        return

    if cfg.expensive_failure_cooldown_seconds <= 0:
        return

    now = time.monotonic()
    window_seconds = cfg.expensive_failure_window_seconds
    max_attempts = cfg.expensive_failure_max_attempts
    cooldown_seconds = cfg.expensive_failure_cooldown_seconds

    state = _EXPENSIVE_FAILURE_CACHE.get(key)

    # 没有状态，或者已经超过统计窗口，则重新开始一个窗口。
    if not state or now - state.first_failed_at > window_seconds:
        state = ExpensiveFailureState(first_failed_at=now)
        _EXPENSIVE_FAILURE_CACHE[key] = state

    state.fail_count += 1
    state.last_failure_type = failure_type
    state.last_reason = _short_reason(reason)

    if state.fail_count >= max_attempts:
        state.cooldown_until = now + cooldown_seconds


def _clear_expensive_failure(key: str) -> None:
    """
    成功后清理当前 key 的高成本失败状态。

    为什么成功后要清理：
    - 说明该 key 对应的代理/视频/客户端已经恢复；
    - 后续不应继续受之前失败影响。
    """
    _EXPENSIVE_FAILURE_CACHE.pop(key, None)


# =============================================================================
# Provider 实现
# =============================================================================

@register_provider
class WPCPTP(PoTokenProvider):
    """
    基于浏览器 WebPoClient 的 PO Token Provider。

    当前设计特点：
    - 使用一次性临时浏览器生成 token；
    - 浏览器不常驻；
    - 缓存是“单 key 单文件”；
    - 默认每个 content_binding 单独加锁；
    - 支持 WPC_DISABLE_CACHE_LOCK=1 跳过 content_binding 锁；
    - 支持 WPC_DISABLE_RESOURCE_GATE=1 跳过浏览器启动 ResourceGate；
    - 支持高成本失败次数限制，避免一次视频解析过程中反复启动浏览器。

    为什么要允许跳过目录锁：
    - 在高并发下载时，WPC 通常作为 bgutil 的兜底 provider；
    - 如果同一个 content_binding 的第一个请求卡在浏览器启动或页面加载，
      后续相同 content_binding 会全部等待锁；
    - 这可能放大整体延迟，甚至和 yt-dlp 外层超时撞上；
    - 因此提供 WPC_DISABLE_CACHE_LOCK=1，让线上可以选择“尽量生成，不内部排队”。

    注意：
    - 禁用目录锁不等于禁用缓存；
    - 仍然会读缓存、写缓存；
    - 只是不会围绕“读缓存 + mint + 写缓存”做同 key 串行化。

    高成本失败次数限制：
    - 只针对 page load timeout / WebPoClient not found / mint timeout；
    - 允许有限次数真实重试；
    - 超过次数后短期 fail fast；
    - 避免 WPC 作为兜底 provider 时造成浏览器风暴。

    异常策略：
    - 内部拒绝类问题统一用 WPCRejectedRequest；
    - 内部执行类问题统一用 WPCError；
    - getpot_wpc.py 负责转成 yt-dlp POT 框架异常。
    """

    PROVIDER_VERSION = __version__

    # Define a unique display name for the provider
    PROVIDER_NAME = 'wpc'

    BUG_REPORT_LOCATION = 'https://github.com/coletdjnz/yt-dlp-getpot-wpc/issues'

    _SUPPORTED_CLIENTS = WEBPO_CLIENTS

    _SUPPORTED_CONTEXTS = (
        PoTokenContext.GVS,
        PoTokenContext.PLAYER,
        PoTokenContext.SUBS,
    )

    _SUPPORTED_EXTERNAL_REQUEST_FEATURES = (
        ExternalRequestFeature.PROXY_SCHEME_HTTP,
        ExternalRequestFeature.PROXY_SCHEME_SOCKS4,
        ExternalRequestFeature.PROXY_SCHEME_SOCKS4A,
        ExternalRequestFeature.PROXY_SCHEME_SOCKS5,
        ExternalRequestFeature.PROXY_SCHEME_SOCKS5H,
    )

    def __init__(self, *args, **kwargs):
        """
        初始化 provider。

        当前仅保留 nodriver loop 状态：
        - 不持有浏览器实例；
        - 不持有 runtime_dirs；
        - 每次 mint_po_token_once() 内部创建临时浏览器，结束后关闭。
        """
        super().__init__(*args, **kwargs)
        self.__loop = None

    @property
    def _loop(self):
        """
        返回当前 provider 使用的 nodriver event loop。

        说明：
        - 采用延迟初始化；
        - 在同一个 provider 实例生命周期内复用同一个 loop 对象；
        - 浏览器本身仍然是一次性临时实例，不在这里复用。
        """
        if not self.__loop:
            self.__loop = loop()

        return self.__loop

    def close(self):
        """
        关闭 provider。

        当前 provider 不持有常驻浏览器资源，因此这里只保留基类关闭逻辑。
        """
        super().close()

    @functools.cached_property
    def _browser_executable_path(self) -> str | None:
        """
        返回浏览器可执行文件路径。

        优先读取：
        - youtubepot-wpc:browser_path
        - youtube-wpc:browser_path（兼容旧写法）

        返回：
        - str：显式配置的浏览器路径；
        - None：未配置。

        说明：
        - 当前实现要求必须显式配置 browser_path；
        - 若为空，则 provider 会认为不可用。
        """
        return (
            self._configuration_arg(
                'browser_path',
                casesense=True,
                default=[None],
            )[0]
            or self.ie._configuration_arg(
                'browser_path',
                [None],
                ie_key='youtube-wpc',
                casesense=True,
            )[0]
        )

    @functools.cache
    def is_available(self):
        """
        检查当前环境是否可用于 WPC provider。

        检查内容：
        - browser_path 是否已配置；
        - 指定的浏览器路径是否存在；
        - 指定路径是否为可执行文件。

        返回：
        - True：可用；
        - False：不可用。

        说明：
        - 这里只做环境前置检查；
        - 不启动浏览器；
        - 不访问 YouTube。
        """
        try:
            return is_browser_available(
                logger=self.logger,
                browser_executable_path=self._browser_executable_path,
            )
        except Exception as e:
            self.logger.warning(
                f'Unexpected error while checking browser availability: '
                f'{e}{provider_bug_report_message(self)}'
            )
            return False

    @functools.cached_property
    def _cachedir(self) -> str:
        """
        返回 WPC cache 根目录。

        当前由外层统一使用 wpc_paths 提供顶层业务目录：
        ~/.cache/yt-dlp-getpot-wpc/cache

        说明：
        - cache 模块内部如何拆分 entries / locks，由 wpc_cache.py 自己维护；
        - provider 这里只负责把 cache 根目录传给 cache 模块。
        """
        return ensure_wpc_cache_dir()

    def _token_ttl_hours(self) -> int:
        """
        返回 token 缓存 TTL（小时）。

        当前固定返回默认值 6，与 bgutil 默认行为保持一致。

        后续若需要环境变量化，可以新增：
        - WPC_TOKEN_TTL_HOURS
        """
        return DEFAULT_TOKEN_TTL_HOURS

    def _normalize_proxy(self, proxy: str | None) -> str | None:
        """
        规范化 yt-dlp 传入的代理 URL。

        当前处理：
        - socks5h -> socks5

        注意：
        经测试，目前 浏览器 使用 socks5 时，访问 youtube 会有问题，可能是因为 dns 泄露
        """
        if not proxy:
            return None

        # WARNING: 由于 当前基于插件实现的 代理认证 方案，只支持 http 协议的，因而这里强行将 socks5 协议转为 http 使用
        # 这里存在风险项为，有的代理支持 socks5 ，但不会同时支持 http
        # todo: 针对这种代理情况，需要在 yt-downloader 中进行兼容，发现若配置使用 wpc，则需要 转换代理协议
        return proxy.replace('socks5h', 'http').replace('socks5', 'http')

    def _read_cache_if_allowed(
        self,
        *,
        request: PoTokenRequest,
        content_binding: str,
    ):
        """
        在允许的情况下读取单 key 缓存。

        返回：
        - 命中时返回 session_data；
        - 未命中或 bypass_cache=True 时返回 None。

        注意：
        - 这个函数不负责加锁；
        - 是否在锁内调用，由 _real_request_pot() 根据配置决定。
        """
        if request.bypass_cache:
            self.logger.debug('bypass_cache=True, skip reading cache')
            return None

        session_data = get_youtube_session_data_locked(
            logger=self.logger,
            cachedir=self._cachedir,
            content_binding=content_binding,
            cleanup=True,
        )

        if session_data:
            self.logger.debug(
                f'Using cached {request.context.value} PO Token for '
                f'{request.internal_client_name} via cache'
            )
            return session_data

        self.logger.debug(
            f'Cache miss for {request.context.value} PO Token. '
            f'content_binding={content_binding}'
        )
        return None

    def _mint_and_write_cache(
        self,
        *,
        request: PoTokenRequest,
        proxy: str | None,
        content_binding: str,
    ) -> str:
        """
        使用一次性浏览器 mint PO Token，并写入单 key 缓存。

        高成本失败次数限制：
        1. 启动浏览器前检查当前 key 是否在冷却期；
        2. 如果在冷却期，抛 WPCRejectedRequest，不启动浏览器；
        3. 如果允许尝试，则真实启动浏览器；
        4. 失败后根据错误内容分类；
        5. 如果是高成本失败，则累计次数；
        6. 达到阈值后，后续请求短期内直接 fail fast；
        7. 成功后清除失败状态。

        为什么冷却期使用 WPCRejectedRequest：
        - 这不是“程序 bug”；
        - 也不是需要用户上报 provider 开发者的问题；
        - 这是 WPC 主动拒绝继续高成本尝试；
        - 映射到 PoTokenProviderRejectedRequest 后，yt-dlp POT 框架不会打印 warning。
        """
        failure_key = _make_expensive_failure_cache_key(
            request=request,
            proxy=proxy,
            content_binding=content_binding,
        )

        blocked_reason = _check_expensive_failure_gate(failure_key)

        if blocked_reason:
            raise WPCRejectedRequest(blocked_reason)

        self.logger.debug(
            f'Minting {request.context.value} PO Token for '
            f'{request.internal_client_name} using WebPoClient in temporary browser'
        )

        try:
            po_token = mint_po_token_once(
                logger=self.logger,
                event_loop=self._loop,
                browser_executable_path=self._browser_executable_path,
                proxy=proxy,
                content_binding=content_binding,
            )

        except WPCRejectedRequest:
            # 下层已经明确表达“主动拒绝请求”，这里不记录高成本失败。
            # 典型场景：
            # - 当前代理已被标记为坏；
            # - 下层判断不应该启动浏览器。
            raise

        except WPCError:
            # WPCError 是内部执行失败。
            # 继续走高成本失败分类，必要时记录冷却。
            raise

        except Exception as e:
            reason = repr(e)
            failure_type = _classify_expensive_failure(reason)

            if failure_type != EXPENSIVE_FAILURE_NONE:
                _record_expensive_failure(
                    key=failure_key,
                    failure_type=failure_type,
                    reason=reason,
                )

                self.logger.debug(
                    'Recorded WPC expensive failure. '
                    f'failure_type={failure_type}; '
                    f'window_seconds={WPC_RUNTIME_CONFIG.expensive_failure_window_seconds}; '
                    f'max_attempts={WPC_RUNTIME_CONFIG.expensive_failure_max_attempts}; '
                    f'cooldown_seconds={WPC_RUNTIME_CONFIG.expensive_failure_cooldown_seconds}; '
                    f'reason={_short_reason(reason)}'
                )

            raise WPCError(
                f'Failed to mint PO Token via WPC browser: {_short_reason(reason)}'
            ) from e

        _clear_expensive_failure(failure_key)

        self.logger.trace(
            f'Retrieved {request.context.value} PO Token: {po_token}'
        )

        youtube_session_data = put_youtube_session_data(
            content_binding=content_binding,
            po_token=po_token,
            token_ttl_hours=self._token_ttl_hours(),
        )

        set_youtube_session_data_locked(
            logger=self.logger,
            cachedir=self._cachedir,
            youtube_session_data=youtube_session_data,
        )

        return po_token

    def _request_pot_inner(
        self,
        *,
        request: PoTokenRequest,
        proxy: str | None,
        content_binding: str,
    ) -> PoTokenResponse:
        """
        真正执行“读缓存 -> miss 后 mint -> 写缓存 -> 返回”的逻辑。

        说明：
        - 这个函数不关心是否持有 content_binding 锁；
        - 外层可以在锁内调用，也可以在禁用锁时直接调用；
        - 这样可以让锁策略和业务逻辑解耦。
        """
        session_data = self._read_cache_if_allowed(
            request=request,
            content_binding=content_binding,
        )

        if session_data:
            return PoTokenResponse(po_token=session_data.po_token)

        po_token = self._mint_and_write_cache(
            request=request,
            proxy=proxy,
            content_binding=content_binding,
        )

        return PoTokenResponse(po_token=po_token)

    def _real_request_pot(self, request: PoTokenRequest) -> PoTokenResponse:
        """
        生成或读取一个 PO Token。

        整体流程：
        1. 规范化代理 scheme；
        2. 处理 web_safari 的特殊兼容逻辑；
        3. 计算 content_binding；
        4. 根据 WPC_DISABLE_CACHE_LOCK 决定是否获取单 key 锁；
        5. 读缓存；
        6. miss 时启动一次性浏览器 mint token；
        7. 写回缓存；
        8. 返回结果。

        目录锁策略：
        - 默认：每个 content_binding 一把锁，锁内执行读缓存 + mint + 写缓存；
        - WPC_DISABLE_CACHE_LOCK=1：跳过目录锁，直接执行读缓存 + mint + 写缓存。

        高成本失败策略：
        - 即使禁用目录锁，高成本失败次数限制仍然生效；
        - 这样可以防止 yt-dlp 反复请求 POT 时重复启动浏览器。

        异常适配：
        - 内部 WPCRejectedRequest 在这里转为 PoTokenProviderRejectedRequest；
        - 内部 WPCError 在这里转为 PoTokenProviderError；
        - 其他异常尽量包装成 PoTokenProviderError，避免裸异常进入 POT 框架。
        """
        try:
            proxy = self._normalize_proxy(request.request_proxy)

            # web_safari 的 GVS POT 在当前场景下需要强制绑定 video_id，
            # 否则生成出来的 token 可能无效。
            if request.internal_client_name == 'web_safari':
                request._gvs_bind_to_video_id = True

            content_binding = get_webpo_content_binding(request)[0]

            if not self._browser_executable_path:
                raise WPCError(
                    'Browser executable path is not configured or not available'
                )

            if WPC_RUNTIME_CONFIG.disable_cache_lock:
                self.logger.debug(
                    'WPC content_binding cache lock disabled by '
                    'WPC_DISABLE_CACHE_LOCK=1; run without single-key lock'
                )
                lock_context = nullcontext()
            else:
                lock_context = get_cache_entry_lock(
                    content_binding,
                    self._cachedir,
                )

            with lock_context:
                return self._request_pot_inner(
                    request=request,
                    proxy=proxy,
                    content_binding=content_binding,
                )

        except (WPCRejectedRequest, WPCError) as e:
            # WPC 内部异常统一在 provider 边界转换为 yt-dlp 框架异常。
            _raise_framework_exception(e)

        except Exception as e:
            # 兜底保护：
            # 未预期异常仍然作为 provider error 交给 yt-dlp POT 框架。
            # 这种异常保留 bug report 语义，由框架 warning。
            raise PoTokenProviderError(
                f'Unexpected WPC provider error: {_short_reason(repr(e))}'
            ) from e


@register_preference(WPCPTP)
def wpc_preference(_, __):
    """
    返回 WPC provider 的偏好分数。

    当前维持原有偏好值：-100

    说明：
    - 该值越高通常越容易优先被选中
    """
    return -100
