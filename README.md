# WebPoClient PO Token Provider

## chrome 定制分支

当前版本为 `1.1.2+chrome.1`：在本分支原有实现上移植官方 1.1.2 的兼容性修正，
并尝试官方 [PR #25](https://github.com/coletdjnz/yt-dlp-getpot-wpc/pull/25) 提议的 `nodriver==0.50.99`。
该 PR 尚未合并；这里的版本号明确区别于 PyPI 官方 1.1.2。

保留的能力包括：无头临时浏览器、MV3 HTTP 代理认证扩展及激活检查、Token 磁盘缓存与锁、
资源/并发门禁、失败冷却，以及初始化失败和生成 Token 后的浏览器/目录清理。
清 Cookie 优先使用 `browser.cookies.clear()`，必要时回退旧 CDP 接口，失败仍按 best-effort 处理。
由于本分支使用 `headless=True`，没有照搬官方有头浏览器的窗口最小化逻辑。

本次更新的是 PO Token 生成链路的浏览器/依赖兼容性，没有更换生成算法：
WebPoClient 查找与 `mws(...)` 调用、内容绑定和 `web_safari` 的 video ID 处理均保留。
本地缓存仍为 6 小时，过期或显式 `bypass_cache` 时重新生成；没有新增遇到 HTTP 403 自动废弃 Token 的策略。
真实冷缓存请求已生成新 Token，并通过会员媒体下载验证，详见下方测试说明。

运行要求：Python >=3.10、yt-dlp >=2025.09.26、nodriver 0.50.99、psutil，以及可运行的 Chromium。
`browser_path` 必须显式指定。运行时 Provider 的名称仍是 `wpc`，默认偏好值仍为 -100，属于兜底提供方。

在本仓库根目录安装定制版，不要用 PyPI 官方包覆盖它：

```bash
uv sync --no-editable
```

已有锁文件时可追加 `--locked`。当前 consumer 把本仓库 `.venv` 的 `site-packages` 追加为插件运行环境，
不会自动执行其中 editable 安装的 `.pth`，因此使用非 editable 安装；修改源码后需要重新同步安装。
不能只看 Git 分支或版本号，应核对实际 `getpot_wpc.__file__` 和 `PROVIDER_VERSION`。

定制版的基本配置示例：

```python
extractor_args = {
    "youtubepot-wpc": {"browser_path": ["/path/to/Chromium"]},
}
```

代理由 yt-dlp 的 `proxy` 参数传入；本插件不会自行调用代理服务获取新代理。
认证代理优先使用 HTTP；本分支既有 SOCKS 转换策略只适用于入口也支持 HTTP 的情况。
缓存与临时运行目录位于 `XDG_CACHE_HOME/yt-dlp-getpot-wpc`，未设置时使用用户缓存目录。

离线兼容性检查：

```bash
.venv/bin/python -B scripts/test/test_wpc_compatibility.py
```

真实浏览器、代理和 Token 测试见 [测试说明](scripts/test/README.md)。真实 Token 不写入报告或提交。
本地版本后缀用于区分定制安装；本轮没有触发或验证公开 PyPI 发布。

## 上游项目说明

An experimental yt-dlp [PO Token Provider](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#po-token-provider-plugins) that uses [nodriver](https://github.com/ultrafunkamsterdam/nodriver) and YouTube's "WebPoClient" in the browser mint PO Tokens.

Supports:
- WebPO-based clients (`web`, `web_safari`, `web_music`, `mweb` `tv`, `tv_embedded`, `web_embedded`, `web_creator`, ...)
- Minting GVS and Player PO Tokens
- Minting PO Tokens for both guest and logged-in sessions

<!-- TOC -->
* [WebPoClient PO Token Provider](#webpoclient-po-token-provider)
  * [Installing](#installing)
    * [pip/pipx](#pippipx)
  * [Usage](#usage)
    * [Options](#options)
      * [Custom Chrome Location](#custom-chrome-location)
<!-- TOC -->

## Installing

**Requires yt-dlp `2025.09.26` or above.**

Chrome or Chromium must be installed.

### pip/pipx（仅上游官方版，不保留 chrome 定制）

```
pipx inject yt-dlp yt-dlp-getpot-wpc
```

or

```
python3 -m pip install -U yt-dlp-getpot-wpc
```


If installed correctly, you should see the `wpc` PO Token provider in `yt-dlp -v YOUTUBE_URL` output

    [debug] [youtube] [pot] PO Token Providers: wpc-1.x.y (external)


## Usage

下列说明描述上游官方版的常驻有头浏览器行为；本分支的无头临时浏览器行为以上方定制说明为准。

This provider will automatically be used when a PO Token is requested by yt-dlp. It will launch a web browser while yt-dlp is running which it will use to mint PO Token(s).

> [!WARNING]
> Do not close the browser that is launched when yt-dlp is running!

### Options


#### Custom Chrome Location

Set the location of the Chrome browser executable to use.

`--extractor-args "youtubepot-wpc:browser_path=/usr/bin/chromium"`
