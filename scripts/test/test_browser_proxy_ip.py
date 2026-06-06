# scripts/test/test_browser_proxy_ip.py
from __future__ import annotations

import argparse
import asyncio
import gc
import os
import pathlib
import re
import shutil
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import nodriver
import requests
from nodriver import start

from yt_dlp_plugins.extractor.proxy_auth_ext_v3 import (
    parse_proxy_url,
    proxy_has_auth,
    write_proxy_auth_extension,
    maybe_activate_proxy_extension, debug_proxy_extension_activation,
)

IPINFO_IP_URL = "https://ipinfo.io/ip"


@dataclass
class RuntimeDirs:
    """
    一次浏览器代理 IP 测试使用的临时目录集合。

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
    - 每次测试都使用独立 profile/ext，避免状态污染；
    - 默认测试结束后删除 root_dir；
    - 可通过 --keep-profile 保留目录用于排查。
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
    """

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def info(self, msg: str) -> None:
        print(f"{self._now()} - INFO - {msg}", flush=True)

    def warning(self, msg: str) -> None:
        print(f"{self._now()} - WARNING - {msg}", flush=True)

    def error(self, msg: str) -> None:
        print(f"{self._now()} - ERROR - {msg}", flush=True)

    def debug(self, msg: str) -> None:
        print(f"{self._now()} - DEBUG - {msg}", flush=True)


def mask_proxy_url(proxy: str | None) -> str | None:
    """
    脱敏代理 URL，避免日志泄露账号密码。

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


def make_runtime_dirs(base_dir: str | None) -> RuntimeDirs:
    """
    创建本次测试的临时运行目录。
    """
    run_id = uuid.uuid4().hex

    if not base_dir:
        base_dir = "/tmp/wpc-proxy-ip-test"

    root_dir = pathlib.Path(base_dir) / run_id
    profile_dir = root_dir / "profile"
    ext_dir = root_dir / "ext"

    profile_dir.mkdir(parents=True, exist_ok=True)
    ext_dir.mkdir(parents=True, exist_ok=True)

    return RuntimeDirs(
        run_id=run_id,
        root_dir=str(root_dir),
        profile_dir=str(profile_dir),
        ext_dir=str(ext_dir),
    )


def extract_ip(text: str) -> str:
    """
    从响应文本中提取 IP。

    ipinfo.io/ip 正常返回纯文本：
        1.2.3.4

    这里做宽松提取，兼容：
    - 多余换行；
    - HTML 错误页；
    - IPv4；
    - IPv6。
    """
    if not text:
        return ""

    text = text.strip()

    ipv4 = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    if ipv4:
        return ipv4.group(0)

    # IPv6 至少必须包含冒号，避免把 be、dead、face 这种普通文本误判成 IP。
    ipv6 = re.search(r"\b(?=[0-9a-fA-F:]*:)[0-9a-fA-F:]{2,}\b", text)
    if ipv6:
        return ipv6.group(0)

    return text[:200]


def build_browser_args(
    *,
    proxy: str | None,
    proxy_auth_mode: str,
    runtime_dirs: RuntimeDirs,
    logger: SimpleLogger,
    extra_args: list[str],
) -> list[str]:
    """
    构造 Chromium 启动参数。

    proxy_auth_mode:
    - auto:
        不带认证代理：使用 --proxy-server；
        带认证代理：使用项目真实 MV3 代理认证扩展。
    - direct:
        强制使用 --proxy-server。
        注意：Chrome 对 http://user:pass@host:port 这种命令行形式支持不稳定，
        该模式主要用于复现问题。
    - extension:
        强制使用项目真实 MV3 代理认证扩展。

    与生产逻辑保持一致的关键点：
    - 带认证代理时，只传 --load-extension；
    - 不同时传 --proxy-server，避免命令行代理和扩展代理双来源竞争。
    """
    args = [
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]

    if not proxy:
        logger.info("[proxy] no proxy configured")
        args.extend(extra_args or [])
        return args

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
            "[proxy] direct mode selected; "
            "proxy with username/password may fail in Chromium"
        )
        args.append(f"--proxy-server={proxy}")
        args.extend(extra_args or [])
        return args

    if proxy_auth_mode == "auto" and not has_auth:
        args.append(f"--proxy-server={proxy}")
        args.extend(extra_args or [])
        return args

    # 带认证代理，或者强制 extension 模式：
    # 直接使用项目真实 proxy_auth_ext_v3 实现。
    proxy_cfg = parse_proxy_url(proxy)
    write_proxy_auth_extension(pathlib.Path(runtime_dirs.ext_dir), proxy_cfg)

    logger.info(f"[proxy] using project MV3 proxy auth extension: {runtime_dirs.ext_dir}")

    args.append(f"--load-extension={runtime_dirs.ext_dir}")

    args.extend(extra_args or [])
    return args


def request_ip_via_requests(
    *,
    proxy: str | None,
    timeout: float,
    logger: SimpleLogger,
) -> tuple[bool, str, str]:
    """
    使用 Python requests 通过同一个代理访问 ipinfo.io/ip。

    返回：
    - ok:
        请求是否成功。
    - raw_text:
        原始响应文本。
    - extracted_ip:
        从响应中提取的 IP。
    """
    proxies = None

    if proxy:
        proxies = {
            "http": proxy,
            "https": proxy,
        }

    try:
        logger.info(f"[requests] requesting {IPINFO_IP_URL}")
        resp = requests.get(
            IPINFO_IP_URL,
            proxies=proxies,
            timeout=timeout,
        )

        raw = resp.text.strip()
        ip = extract_ip(raw)

        logger.info(f"[requests] status_code={resp.status_code}")
        logger.info(f"[requests] raw_text={raw!r}")
        logger.info(f"[requests] extracted_ip={ip!r}")

        return resp.ok, raw, ip

    except Exception as e:
        logger.error(f"[requests] failed: {e!r}")
        logger.error(traceback.format_exc())
        return False, repr(e), ""


async def safe_evaluate(tab, js: str, *, await_promise: bool = False) -> tuple[bool, Any]:
    """
    安全执行 tab.evaluate。

    返回：
    - (True, value)
    - (False, error_repr)
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
    收集当前浏览器页面诊断信息。
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
        "body_text": await _eval("document.body ? document.body.innerText : ''"),
    }


async def wait_browser_ready(
    browser,
    *,
    logger: SimpleLogger,
    timeout_seconds: float,
    interval_seconds: float = 0.2,
) -> None:
    """
    等待 browser/main_tab 达到最低可用状态。

    注意：
    - 不要求 browser.connection 非空；
    - Linux/headless/nodriver 场景下 browser.connection 可能为 None；
    - 只要 main_tab 存在，browser.get() 和 tab.evaluate 通常仍可用。
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

        logger.info(f"[browser-ready] state={last_state}")

        if browser is not None and main_tab is not None:
            return

        await asyncio.sleep(interval_seconds)

    raise RuntimeError(
        "browser started but main_tab is not ready; "
        f"timeout_seconds={timeout_seconds}; last_state={last_state}"
    )


async def request_ip_via_browser(
    *,
    browser_path: str,
    proxy: str | None,
    proxy_auth_mode: str,
    runtime_dirs: RuntimeDirs,
    timeout: float,
    activation_timeout: float,
    pre_nav_sleep: float,
    logger: SimpleLogger,
    extra_args: list[str],
) -> tuple[bool, str, str, dict[str, Any]]:
    """
    使用 Chromium 打开 https://ipinfo.io/ip，并提取页面返回文本。

    与生产逻辑一致的关键点：
    - 如果使用 MV3 代理认证扩展，则必须执行 maybe_activate_proxy_extension；
    - 激活失败直接判定浏览器代理测试失败；
    - 避免浏览器实际未走代理时还继续访问目标 URL。
    """
    browser = None

    try:
        browser_args = build_browser_args(
            proxy=proxy,
            proxy_auth_mode=proxy_auth_mode,
            runtime_dirs=runtime_dirs,
            logger=logger,
            extra_args=extra_args or [],
        )

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

        logger.info("[browser] starting")
        browser = await asyncio.wait_for(
            start(config=config),
            timeout=timeout,
        )
        logger.info(f"[browser] started: {browser!r}")

        await wait_browser_ready(
            browser,
            logger=logger,
            timeout_seconds=min(10.0, timeout),
        )

        # 是否需要激活扩展：
        # - auto + 带认证代理：需要；
        # - extension：需要；
        # - direct：不需要；
        # - auto + 不带认证代理：不需要。
        proxy_normalized = normalize_proxy_scheme_for_chrome(proxy) if proxy else None
        needs_extension_activation = bool(
            proxy_normalized
            and (
                proxy_auth_mode == "extension"
                or (proxy_auth_mode == "auto" and proxy_has_auth(proxy_normalized))
            )
        )

        extension_id = ""

        if needs_extension_activation:
            logger.info(
                "[proxy-extension] activating MV3 proxy extension before opening ipinfo"
            )

            # extension_id = await maybe_activate_proxy_extension(
            #     browser,
            #     timeout=activation_timeout,
            #     visible_probe=False,
            #     proxy_url="",
            # )
            #
            # logger.info(f"[proxy-extension] activation result extension_id={extension_id!r}")
            #
            # if not extension_id:
            #     raise RuntimeError(
            #         "MV3 proxy extension activation failed; "
            #         "browser may not use configured proxy"
            #     )

            diag = await debug_proxy_extension_activation(
                browser,
                timeout=activation_timeout,
                proxy_url="",
            )

            logger.info(f"[proxy-extension-debug] {diag!r}")

            extension_id = str(diag.get("activated_extension_id") or "")

            logger.info(f"[proxy-extension] activation result extension_id={extension_id!r}")

            if not extension_id:
                raise RuntimeError(
                    "MV3 proxy extension activation failed; "
                    f"diag={diag!r}"
                )

        if pre_nav_sleep > 0:
            logger.info(f"[browser] pre_nav_sleep={pre_nav_sleep}s")
            await asyncio.sleep(pre_nav_sleep)

        logger.info(f"[browser] opening {IPINFO_IP_URL}")
        await asyncio.wait_for(
            browser.get(IPINFO_IP_URL),
            timeout=timeout,
        )

        tab = getattr(browser, "main_tab", None)
        if tab is None:
            raise RuntimeError("browser.main_tab is None after browser.get")

        deadline = time.monotonic() + timeout
        raw_text = ""
        diagnostics: dict[str, Any] = {}

        while time.monotonic() < deadline:
            diagnostics = await collect_page_diagnostics(browser)

            raw_text = str(diagnostics.get("body_text") or "").strip()

            logger.info(
                "[browser] "
                f"href={diagnostics.get('href')!r}, "
                f"title={diagnostics.get('title')!r}, "
                f"readyState={diagnostics.get('readyState')!r}, "
                f"body_text={raw_text!r}"
            )

            if raw_text:
                break

            await asyncio.sleep(0.5)

        # 如果是错误页，直接判失败，不提取 IP：
        is_chrome_error = str(diagnostics.get("href", "")).startswith("chrome-error://")
        if is_chrome_error:
            extracted_ip = ""
        else:
            extracted_ip = extract_ip(raw_text)

        logger.info(f"[browser] raw_text={raw_text!r}")
        logger.info(f"[browser] extracted_ip={extracted_ip!r}")

        href = str(diagnostics.get("href") or "")
        ok = bool(raw_text) and not href.startswith("chrome-error://")

        if extension_id:
            diagnostics["proxy_extension_id"] = extension_id

        return ok, raw_text, extracted_ip, diagnostics

    except Exception as e:
        logger.error(f"[browser] failed: {e!r}")
        logger.error(traceback.format_exc())
        return False, repr(e), "", {}

    finally:
        if browser is not None:
            try:
                logger.info("[browser] stopping")
                browser.stop()
                logger.info("[browser] stopped")
            except Exception as e:
                logger.warning(f"[browser] stop failed: {e!r}")

        try:
            await asyncio.sleep(0.5)
            gc.collect()
            await asyncio.sleep(0.2)
        except Exception:
            pass


async def main_async(args) -> int:
    logger = SimpleLogger()

    logger.info("========== Browser Proxy IP Test Started ==========")
    logger.info(f"python={sys.version}")
    logger.info(f"platform={sys.platform}")
    logger.info(f"pid={os.getpid()}")
    logger.info(f"browser_path={args.browser_path}")
    logger.info(f"proxy={mask_proxy_url(args.proxy)}")
    logger.info(f"proxy_auth_mode={args.proxy_auth_mode}")
    logger.info(f"runtime_base_dir={args.runtime_base_dir}")

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

    try:
        requests_ok, requests_raw, requests_ip = request_ip_via_requests(
            proxy=args.proxy,
            timeout=args.requests_timeout,
            logger=logger,
        )

        browser_ok, browser_raw, browser_ip, browser_diag = await request_ip_via_browser(
            browser_path=str(browser_path),
            proxy=args.proxy,
            proxy_auth_mode=args.proxy_auth_mode,
            runtime_dirs=runtime_dirs,
            timeout=args.browser_timeout,
            activation_timeout=args.activation_timeout,
            pre_nav_sleep=args.pre_nav_sleep,
            logger=logger,
            extra_args=args.extra_arg or [],
        )

        same = bool(requests_ip and browser_ip and requests_ip == browser_ip)

        logger.info("========== Result ==========")
        logger.info(f"requests_ok={requests_ok}")
        logger.info(f"requests_raw={requests_raw!r}")
        logger.info(f"requests_ip={requests_ip!r}")
        logger.info(f"browser_ok={browser_ok}")
        logger.info(f"browser_raw={browser_raw!r}")
        logger.info(f"browser_ip={browser_ip!r}")
        logger.info(f"browser_diag={browser_diag!r}")
        logger.info(f"ip_match={same}")

        if same:
            logger.info("========== PASSED: browser IP matches requests IP ==========")
            return 0

        logger.warning("========== FAILED: browser IP does not match requests IP ==========")
        return 1

    finally:
        if args.keep_profile:
            logger.warning(f"[cleanup] keep profile enabled: {runtime_dirs.root_dir}")
        else:
            logger.info(f"[cleanup] removing runtime dir: {runtime_dirs.root_dir}")
            shutil.rmtree(runtime_dirs.root_dir, ignore_errors=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Compare IP from Python requests via proxy and Chromium browser via "
            "the same proxy. Auth proxy uses the project MV3 proxy extension."
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
        help="Proxy URL, e.g. http://user:pass@host:port",
    )

    parser.add_argument(
        "--proxy-auth-mode",
        choices=["auto", "direct", "extension"],
        default="auto",
        help=(
            "auto: auth proxy uses MV3 extension, no-auth proxy uses --proxy-server. "
            "direct: always use --proxy-server. "
            "extension: always use MV3 proxy extension."
        ),
    )

    parser.add_argument(
        "--runtime-base-dir",
        default="/tmp/wpc-proxy-ip-test",
        help="Base dir for temporary browser profile and extension.",
    )

    parser.add_argument(
        "--requests-timeout",
        type=float,
        default=30.0,
        help="Timeout seconds for Python requests.",
    )

    parser.add_argument(
        "--browser-timeout",
        type=float,
        default=60.0,
        help="Timeout seconds for browser launch/navigation.",
    )

    parser.add_argument(
        "--activation-timeout",
        type=float,
        default=15.0,
        help="Timeout seconds for MV3 proxy extension activation.",
    )

    parser.add_argument(
        "--pre-nav-sleep",
        type=float,
        default=0.0,
        help="Sleep seconds after browser starts and before navigating.",
    )

    parser.add_argument(
        "--keep-profile",
        action="store_true",
        help="Keep runtime dir for debugging.",
    )

    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra Chrome arg. Can be used multiple times.",
    )

    return parser.parse_args()


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())