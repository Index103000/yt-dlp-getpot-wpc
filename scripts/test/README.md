# Chromium 浏览器代理 测试脚本

本文档说明如何使用 `test_browser_proxy_ip.py` 和 `test_wpc_browser_boot.py` 在 Windows 和 Linux 环境下测试代理 IP 与 WebPoClient 启动情况。

## 升级回归与验收边界

- `test_wpc_compatibility.py` 是离线回归：使用真实 nodriver 0.50.99 API 和临时扩展文件，
  浏览器及网络交互由替身隔离，覆盖 Cookie 新/旧接口、无头与认证代理配置及清理。
- 下方 boot/mint 测试用于真实浏览器。WebPoClient 可用、生成 Token、缓存命中、媒体成功应分别记录，
  不能仅凭“插件已加载”或“窗口已打开”判定 PO Token 成功。
- Provider 名称仍为 `wpc`。与 bgutil 同时存在时，它可能没有被实际调用；专项测试应在独立进程中限定提供方，
  验证真实返回和缓存命中，不修改生产偏好或全局配置。
- 真实测试请使用独立 `XDG_CACHE_HOME`，只清理本次创建的 profile、扩展、缓存与浏览器 PID。
  旧 boot 脚本的 `--mint-test` 会打印原始 Token，provider 的 trace 也包含 Token；
  报告应仅保留长度/摘要、来源、耗时和成功状态，不能原样保存这些输出。

```bash
.venv/bin/python -B scripts/test/test_wpc_compatibility.py
```

### 2026-11-07 本机验证记录

- 基于 `chrome` 分支 `769cbd3`，自定义 Provider 版本 `1.1.2+chrome.1`，nodriver `0.50.99`，
  Python `3.14.2`，外部 yt-dlp `2026.10.26`，Chromium `148.0.7778.215`。
- consumer 实际加载的模块位于本仓库 `.venv/lib/python3.14/site-packages/yt_dlp_plugins/extractor/`，
  含 `wpc_browser`、代理认证扩展、缓存及门禁模块；不再是原来的官方单文件 1.0.0 安装。
- nodriver 70 个 Python 文件通过 UTF-8/语法检查，`network.py` 与新发行包 RECORD 一致，未添加本地编码补丁。
- 离线回归 8 项通过，涵盖新旧 Cookie API、无头/MV3 配置及多种失败清理路径。
- 冷缓存真实 GVS 请求：使用带认证 HTTP 代理与 `mweb`，WPC 返回非空 Token；重复同一请求时命中自己的磁盘缓存。
  共实际生成 1 次、启动 1 个无头浏览器、缓存命中 1 次，结束后没有遗留测试浏览器进程。
- 会员媒体验证：限定当前测试进程的 Provider 为 WPC，通过真实 consumer worker 下载 `ghPG2NW2Lkc`。
  使用当前 Chrome 登录态、同一 HTTP 代理，`mweb` 客户端；WPC 真实返回 Token 1 次，启动 1 个无头浏览器。
  最终为 `598+599-drc`，文件 625,824 字节，256×144，90.279 秒，包含音视频且 `.info.json` 存在。
  `availability=subscriber_only`，已知 Cookie/Token 秘密值扫描未发现泄漏。
- 媒体验证结束后，浏览器 PID、profile 与该次运行目录均无残留；整体测试缓存和媒体也已删除。

这些结果只覆盖上述本机 Chromium、代理、客户端及小流格式，不代表所有平台/客户端已验收；
尤其直连、无认证代理路径不是此次真实验证范围。真实 Token 和代理密码不保存在本记录中。

------

## 一、测试目的

1. 验证指定代理在 Python requests 与 Chromium 浏览器中是否一致。
2. 检查 MV3 代理认证扩展是否正确激活（带认证代理）。
3. 测试浏览器启动、页面加载、document.readyState、ytcfg 注入和 WebPoClient 是否可用。
4. 可选：通过 `--mint-test` 调用 WebPoClient.mws() 验证 PO Token mint 流程。

------

## 二、测试脚本

### 1. 测试代理 IP 脚本

文件：`scripts/test/test_browser_proxy_ip.py`

功能：

- 启动 Chromium 浏览器。
- 通过相同代理访问 `https://ipinfo.io/ip`。
- 比对 Python requests 与浏览器获取到的 IP 是否一致。
- 支持带认证代理 MV3 扩展或命令行代理参数。
- 支持临时 profile/ext 目录，默认自动清理。

### 2. 测试 WPC 浏览器启动脚本

文件：`scripts/test/test_wpc_browser_boot.py`

功能：

- 启动 Chromium 浏览器。

- 激活 MV3 代理扩展（根据代理类型）。

- 打开 YouTube 并检查：
  - document.readyState
  - ytcfg 是否注入
  - WebPoClient 是否存在
  
- 可选 `--mint-test` 调用 WebPoClient.mws() mint PO Token。

- 支持自定义 timeout、保留 profile、打印扩展调试信息。

  

------

## 三、目录结构说明（临时运行目录）

```
<runtime_base_dir>/<run_id>/
├── profile/       # Chromium user-data-dir
└── ext/           # MV3 代理认证扩展目录
```

- 每次测试自动生成唯一 run_id。

- 默认测试结束后删除 root_dir，可通过 `--keep-profile` 保留目录用于排查。

- profile/ext 与生产 `wpc_browser.py` 逻辑一致。

  

------

## 四、测试命令示例

### 1. Linux / macOS

#### a) 代理 IP 测试

```
python3 ./scripts/test/test_browser_proxy_ip.py \
  --browser-path "/opt/yt-downloader/current/browser/current/chrome" \
  --runtime-base-dir "/opt/yt-downloader/current/test" \
  --proxy "xxxx" \
  --proxy-auth-mode auto \
  --activation-timeout 5
```

#### b) WPC 浏览器启动与 mint 测试

```
python3 ./scripts/test/test_wpc_browser_boot.py \
  --browser-path "/opt/yt-downloader/current/browser/current/chrome" \
  --runtime-base-dir "/opt/yt-downloader/current/test" \
  --proxy "xxxx" \
  --debug-proxy-extension \
  --document-ready-timeout 30 \
  --ytcfg-timeout 30 \
  --webpo-timeout 30 \
  --mint-test \
  --content-binding "MPNbzS8el70"
```

------

### 2. Windows CMD

#### a) 代理 IP 测试

```
python ./scripts/test/test_browser_proxy_ip.py ^
  --browser-path "C:/Users/Administrator/AppData/Local/Chromium/Application/chrome.exe" ^
  --runtime-base-dir "D:/code/project/yt-dlp/index103000/yt-dlp-plugins_yt-dlp-getpot-wpc/yt-dlp-getpot-wpc/scripts/test" ^
  --proxy "xxxx"
```

#### b) WPC 浏览器启动与 mint 测试

```
python ./scripts/test/test_wpc_browser_boot.py ^
  --browser-path "C:/Users/Administrator/AppData/Local/Chromium/Application/chrome.exe" ^
  --runtime-base-dir "D:/code/project/yt-dlp/index103000/yt-dlp-plugins_yt-dlp-getpot-wpc/yt-dlp-getpot-wpc/scripts/test" ^
  --proxy "xxxx" ^
  --debug-proxy-extension ^
  --document-ready-timeout 30 ^
  --ytcfg-timeout 30 ^
  --webpo-timeout 30 ^
  --mint-test ^
  --content-binding "MPNbzS8el70"
```

------

### 3. Windows PowerShell

#### a) 代理 IP 测试

```
python ./scripts/test/test_browser_proxy_ip.py `
  --browser-path "C:/Users/Administrator/AppData/Local/Chromium/Application/chrome.exe" `
  --runtime-base-dir "D:/code/project/yt-dlp/index103000/yt-dlp-plugins_yt-dlp-getpot-wpc/yt-dlp-getpot-wpc/scripts/test" `
  --proxy "xxxx"
```

#### b) WPC 浏览器启动与 mint 测试

```
python ./scripts/test/test_wpc_browser_boot.py `
  --browser-path "C:/Users/Administrator/AppData/Local/Chromium/Application/chrome.exe" `
  --runtime-base-dir "D:/code/project/yt-dlp/index103000/yt-dlp-plugins_yt-dlp-getpot-wpc/yt-dlp-getpot-wpc/scripts/test" `
  --proxy "xxxx" `
  --debug-proxy-extension `
  --document-ready-timeout 30 `
  --ytcfg-timeout 30 `
  --webpo-timeout 30 `
  --mint-test `
  --content-binding "MPNbzS8el70"
```

------

> 注意：
>
> - PowerShell 使用 ``` 作为行续接符，CMD 使用 `^`。
> - Linux/macOS 使用 `\` 续行。
> - 参数含有特殊字符（如 `@` 或 `:`）无需额外转义，但确保整条命令在一行或正确续行。

------



------

## 五、参数说明（常用）

| 参数                       | 说明                                    | 默认值                                              |
| -------------------------- | --------------------------------------- | --------------------------------------------------- |
| `--browser-path`           | Chrome/Chromium 可执行文件路径          | -                                                   |
| `--proxy`                  | 代理 URL，可带认证或不带                | None                                                |
| `--proxy-auth-mode`        | 按脚本帮助选择自动、直接或扩展认证策略  | 以对应脚本 `--help` 为准                             |
| `--runtime-base-dir`       | 临时 profile/ext 基目录                 | `/tmp/wpc-proxy-ip-test` 或 `/tmp/wpc-browser-test` |
| `--activation-timeout`     | MV3 代理扩展激活超时时间（秒）          | 5                                                   |
| `--document-ready-timeout` | 等待 document.readyState 超时时间（秒） | 30                                                  |
| `--ytcfg-timeout`          | 等待 ytcfg 注入超时时间（秒）           | 30                                                  |
| `--webpo-timeout`          | 等待 WebPoClient 出现超时时间（秒）     | 30~60                                               |
| `--mint-test`              | 是否执行 PO Token mint 测试             | False                                               |
| `--content-binding`        | mint 测试内容绑定                       | 3dZryjBuSno / MPNbzS8el70                           |
| `--keep-profile`           | 保留 profile/ext 目录，用于排查         | False                                               |
| `--extra-arg`              | 额外 Chrome 启动参数，可多次            | -                                                   |

------

## 六、测试流程

1. Python requests 获取代理 IP。
2. 启动 Chromium：
   - 使用临时 profile 和 ext。
   - 激活 MV3 代理扩展（带认证代理）。
3. 浏览器访问 `https://ipinfo.io/ip` 或指定 URL。
4. 收集页面诊断信息（href、title、readyState、ytcfg、WebPoClient）。
5. 可选 mint 测试：
   - 调用 WebPoClient.mws()。
6. 比对 requests IP 与浏览器 IP。
7. 输出测试结果：
   - PASSED：requests IP 与浏览器 IP 一致，WebPoClient 可用。
   - FAILED：浏览器未成功访问或 WebPoClient 不可用。
8. 自动清理临时目录，除非 `--keep-profile`。

------

## 七、注意事项

1. 带认证 SOCKS 代理在 Chrome 上可能失败：
   - 建议通过 HTTP 代理或 MV3 扩展。
2. 确保浏览器版本与 nodriver 兼容。
3. Windows 命令行需用 `^` 换行。
4. Linux/macOS 命令行用 `\` 换行。
5. 若测试失败，可保留 `--keep-profile` 并使用浏览器手动调试。
6. `--debug-proxy-extension` 可打印扩展激活诊断信息，用于定位 MV3 扩展问题。

------
