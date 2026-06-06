# scripts/test/test_wpc_browser_boot.py
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import pathlib
import shutil
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import nodriver
from nodriver import cdp, start

from yt_dlp_plugins.extractor.proxy_auth_ext_v3 import (
    parse_proxy_url,
    proxy_has_auth,
    write_proxy_auth_extension,
    maybe_activate_proxy_extension,
    debug_proxy_extension_activation,
)


@dataclass
class BrowserRuntimeDirs:
    """
    一次浏览器启动测试使用的临时目录集合。

    目录结构：
    <runtime_base_dir>/<run_id>/
    ├── profile/
    └── ext/

    字段说明：
    - run_id:
        本次测试唯一 ID。
    - root_dir:
        本次测试根目录。
    - profile_dir:
        Chromium user-data-dir。
    - ext_dir:
        MV3 代理认证扩展目录。

    说明：
    - 每次测试都使用独立 profile/ext，避免并发冲突和状态污染；
    - 默认测试结束后删除 root_dir；
    - 可以通过 --keep-profile 保留目录用于排查；
    - profile/ext 的结构与生产 wpc_browser.py 保持一致。
    """

    run_id: str
    root_dir: str
    profile_dir: str
    ext_dir: str


class SimpleLogger:
    """
    简单控制台 logger。

    目的：
    - 保持脚本独立；
    - 不依赖 yt-dlp logger；
    - 输出带时间戳，方便和 systemd / 线上日志对照。

    注意：
    - warning() 支持 once 参数，仅为了兼容项目 logger 的调用风格；
    - 本脚本不会真的做 once 去重。
    """

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def info(self, msg: str) -> None:
        print(f"{self._now()} - INFO - {msg}", flush=True)

    def warning(self, msg: str, *, once: bool = False) -> None:
        print(f"{self._now()} - WARNING - {msg}", flush=True)

    def error(self, msg: str) -> None:
        print(f"{self._now()} - ERROR - {msg}", flush=True)

    def debug(self, msg: str) -> None:
        print(f"{self._now()} - DEBUG - {msg}", flush=True)


def mask_proxy_url(proxy: str | None) -> str | None:
    """
    脱敏代理 URL，避免日志里泄露账号密码。

    示例：
    http://user:pass@host:port
    ->
    http://***:***@host:port
    """
    if not proxy:
        return proxy

    parsed = urlparse(proxy)

    if not parsed.username and not parsed.password:
        return proxy

    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""

    return f"{scheme}://***:***@{host}{port}"


def normalize_proxy_scheme_for_chrome(proxy: str) -> str:
    """
    将 yt-dlp / requests 常见代理 scheme 规整成 Chrome 更容易接受的形式。

    说明：
    - socks5h:// 表示由代理端解析 DNS，这是 requests/urllib3 常见写法；
    - Chrome 通常使用 socks5://；
    - socks4a:// 同理规整为 socks4://。
    """
    if proxy.startswith("socks5h://"):
        return "socks5://" + proxy[len("socks5h://"):]

    if proxy.startswith("socks4a://"):
        return "socks4://" + proxy[len("socks4a://"):]

    return proxy


def make_runtime_dirs(base_dir: str | None = None) -> BrowserRuntimeDirs:
    """
    创建测试用浏览器运行目录。

    参数：
    - base_dir:
        临时目录根目录。
        如果为空，则默认使用 /tmp/wpc-browser-test。

    返回：
    - BrowserRuntimeDirs

    说明：
    - run_id 使用 uuid4 hex；
    - 每次运行独立目录；
    - 这样可以避免 Chrome profile 锁冲突，也方便保留现场排查。
    """
    run_id = uuid.uuid4().hex

    if not base_dir:
        base_dir = "/tmp/wpc-browser-test"

    root_dir = pathlib.Path(base_dir) / run_id
    profile_dir = root_dir / "profile"
    ext_dir = root_dir / "ext"

    profile_dir.mkdir(parents=True, exist_ok=True)
    ext_dir.mkdir(parents=True, exist_ok=True)

    return BrowserRuntimeDirs(
        run_id=run_id,
        root_dir=str(root_dir),
        profile_dir=str(profile_dir),
        ext_dir=str(ext_dir),
    )


def setup_proxy_for_browser_args(
    *,
    proxy: str | None,
    proxy_auth_mode: str,
    runtime_dirs: BrowserRuntimeDirs,
    logger: SimpleLogger,
) -> tuple[list[str], bool]:
    """
    根据 proxy 和 proxy_auth_mode 构造 Chrome 启动参数。

    返回：
    - browser_args:
        需要追加到 Chromium 的启动参数。
    - needs_extension_activation:
        是否需要调用 maybe_activate_proxy_extension() 强制激活 MV3 扩展。

    proxy_auth_mode:
    - auto:
        不带认证代理：--proxy-server；
        带认证代理：项目真实 MV3 代理认证扩展。
    - direct:
        无论是否带认证，都直接 --proxy-server。
        用于复现 Chrome 对 user:pass@proxy 的行为。
    - extension:
        强制使用项目真实 MV3 代理认证扩展。

    与生产逻辑一致的关键点：
    - 带认证代理时，只传 --load-extension；
    - 不再同时传 --proxy-server；
    - 避免命令行代理和扩展 proxy.settings 互相竞争。
    """
    if not proxy:
        logger.info("[proxy] no proxy configured")
        return [], False

    proxy = normalize_proxy_scheme_for_chrome(proxy)
    has_auth = proxy_has_auth(proxy)

    logger.info(
        "[proxy] "
        f"mode={proxy_auth_mode}; "
        f"has_auth={has_auth}; "
        f"proxy={mask_proxy_url(proxy)}"
    )

    if proxy_auth_mode not in {"auto", "direct", "extension"}:
        raise ValueError(f"invalid proxy_auth_mode: {proxy_auth_mode}")

    if proxy_auth_mode == "direct":
        logger.warning(
            "[proxy] direct mode selected; proxy with username/password may fail in Chromium"
        )
        return [f"--proxy-server={proxy}"], False

    if proxy_auth_mode == "auto" and not has_auth:
        logger.info("[proxy] no-auth proxy uses --proxy-server")
        return [f"--proxy-server={proxy}"], False

    # 使用项目真实 MV3 扩展。
    proxy_cfg = parse_proxy_url(proxy)
    write_proxy_auth_extension(pathlib.Path(runtime_dirs.ext_dir), proxy_cfg)

    logger.info("[proxy] using project proxy_auth_ext_v3 extension implementation")
    logger.info(f"[proxy] extension_dir={runtime_dirs.ext_dir}")

    return [f"--load-extension={runtime_dirs.ext_dir}"], True


async def safe_evaluate(tab, js: str, *, await_promise: bool = False) -> tuple[bool, Any]:
    """
    安全执行 tab.evaluate。

    返回：
    - (True, value)
    - (False, error_repr)

    说明：
    - nodriver 不同版本的 evaluate 参数兼容性可能不同；
    - 某些版本不支持 await_promise；
    - 所以这里先尝试带 await_promise，再在 TypeError 时降级。
    """
    try:
        value = await tab.evaluate(js, await_promise=await_promise)
        return True, value
    except TypeError:
        try:
            value = await tab.evaluate(js)
            return True, value
        except Exception as e:
            return False, repr(e)
    except Exception as e:
        return False, repr(e)


async def collect_page_diagnostics(browser) -> dict[str, Any]:
    """
    收集当前页面诊断信息。

    用于判断：
    - 是否真的打开了 YouTube；
    - 是否进入 chrome-error://chromewebdata/；
    - 是否被代理拦截；
    - 是否出现 consent/captcha/access denied 页面；
    - ytcfg / WebPoClient 为什么不可用。
    """
    tab = getattr(browser, "main_tab", None)

    if tab is None:
        return {
            "error": "browser.main_tab is None",
        }

    async def _eval(js: str) -> Any:
        ok, value = await safe_evaluate(tab, js)
        return value if ok else f"<evaluate failed: {value}>"

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


def log_page_diagnostics(
    *,
    logger: SimpleLogger,
    prefix: str,
    diagnostics: dict[str, Any],
) -> None:
    """
    打印页面诊断信息。

    说明：
    - body_preview 只截取前 1000 字符；
    - 主要用于快速判断是否进入 chrome-error、YouTube 正常页、验证码页、代理错误页等。
    """
    logger.warning(
        f"{prefix} "
        f"href={diagnostics.get('href')!r}; "
        f"title={diagnostics.get('title')!r}; "
        f"readyState={diagnostics.get('readyState')!r}; "
        f"ytcfg_exists={diagnostics.get('ytcfg_exists')!r}; "
        f"webpo_exists={diagnostics.get('webpo_exists')!r}; "
        f"bg_st_hr_enabled={diagnostics.get('bg_st_hr_enabled')!r}; "
        f"body_preview={diagnostics.get('body_preview')!r}"
    )


async def wait_browser_state(
    browser,
    *,
    logger: SimpleLogger,
    timeout_seconds: float = 20.0,
    interval_seconds: float = 0.5,
) -> dict[str, Any]:
    """
    等待 browser 对象进入最低可用状态。

    注意：
    - 不要求 browser.connection 一定存在；
    - Linux/headless/nodriver 环境下 browser.connection 可能一直是 None；
    - 只要求 browser.main_tab 存在；
    - main_tab 存在时，browser.get() 和 tab.evaluate 通常仍可工作。
    """
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    last_state: dict[str, Any] = {}

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
            "elapsed": round(time.monotonic() - started_at, 3),
        }

        logger.info(f"[browser-state] {last_state}")

        if browser is not None and main_tab is not None:
            return last_state

        await asyncio.sleep(interval_seconds)

    return last_state


async def try_clear_cookies(browser, *, logger: SimpleLogger) -> bool:
    """
    尝试通过 browser.connection 清理 cookies。

    说明：
    - 这不是 WPC 生成 PO Token 的硬前置；
    - 当前每次测试使用独立 profile，正常情况下 cookie 本来就是空的；
    - browser.connection 为 None 时跳过；
    - 保留该测试仅用于观察不同平台差异。
    """
    connection = getattr(browser, "connection", None)

    if connection is None:
        logger.warning("[clear-cookies] browser.connection is None, skip clear cookies")
        return False

    try:
        logger.info("[clear-cookies] try browser.connection.send(cdp.storage.clear_cookies())")
        await connection.send(cdp.storage.clear_cookies())
        logger.info("[clear-cookies] success")
        return True
    except Exception as e:
        logger.error(f"[clear-cookies] failed: {e!r}")
        logger.error(traceback.format_exc())
        return False


async def activate_proxy_extension_if_needed(
    browser,
    *,
    needs_extension_activation: bool,
    logger: SimpleLogger,
    timeout_seconds: float,
    print_debug: bool,
) -> str:
    """
    按生产逻辑强制激活 MV3 代理认证扩展。

    返回：
    - 不需要扩展激活：返回 ""；
    - 激活成功：返回 extension_id；
    - 激活失败：抛 RuntimeError。

    为什么这里要强制失败：
    - MV3 background 是 service worker；
    - 只加载 --load-extension 不代表 proxy.settings / onAuthRequired 已经完成注册；
    - 如果不激活就打开 YouTube，可能出现浏览器未走代理；
    - 生产逻辑已经改为“扩展激活失败则终止”，测试也应一致。

    print_debug:
    - True 时额外调用 debug_proxy_extension_activation()；
    - 会打印 command_line、候选 extension_id、health 返回摘要；
    - 适合排查扩展页面已打开但 JS/SW 未响应的问题。
    """
    if not needs_extension_activation:
        logger.info("[proxy-extension] no activation needed")
        return ""

    logger.info(
        "[proxy-extension] activating project MV3 proxy extension before opening target URL"
    )

    if print_debug:
        diag = await debug_proxy_extension_activation(
            browser,
            timeout=timeout_seconds,
            proxy_url="",
        )
        logger.info(f"[proxy-extension-debug] {diag!r}")

        extension_id = str(diag.get("activated_extension_id") or "")
    else:
        extension_id = await maybe_activate_proxy_extension(
            browser,
            timeout=timeout_seconds,
            visible_probe=False,
            proxy_url="",
        )

    logger.info(f"[proxy-extension] activation result extension_id={extension_id!r}")

    if not extension_id:
        raise RuntimeError(
            "MV3 proxy extension activation failed; "
            "browser may not use configured proxy"
        )

    return extension_id


async def test_browser_get(
    browser,
    *,
    logger: SimpleLogger,
    url: str,
    timeout_seconds: float,
) -> bool:
    """
    测试 browser.get(url)。

    注意：
    - browser.get() 成功只代表导航调用没有抛异常；
    - 不代表页面一定正常加载；
    - 如果代理异常，Chrome 仍可能返回 chrome-error://chromewebdata/；
    - 所以后续必须通过 collect_page_diagnostics() 检查 href / body / ytcfg / WebPoClient。
    """
    try:
        logger.info(f"[browser-get] opening url={url!r}, timeout={timeout_seconds}s")

        await asyncio.wait_for(
            browser.get(url),
            timeout=timeout_seconds,
        )

        logger.info("[browser-get] success")
        return True

    except asyncio.TimeoutError:
        logger.error(f"[browser-get] timeout after {timeout_seconds}s")
        return False

    except Exception as e:
        logger.error(f"[browser-get] failed: {e!r}")
        logger.error(traceback.format_exc())
        return False


async def wait_document_ready(
    browser,
    *,
    logger: SimpleLogger,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.5,
) -> bool:
    """
    等待 document.readyState 进入 interactive 或 complete。

    说明：
    - browser.get() 返回时，页面可能仍然是 loading；
    - 过早检查 WebPoClient 容易误判失败；
    - 这里与生产 wait_document_ready 逻辑保持一致。
    """
    tab = getattr(browser, "main_tab", None)

    if tab is None:
        logger.error("[document-ready] browser.main_tab is None")
        return False

    started_at = time.monotonic()
    deadline = started_at + timeout_seconds

    while time.monotonic() < deadline:
        ok, value = await safe_evaluate(tab, "document.readyState")

        logger.info(
            f"[document-ready] ok={ok}, readyState={value!r}, "
            f"elapsed={time.monotonic() - started_at:.2f}s"
        )

        if ok and value in ("interactive", "complete"):
            return True

        await asyncio.sleep(interval_seconds)

    logger.warning(f"[document-ready] timeout after {timeout_seconds}s")

    diagnostics = await collect_page_diagnostics(browser)
    log_page_diagnostics(
        logger=logger,
        prefix="[document-ready-timeout]",
        diagnostics=diagnostics,
    )

    return False


async def wait_ytcfg_available(
    browser,
    *,
    logger: SimpleLogger,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.5,
) -> bool:
    """
    等待 YouTube 页面中的 ytcfg 可用。

    说明：
    - WPC 判断实验开关时会访问 ytcfg；
    - 如果 ytcfg 尚未注入，WebPoClient 通常也还没准备好；
    - 若页面是 chrome-error://chromewebdata/，这里会持续 False 并最终超时。
    """
    tab = getattr(browser, "main_tab", None)

    if tab is None:
        logger.error("[ytcfg] browser.main_tab is None")
        return False

    started_at = time.monotonic()
    deadline = started_at + timeout_seconds

    js = "!!window.top['ytcfg'] && typeof window.top['ytcfg'].get === 'function'"

    while time.monotonic() < deadline:
        ok, value = await safe_evaluate(tab, js)

        logger.info(
            f"[ytcfg] ok={ok}, exists={value!r}, "
            f"elapsed={time.monotonic() - started_at:.2f}s"
        )

        if ok and value is True:
            return True

        await asyncio.sleep(interval_seconds)

    logger.warning(f"[ytcfg] timeout after {timeout_seconds}s")

    diagnostics = await collect_page_diagnostics(browser)
    log_page_diagnostics(
        logger=logger,
        prefix="[ytcfg-timeout]",
        diagnostics=diagnostics,
    )

    return False


async def wait_webpo_client(
    browser,
    *,
    logger: SimpleLogger,
    timeout_seconds: float = 60.0,
    interval_seconds: float = 1.0,
) -> bool:
    """
    等待 WebPoClient 出现。

    当前 WPC 使用固定路径：
        window.top['havuokmhhs-0']?.bevasrs?.wpc

    与生产逻辑保持一致。
    """
    tab = getattr(browser, "main_tab", None)

    if tab is None:
        logger.error("[webpo] browser.main_tab is None")
        return False

    started_at = time.monotonic()
    deadline = started_at + timeout_seconds

    webpo_js = "!!window.top['havuokmhhs-0']?.bevasrs?.wpc"

    bg_st_hr_js = (
        "!window.top['ytcfg']?.get('EXPERIMENT_FLAGS') "
        "|| !!ytcfg.get('EXPERIMENT_FLAGS')?.bg_st_hr"
    )

    while time.monotonic() < deadline:
        ok_webpo, webpo_exists = await safe_evaluate(tab, webpo_js)
        ok_exp, bg_st_hr_enabled = await safe_evaluate(tab, bg_st_hr_js)
        ok_ready, ready_state = await safe_evaluate(tab, "document.readyState")
        ok_href, href = await safe_evaluate(tab, "location.href")

        logger.info(
            "[webpo] "
            f"webpo_ok={ok_webpo}, webpo_exists={webpo_exists!r}, "
            f"bg_st_hr_ok={ok_exp}, bg_st_hr_enabled={bg_st_hr_enabled!r}, "
            f"ready_ok={ok_ready}, readyState={ready_state!r}, "
            f"href_ok={ok_href}, href={href!r}, "
            f"elapsed={time.monotonic() - started_at:.2f}s"
        )

        if ok_webpo and webpo_exists is True:
            logger.info("[webpo] WebPoClient is available")
            return True

        await asyncio.sleep(interval_seconds)

    logger.warning(f"[webpo] timeout after {timeout_seconds}s")

    diagnostics = await collect_page_diagnostics(browser)
    log_page_diagnostics(
        logger=logger,
        prefix="[webpo-timeout]",
        diagnostics=diagnostics,
    )

    return False


async def test_webpo_mint(
    browser,
    *,
    logger: SimpleLogger,
    content_binding: str,
    timeout_seconds: float,
) -> bool:
    """
    尝试真正调用 WebPoClient.mws() 生成 PO Token。

    说明：
    - 这一步是可选的；
    - 通过 --mint-test 开启；
    - 若只想测试浏览器启动、代理激活、WebPoClient 注入，可以不加 --mint-test。

    关键修正：
    - mws 参数使用 json.dumps() 输出为合法 JS 对象；
    - 不使用 Python repr(dict)，避免 JS 侧出现 True/False/None 等非法字面量。
    """
    tab = getattr(browser, "main_tab", None)

    if tab is None:
        logger.error("[mint] browser.main_tab is None")
        return False

    webpo_client_path = "window.top['havuokmhhs-0']?.bevasrs?.wpc"

    mws_params = {
        "c": content_binding,
        "mc": False,
        "me": False,
    }

    mint_js = f"""
        {webpo_client_path}().then((client) => client.mws({json.dumps(mws_params, ensure_ascii=False)})).catch(
            (e) => {{
                if (String(e).includes('SDF:notready')) {{
                    return 'backoff';
                }} else {{
                    throw e;
                }}
            }}
        )
    """

    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    tries = 0

    while time.monotonic() < deadline:
        remaining = max(1.0, deadline - time.monotonic())

        try:
            logger.info(
                f"[mint] trying WebPoClient.mws, tries={tries}, "
                f"remaining={remaining:.2f}s, content_binding={content_binding!r}"
            )

            value = await asyncio.wait_for(
                tab.evaluate(mint_js, await_promise=True),
                timeout=remaining,
            )

        except TypeError:
            try:
                value = await asyncio.wait_for(
                    tab.evaluate(mint_js),
                    timeout=remaining,
                )
            except Exception as e:
                logger.error(f"[mint] evaluate failed: {e!r}")
                logger.error(traceback.format_exc())
                return False

        except asyncio.TimeoutError:
            logger.error(f"[mint] timeout after {timeout_seconds}s")
            return False

        except Exception as e:
            logger.error(f"[mint] failed: {e!r}")
            logger.error(traceback.format_exc())

            diagnostics = await collect_page_diagnostics(browser)
            log_page_diagnostics(
                logger=logger,
                prefix="[mint-failed]",
                diagnostics=diagnostics,
            )

            return False

        logger.info(f"[mint] result={value!r}")

        if value and value != "backoff":
            logger.info(f"[mint] success, token_length={len(str(value))}")
            return True

        tries += 1
        await asyncio.sleep(1.0)

    logger.error(f"[mint] timeout waiting token after {timeout_seconds}s")
    return False


async def test_tab_evaluate(browser, *, logger: SimpleLogger) -> None:
    """
    测试 main_tab.evaluate 是否可用，并输出基础页面信息。

    说明：
    - 这个函数不参与成功/失败判断；
    - 只用于测试过程中的辅助诊断。
    """
    tab = getattr(browser, "main_tab", None)

    if tab is None:
        logger.error("[evaluate] browser.main_tab is None, cannot evaluate")
        return

    checks = [
        ("document.readyState", "document.readyState"),
        ("location.href", "location.href"),
        ("document.title", "document.title"),
        ("navigator.userAgent", "navigator.userAgent"),
        ("ytcfg_exists", "!!window.top['ytcfg'] && typeof window.top['ytcfg'].get === 'function'"),
        ("webpo_client_exists", "!!window.top['havuokmhhs-0']?.bevasrs?.wpc"),
        (
            "bg_st_hr_enabled",
            "!window.top['ytcfg']?.get('EXPERIMENT_FLAGS') || !!ytcfg.get('EXPERIMENT_FLAGS')?.bg_st_hr",
        ),
        (
            "body_preview",
            "document.body ? document.body.innerText.slice(0, 300) : ''",
        ),
    ]

    for name, js in checks:
        ok, value = await safe_evaluate(tab, js)

        if ok:
            logger.info(f"[evaluate] {name} = {value!r}")
        else:
            logger.error(f"[evaluate] {name} failed: {value}")


def build_config(
    *,
    browser_path: str,
    runtime_dirs: BrowserRuntimeDirs,
    proxy: str | None,
    proxy_auth_mode: str,
    extra_args: list[str],
    logger: SimpleLogger,
) -> tuple[nodriver.core.config.Config, bool]:
    """
    构造 nodriver Config。

    返回：
    - config:
        nodriver.core.config.Config。
    - needs_extension_activation:
        是否需要在 browser.start 后强制激活 MV3 代理扩展。

    代理逻辑：
    - 无代理：不加代理参数；
    - 不带认证代理：--proxy-server；
    - 带认证代理：项目真实 proxy_auth_ext_v3 扩展；
    - 可通过 --proxy-auth-mode direct/extension/auto 控制。
    """
    browser_args = [
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]

    proxy_args, needs_extension_activation = setup_proxy_for_browser_args(
        proxy=proxy,
        proxy_auth_mode=proxy_auth_mode,
        runtime_dirs=runtime_dirs,
        logger=logger,
    )

    browser_args.extend(proxy_args)
    browser_args.extend(extra_args or [])

    logger.info("[browser-args] begin")
    for arg in browser_args:
        if "proxy" in arg.lower() and "@" in arg:
            logger.info(f"[browser-args] {mask_proxy_url(arg)}")
        else:
            logger.info(f"[browser-args] {arg}")
    logger.info("[browser-args] end")

    config = nodriver.core.config.Config(
        user_data_dir=runtime_dirs.profile_dir,
        headless=True,
        browser_executable_path=browser_path,
        browser_args=browser_args,
    )

    return config, needs_extension_activation


def is_chrome_error_page(diagnostics: dict[str, Any]) -> bool:
    """
    判断当前页面是否是 Chrome 错误页。

    说明：
    - browser.get() 成功后仍可能进入 chrome-error://chromewebdata/；
    - 这种情况必须视为失败；
    - 否则后续等待 ytcfg/WebPoClient 会浪费时间。
    """
    href = str(diagnostics.get("href") or "")
    return href.startswith("chrome-error://")


async def main_async(args) -> int:
    """
    测试主流程。

    完整流程：
    1. 校验浏览器路径；
    2. 创建 runtime profile/ext；
    3. 构造 nodriver Config；
    4. 启动浏览器；
    5. 等待 main_tab ready；
    6. 尝试清理 cookie；
    7. 若使用 MV3 代理扩展，则强制激活；
    8. 打开目标 URL；
    9. 打印页面诊断；
    10. 若进入 chrome-error，立即失败；
    11. 等 document.readyState；
    12. 等 ytcfg；
    13. 等 WebPoClient；
    14. 可选 mint 测试；
    15. 停止浏览器并清理临时目录。
    """
    logger = SimpleLogger()

    logger.info("========== WPC browser boot test started ==========")
    logger.info(f"python={sys.version}")
    logger.info(f"platform={sys.platform}")
    logger.info(f"pid={os.getpid()}")
    logger.info(f"browser_path={args.browser_path}")
    logger.info(f"proxy={mask_proxy_url(args.proxy)}")
    logger.info(f"proxy_auth_mode={args.proxy_auth_mode}")
    logger.info(f"url={args.url}")
    logger.info(f"keep_profile={args.keep_profile}")
    logger.info(f"mint_test={args.mint_test}")
    logger.info(f"content_binding={args.content_binding}")

    browser_path = pathlib.Path(args.browser_path)

    if not browser_path.exists():
        logger.error(f"browser_path does not exist: {browser_path}")
        return 2

    if not browser_path.is_file():
        logger.error(f"browser_path is not a file: {browser_path}")
        return 2

    runtime_dirs = make_runtime_dirs(args.runtime_base_dir)

    logger.info(f"runtime_root={runtime_dirs.root_dir}")
    logger.info(f"profile_dir={runtime_dirs.profile_dir}")
    logger.info(f"ext_dir={runtime_dirs.ext_dir}")

    browser = None

    try:
        config, needs_extension_activation = build_config(
            browser_path=str(browser_path),
            runtime_dirs=runtime_dirs,
            proxy=args.proxy,
            proxy_auth_mode=args.proxy_auth_mode,
            extra_args=args.extra_arg or [],
            logger=logger,
        )

        logger.info("[start] starting browser")
        started_at = time.monotonic()

        try:
            browser = await asyncio.wait_for(
                start(config=config),
                timeout=args.launch_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"[start] timeout after {args.launch_timeout}s")
            return 3
        except Exception as e:
            logger.error(f"[start] failed: {e!r}")
            logger.error(traceback.format_exc())
            return 3

        logger.info(f"[start] returned browser object, cost={time.monotonic() - started_at:.3f}s")
        logger.info(f"[start] browser_type={type(browser).__name__}")
        logger.info(f"[start] browser_repr={browser!r}")

        state = await wait_browser_state(
            browser,
            logger=logger,
            timeout_seconds=args.ready_timeout,
            interval_seconds=0.5,
        )
        logger.info(f"[ready] final_state={state}")

        if state.get("main_tab_is_none") is True:
            logger.error("========== WPC browser boot test FAILED: main_tab is not ready ==========")
            return 3

        clear_ok = await try_clear_cookies(browser, logger=logger)
        logger.info(f"[result] clear_cookies_ok={clear_ok}")

        try:
            extension_id = await activate_proxy_extension_if_needed(
                browser,
                needs_extension_activation=needs_extension_activation,
                logger=logger,
                timeout_seconds=args.activation_timeout,
                print_debug=args.debug_proxy_extension,
            )
            logger.info(f"[result] proxy_extension_id={extension_id!r}")
        except Exception as e:
            logger.error(f"[proxy-extension] activation failed: {e!r}")
            logger.error(traceback.format_exc())
            logger.error("========== WPC browser boot test FAILED proxy extension activation ==========")
            return 7

        get_ok = await test_browser_get(
            browser,
            logger=logger,
            url=args.url,
            timeout_seconds=args.page_timeout,
        )
        logger.info(f"[result] browser_get_ok={get_ok}")

        diagnostics = await collect_page_diagnostics(browser)
        log_page_diagnostics(
            logger=logger,
            prefix="[after-browser-get]",
            diagnostics=diagnostics,
        )

        if not get_ok:
            logger.error("========== WPC browser boot test FAILED browser.get ==========")
            return 4

        if is_chrome_error_page(diagnostics):
            logger.error(
                "========== WPC browser boot test FAILED chrome-error page =========="
            )
            return 4

        await test_tab_evaluate(browser, logger=logger)

        ready_ok = await wait_document_ready(
            browser,
            logger=logger,
            timeout_seconds=args.document_ready_timeout,
            interval_seconds=0.5,
        )
        logger.info(f"[result] document_ready_ok={ready_ok}")

        if not ready_ok:
            logger.error("========== WPC browser boot test FAILED document.readyState ==========")
            return 4

        ytcfg_ok = await wait_ytcfg_available(
            browser,
            logger=logger,
            timeout_seconds=args.ytcfg_timeout,
            interval_seconds=0.5,
        )
        logger.info(f"[result] ytcfg_ok={ytcfg_ok}")

        if not ytcfg_ok:
            logger.error("========== WPC browser boot test FAILED ytcfg ==========")
            return 5

        webpo_ok = await wait_webpo_client(
            browser,
            logger=logger,
            timeout_seconds=args.webpo_timeout,
            interval_seconds=1.0,
        )
        logger.info(f"[result] webpo_ok={webpo_ok}")

        await test_tab_evaluate(browser, logger=logger)

        if not webpo_ok:
            logger.error("========== WPC browser boot test FAILED WebPoClient ==========")
            return 5

        if args.mint_test:
            mint_ok = await test_webpo_mint(
                browser,
                logger=logger,
                content_binding=args.content_binding,
                timeout_seconds=args.mint_timeout,
            )
            logger.info(f"[result] mint_ok={mint_ok}")

            if not mint_ok:
                logger.error("========== WPC browser boot test FAILED mint ==========")
                return 6

            logger.info("========== WPC browser boot test PASSED WebPoClient + mint ==========")
            return 0

        logger.info("========== WPC browser boot test PASSED WebPoClient ==========")
        return 0

    finally:
        if browser is not None:
            try:
                logger.info("[cleanup] stopping browser")
                browser.stop()
                logger.info("[cleanup] browser stopped")
            except Exception as e:
                logger.warning(f"[cleanup] browser.stop failed: {e!r}")

        try:
            await asyncio.sleep(0.5)
            gc.collect()
            await asyncio.sleep(0.2)
        except Exception:
            pass

        if args.keep_profile:
            logger.warning(f"[cleanup] keep profile enabled: {runtime_dirs.root_dir}")
        else:
            try:
                logger.info(f"[cleanup] removing runtime dir: {runtime_dirs.root_dir}")
                shutil.rmtree(runtime_dirs.root_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"[cleanup] remove runtime dir failed: {e!r}")


def parse_args():
    """
    解析命令行参数。
    """
    parser = argparse.ArgumentParser(
        description=(
            "Test whether nodriver can start Chromium, activate project MV3 proxy "
            "auth extension, load YouTube, find WebPoClient, and optionally mint token."
        )
    )

    parser.add_argument(
        "--browser-path",
        required=True,
        help="Chrome/Chromium executable path.",
    )

    parser.add_argument(
        "--proxy",
        default=None,
        help=(
            "Optional proxy. Examples: "
            "http://user:pass@host:port, socks5://user:pass@host:port, socks5h://user:pass@host:port"
        ),
    )

    parser.add_argument(
        "--proxy-auth-mode",
        choices=["auto", "direct", "extension"],
        default="auto",
        help=(
            "Proxy auth handling mode. "
            "auto: auth proxy uses project MV3 extension, no-auth proxy uses --proxy-server. "
            "direct: always use --proxy-server. "
            "extension: always use project MV3 proxy auth extension."
        ),
    )

    parser.add_argument(
        "--url",
        default="https://www.youtube.com?themeRefresh=1",
        help="URL to open after browser starts and proxy extension is activated.",
    )

    parser.add_argument(
        "--runtime-base-dir",
        default="/tmp/wpc-browser-test",
        help="Base dir for temporary browser profiles.",
    )

    parser.add_argument(
        "--launch-timeout",
        type=float,
        default=60.0,
        help="Timeout seconds for nodriver.start().",
    )

    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=20.0,
        help="Timeout seconds for waiting browser/main_tab state.",
    )

    parser.add_argument(
        "--activation-timeout",
        type=float,
        default=5.0,
        help="Timeout seconds for project MV3 proxy extension activation.",
    )

    parser.add_argument(
        "--debug-proxy-extension",
        action="store_true",
        help=(
            "Print detailed proxy extension activation diagnostics. "
            "Useful when activation returns empty extension_id."
        ),
    )

    parser.add_argument(
        "--page-timeout",
        type=float,
        default=60.0,
        help="Timeout seconds for browser.get(url).",
    )

    parser.add_argument(
        "--document-ready-timeout",
        type=float,
        default=30.0,
        help="Timeout seconds for document.readyState interactive/complete.",
    )

    parser.add_argument(
        "--ytcfg-timeout",
        type=float,
        default=30.0,
        help="Timeout seconds for ytcfg availability.",
    )

    parser.add_argument(
        "--webpo-timeout",
        type=float,
        default=60.0,
        help="Timeout seconds for WebPoClient availability.",
    )

    parser.add_argument(
        "--mint-test",
        action="store_true",
        help="Also call WebPoClient.mws() to verify real token mint flow.",
    )

    parser.add_argument(
        "--content-binding",
        default="3dZryjBuSno",
        help="Content binding used by --mint-test.",
    )

    parser.add_argument(
        "--mint-timeout",
        type=float,
        default=60.0,
        help="Timeout seconds for WebPoClient.mws() mint test.",
    )

    parser.add_argument(
        "--keep-profile",
        action="store_true",
        help="Keep profile dir for debugging.",
    )

    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra Chrome arg. Can be used multiple times.",
    )

    return parser.parse_args()


def main() -> int:
    """
    脚本入口。
    """
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())