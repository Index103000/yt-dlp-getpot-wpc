# -*- coding: utf-8 -*-
"""
yt_dlp_plugins/extractor/proxy_auth_ext_v3.py

===============================================================================
为什么必须使用 Manifest V3（MV3）
===============================================================================
在 Chrome 144（Stable）中加载 MV2 扩展时报错：
    “无法安装扩展程序，因为它使用了不受支持的清单版本”
这意味着该 Chrome 构建对 MV2 的加载/使用已被限制或禁用。

因此，要在 Chrome Stable 里通过 “--load-extension” 动态加载扩展，并实现：
- 自动设置代理
- 自动处理代理认证（HTTP 407）
需要使用 Manifest V3（MV3）。

===============================================================================
MV3 下“代理认证”的关键实现点
===============================================================================
MV2 常用方式是：
- webRequestBlocking + onAuthRequired 返回 authCredentials，同步 blocking。

MV3 已不支持 MV2 的 blocking 方案，需要改用：
- permissions: ["webRequest", "webRequestAuthProvider"]
- chrome.webRequest.onAuthRequired.addListener(..., ["asyncBlocking"])
- 在回调中使用 callback({ authCredentials: { username, password } })

===============================================================================
模块目标
===============================================================================
1) 支持带账号密码代理（HTTP/HTTPS/SOCKS4/SOCKS5）：
    http://user:pass@host:port
    https://user:pass@host:port
    socks4://user:pass@host:port
    socks4a://user:pass@host:port
    socks5://user:pass@host:port
    socks5h://user:pass@host:port

2) Chrome 扩展层实际只使用 Chrome 支持的 scheme：
    http / https / socks4 / socks5

   因此：
    - socks5h 会规整为 socks5；
    - socks4a 会规整为 socks4。

   重要限制：
    - socks5h 的“由代理端解析 DNS”语义不能在 Chrome proxy.settings 里完整保留；
    - SOCKS 带账号密码在 Chrome 扩展 onAuthRequired 下不一定稳定；
    - 若线上 SOCKS 带认证出现 ERR_SOCKS_CONNECTION_FAILED，建议使用本地 HTTP wrapper，
      即：Chrome -> 本地 HTTP 代理 -> 上游 socks5/socks5h。

3) 扩展目录由外层 browser 模块提供：
   - 当前主链路中，browser 运行期目录已由 wpc_browser.py 创建与清理；
   - 本模块只负责向给定的 ext_dir 写入临时扩展内容。

4) 探活（建议在启动后立刻做一次）：
   - 通过 CDP 打开 chrome://version 读取 “命令行(command_line)” 并解析 --load-extension；
   - 以 --load-extension 为锚点推导 expected_run_id；
   - 枚举扩展 SW targets，并追加固定 extension_id 作为 fallback；
   - 逐个打开 chrome-extension://<id>/health.html；
   - 等待 health.js 将 #raw 从 loading... 更新为 JSON；
   - 当 JSON 中 ok=true，且 run_id 匹配 expected_run_id 时，即认为扩展已激活；
   - 若无法解析 expected_run_id，则固定 extension_id 返回 ok=true 时也认为激活成功；
   - 仅能打开 health.html 静态页面不代表成功，因为此时 Service Worker 可能还没响应。

5) 代理动态切换（偏好 URL 直传形式）：
   - 访问：
     chrome-extension://<ext_id>/health.html?proxy=http://user:pass@host:port
   - 若传 proxy 参数：切换到新代理（并持久化）
   - 若不传：仅 ping + 展示当前配置

6) 重启命令展示（用于“切换后预计生效”）：
   - health 页面会展示 restart_cmd（来自 chrome://version 的 command_line）
   - 支持一键复制
   - 切换发生后会提示 need_restart=true

7) “当前实际生效”与“重启后预计生效”两套展示（health 页面侧）：
   - 切换时把“切换前配置”保存为 prev_cfg
   - 切换后配置为 next_cfg（即 current cfg）
   - 页面实时请求公网 IP 服务显示“当前请求出去的 IP”
   - next_cfg 的 IP 只能在重启后再刷新页面验证，因此 UI 明确提示重启方案

===============================================================================
注意事项 / 风险点
===============================================================================
1) 扩展目录会写入明文 username/password（仅写在临时目录里）。
   运行结束应调用 cleanup_runtime_dirs(dirs) 清理，减少凭据落盘风险。

2) storage 持久化会把“最后一次代理配置”写入 profile（chrome.storage.local），
   其中包含明文 password（用于自动代理认证）。

3) Chrome 连接复用导致“切换后 IP 不变”是常见现象：
   - 扩展层只能改 proxy settings，无法保证立即断开旧 socket/隧道
   - 结论：切换后需重启进程才能更可靠地建立新隧道/IP

4) 某些 Chrome/企业策略/定制构建可能禁用命令行加载扩展（--load-extension 无效）。
   这种情况下：
   - 扩展不会出现
   - 探活会失败（返回 ""）
   - 但模块不会阻断主流程

5) 经测试，在 Linux/headless/nodriver 环境下，可能出现 browser connection 不可用，此时，使用固定 extension_id fallback 激活扩展。

===============================================================================
外层调用方式（尽量不改外层）
===============================================================================
生成扩展：
    # ext_dir 由外层 browser 模块创建
    write_proxy_auth_extension(ext_dir, proxy_cfg)

启动时由外层传入：
    --user-data-dir=<profile_dir>
    --load-extension=<ext_dir>

启动后探活（任选其一）：
    1) 异步：
        await maybe_activate_proxy_extension(browser, proxy_url=proxy_url)
    2) 同步：
        probe_extension_health_sync(browser, loop=loop, proxy_url=proxy_url)

===============================================================================
注意事项
===============================================================================
1) 扩展目录会写入明文 username/password。
   该目录必须是临时目录，运行结束应清理。

2) chrome.storage.local 可能持久化最后一次代理配置，其中包含明文 password。
   当前项目每次使用独立 profile，一般会随 runtime 目录清理。

3) Chrome 连接复用导致“切换后 IP 不变”是正常现象。
   扩展只能修改 proxy settings，不能强制关闭所有旧 socket / tunnel。
   动态切换后，可靠方案仍然是重启浏览器进程。

4) 某些 Chrome/企业策略/定制构建可能禁用 --load-extension。
   此时探活会失败。

5) Linux/headless/nodriver 环境中 browser.connection 可能为 None。
   本模块使用固定 extension_id fallback 激活扩展。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

# nodriver 的 CDP 协议对象：用于枚举 targets（包含扩展 SW），以及访问内部页面
from nodriver import cdp


# =============================================================================
# 固定扩展 ID 支持（用于 connection 不可用的 fallback）
# =============================================================================
# 背景：
# - Chrome unpacked extension 默认 extension_id 随路径变化
# - 通过 manifest.key 固定，生成稳定 extension_id
_FIXED_EXTENSION_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAskVrVAfkpXpUEegtsEHjop3Zfqa2RpWbe3TUvO/Rjq/ZdmW1SdDUBRFYxwZK+3ZTfcS8ASpHcOVuN+/hw0DvJvZra9K4CpL7F3jXm4NIvR6UIg9ozkIhkmqoCUDkm2kvZM+GaIBJ17Re2Y1+ynCjDM7ur1ysrLJQZ5quikOIx7sf1HXV1DZnVCO7TK64ZrFLfVWc0ybHTP6GVgj44Kg6Jmb3ubMcD8Jzb7ENmL/RJL87oNaF331kqnDARam0y6GoLFsdaA7/WEuv0H7TJiJd0b4etrEj1pQ08oh71qGXyAwodG1STNXB/M8wXL/mxigkhcFAsd3Yoan0dt7az2NP9wIDAQAB"
)


def _chrome_extension_id_from_public_key_b64(public_key_b64: str) -> str:
    """
    根据 manifest.key 计算固定 Chrome extension_id。

    规则：
    1. base64 decode public key 得到 DER bytes；
    2. sha256，取前 16 字节；
    3. 每个 4-bit 映射到 a-p，组合成 32 字符 extension_id。
    """
    der = base64.b64decode(public_key_b64)
    digest = hashlib.sha256(der).digest()[:16]
    alphabet = "abcdefghijklmnop"

    return "".join(
        alphabet[b >> 4] + alphabet[b & 0x0F]
        for b in digest
    )


_FIXED_EXTENSION_ID = _chrome_extension_id_from_public_key_b64(
    _FIXED_EXTENSION_PUBLIC_KEY_B64
)


# =============================================================================
# 常量：固定文件名（外层不需要传任何参数）
# =============================================================================
# MV3 service worker 文件名固定；探活不靠文件名匹配，而靠 run_id 校验，避免误匹配
_SW_FILENAME = "wpc_sw.js"

# 健康检查页面固定名：探活会打开 chrome-extension://<id>/health.html
_HEALTH_FILENAME = "health.html"

# 健康检查脚本文件名：用于避免 MV3 CSP 禁止 inline script 导致 health 卡死
_HEALTH_JS_FILENAME = "health.js"


# =============================================================================
# 代理配置
# =============================================================================

@dataclass(frozen=True)
class ProxyCfg:
    """
    代理配置。

    字段说明：
    - scheme:
        Chrome 实际使用的 scheme，只能是 http / https / socks4 / socks5。
    - original_scheme:
        用户原始传入的 scheme，例如 socks5h / socks4a。
    - host / port:
        代理地址。
    - username / password:
        代理认证凭证，可为空。
    - chrome_support_warning:
        对 Chrome 支持限制的提示信息，写入 health 页面诊断。
    """

    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    original_scheme: str = ""
    chrome_support_warning: str = ""


_CHROME_PROXY_SCHEMES = {"http", "https", "socks4", "socks5"}

_PROXY_SCHEME_ALIASES_FOR_CHROME = {
    "socks5h": "socks5",
    "socks4a": "socks4",
}


def normalize_proxy_scheme_for_chrome(scheme: str) -> str:
    """
    将代理 scheme 规整成 Chrome proxy.settings 支持的 scheme。

    说明：
    - Chrome 不认识 requests / urllib3 里的 socks5h；
    - Chrome proxy.settings 中 socks5 不能表达 socks5h 的远端 DNS 语义；
    - 因此 socks5h 只能规整为 socks5；
    - socks4a 同理规整为 socks4。
    """
    s = (scheme or "").strip().lower()

    if s in _PROXY_SCHEME_ALIASES_FOR_CHROME:
        return _PROXY_SCHEME_ALIASES_FOR_CHROME[s]

    if s in _CHROME_PROXY_SCHEMES:
        return s

    raise ValueError(f"Unsupported proxy scheme: {scheme}")


def is_socks_proxy_scheme(scheme: str) -> bool:
    """
    判断 scheme 是否是 SOCKS 代理族。
    """
    return (scheme or "").strip().lower() in {
        "socks4",
        "socks4a",
        "socks5",
        "socks5h",
    }


def proxy_requires_http_wrapper(proxy_url: str) -> bool:
    """
    判断当前代理是否建议使用 HTTP wrapper。

    触发条件：
    - SOCKS 代理；
    - 且代理 URL 中带 username/password。

    原因：
    - Chrome 对 SOCKS 认证的支持不如 HTTP Proxy 认证稳定；
    - MV3 onAuthRequired 对 HTTP 407 更可靠；
    - SOCKS 认证失败时常见 ERR_SOCKS_CONNECTION_FAILED。
    """
    u = urlsplit(proxy_url)

    return bool(
        is_socks_proxy_scheme(u.scheme)
        and (u.username or u.password)
    )


# =============================================================================
# 代理 URL 解析（Python 侧）
# =============================================================================

def parse_proxy_url(proxy_url: str) -> ProxyCfg:
    """
    解析代理 URL 并返回 ProxyCfg。

    支持：
    - http://user:pass@host:port
    - https://user:pass@host:port
    - socks4://user:pass@host:port
    - socks4a://user:pass@host:port
    - socks5://user:pass@host:port
    - socks5h://user:pass@host:port

    注意：
    - 返回的 scheme 是 Chrome 实际使用的 scheme；
    - original_scheme 保留原始 scheme。
    """
    u = urlsplit(proxy_url)

    if not u.scheme or not u.hostname or not u.port:
        raise ValueError(f"Invalid proxy url: {proxy_url}")

    original_scheme = u.scheme.lower()
    chrome_scheme = normalize_proxy_scheme_for_chrome(original_scheme)

    username = unquote(u.username or "")
    password = unquote(u.password or "")

    warnings: list[str] = []

    if original_scheme in ("socks5h", "socks4a"):
        warnings.append(
            f"{original_scheme} normalized to {chrome_scheme}; "
            "Chrome proxy.settings cannot preserve remote-DNS semantics"
        )

    if is_socks_proxy_scheme(original_scheme) and (username or password):
        warnings.append(
            "authenticated SOCKS proxy may fail in Chrome; "
            "if ERR_SOCKS_CONNECTION_FAILED occurs, use local HTTP wrapper"
        )

    return ProxyCfg(
        scheme=chrome_scheme,
        original_scheme=original_scheme,
        host=u.hostname,
        port=int(u.port),
        username=username,
        password=password,
        chrome_support_warning="; ".join(warnings),
    )


def proxy_has_auth(proxy_url: str) -> bool:
    """
    判断代理 URL 是否包含 username/password。
    """
    u = urlsplit(proxy_url)
    return bool(u.username or u.password)


def proxy_server_without_auth(proxy_url: str) -> str:
    """
    返回不含 user:pass 的 proxy-server 值。

    示例：
        http://user:pass@host:port
    ->
        http://host:port
    """
    cfg = parse_proxy_url(proxy_url)
    return f"{cfg.scheme}://{cfg.host}:{cfg.port}"


# =============================================================================
# Manifest V3
# =============================================================================

def _manifest_v3(sw_filename: str) -> dict[str, Any]:
    """
    生成 MV3 的 manifest.json。

    权限解释：
    - proxy:
        允许扩展设置系统代理规则（chrome.proxy.settings.set）
    - storage:
        用于持久化 last_cfg / prev_cfg / restart_cmd（chrome.storage.local）
    - webRequest:
        允许监听网络请求事件（包括 onAuthRequired）
    - webRequestAuthProvider:
        MV3 中用于“提供认证凭据”的权限（注入 authCredentials）
    - host_permissions:
        webRequest 监听范围；<all_urls> 覆盖全部 URL

    注意：
    - 固定 key 保证 extension_id 可预测。
    """
    return {
        "name": "WPC Proxy Auth Helper (MV3)",
        "version": "0.9.0",
        "manifest_version": 3,
        "minimum_chrome_version": "108.0.0",
        "key": _FIXED_EXTENSION_PUBLIC_KEY_B64,
        "permissions": [
            "proxy",
            "storage",
            "webRequest",
            "webRequestAuthProvider",
        ],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": sw_filename},
        # 注意：不放开 unsafe-inline。health 页面改为外部 health.js 来规避 CSP。
    }


# =============================================================================
# 安全获取 browser.connection
# =============================================================================

def _get_browser_connection(browser) -> Any:
    """
    安全获取 browser.connection。
    - Windows/nodriver: 通常可用
    - Linux/headless/nodriver: 可能为 None
    """
    try:
        return getattr(browser, "connection", None)
    except Exception:
        return None


# =============================================================================
# 扩展代码：Service Worker + Health 页面
# =============================================================================

# 设计要点：
# 1) 顶层尽早加载持久化配置并 applyProxy()：减少重启后竞态
# 2) 引入 ts 防回滚：避免 storage.get 异步回调覆盖运行时切换
# 3) onAuthRequired：仅处理 details.isProxy == true（代理 407）
# 4) health：支持 query 参数 proxy=... / cmd=...；用于切换代理与写入 restart_cmd
# 5) 日志：在“页面请求(main_frame/sub_frame)”时打印当前账号信息（不含 password）
# 6) 回包：包含 prev/next、need_restart、restart_cmd，便于页面展示“两套配置 + 重启方案”
_SW_JS_TEMPLATE = r"""
"use strict";

const TAG = "[wpc-proxy-auth]";

// storage keys：仅保留“当前/切换前/重启命令”，不记录历史
const STORAGE_KEY_LAST_CFG   = "wpc_last_cfg_v1";     // next_cfg：重启后预计生效（当前 cfg）
const STORAGE_KEY_PREV_CFG   = "wpc_prev_cfg_v1";     // prev_cfg：切换前（用于“当前实际可能仍生效”展示）
const STORAGE_KEY_RESTARTCMD = "wpc_restart_cmd_v1";  // restart_cmd：来自 chrome://version command_line

/**
 * 当前内存配置的“最后更新时间戳”（revision）
 * 目的：避免 storage.get 的异步回调在运行时切换之后返回旧配置，覆盖新配置
 *
 * 说明：
 * - 运行时切换 applyConfigPatch() 会把 cfg._ts 与 self.WPC_CFG_TS 更新为 Date.now()
 * - storage.get 回调只在 saved.ts > self.WPC_CFG_TS 时才允许覆盖
 */
self.WPC_CFG_TS = (typeof self.WPC_CFG_TS === "number") ? self.WPC_CFG_TS : 0;

function log(...args) {
  console.log(TAG, ...args);
}

function normalizeChromeProxyScheme(scheme) {
  scheme = String(scheme || "").trim().toLowerCase();

  if (scheme === "socks5h") return "socks5";
  if (scheme === "socks4a") return "socks4";

  if (["http", "https", "socks4", "socks5"].includes(scheme)) return scheme;

  return "";
}

function isSocksScheme(scheme) {
  scheme = String(scheme || "").trim().toLowerCase();
  return ["socks4", "socks4a", "socks5", "socks5h"].includes(scheme);
}

function buildChromeSupportWarning(originalScheme, chromeScheme, username, password) {
  const warnings = [];

  originalScheme = String(originalScheme || "").trim().toLowerCase();
  chromeScheme = String(chromeScheme || "").trim().toLowerCase();

  if (originalScheme === "socks5h" || originalScheme === "socks4a") {
    warnings.push(
      `${originalScheme} normalized to ${chromeScheme}; ` +
      "Chrome proxy.settings cannot preserve remote-DNS semantics"
    );
  }

  if (isSocksScheme(originalScheme || chromeScheme) && (username || password)) {
    warnings.push(
      "authenticated SOCKS proxy may fail in Chrome; " +
      "if ERR_SOCKS_CONNECTION_FAILED occurs, use local HTTP wrapper"
    );
  }

  return warnings.join("; ");
}


/**
 * 当前身份摘要（不含 password）
 * - username：当前代理账号（可能为空）
 * - proxy_key：scheme://username@host:port（不含 password），便于识别
 */
function currentIdentity(cfgOpt) {
  const cfg = cfgOpt || self.WPC_CFG || {};
  const scheme = cfg.scheme || "";
  const originalScheme = cfg.original_scheme || cfg.scheme || "";
  const host = cfg.host || "";
  const port = cfg.port || 0;
  const username = cfg.username || "";

  const proxyKey = host && port
    ? (username
        ? `${scheme}://${username}@${host}:${port}`
        : `${scheme}://${host}:${port}`)
    : "";

  return {
    run_id: self.WPC_RUN_ID,
    scheme,
    original_scheme: originalScheme,
    username,
    proxy_key: proxyKey,
  };
}

/**
 * 是否打印身份信息（默认开启）
 * - 可通过 cfg.log_identity 控制（布尔）
 */
function shouldLogIdentity() {
  const cfg = self.WPC_CFG || {};
  return cfg.log_identity !== false; // 默认 true
}

/**
 * 配置摘要（避免回包泄露明文 password）
 */
function summarizeCfg(cfg) {
  cfg = cfg || {};

  const scheme = cfg.scheme || "";
  const originalScheme = cfg.original_scheme || cfg.scheme || "";
  const host = cfg.host || "";
  const port = cfg.port || 0;
  const username = cfg.username || "";
  const password = cfg.password || "";
  const hasAuth = !!(username || password);

  const proxyKey = host && port
    ? (username
        ? `${scheme}://${username}@${host}:${port}`
        : `${scheme}://${host}:${port}`)
    : "";

  return {
    scheme,
    original_scheme: originalScheme,
    host,
    port,
    username,
    has_auth: hasAuth,
    proxy_key: proxyKey,
    scope: "regular",
    bypass_len: Array.isArray(cfg.bypassList) ? cfg.bypassList.length : 0,
    persist_last_proxy: cfg.persist_last_proxy !== false, // 默认 true
    log_identity: cfg.log_identity !== false,             // 默认 true
    chrome_support_warning: cfg.chrome_support_warning || "",
    ts: (typeof cfg._ts === "number") ? cfg._ts : 0,
  };
}

function decodeURIComponentSafe(s) {
  try {
    return decodeURIComponent(String(s));
  } catch (e) {
    return String(s);
  }
}

/**
 * 宽松解析代理 URL：
 *   scheme://[user[:pass]@]host:port
 */
function parseProxyUrlLoose(proxyUrl) {
  try {
    if (!proxyUrl || typeof proxyUrl !== "string") return null;

    const s = proxyUrl.trim();
    const m = s.match(/^([a-zA-Z][a-zA-Z0-9+\-.]*):\/\/([^\/?#]+)$/);

    if (!m) return null;

    const originalScheme = (m[1] || "").toLowerCase();
    const chromeScheme = normalizeChromeProxyScheme(originalScheme);

    if (!chromeScheme) return null;

    let rest = m[2] || "";
    let authPart = "";
    let hostPort = rest;

    const at = rest.lastIndexOf("@");

    if (at >= 0) {
      authPart = rest.slice(0, at);
      hostPort = rest.slice(at + 1);
    }

    const hp = hostPort.match(/^(.+):(\d+)$/);

    if (!hp) return null;

    const host = (hp[1] || "").trim();
    const port = parseInt(hp[2], 10);

    if (!host || !port) return null;

    let username = "";
    let password = "";

    if (authPart) {
      const c = authPart.split(":");
      username = c[0] ? decodeURIComponentSafe(c[0]) : "";
      password = (c.length >= 2 && c[1] != null)
        ? decodeURIComponentSafe(c.slice(1).join(":"))
        : "";
    }

    return {
      scheme: chromeScheme,
      original_scheme: originalScheme,
      host,
      port,
      username,
      password,
      chrome_support_warning: buildChromeSupportWarning(
        originalScheme,
        chromeScheme,
        username,
        password
      ),
    };
  } catch (e) {
    return null;
  }
}

/**
 * 将代理配置应用到浏览器（fixed_servers + singleProxy）
 *
 * 说明：
 * - direct->fixed 的强制切换能提升“尽快走新代理”的概率
 * - 但 Chrome 仍可能复用旧隧道；因此 UI 明确以“重启进程”为可靠生效方案
 */
function applyProxy(tag) {
  try {
    const cfg = self.WPC_CFG || {};

    const scheme = normalizeChromeProxyScheme(cfg.scheme || "http");
    const host = cfg.host || "";
    const port = cfg.port || 0;
    const bypassList = cfg.bypassList || ["localhost", "127.0.0.1", "::1"];

    // 默认只要切换就强制 direct->fixed
    const force = (tag === "force_switch");

    if (shouldLogIdentity()) {
      const ident = currentIdentity(cfg);
      log("applyProxy()", tag, {
        run_id: ident.run_id,
        scheme,
        original_scheme: ident.original_scheme,
        host,
        port,
        username: ident.username,
        proxy_key: ident.proxy_key,
        chrome_support_warning: cfg.chrome_support_warning || "",
        ts: (typeof cfg._ts === "number") ? cfg._ts : 0,
        bypassList,
        force,
      });
    }

    if (!scheme || !host || !port) {
      log("applyProxy skipped: invalid scheme/host/port", { scheme, host, port });
      return;
    }

    const setFixed = () => {
      chrome.proxy.settings.set(
        {
          value: {
            mode: "fixed_servers",
            rules: {
              singleProxy: {
                scheme,
                host,
                port,
              },
              bypassList,
            },
          },
          scope: "regular",
        },
        () => {
          const err = chrome.runtime.lastError;

          if (err) {
            log("proxy.settings.set fixed failed:", err.message);
          } else {
            log("proxy.settings.set fixed OK", {
              scheme,
              host,
              port,
              chrome_support_warning: cfg.chrome_support_warning || "",
            });
          }
        }
      );
    };

    if (!force) {
      setFixed();
      return;
    }

    // 强制：先 direct 再 fixed
    chrome.proxy.settings.set(
      {
        value: {
          mode: "direct",
        },
        scope: "regular",
      },
      () => {
        const err = chrome.runtime.lastError;

        if (err) {
          log("proxy.settings.set direct failed:", err.message);
        } else {
          log("proxy.settings.set direct OK");
        }

        setTimeout(setFixed, 500);
      }
    );
  } catch (e) {
    log("applyProxy exception:", String(e));
  }
}

/**
 * 保存 next_cfg（最后一次代理配置）到 storage。
 * 备注：包含明文 password（用于自动认证）
 */
function persistLastCfgIfEnabled() {
  try {
    const cfg = self.WPC_CFG || {};

    if (cfg.persist_last_proxy === false) return;

    const ts = (typeof cfg._ts === "number" && cfg._ts > 0)
      ? cfg._ts
      : Date.now();

    const originalScheme = cfg.original_scheme || cfg.scheme || "";
    const chromeScheme = normalizeChromeProxyScheme(cfg.scheme || originalScheme || "http");

    const toSave = {
      scheme: chromeScheme,
      original_scheme: originalScheme,
      host: cfg.host || "",
      port: cfg.port || 0,
      username: cfg.username || "",
      password: cfg.password || "",
      bypassList: Array.isArray(cfg.bypassList)
        ? cfg.bypassList
        : ["localhost", "127.0.0.1", "::1"],
      persist_last_proxy: cfg.persist_last_proxy !== false,
      log_identity: cfg.log_identity !== false,
      chrome_support_warning: cfg.chrome_support_warning || "",
      scope: "regular",
      ts,
    };

    chrome.storage.local.set(
      {
        [STORAGE_KEY_LAST_CFG]: toSave,
      },
      () => {
        const err = chrome.runtime.lastError;

        if (err) {
          log("storage.set last_cfg failed:", err.message);
        } else {
          log("storage.set last_cfg OK", {
            next: summarizeCfg(self.WPC_CFG),
          });
        }
      }
    );
  } catch (e) {
    log("persistLastCfgIfEnabled exception:", String(e));
  }
}

/**
 * 保存 prev_cfg（切换前配置摘要）到 storage。
 * 说明：
 * - 仅用于 health 展示“当前实际可能仍生效”
 * - 不保存 password（避免额外落盘）
 */
function persistPrevCfg(summary) {
  try {
    const toSave = summary || null;

    if (!toSave) return;

    chrome.storage.local.set(
      {
        [STORAGE_KEY_PREV_CFG]: toSave,
      },
      () => {
        const err = chrome.runtime.lastError;

        if (err) {
          log("storage.set prev_cfg failed:", err.message);
        } else {
          log("storage.set prev_cfg OK", {
            prev: toSave,
          });
        }
      }
    );
  } catch (e) {
    log("persistPrevCfg exception:", String(e));
  }
}

/**
 * 保存 restart_cmd 到 storage（便于 health 页面展示与复制）
 */
function persistRestartCmd(cmd) {
  try {
    if (!cmd || typeof cmd !== "string") return;

    const s = cmd.trim();

    if (!s) return;

    chrome.storage.local.set(
      {
        [STORAGE_KEY_RESTARTCMD]: s,
      },
      () => {
        const err = chrome.runtime.lastError;

        if (err) {
          log("storage.set restart_cmd failed:", err.message);
        } else {
          log("storage.set restart_cmd OK");
        }
      }
    );
  } catch (e) {
    log("persistRestartCmd exception:", String(e));
  }
}

/**
 * 读取 restart_cmd（异步）
 */
function loadRestartCmd(cb) {
  try {
    chrome.storage.local.get(
      [STORAGE_KEY_RESTARTCMD],
      (items) => {
        const err = chrome.runtime.lastError;

        if (err) {
          cb("");
          return;
        }

        const v = items ? items[STORAGE_KEY_RESTARTCMD] : "";

        cb((typeof v === "string") ? v : "");
      }
    );
  } catch (e) {
    cb("");
  }
}

/**
 * 读取 prev_cfg（异步）
 */
function loadPrevCfg(cb) {
  try {
    chrome.storage.local.get(
      [STORAGE_KEY_PREV_CFG],
      (items) => {
        const err = chrome.runtime.lastError;

        if (err) {
          cb(null);
          return;
        }

        const v = items ? items[STORAGE_KEY_PREV_CFG] : null;

        cb((v && typeof v === "object") ? v : null);
      }
    );
  } catch (e) {
    cb(null);
  }
}

/**
 * 从 storage 读取 next_cfg（last_cfg），并覆盖到内存 cfg，再 applyProxy。
 * 关键：只接受“更新的”持久化配置（ts 防回滚）
 */
function loadPersistedCfgAndApply(tag) {
  try {
    chrome.storage.local.get(
      [STORAGE_KEY_LAST_CFG],
      (items) => {
        try {
          const err = chrome.runtime.lastError;

          if (err) {
            log("storage.get last_cfg failed:", err.message);
            return;
          }

          const saved = items ? items[STORAGE_KEY_LAST_CFG] : null;

          if (!saved || typeof saved !== "object") {
            log("storage.get last_cfg empty");
            return;
          }

          const savedTs = (saved && typeof saved.ts === "number") ? saved.ts : 0;
          const curTs = (typeof self.WPC_CFG_TS === "number") ? self.WPC_CFG_TS : 0;

          if (savedTs > 0 && curTs > 0 && savedTs <= curTs) {
            log("storage.get last_cfg ignored older/equal", {
              saved_ts: savedTs,
              current_ts: curTs,
              tag,
            });
            return;
          }

          const cfg = self.WPC_CFG || {};

          const originalScheme = saved.original_scheme || saved.scheme || cfg.original_scheme || cfg.scheme || "http";
          const chromeScheme = normalizeChromeProxyScheme(saved.scheme || originalScheme || "http");

          cfg.scheme = chromeScheme;
          cfg.original_scheme = originalScheme;
          cfg.host = saved.host || cfg.host || "";
          cfg.port = saved.port || cfg.port || 0;
          cfg.username = saved.username || cfg.username || "";
          cfg.password = (saved.password != null)
            ? String(saved.password)
            : (cfg.password || "");
          cfg.bypassList = Array.isArray(saved.bypassList)
            ? saved.bypassList
            : (cfg.bypassList || ["localhost", "127.0.0.1", "::1"]);
          cfg.persist_last_proxy = saved.persist_last_proxy !== false;
          cfg.log_identity = saved.log_identity !== false;
          cfg.chrome_support_warning = saved.chrome_support_warning || buildChromeSupportWarning(
            originalScheme,
            chromeScheme,
            cfg.username,
            cfg.password
          );
          cfg.scope = "regular";
          cfg._ts = savedTs || Date.now();

          self.WPC_CFG_TS = cfg._ts;
          self.WPC_CFG = cfg;

          log("loaded persisted last_cfg", tag, {
            next: summarizeCfg(self.WPC_CFG),
          });

          applyProxy("loadPersistedCfg");
        } catch (e) {
          log("storage.get last_cfg handler exception:", String(e));
        }
      }
    );
  } catch (e) {
    log("loadPersistedCfgAndApply exception:", String(e));
  }
}

/**
 * 应用 patch（来自 health 控制面），并返回结构化结果。
 * patch 支持：
 * - proxy_url：完整代理 URL（health.html?proxy=... 或输入框切换）
 * - restart_cmd：用于保存重启命令到 storage（来自 chrome://version command_line）
 */
function applyConfigPatch(patch) {
  const cfg = self.WPC_CFG || {};

  let switched = false;
  let switchErr = "";
  let needRestart = false;

  try {
    patch = patch || {};

    // 写入 restart_cmd（用于页面展示复制）
    if (patch.restart_cmd && typeof patch.restart_cmd === "string") {
      persistRestartCmd(patch.restart_cmd);
    }

    // 仅处理 proxy_url；其它字段维持默认
    if (patch.proxy_url && typeof patch.proxy_url === "string") {
      const parsed = parseProxyUrlLoose(patch.proxy_url);

      if (!parsed) {
        switchErr = "invalid proxy_url";
      } else {
        // 切换前摘要保存为 prev_cfg（用于展示当前实际可能仍生效）
        const prevSummary = summarizeCfg(cfg);
        persistPrevCfg(prevSummary);

        cfg.scheme = parsed.scheme;
        cfg.original_scheme = parsed.original_scheme || parsed.scheme;
        cfg.host = parsed.host;
        cfg.port = parsed.port;
        cfg.username = parsed.username || "";
        cfg.password = parsed.password || "";
        cfg.chrome_support_warning = parsed.chrome_support_warning || "";
        switched = true;
        needRestart = true; // 关键：切换后明确要求重启作为可靠生效方案
      }
    }

    cfg.bypassList = cfg.bypassList || ["localhost", "127.0.0.1", "::1"];
    cfg.scope = "regular";

    if (cfg.persist_last_proxy !== false) cfg.persist_last_proxy = true;
    if (cfg.log_identity !== false) cfg.log_identity = true;

    self.WPC_CFG = cfg;

    if (switched) {
      // 运行时切换先写 revision（防 storage 回调覆盖）
      const now = Date.now();

      cfg._ts = now;
      self.WPC_CFG_TS = now;
      self.WPC_CFG = cfg;

      // 默认强制 direct->fixed
      applyProxy("force_switch");

      // 持久化 next_cfg（last_cfg）
      persistLastCfgIfEnabled();
    }
  } catch (e) {
    switchErr = String(e);
  }

  return {
    switched,
    need_restart: needRestart,
    switch_err: switchErr,
    next: summarizeCfg(self.WPC_CFG),
  };
}

/**
 * 组合返回给 health 的信息：
 * - next：当前 cfg 摘要（重启后预计生效）
 * - prev：切换前 cfg 摘要（当前实际可能仍生效）
 * - restart_cmd：用于重启复制
 */
function buildHealthPayload(base) {
  return new Promise((resolve) => {
    const payload = base || {};

    payload.ok = true;
    payload.run_id = self.WPC_RUN_ID;
    payload.ts = Date.now();

    // next：当前 cfg
    payload.next = summarizeCfg(self.WPC_CFG);

    // prev + restart_cmd 需要从 storage 读取
    loadPrevCfg((prev) => {
      payload.prev = prev || null;
      
      // 若当前有要切换的代理，则需要重启才能生效
      if (prev) {
        payload.need_restart = true;
      }
      
      loadRestartCmd((cmd) => {
        payload.restart_cmd = cmd || "";
        resolve(payload);
      });
    });
  });
}

/**
 * health / 探活 / 控制面：
 * - wpc_ping：只唤醒并回包
 * - wpc_config：可带 patch.proxy_url / patch.restart_cmd
 *
 * 回包包含：
 * - prev / next
 * - need_restart（切换发生时为 true）
 * - restart_cmd
 */
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  try {
    if (msg && msg.type === "wpc_ping") {
      applyProxy("wpc_ping");

      buildHealthPayload({
        action: "ping",
      }).then((p) => sendResponse(p));

      return true;
    }

    if (msg && msg.type === "wpc_config") {
      const patch = (msg.patch && typeof msg.patch === "object")
        ? msg.patch
        : {};

      const r = applyConfigPatch(patch);

      buildHealthPayload({
        action: "config",
        switched: r.switched,
        switch_err: r.switch_err || "",
        // next 已在 buildHealthPayload 中覆盖为当前 cfg；此处保留字段便于诊断
      }).then((p) => sendResponse(p));

      return true;
    }
  } catch (e) {
    log("onMessage exception:", String(e));
  }

  buildHealthPayload({
    ok: false,
    action: "unknown",
  }).then((p) => sendResponse(p));

  return true;
});

/**
 * 顶层：SW 加载
 * - 先尝试从 storage 恢复 last_cfg（next_cfg）
 * - 再 applyProxy（立即执行一次，减少无代理窗口）
 */
log("service worker loaded", {
  run_id: self.WPC_RUN_ID,
  initial_cfg: summarizeCfg(self.WPC_CFG),
});

loadPersistedCfgAndApply("top-level");
applyProxy("top-level");

chrome.runtime.onInstalled.addListener((details) => {
  log("onInstalled", {
    run_id: self.WPC_RUN_ID,
    details,
  });

  loadPersistedCfgAndApply("onInstalled");
  applyProxy("onInstalled");
});

chrome.runtime.onStartup.addListener(() => {
  log("onStartup", {
    run_id: self.WPC_RUN_ID,
  });

  loadPersistedCfgAndApply("onStartup");
  applyProxy("onStartup");
});

/**
 * 页面请求日志：
 * - 仅记录 main_frame/sub_frame，避免刷屏
 * - 同时打印当前账号信息（不含 password）
 */
chrome.webRequest.onBeforeRequest.addListener(
  (d) => {
    try {
      if (!shouldLogIdentity()) return;

      const ident = currentIdentity(self.WPC_CFG);

      log("onBeforeRequest", {
        run_id: ident.run_id,
        url: d.url,
        type: d.type,
        username: ident.username,
        proxy_key: ident.proxy_key,
        scheme: ident.scheme,
        original_scheme: ident.original_scheme,
      });
    } catch (e) {}
  },
  {
    urls: ["<all_urls>"],
    types: ["main_frame", "sub_frame"],
  }
);

/**
 * 代理认证处理（HTTP 407 Proxy Authentication Required）
 */
chrome.webRequest.onAuthRequired.addListener(
  (details, callback) => {
    try {
      const cfg = self.WPC_CFG || {};
      const ident = currentIdentity(cfg);

      if (shouldLogIdentity()) {
        log("onAuthRequired fired", {
          run_id: ident.run_id,
          url: details.url,
          isProxy: details.isProxy,
          statusLine: details.statusLine,
          challenger: details.challenger,
          realm: details.realm,
          username: ident.username,
          proxy_key: ident.proxy_key,
          scheme: ident.scheme,
          original_scheme: ident.original_scheme,
          chrome_support_warning: cfg.chrome_support_warning || "",
        });
      }

      if (details.isProxy !== true) {
        callback({});
        return;
      }

      const username = cfg.username || "";
      const password = cfg.password || "";

      if (!username && !password) {
        if (shouldLogIdentity()) {
          log("onAuthRequired: empty credentials, skip", {
            run_id: ident.run_id,
            username: ident.username,
            proxy_key: ident.proxy_key,
          });
        }

        callback({});
        return;
      }

      if (shouldLogIdentity()) {
        log("onAuthRequired: supplying credentials", {
          run_id: ident.run_id,
          username: ident.username,
          proxy_key: ident.proxy_key,
          chrome_support_warning: cfg.chrome_support_warning || "",
        });
      }

      callback({
        authCredentials: {
          username,
          password,
        },
      });
    } catch (e) {
      callback({});
    }
  },
  {
    urls: ["<all_urls>"],
  },
  ["asyncBlocking"]
);

chrome.webRequest.onCompleted.addListener(
  (d) => {
    try {
      if (typeof d.statusCode === "number" && d.statusCode >= 400) {
        if (!shouldLogIdentity()) return;

        const cfg = self.WPC_CFG || {};
        const ident = currentIdentity(cfg);

        log("onCompleted ERR", {
          run_id: ident.run_id,
          url: d.url,
          statusCode: d.statusCode,
          fromCache: d.fromCache,
          username: ident.username,
          proxy_key: ident.proxy_key,
          scheme: ident.scheme,
          original_scheme: ident.original_scheme,
          chrome_support_warning: cfg.chrome_support_warning || "",
        });
      }
    } catch (e) {}
  },
  {
    urls: ["<all_urls>"],
    types: ["main_frame", "sub_frame"],
  }
);

chrome.webRequest.onErrorOccurred.addListener(
  (d) => {
    try {
      if (!shouldLogIdentity()) return;

      const cfg = self.WPC_CFG || {};
      const ident = currentIdentity(cfg);

      log("onErrorOccurred", {
        run_id: ident.run_id,
        url: d.url,
        error: d.error,
        username: ident.username,
        proxy_key: ident.proxy_key,
        scheme: ident.scheme,
        original_scheme: ident.original_scheme,
        chrome_support_warning: cfg.chrome_support_warning || "",
      });
    } catch (e) {}
  },
  {
    urls: ["<all_urls>"],
    types: ["main_frame", "sub_frame"],
  }
);
"""


# =============================================================================
# health.html / health.js（MV3 CSP 兼容：无 inline script）
# =============================================================================

_HEALTH_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>WPC Proxy Auth - Health</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
</head>
<body>
  <h2>WPC Proxy Auth - Health</h2>

  <p>
    该页面用于：<b>探活</b> / <b>查看代理配置</b> / <b>切换代理</b> / <b>展示重启命令</b> / <b>查询出口公网 IP</b>。
  </p>

  <hr />

  <h3>使用说明</h3>
  <ul>
    <li>
      URL 方式切换：
      <br />
      <code>.../health.html?proxy=http://user:pass@host:port</code>
    </li>
    <li>
      支持解析但会规整的 scheme：
      <br />
      <code>socks5h://</code> 会规整为 <code>socks5://</code>；
      <code>socks4a://</code> 会规整为 <code>socks4://</code>。
    </li>
    <li>
      注意：
      <br />
      Chrome proxy.settings 不能完整保留 <code>socks5h</code> 的远端 DNS 语义。
      SOCKS 带账号密码在 Chrome 中也可能不稳定。
      如果出现 <code>ERR_SOCKS_CONNECTION_FAILED</code>，建议改用本地 HTTP wrapper。
    </li>
    <li>
      URL 方式注入重启命令（由自动化探活写入 storage，用于复制重启）：
      <br />
      <code>.../health.html?cmd=&lt;urlencoded command_line&gt;</code>
    </li>
    <li>
      参数可组合：
      <br />
      <code>.../health.html?proxy=...&amp;cmd=...</code>
    </li>
    <li>
      编码注意：
      <br />
      如果账号/密码包含 <code>&amp;</code>、<code>#</code>、空格等特殊字符，请进行 URL 编码（空格使用 <code>%20</code>）。
    </li>
    <li>
      切换后 IP 不变的常见原因：
      <br />
      Chrome 可能复用旧代理隧道/连接池；扩展无法保证立刻断开旧连接。
      <b>切换后需重启浏览器进程才能更可靠地建立新隧道/IP</b>。
    </li>
  </ul>

  <hr />

  <h3>切换代理</h3>
  <p>
    代理 URL 示例：<code>http://user:pass@host:port</code>
  </p>

  <div>
    <input id="proxyInput" type="text" style="width: 96%;" placeholder="粘贴 proxy url，例如：http://user:pass@host:port" />
  </div>

  <p>
    <button id="btnSwitch">切换代理</button>
    <button id="btnPing">刷新状态</button>
    <button id="btnFillFromUrl">从 URL 参数填充</button>
  </p>

  <div id="banner" style="white-space: pre-wrap;"></div>

  <hr />

  <h3>重启命令（切换后预计生效方案）</h3>
  <p>
    切换发生后通常需要重启进程以确保新代理隧道/IP 生效。
  </p>

  <div style="white-space: pre-wrap;">
    <div><b>need_restart</b>: <span id="needRestart">-</span></div>
    <div><b>restart_cmd</b>:</div>
    <pre id="restartCmd" style="white-space: pre-wrap;">-</pre>
    <button id="btnCopyRestart">一键复制重启命令</button>
    <button id="btnOpenVersion">打开 chrome://version</button>
  </div>

  <hr />

  <h3>代理配置展示</h3>

  <h4>当前实际生效（可能仍复用旧隧道）</h4>
  <div style="white-space: pre-wrap;">
    <div><b>proxy_key</b>: <span id="curProxyKey">-</span></div>
    <div><b>username</b>: <span id="curUsername">-</span></div>
    <div><b>scheme</b>: <span id="curScheme">-</span></div>
    <div><b>original_scheme</b>: <span id="curOriginalScheme">-</span></div>
    <div><b>host</b>: <span id="curHost">-</span></div>
    <div><b>port</b>: <span id="curPort">-</span></div>
    <div><b>warning</b>: <span id="curWarning">-</span></div>
    <div><b>ts</b>: <span id="curTs">-</span></div>
  </div>

  <h4>重启后预计生效（next）</h4>
  <div style="white-space: pre-wrap;">
    <div><b>proxy_key</b>: <span id="nextProxyKey">-</span></div>
    <div><b>username</b>: <span id="nextUsername">-</span></div>
    <div><b>scheme</b>: <span id="nextScheme">-</span></div>
    <div><b>original_scheme</b>: <span id="nextOriginalScheme">-</span></div>
    <div><b>host</b>: <span id="nextHost">-</span></div>
    <div><b>port</b>: <span id="nextPort">-</span></div>
    <div><b>persist_last_proxy</b>: <span id="nextPersist">-</span></div>
    <div><b>warning</b>: <span id="nextWarning">-</span></div>
    <div><b>ts</b>: <span id="nextTs">-</span></div>
  </div>

  <hr />

  <h3>出口公网 IP（通过当前实际链路请求）</h3>
  <p>
    下方按钮会从页面发起请求到公网 IP 服务，结果反映“当前这次请求实际走出去的出口 IP”。
    若切换后仍显示旧 IP，则符合“需重启进程”的预期。
  </p>

  <p>
    <button id="btnCheckIp">查询当前出口 IP</button>
  </p>

  <pre id="ipOut" style="white-space: pre-wrap;">-</pre>

  <hr />

  <h3>原始返回（resp JSON）</h3>
  <pre id="raw" style="white-space: pre-wrap;">loading...</pre>

  <script src="./health.js"></script>
</body>
</html>
"""


# =============================================================================
# health.js
# =============================================================================

_HEALTH_JS = r"""
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  const elProxyInput = $("proxyInput");
  const elBtnSwitch = $("btnSwitch");
  const elBtnPing = $("btnPing");
  const elBtnFillFromUrl = $("btnFillFromUrl");

  const elBanner = $("banner");

  const elNeedRestart = $("needRestart");
  const elRestartCmd = $("restartCmd");
  const elBtnCopyRestart = $("btnCopyRestart");
  const elBtnOpenVersion = $("btnOpenVersion");

  const elCurProxyKey = $("curProxyKey");
  const elCurUsername = $("curUsername");
  const elCurScheme = $("curScheme");
  const elCurOriginalScheme = $("curOriginalScheme");
  const elCurHost = $("curHost");
  const elCurPort = $("curPort");
  const elCurWarning = $("curWarning");
  const elCurTs = $("curTs");

  const elNextProxyKey = $("nextProxyKey");
  const elNextUsername = $("nextUsername");
  const elNextScheme = $("nextScheme");
  const elNextOriginalScheme = $("nextOriginalScheme");
  const elNextHost = $("nextHost");
  const elNextPort = $("nextPort");
  const elNextPersist = $("nextPersist");
  const elNextWarning = $("nextWarning");
  const elNextTs = $("nextTs");

  const elBtnCheckIp = $("btnCheckIp");
  const elIpOut = $("ipOut");

  const elRaw = $("raw");

  function setBanner(msg, isError) {
    if (!msg) {
      elBanner.textContent = "";
      return;
    }

    elBanner.textContent = (isError ? "[ERROR] " : "[INFO] ") + msg;
  }

  function safeJson(obj) {
    try {
      return JSON.stringify(obj, null, 2);
    } catch (e) {
      return String(obj);
    }
  }

  /**
   * 更宽容读取 query 参数：
   * - 避免 URLSearchParams 因非法 %xx 抛异常
   * - '+' 按字面处理（替换为 %2B），避免被当空格
   */
  function readQueryRaw(keyName) {
    try {
      const qs = (location.search || "").replace(/^\?/, "");

      if (!qs) return "";

      const key = keyName + "=";
      const idx = qs.indexOf(key);

      if (idx < 0) return "";

      let val = qs.slice(idx + key.length);
      const amp = val.indexOf("&");

      if (amp >= 0) {
        val = val.slice(0, amp);
      }

      val = val.replace(/\+/g, "%2B");

      try {
        return decodeURIComponent(val).trim();
      } catch (e) {
        return (val || "").trim();
      }
    } catch (e) {
      return "";
    }
  }

  function normalizeCfg(obj) {
    obj = obj || {};

    return {
      proxy_key: (obj.proxy_key != null) ? String(obj.proxy_key) : "-",
      username: (obj.username != null) ? String(obj.username) : "-",
      scheme: (obj.scheme != null) ? String(obj.scheme) : "-",
      original_scheme: (obj.original_scheme != null) ? String(obj.original_scheme) : "-",
      host: (obj.host != null) ? String(obj.host) : "-",
      port: (obj.port != null) ? String(obj.port) : "-",
      ts: (obj.ts != null) ? String(obj.ts) : "-",
      persist_last_proxy: (obj.persist_last_proxy != null) ? String(obj.persist_last_proxy) : "-",
      chrome_support_warning: (obj.chrome_support_warning != null)
        ? String(obj.chrome_support_warning || "-")
        : "-",
    };
  }
    
  /**
  * 根据是否拿到 command_line(cmd) 来切换 UI：
  * - cmd 为空：隐藏“一键复制”，显示“打开 chrome://version 获取”
  * - cmd 非空：显示“一键复制”，隐藏“打开 chrome://version 获取”
  *
  * 约定：
  * - elBtnCopyRestart：按钮元素（点击复制 cmd）
  * - elBtnOpenVersion：按钮元素（点击打开 chrome://version）
  * - elRestartCmd：展示命令行的文本区域/输入框（可选）
  */
  function updateCmdUI(cmd) {
    cmd = (cmd || "").trim();

    // 工具函数：显隐
    function show(el) {
      if (!el) return;
      el.style.display = "";
      el.disabled = false;
    }

    function hide(el) {
      if (!el) return;
      el.style.display = "none";
      el.disabled = true;
    }

    // 若获取到 cmd ，则展示一键复制按钮，否则展示获取按钮
    if (cmd === "") {
      hide(elBtnCopyRestart);
      show(elBtnOpenVersion);
    } else {
      show(elBtnCopyRestart);
      hide(elBtnOpenVersion);
    }
  }

  function render(resp) {
    elRaw.textContent = safeJson(resp);

    const needRestart = !!(resp && resp.need_restart);
    elNeedRestart.textContent = needRestart ? "true" : "false";

    // restart_cmd（从 storage 读取到的）
    const cmd = (resp && resp.restart_cmd) ? String(resp.restart_cmd) : "";

    elRestartCmd.textContent = cmd || (
      "未成功获取 restart_cmd，点击下方“打开 chrome://version”，" +
      "复制页面中“命令行 / command_line”那一行的全文作为重启命令。"
    );

    // 更新cmd按钮
    updateCmdUI(cmd);

    // next：重启后预计生效
    const next = normalizeCfg(resp && resp.next);

    elNextProxyKey.textContent = next.proxy_key;
    elNextUsername.textContent = next.username;
    elNextScheme.textContent = next.scheme;
    elNextOriginalScheme.textContent = next.original_scheme;
    elNextHost.textContent = next.host;
    elNextPort.textContent = next.port;
    elNextPersist.textContent = next.persist_last_proxy;
    elNextWarning.textContent = next.chrome_support_warning;
    elNextTs.textContent = next.ts;

    // prev：当前实际可能仍生效（若不存在 prev，则退化为 next 展示）
    const prevObj = (resp && resp.prev) ? resp.prev : null;
    const cur = normalizeCfg(prevObj || (resp && resp.next));

    elCurProxyKey.textContent = cur.proxy_key;
    elCurUsername.textContent = cur.username;
    elCurScheme.textContent = cur.scheme;
    elCurOriginalScheme.textContent = cur.original_scheme;
    elCurHost.textContent = cur.host;
    elCurPort.textContent = cur.port;
    elCurWarning.textContent = cur.chrome_support_warning;
    elCurTs.textContent = cur.ts;

    const warning = next.chrome_support_warning && next.chrome_support_warning !== "-"
      ? "\n\nChrome 代理支持提示：" + next.chrome_support_warning
      : "";

    if (resp && resp.switched) {
      if (needRestart) {
        setBanner(
          "代理已切换并持久化。Chrome 可能复用旧隧道；建议使用 restart_cmd 重启进程。" + warning,
          false
        );
      } else {
        setBanner("代理已切换。" + warning, false);
      }
      return;
    }

    if (resp && resp.ok) {
      setBanner("状态已刷新。" + warning, false);
      return;
    }

    setBanner("未获取到有效响应，可能 Service Worker 未加载或消息被拦截。", true);
  }

  function sendMessage(msg, onOk) {
    chrome.runtime.sendMessage(msg, (resp) => {
      const err = chrome.runtime.lastError;

      if (err) {
        setBanner("sendMessage 失败：" + err.message, true);
        elRaw.textContent = "sendMessage error: " + err.message;
        return;
      }

      onOk(resp);
    });
  }

  function sendPing() {
    setBanner("正在刷新状态...", false);
    elRaw.textContent = "pinging service worker...";

    sendMessage(
      {
        type: "wpc_ping",
      },
      (resp) => render(resp)
    );
  }

  function sendConfig(proxyUrl, restartCmdMaybe) {
    const patch = {};

    if (proxyUrl && typeof proxyUrl === "string" && proxyUrl.trim()) {
      patch.proxy_url = proxyUrl.trim();
    }

    if (restartCmdMaybe && typeof restartCmdMaybe === "string" && restartCmdMaybe.trim()) {
      patch.restart_cmd = restartCmdMaybe.trim();
    }

    setBanner("正在发送配置...", false);
    elRaw.textContent = "sending config...";

    sendMessage(
      {
        type: "wpc_config",
        patch,
      },
      (resp) => render(resp)
    );
  }

  /**
   * 一键复制（优先 clipboard API；失败则降级到 execCommand）
   */
  async function copyText(text) {
    try {
      if (!text) return false;

      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) {
      // ignore
    }

    try {
      const ta = document.createElement("textarea");

      ta.value = text;
      ta.setAttribute("readonly", "readonly");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";

      document.body.appendChild(ta);
      ta.select();

      const ok = document.execCommand("copy");

      document.body.removeChild(ta);

      return !!ok;
    } catch (e) {
      return false;
    }
  }

  /**
   * 查询出口 IP（多源兜底）
   * - 结果反映“当前页面请求实际走出去的出口 IP”
   * - 若切换后仍为旧 IP，符合“需重启进程”的预期
   */
  async function checkIp() {
    elIpOut.textContent = "checking...";

    const results = [];

    async function tryFetch(name, url, parser) {
      try {
        const r = await fetch(url, {
          method: "GET",
          cache: "no-store",
        });

        const txt = await r.text();
        const v = parser(txt);

        results.push({
          source: name,
          ok: true,
          value: v,
        });
      } catch (e) {
        results.push({
          source: name,
          ok: false,
          error: String(e),
        });
      }
    }

    // ipify（JSON，通常 CORS 友好）
    await tryFetch("ipify", "https://api.ipify.org?format=json", (txt) => {
      try {
        const j = JSON.parse(txt);
        return j && j.ip ? j.ip : txt;
      } catch (e) {
        return txt;
      }
    });

    // ipinfo（JSON，部分环境可能限流/CORS；失败会展示错误）
    await tryFetch("ipinfo", "https://ipinfo.io/json", (txt) => {
      try {
        const j = JSON.parse(txt);
        return j;
      } catch (e) {
        return txt;
      }
    });

    // ifconfig.me（可能 CORS 不稳定；作为兜底）
    await tryFetch("ifconfig.me", "https://ifconfig.me/ip", (txt) => (txt || "").trim());

    elIpOut.textContent = safeJson({
      note: "该结果反映当前页面请求实际走出去的出口 IP（即“当前实际生效链路”）",
      ts: Date.now(),
      results,
    });
  }

  // 按钮绑定
  elBtnPing.addEventListener("click", () => sendPing());

  elBtnFillFromUrl.addEventListener("click", () => {
    const p = readQueryRaw("proxy");

    if (p) {
      elProxyInput.value = p;
      setBanner("已从 URL 参数 proxy=... 填充到输入框。", false);
    } else {
      setBanner("URL 中未找到 proxy=... 参数。", true);
    }
  });

  elBtnSwitch.addEventListener("click", () => {
    const proxyUrl = (elProxyInput.value || "").trim();

    if (!proxyUrl) {
      setBanner("proxy url 不能为空。", true);
      return;
    }

    // 若 URL 中带 cmd=...（通常由自动化探活注入），同步写入 storage
    const cmd = readQueryRaw("cmd");

    sendConfig(proxyUrl, cmd);
  });

  elBtnCopyRestart.addEventListener("click", async () => {
    const text = (elRestartCmd.textContent || "").trim();

    if (!text || text === "-") {
      setBanner("restart_cmd 为空，无法复制。", true);
      return;
    }

    const ok = await copyText(text);

    setBanner(ok ? "已复制重启命令。" : "复制失败（可能权限受限）。", !ok);
  });

  // 按钮：打开 chrome://version
  elBtnOpenVersion.addEventListener("click", () => {
    try {
      // chrome://version 允许在浏览器中打开（不依赖扩展权限）
      chrome.tabs.create({
        url: "chrome://version/",
      });
    } catch (e) {
      // 兜底：直接 location 跳转（可能被拦截，但不影响）
      try {
        window.open("chrome://version/");
      } catch (e2) {}
    }
  });

  elBtnCheckIp.addEventListener("click", () => {
    checkIp();
  });

  // 初始化：
  // - 若存在 cmd=...：优先写入 restart_cmd（不改变代理），便于页面立刻可复制
  // - 若存在 proxy=...：自动切换代理（并提示 need_restart）
  // - 否则 ping 刷新
  (function init() {
    const proxyUrl = readQueryRaw("proxy");
    const cmd = readQueryRaw("cmd");

    if (proxyUrl) {
      elProxyInput.value = proxyUrl;
      setBanner("检测到 URL proxy 参数，将自动切换代理...", false);
      sendConfig(proxyUrl, cmd);
      return;
    }

    if (cmd) {
      setBanner("检测到 URL cmd 参数，将写入 restart_cmd...", false);
      sendConfig("", cmd);
      return;
    }

    sendPing();
  })();
})();
"""


# =============================================================================
# 写入扩展
# =============================================================================

def _infer_run_id_from_ext_dir(ext_dir: Path) -> str:
    """
    从 ext_dir 推导 run_id。

    ext_dir 结构固定：
        .../wpc_tmp/<run_id>/ext
    """
    try:
        return ext_dir.parent.name
    except Exception:
        return "unknown_run_id"


def write_proxy_auth_extension(ext_dir: Path, proxy_cfg: ProxyCfg) -> None:
    """
    写入 MV3 扩展到 ext_dir。

    写入文件：
    - manifest.json
    - wpc_sw.js（service worker，内嵌 run_id + 初始代理配置）
    - health.html
    - health.js（MV3 CSP 兼容：避免 inline script 被拦截）
    """
    ext_dir.mkdir(parents=True, exist_ok=True)

    run_id = _infer_run_id_from_ext_dir(ext_dir)

    # 1) manifest.json
    manifest = _manifest_v3(_SW_FILENAME)

    (ext_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 2) 初始代理配置对象：供 SW 使用
    #    - persist_last_proxy: 默认开启（重启后自动沿用最后一次切换）
    #    - log_identity: 默认开启（在页面请求/认证/错误时打印当前账号信息）
    #    - _ts: 初始 revision（真正切换时会更新为 Date.now()）
    cfg_obj = {
        "scheme": proxy_cfg.scheme,
        "original_scheme": proxy_cfg.original_scheme or proxy_cfg.scheme,
        "host": proxy_cfg.host,
        "port": proxy_cfg.port,
        "username": proxy_cfg.username,
        "password": proxy_cfg.password,
        "chrome_support_warning": proxy_cfg.chrome_support_warning,
        "bypassList": ["localhost", "127.0.0.1", "::1"],
        "persist_last_proxy": True,
        "log_identity": True,
        "scope": "regular",
        "_ts": 0,
    }

    # 3) service worker JS：先注入全局变量，再拼接逻辑模板
    sw_prefix = (
        f"self.WPC_RUN_ID = {json.dumps(run_id)};\n"
        f"self.WPC_CFG = {json.dumps(cfg_obj, ensure_ascii=False)};\n\n"
    )

    sw_js = sw_prefix + _SW_JS_TEMPLATE.strip() + "\n"

    (ext_dir / _SW_FILENAME).write_text(sw_js, encoding="utf-8")

    # 4) health.html（注意：不允许 inline script）
    (ext_dir / _HEALTH_FILENAME).write_text(_HEALTH_HTML, encoding="utf-8")

    # 5) health.js（与 health.html 配套）
    (ext_dir / _HEALTH_JS_FILENAME).write_text(_HEALTH_JS.strip() + "\n", encoding="utf-8")


# =============================================================================
# 探活：以 command_line 的 --load-extension 为锚点推导 run_id
# =============================================================================

async def _read_command_line_from_chrome_version(
    browser,
    timeout: float = 2.5,
    visible_probe: bool = False,
) -> str:
    """
    打开 chrome://version 读取 “命令行(command_line)” 文本。

    关键点：
    - 不依赖页面语言（中文/英文都可用）
    - 直接读取 id="command_line" 的内容
    """
    try:
        tab = await browser.get("chrome://version/")

        if visible_probe:
            await asyncio.sleep(0.3)

        deadline = asyncio.get_event_loop().time() + timeout

        js = r"""
(() => {
  const el = document.getElementById("command_line");
  if (el && el.innerText) return el.innerText.trim();
  if (el && el.textContent) return el.textContent.trim();
  return "";
})()
"""

        while asyncio.get_event_loop().time() < deadline:
            try:
                v = await tab.evaluate(js)
            except Exception:
                v = ""

            if isinstance(v, str) and v.strip():
                return v.strip()

            await asyncio.sleep(0.15)

        return ""
    except Exception:
        return ""


def _extract_load_extension_path(command_line: str) -> str:
    """
    从命令行字符串提取 --load-extension 的参数值（扩展目录）。

    支持形态：
    - --load-extension="C:/.../wpc_tmp/<run_id>/ext"
    - --load-extension=C:/.../wpc_tmp/<run_id>/ext
    - --load-extension "C:/.../wpc_tmp/<run_id>/ext"
    """
    if not command_line:
        return ""

    s = command_line

    # 1) --load-extension="...":
    key1 = '--load-extension="'
    i = s.find(key1)

    if i >= 0:
        j = s.find('"', i + len(key1))

        if j > i:
            return s[i + len(key1):j].strip()

    # 2) --load-extension=...
    key2 = "--load-extension="
    i = s.find(key2)

    if i >= 0:
        rest = s[i + len(key2):].lstrip()

        if rest.startswith('"'):
            j = rest.find('"', 1)
            if j > 1:
                return rest[1:j].strip()

        parts = rest.split()
        return (parts[0] if parts else "").strip().strip('"')

    # 3) --load-extension "..."
    key3 = "--load-extension"
    i = s.find(key3)

    if i >= 0:
        rest = s[i + len(key3):].lstrip()

        if rest.startswith('"'):
            j = rest.find('"', 1)
            if j > 1:
                return rest[1:j].strip()

        parts = rest.split()
        return (parts[0] if parts else "").strip().strip('"')

    return ""


def _infer_run_id_from_load_extension_path(ext_path: str) -> str:
    """
    以 --load-extension 参数为锚点推导 run_id。

    期望 ext_path 形如：
        .../wpc_tmp/<run_id>/ext
    """
    try:
        if not ext_path:
            return ""

        p = Path(ext_path)

        if p.name.lower() == "ext":
            return p.parent.name

        parts_lower = [str(x).lower() for x in p.parts]

        if "wpc_tmp" in parts_lower:
            idx = parts_lower.index("wpc_tmp")

            if idx + 1 < len(p.parts):
                return p.parts[idx + 1]

        return ""
    except Exception:
        return ""


# =============================================================================
# CDP 枚举 Extension SW target
# =============================================================================

async def _list_extension_candidate_ids(browser) -> list[str]:
    """
    从 CDP targets 中提取候选扩展 id 列表。

    兼容性说明（关键）：
    - 原生 CDP：Target.getTargets() 返回 dict，形如 {"targetInfos":[...]}
    - nodriver 等封装：可能直接返回 list[TargetInfo]（你看到的就是这种）
    - 也可能返回 list[dict] 或者返回一个对象，其中包含 targetInfos 属性

    提取规则：
    - 只收集 Service Worker targets
    - 且 URL 以 "chrome-extension://" 开头
    - ext_id 从 URL 的 netloc 得到：chrome-extension://<ext_id>/...

    注意：
    - connection 不可用时返回空列表，不抛异常
    """
    conn = _get_browser_connection(browser)

    if conn is None:
        return []

    try:
        resp = await conn.send(cdp.target.get_targets())
    except Exception:
        return []

    # -------- 统一“展开 targets 列表” --------
    targets: list[Any] = []

    if resp is None:
        targets = []
    elif isinstance(resp, dict):
        # 原生 CDP 返回
        targets = resp.get("targetInfos") or resp.get("target_infos") or []
    elif isinstance(resp, (list, tuple)):
        # nodriver 常见：直接给 list[TargetInfo]
        targets = list(resp)
    else:
        # 兜底：对象上可能有 targetInfos 属性
        v = getattr(resp, "targetInfos", None) or getattr(resp, "target_infos", None)

        if isinstance(v, (list, tuple)):
            targets = list(v)

    # -------- 工具：统一读取字段（兼容 dict / dataclass / namedtuple-like） --------
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default

        if isinstance(obj, dict):
            return obj.get(key, default)

        return getattr(obj, key, default)

    ids: list[str] = []

    for t in targets:
        # nodriver: type_；原生: type
        t_type = _get(t, "type_", None) or _get(t, "type", None) or ""

        if t_type != "service_worker":
            continue

        url = _get(t, "url", "") or ""
        if not isinstance(url, str) or not url.startswith("chrome-extension://"):
            continue

        ext_id = urlsplit(url).netloc  # chrome-extension://<ext_id>/...
        if ext_id:
            ids.append(ext_id)

    # 去重（保序）
    seen = set()
    uniq: list[str] = []

    for x in ids:
        if x not in seen:
            seen.add(x)
            uniq.append(x)

    return uniq


# =============================================================================
# 构造最终候选 extension_id 列表（CDP + 固定 fallback）
# =============================================================================

async def _build_extension_candidate_ids(browser) -> list[str]:
    """
    构造扩展候选 ID：
    1. CDP 枚举到的 service worker extension_id；
    2. 固定 extension_id fallback。
    """
    ids: list[str] = []

    try:
        ids.extend(await _list_extension_candidate_ids(browser))
    except Exception:
        pass

    if _FIXED_EXTENSION_ID:
        ids.append(_FIXED_EXTENSION_ID)

    seen = set()
    uniq: list[str] = []

    for x in ids:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)

    return uniq


# =============================================================================
# health 探活
# =============================================================================

def _looks_like_health_payload(text: str) -> bool:
    """
    判断文本是否像 health.js 回填的探活 JSON。

    为什么不用简单的 `"ok" in text`：
    - 静态页面里可能出现 ok / raw / resp 等字样；
    - 但 #raw 的内容如果是 JSON，才说明 health.js 已经执行，且 Service Worker 有机会响应。

    返回：
    - True:
        text 看起来是 health payload；
    - False:
        text 仍然像静态页面文本或 loading。
    """
    if not text:
        return False

    s = text.strip()

    if not s:
        return False

    if s.lower() == "loading...":
        return False

    # JSON 形式是最可靠的。
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)

            return isinstance(obj, dict) and (
                "ok" in obj
                or "run_id" in obj
                or "action" in obj
                or "next" in obj
            )
        except Exception:
            return False

    return False


async def _try_health_for_ext_id(
    browser,
    ext_id: str,
    timeout: float,
    visible_probe: bool = False,
    proxy_url: str = "",
    restart_cmd: str = "",
) -> str:
    """
    打开 chrome-extension://<id>/health.html，并等待 health.js 真正执行完成。

    重要修正：
    - 旧逻辑只要 document.body.innerText 中出现 raw / resp / ok 就提前返回；
    - 但 health.html 静态页面本身就包含：
        原始返回（resp JSON）
        loading...
      所以旧逻辑会在 health.js 尚未执行、Service Worker 尚未响应前就提前返回；
    - 这会导致探活误判：
        health 页面能打开，但 raw 仍然是 loading...
        _parse_health_text_for_ok_run_id() 解析不到 ok/run_id
        最终 activation_id 为空。

    逻辑：
    1. 先打开 health.html；
    2. 优先读取 #raw 元素内容；
    3. 等 #raw 从 loading... 变成 JSON；
    4. JSON 中包含 ok/run_id/action/next 等字段时才返回；
    5. 如果 health.js 长时间未执行，则返回最后一次页面正文，便于诊断。

    参数：
    - browser:
        nodriver Browser 对象。
    - ext_id:
        待探活的扩展 ID。
    - timeout:
        等待 health.js 完成的最长时间。
    - visible_probe:
        是否额外等待一小段时间，方便有头模式观察。
    - proxy_url:
        非空时会追加到 query 参数 proxy=，触发扩展动态切换代理。
        生产一次性浏览器场景通常传空字符串，只做唤醒，不做切换。
    - restart_cmd:
        非空时会追加到 query 参数 cmd=，写入扩展 storage，便于 health 页面展示重启命令。

    返回：
    - 成功时返回 health 页面中的 raw JSON 文本；
    - 失败时返回最后观察到的页面文本摘要；
    - 不在这里抛异常，外层负责判断。

    说明：
    - 若 proxy_url 非空：追加 ?proxy=...（触发切换）
    - 若 restart_cmd 非空：追加 &cmd=...（写入 storage，用于页面复制重启）
    - 参数值做 URL 编码以避免破坏 query
    """
    base = f"chrome-extension://{ext_id}/{_HEALTH_FILENAME}"
    qs: list[str] = []

    if proxy_url:
        # 保留常见字符，提高诊断日志可读性。
        # 注意：如果账号/密码中包含 &、#、空格等特殊字符，quote 会正确编码。
        qs.append("proxy=" + quote(proxy_url, safe=":/@"))

    if restart_cmd:
        # command_line 通常包含空格、引号、反斜杠等，必须完整编码。
        qs.append("cmd=" + quote(restart_cmd, safe=""))

    url = base + ("?" + "&".join(qs) if qs else "")

    tab = await browser.get(url)

    if visible_probe:
        await asyncio.sleep(0.3)

    deadline = asyncio.get_event_loop().time() + timeout

    last_text = ""
    last_raw = ""

    # 读取 raw JSON 的 JS。
    # health.html 中有：
    #   <pre id="raw">loading...</pre>
    #
    # health.js 执行成功后，会把这里更新为 JSON：
    #   {
    #     "ok": true,
    #     "run_id": "...",
    #     ...
    #   }
    raw_js = r"""
(() => {
  const el = document.getElementById("raw");
  if (!el) return "";
  return (el.innerText || el.textContent || "").trim();
})()
"""

    # 读取整体页面文本的 JS。
    # 用于 health.js 未执行时保留诊断内容。
    body_js = r"""
(() => {
  if (!document.body) return "";
  return (document.body.innerText || document.body.textContent || "").trim();
})()
"""

    while asyncio.get_event_loop().time() < deadline:
        raw_text = ""
        body_text = ""

        try:
            raw_value = await tab.evaluate(raw_js)

            if isinstance(raw_value, str):
                raw_text = raw_value.strip()
        except Exception:
            raw_text = ""

        try:
            body_value = await tab.evaluate(body_js)

            if isinstance(body_value, str):
                body_text = body_value.strip()
        except Exception:
            body_text = ""

        if raw_text:
            last_raw = raw_text

        if body_text:
            last_text = body_text

        # 关键判断：
        # 只有 #raw 不再是 loading...，且看起来像 health.js 回填的 JSON，才认为 health 完成。
        if raw_text and raw_text.lower() != "loading...":
            # 最理想：raw_text 本身就是 JSON。
            if _looks_like_health_payload(raw_text):
                return raw_text

            # 某些环境下 raw 可能不是严格 JSON，但已经有 ok/run_id 等关键信息，也允许返回。
            if '"ok"' in raw_text or "run_id" in raw_text or "proxy_key" in raw_text:
                return raw_text

        await asyncio.sleep(0.15)

    # 超时兜底：
    # 优先返回 raw，因为 raw 最能说明 health.js 是否执行；
    # raw 为空时返回 body，便于确认 health.html 是否能打开。
    return last_raw or last_text


def _parse_health_text_for_ok_run_id(text: str) -> Tuple[bool, str]:
    """
    从 health 返回文本中解析 ok 与 run_id。

    兼容两类输入：
    1. 新逻辑返回的 #raw JSON：
        {
          "ok": true,
          "run_id": "...",
          ...
        }

    2. 旧逻辑或异常兜底返回的页面正文：
        WPC Proxy Auth - Health
        ...
        loading...

    返回：
    - ok:
        True 表示 health.js 已执行，并且 Service Worker 返回了 ok=true；
    - run_id:
        扩展内注入的 self.WPC_RUN_ID。

    注意：
    - 仅能打开 health.html 静态页面，不代表扩展 Service Worker 已经响应；
    - 因此静态页面正文不应被判定为 ok。
    """
    if not text:
        return False, ""

    s = text.strip()

    if not s or s.lower() == "loading...":
        return False, ""

    # 优先按 JSON 解析。
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)

            if not isinstance(obj, dict):
                return False, ""

            ok = obj.get("ok") is True
            run_id = obj.get("run_id") or ""

            return ok, str(run_id)
        except Exception:
            pass

    # 兜底：兼容格式化 JSON 文本。
    # 例如：
    #   "ok": true
    #   "run_id": "xxxx"
    ok = ('"ok": true' in s) or ('"ok":true' in s)

    run_id = ""
    key_patterns = [
        '"run_id": "',
        '"run_id":"',
    ]

    for key in key_patterns:
        idx = s.find(key)

        if idx >= 0:
            start = idx + len(key)
            end = s.find('"', start)

            if end > start:
                run_id = s[start:end]
                break

    return ok, run_id


def _health_text_is_loaded_page(text: str) -> bool:
    """
    判断 health.html 静态页面是否至少成功打开。

    用途：
    - 作为诊断辅助；
    - 不作为强成功条件；
    - 因为页面能打开只能证明扩展资源可访问，不能证明 Service Worker 已完成代理设置。

    返回：
    - True:
        health.html 静态页面已打开；
    - False:
        可能是扩展未加载、页面打不开、chrome-error 等。
    """
    if not text:
        return False

    return (
        "WPC Proxy Auth - Health" in text
        and "原始返回" in text
        and "loading" in text.lower()
    )


# =============================================================================
# health 探活逻辑
# =============================================================================

async def debug_proxy_extension_activation(
    browser,
    *,
    timeout: float = 3.0,
    proxy_url: str = "",
) -> dict[str, Any]:
    """
    调试 MV3 代理扩展激活过程。

    返回结构化诊断信息，不抛异常。

    用途：
    - 确认 browser.connection 是否可用；
    - 打印 chrome://version command_line；
    - 打印 --load-extension 路径；
    - 打印 expected_run_id；
    - 打印 CDP 枚举到的扩展候选 ID；
    - 打印固定 fallback extension_id；
    - 逐个尝试 health.html；
    - 区分：
        1. 扩展页面是否能打开；
        2. health.js 是否执行；
        3. Service Worker 是否响应 ok=true；
        4. run_id 是否匹配。

    说明：
    - 该函数主要用于测试/诊断；
    - 生产主链路建议调用 maybe_activate_proxy_extension()。
    """
    result: dict[str, Any] = {
        "connection_available": False,
        "fixed_extension_id": _FIXED_EXTENSION_ID,
        "cmdline": "",
        "load_extension_path": "",
        "expected_run_id": "",
        "cdp_candidate_ids": [],
        "final_candidate_ids": [],
        "health_results": [],
        "activated_extension_id": "",
    }

    try:
        conn = _get_browser_connection(browser)
        result["connection_available"] = conn is not None

        cmdline = await _read_command_line_from_chrome_version(
            browser,
            timeout=min(2.5, timeout),
            visible_probe=False,
        )

        result["cmdline"] = cmdline

        ext_path = _extract_load_extension_path(cmdline)

        result["load_extension_path"] = ext_path
        result["expected_run_id"] = _infer_run_id_from_load_extension_path(ext_path)

        try:
            cdp_ids = await _list_extension_candidate_ids(browser)
        except Exception as e:
            cdp_ids = []
            result["cdp_error"] = repr(e)

        result["cdp_candidate_ids"] = cdp_ids

        candidates = await _build_extension_candidate_ids(browser)

        result["final_candidate_ids"] = candidates

        first_ok_ext_id = ""
        first_loaded_page_ext_id = ""
        expected_run_id = str(result.get("expected_run_id") or "")

        for ext_id in candidates:
            item: dict[str, Any] = {
                "ext_id": ext_id,
                "health_ok": False,
                "health_page_loaded": False,
                "run_id": "",
                "run_id_matched": False,
                "text_preview": "",
                "error": "",
            }

            try:
                text = await _try_health_for_ext_id(
                    browser,
                    ext_id,
                    timeout=timeout,
                    visible_probe=False,
                    proxy_url=proxy_url,
                    restart_cmd=cmdline,
                )

                ok, run_id = _parse_health_text_for_ok_run_id(text)
                page_loaded = _health_text_is_loaded_page(text)

                item["health_ok"] = ok
                item["health_page_loaded"] = page_loaded
                item["run_id"] = run_id
                item["text_preview"] = (text or "")[:1000]

                if page_loaded and not first_loaded_page_ext_id:
                    first_loaded_page_ext_id = ext_id

                if ok and not first_ok_ext_id:
                    first_ok_ext_id = ext_id

                if ok and expected_run_id and run_id == expected_run_id:
                    item["run_id_matched"] = True
                    result["activated_extension_id"] = ext_id
                    result["health_results"].append(item)
                    return result

                # 如果命令行里解析不到 run_id，固定 extension_id 成功响应 ok=true 即可认为激活成功。
                if ok and not expected_run_id and ext_id == _FIXED_EXTENSION_ID:
                    result["activated_extension_id"] = ext_id
                    result["health_results"].append(item)
                    return result

            except Exception as e:
                item["error"] = repr(e)

            result["health_results"].append(item)

        # 降级成功条件：
        # 如果没有 expected_run_id，但至少有一个扩展返回 ok=true，则认为激活成功。
        if not expected_run_id and first_ok_ext_id:
            result["activated_extension_id"] = first_ok_ext_id

        # 诊断辅助：
        # 如果这里只能打开静态页面，但没有 ok=true，不设置 activated_extension_id。
        # 因为这说明 health.js 或 Service Worker 没有完成响应，不能证明代理已经应用。
        if not result["activated_extension_id"] and first_loaded_page_ext_id:
            result["only_health_page_loaded_ext_id"] = first_loaded_page_ext_id

        return result

    except Exception as e:
        result["fatal_error"] = repr(e)
        return result


async def maybe_activate_proxy_extension(
    browser,
    timeout: float = 3.0,
    visible_probe: bool = False,
    proxy_url: str = "",
) -> str:
    """
    异步探活 / 激活 MV3 代理认证扩展。

    返回：
    - 成功：extension_id
    - 失败：""

    关键修正：
    - 不再把 health.html 静态页面正文当作成功；
    - 必须等 health.js 执行完成，并从 #raw 中拿到 ok=true；
    - 这样才能证明：
        1. 扩展页面可访问；
        2. health.js 已运行；
        3. chrome.runtime.sendMessage 已触达 Service Worker；
        4. Service Worker 已执行 applyProxy("wpc_ping") 或配置逻辑；
        5. 代理认证监听 onAuthRequired 有机会注册完成。

    行为：
    1. 读取 chrome://version command_line；
    2. 解析 --load-extension，推导 expected_run_id；
    3. 通过 CDP 枚举扩展 Service Worker ID；
    4. 追加固定 extension_id 作为 fallback；
    5. 打开 health.html；
    6. 等待 #raw JSON 中出现 ok=true；
    7. 若 expected_run_id 存在，则要求 run_id 匹配；
    8. 若 expected_run_id 不存在，则固定 ID 返回 ok=true 即可认为成功。

    注意：
    - proxy_url 通常在生产一次性浏览器中传空；
    - 因为代理配置已经写入 wpc_sw.js；
    - 传 proxy_url 会触发动态切换代理，并产生 need_restart 语义。
    """
    try:
        cmdline = await _read_command_line_from_chrome_version(
            browser,
            timeout=min(2.5, timeout),
            visible_probe=visible_probe,
        )

        # 推导 expected run_id
        ext_path = _extract_load_extension_path(cmdline)
        expected_run_id = _infer_run_id_from_load_extension_path(ext_path)

        # 候选 extension_id
        candidates = await _build_extension_candidate_ids(browser)

        if not candidates:
            return ""

        first_ok_ext_id = ""

        for ext_id in candidates:
            try:
                text = await _try_health_for_ext_id(
                    browser,
                    ext_id,
                    timeout=timeout,
                    visible_probe=visible_probe,
                    proxy_url=proxy_url,
                    restart_cmd=cmdline,  # 关键：把重启命令注入到 health，写入 storage
                )

                ok, run_id = _parse_health_text_for_ok_run_id(text)

                if ok and not first_ok_ext_id:
                    first_ok_ext_id = ext_id

                # 标准成功路径：
                # command_line 能解析到本次 ext_dir/run_id 时，必须校验 run_id。
                if ok and expected_run_id and run_id == expected_run_id:
                    return ext_id

                # fallback 成功路径：
                # 某些环境下 chrome://version 读取不到 command_line，
                # 但固定 extension_id 能返回 ok=true，则也认为扩展已激活。
                if ok and not expected_run_id and ext_id == _FIXED_EXTENSION_ID:
                    return ext_id

            except Exception:
                continue

        # 最后兜底：
        # 如果无法解析 expected_run_id，但有任意扩展返回 ok=true，则返回第一个成功的扩展。
        if not expected_run_id and first_ok_ext_id:
            return first_ok_ext_id

        return ""

    except Exception:
        return ""


def probe_extension_health_sync(
    browser,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    timeout: float = 3.0,
    visible_probe: bool = False,
    proxy_url: str = "",
) -> str:
    """
    同步探活包装。

    适用场景：
    - 调用方是同步代码；
    - 但内部需要执行 async maybe_activate_proxy_extension()。

    参数：
    - browser:
        nodriver Browser 对象。
    - loop:
        可选事件循环。
    - timeout:
        探活超时时间。
    - visible_probe:
        是否保留短暂可视观察延迟。
    - proxy_url:
        非空时触发动态代理切换。

    返回：
    - 成功：extension id；
    - 失败：""。

    兼容策略：
    1. loop 存在且未运行：
        直接 loop.run_until_complete；
    2. loop 存在且正在运行：
        尝试 asyncio.run_coroutine_threadsafe；
    3. 没有传 loop：
        尝试当前事件循环；
    4. 仍不可用：
        创建临时事件循环；
    5. 任意异常：
        返回 ""，不抛出。
    """
    coro = maybe_activate_proxy_extension(
        browser,
        timeout=timeout,
        visible_probe=visible_probe,
        proxy_url=proxy_url,
    )

    if loop is not None:
        try:
            if not loop.is_running():
                return loop.run_until_complete(coro)

            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                return fut.result(timeout=timeout + 2.0)
            except Exception:
                return ""
        except Exception:
            return ""

    try:
        loop2 = asyncio.get_event_loop()

        if not loop2.is_running():
            return loop2.run_until_complete(coro)

        return ""
    except Exception:
        pass

    try:
        loop3 = asyncio.new_event_loop()

        try:
            asyncio.set_event_loop(loop3)
            return loop3.run_until_complete(coro)
        finally:
            try:
                loop3.close()
            except Exception:
                pass
    except Exception:
        return ""