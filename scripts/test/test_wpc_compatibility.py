"""离线验证 nodriver 升级后的 WPC 兼容性，不启动浏览器或访问网络。

仅使用临时目录、虚构代理凭据和浏览器替身，不读取真实 Cookie 或 .env。
运行方式：.venv/bin/python scripts/test/test_wpc_compatibility.py
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
YT_DLP_SOURCE = ROOT.parent / "yt-dlp"
if (YT_DLP_SOURCE / "yt_dlp").is_dir():
    # WPC 独立虚拟环境不要求安装 yt-dlp，复用相邻源码但保持本仓库优先。
    sys.path.insert(1, str(YT_DLP_SOURCE))
_MODULE_STACK = None
browser_module = None


def setUpModule():
    global _MODULE_STACK, browser_module
    _MODULE_STACK = ExitStack()
    try:
        # 不使用调用环境中的 WPC 配置、代理或用户目录配置。
        _MODULE_STACK.enter_context(patch.dict(os.environ, {}, clear=True))
        forbidden = AssertionError("离线兼容性测试禁止网络连接和启动外部进程")
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.bind",
            "socket.create_connection", "socket.getaddrinfo", "subprocess.Popen", "os.system",
        ):
            _MODULE_STACK.enter_context(patch(target, side_effect=forbidden))
        for target in ("asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"):
            _MODULE_STACK.enter_context(patch(target, new=AsyncMock(side_effect=forbidden)))

        version = importlib.metadata.version("nodriver")
        if version != "0.50.99":
            raise AssertionError(f"本次升级回归要求 nodriver 0.50.99，实际版本为 {version}")
        browser_module = importlib.import_module("yt_dlp_plugins.extractor.wpc_browser")
        expected_source = ROOT / "yt_dlp_plugins/extractor/wpc_browser.py"
        if Path(browser_module.__file__).resolve() != expected_source.resolve():
            raise AssertionError("离线回归必须使用当前仓库中的 wpc_browser.py")
        # 每个涉及启动流程的用例都必须显式注入浏览器替身。
        _MODULE_STACK.enter_context(patch.object(browser_module, "start", new=AsyncMock(side_effect=forbidden)))
        _MODULE_STACK.enter_context(patch.object(browser_module.nodriver, "start", new=AsyncMock(side_effect=forbidden)))
    except BaseException:
        _MODULE_STACK.close()
        raise


def tearDownModule():
    if _MODULE_STACK is not None:
        _MODULE_STACK.close()


class WPCCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.logger = Mock()

    def browser(self, *, clear=None, send=None):
        return SimpleNamespace(
            cookies=SimpleNamespace(clear=clear),
            connection=SimpleNamespace(send=send),
            main_tab=object(),
            get=AsyncMock(),
            stop=Mock(),
        )

    def prepare_launch(self, browser, *, ytcfg_error=None):
        # 仅替换外部交互，保留 launch_browser 内部的初始化和异常清理流程。
        self.stack.enter_context(patch.object(browser_module, "start", new=AsyncMock(return_value=browser)))
        self.stack.enter_context(patch.object(browser_module, "wait_browser_ready", new=AsyncMock()))
        self.stack.enter_context(patch.object(browser_module, "maybe_activate_proxy_extension", new=AsyncMock(return_value="fixture-extension")))
        self.stack.enter_context(patch.object(browser_module, "collect_page_diagnostics", new=AsyncMock(return_value={})))
        self.stack.enter_context(patch.object(browser_module, "wait_document_ready", new=AsyncMock()))
        return self.stack.enter_context(patch.object(
            browser_module, "wait_ytcfg_available", new=AsyncMock(side_effect=ytcfg_error),
        ))

    def test_cookie_clear_prefers_new_api_without_using_connection(self):
        clear, send = AsyncMock(), AsyncMock()
        browser = self.browser(clear=clear, send=send)
        asyncio.run(browser_module.best_effort_clear_cookies(browser, logger=self.logger))
        clear.assert_awaited_once_with()
        send.assert_not_called()

    def test_cookie_clear_falls_back_to_legacy_cdp_when_new_api_is_absent(self):
        send = AsyncMock()
        browser = SimpleNamespace(connection=SimpleNamespace(send=send))
        command = object()
        with patch.object(browser_module.cdp.storage, "clear_cookies", return_value=command):
            asyncio.run(browser_module.best_effort_clear_cookies(browser, logger=self.logger))
        send.assert_awaited_once_with(command)

    def test_missing_cookie_apis_are_nonfatal(self):
        asyncio.run(browser_module.best_effort_clear_cookies(SimpleNamespace(), logger=self.logger))
        self.logger.debug.assert_called()

    def test_cookie_clear_failures_do_not_block_browser_initialization(self):
        clear = AsyncMock(side_effect=RuntimeError("new cookie API failed"))
        send = AsyncMock(side_effect=RuntimeError("legacy CDP failed"))
        browser = self.browser(clear=clear, send=send)
        wait_ytcfg = self.prepare_launch(browser)

        result = asyncio.run(browser_module.launch_browser(object(), logger=self.logger))

        self.assertIs(result, browser)
        clear.assert_awaited_once_with()
        send.assert_awaited_once()
        browser.get.assert_awaited_once_with(browser_module.WPC_YOUTUBE_BOOT_URL)
        wait_ytcfg.assert_awaited_once()
        browser.stop.assert_not_called()

    def test_cookie_clear_preserves_cancellation(self):
        clear = AsyncMock(side_effect=asyncio.CancelledError())
        send = AsyncMock()
        browser = self.browser(clear=clear, send=send)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(browser_module.best_effort_clear_cookies(browser, logger=self.logger))
        send.assert_not_called()

    def test_headless_and_mv3_authenticated_proxy_configuration_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="wpc-offline-config-") as directory:
            root = Path(directory)
            profile, extension = root / "profile", root / "ext"
            profile.mkdir()
            extension.mkdir()
            runtime = browser_module.BrowserRuntimeDirs("fixture", str(root), str(profile), str(extension))

            config = browser_module.build_nodriver_config(
                browser_executable_path=str(root / "unused-chrome"),
                runtime_dirs=runtime,
                proxy="http://fixture-user:fixture-password@proxy.invalid:18080",
            )
            arguments = config()
            manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
            worker = extension / manifest["background"]["service_worker"]

            self.assertTrue(config.headless)
            self.assertTrue(any(argument.startswith("--headless") for argument in arguments))
            self.assertIn(f"--user-data-dir={profile}", arguments)
            self.assertIn(f"--load-extension={extension}", arguments)
            self.assertFalse(any(argument.startswith("--proxy-server=") for argument in arguments))
            self.assertNotIn("fixture-password", " ".join(arguments))
            self.assertEqual(manifest["manifest_version"], 3)
            self.assertIn("webRequestAuthProvider", manifest["permissions"])
            self.assertIn("proxy", manifest["permissions"])
            self.assertTrue(worker.is_file())
            self.assertIn("fixture-user", worker.read_text(encoding="utf-8"))
            self.assertIn("fixture-password", worker.read_text(encoding="utf-8"))

    def test_failed_initialization_stops_started_browser(self):
        browser = self.browser(clear=AsyncMock(), send=AsyncMock())
        error = browser_module.WPCError("fixture ytcfg timeout")
        self.prepare_launch(browser, ytcfg_error=error)

        with self.assertRaisesRegex(browser_module.WPCError, "fixture ytcfg timeout"):
            asyncio.run(browser_module.launch_browser(object(), logger=self.logger))

        browser.stop.assert_called_once_with()

    def test_mint_cleans_browser_and_runtime_on_success_failure_and_stop_error(self):
        for outcome in ("success", "mint_failure", "stop_failure"):
            with self.subTest(outcome=outcome), ExitStack() as stack:
                root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="wpc-offline-mint-")))
                loop = asyncio.new_event_loop()
                stack.callback(loop.close)
                browser = self.browser()
                if outcome == "stop_failure":
                    browser.stop.side_effect = RuntimeError("fixture stop failure")
                async def mint_fixture(**_kwargs):
                    self.assertTrue((root / "fixture-run/profile").is_dir())
                    self.assertTrue((root / "fixture-run/ext").is_dir())
                    if outcome == "mint_failure":
                        raise browser_module.WPCError("fixture mint failure")
                    return "fixture-po-token"

                mint = AsyncMock(side_effect=mint_fixture)
                config = replace(
                    browser_module.WPC_RUNTIME_CONFIG,
                    resource_gate=replace(browser_module.WPC_RUNTIME_CONFIG.resource_gate, disabled=True),
                    runtime_cleanup_initial_delay_seconds=0,
                    runtime_cleanup_max_retries=1,
                    runtime_cleanup_retry_interval_seconds=0,
                )
                guard = MagicMock()
                guard.__exit__.return_value = False
                for name, value in {
                    "WPC_RUNTIME_CONFIG": config,
                    "WPCLaunchGuard": Mock(return_value=guard),
                    "build_wpc_browser_resource_gate": Mock(),
                    "_log_runtime_config": Mock(),
                    "should_skip_proxy": Mock(return_value=(False, None)),
                    "get_wpc_browser_runtime_root_dir": Mock(return_value=str(root)),
                    "generate_browser_runtime_run_id": Mock(return_value="fixture-run"),
                    "build_nodriver_config": Mock(return_value=object()),
                    "launch_browser": AsyncMock(return_value=browser),
                    "mint_po_token": mint,
                }.items():
                    stack.enter_context(patch.object(browser_module, name, value))

                # 实际创建并清理临时 profile/ext，避免只验证 cleanup mock 被调用。
                kwargs = dict(logger=self.logger, event_loop=loop, browser_executable_path="unused-chrome",
                              proxy=None, content_binding="fixture-content-binding")
                if outcome == "mint_failure":
                    with self.assertRaisesRegex(browser_module.WPCError, "fixture mint failure"):
                        browser_module.mint_po_token_once(**kwargs)
                else:
                    self.assertEqual(browser_module.mint_po_token_once(**kwargs), "fixture-po-token")

                browser.stop.assert_called_once_with()
                mint.assert_awaited_once()
                guard.__exit__.assert_called_once()
                self.assertFalse((root / "fixture-run").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
