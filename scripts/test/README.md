# Chromium 浏览器代理 测试脚本

本文档说明如何使用 `test_browser_proxy_ip.py` 和 `test_wpc_browser_boot.py` 在 Windows 和 Linux 环境下测试代理 IP 与 WebPoClient 启动情况。

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
| `--proxy-auth-mode`        | `auto                                   | direct                                              |
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

我可以帮你再做一份**精简版表格式对比文档**，把 Windows 和 Linux 两条命令直接对照并标记参数差异，方便测试手册查阅。你希望我生成吗？
