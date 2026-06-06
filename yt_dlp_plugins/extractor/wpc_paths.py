from __future__ import annotations

import os


# =============================================================================
# 顶层目录名称
# =============================================================================
# WPC 所有运行期文件统一收敛到这个根目录下。
#
# 设计目标：
# 1. 缓存文件、锁文件、resource gate 文件、浏览器运行期目录等统一管理
# 2. 不再依赖浏览器同级目录可写
# 3. 后续清理、排障、打包诊断时更方便
#
# 注意：
# - 本模块只负责“顶层目录”划分
# - 各模块内部若还要再细分子目录，应由各模块自己维护
DEFAULT_WPC_HOME_DIRNAME = 'yt-dlp-getpot-wpc'


# =============================================================================
# 一级子目录名称
# =============================================================================
# cache 模块使用的根目录。
# cache 模块内部如何再拆分 entries / locks，由 wpc_cache.py 自己决定。
CACHE_DIRNAME = 'cache'

# resource gate 模块使用的根目录。
# resource gate 内部如何组织 gate.lock / reservations，由 resource_gate.py 自己决定。
RESOURCE_GATE_DIRNAME = 'resource_gate'

# browser 模块使用的运行期根目录。
# browser 模块内部如何组织 <run_id>/profile/ext，由 wpc_browser.py 自己决定。
BROWSER_RUNTIME_DIRNAME = 'browser_runtime'

# 预留：日志目录。
LOGS_DIRNAME = 'logs'

# 预留：通用临时目录。
TMP_DIRNAME = 'tmp'


# =============================================================================
# 根目录计算
# =============================================================================
def get_wpc_home_dir() -> str:
    """
    返回 WPC 统一根目录。

    路径优先级：
    1. XDG_CACHE_HOME/yt-dlp-getpot-wpc
    2. HOME/.cache/yt-dlp-getpot-wpc
    3. USERPROFILE/.cache/yt-dlp-getpot-wpc
    4. 当前工作目录下的 .cache-wpc/yt-dlp-getpot-wpc

    返回：
    - WPC 根目录绝对路径

    说明：
    - Linux / macOS 优先走 XDG / HOME
    - Windows 优先走 USERPROFILE
    - 最后一层兜底只是为了极端情况下仍可运行
    """
    xdg_cache_home = os.getenv('XDG_CACHE_HOME')
    home_dir = os.getenv('HOME')
    userprofile = os.getenv('USERPROFILE')

    if xdg_cache_home:
        return os.path.abspath(os.path.join(xdg_cache_home, DEFAULT_WPC_HOME_DIRNAME))

    if home_dir:
        return os.path.abspath(os.path.join(home_dir, '.cache', DEFAULT_WPC_HOME_DIRNAME))

    if userprofile:
        return os.path.abspath(os.path.join(userprofile, '.cache', DEFAULT_WPC_HOME_DIRNAME))

    return os.path.abspath(
        os.path.join(os.getcwd(), '.cache-wpc', DEFAULT_WPC_HOME_DIRNAME)
    )


def ensure_wpc_home_dir() -> str:
    """
    确保 WPC 根目录存在，并返回该路径。

    返回：
    - WPC 根目录绝对路径
    """
    home_dir = get_wpc_home_dir()
    os.makedirs(home_dir, exist_ok=True)
    return home_dir


# =============================================================================
# 一级业务目录
# =============================================================================
def get_wpc_cache_dir() -> str:
    """
    返回 cache 模块使用的根目录。

    目录示例：
    ~/.cache/yt-dlp-getpot-wpc/cache

    注意：
    - 这里只给 cache 模块一个“根目录”
    - cache 模块内部是否再拆成 entries / locks，由 wpc_cache.py 自己决定
    """
    return os.path.join(ensure_wpc_home_dir(), CACHE_DIRNAME)


def get_wpc_resource_gate_dir() -> str:
    """
    返回 resource gate 模块使用的根目录。

    目录示例：
    ~/.cache/yt-dlp-getpot-wpc/resource_gate

    注意：
    - 这里只给 resource_gate 模块一个“根目录”
    - 其内部如何组织 gate lock / reservations，由 resource_gate.py 自己决定
    """
    return os.path.join(ensure_wpc_home_dir(), RESOURCE_GATE_DIRNAME)


def get_wpc_browser_runtime_root_dir() -> str:
    """
    返回 browser 模块使用的运行期根目录。

    目录示例：
    ~/.cache/yt-dlp-getpot-wpc/browser_runtime

    注意：
    - 这里只给 browser 模块一个“运行期根目录”
    - browser 模块内部如何再创建 <run_id>/profile/ext 等目录，
      应由 wpc_browser.py 自己决定
    """
    return os.path.join(ensure_wpc_home_dir(), BROWSER_RUNTIME_DIRNAME)


def get_wpc_logs_dir() -> str:
    """
    返回日志目录。

    当前主要作为预留目录，后续若需要落盘调试日志可直接使用。
    """
    return os.path.join(ensure_wpc_home_dir(), LOGS_DIRNAME)


def get_wpc_tmp_dir() -> str:
    """
    返回通用临时目录。

    当前主要作为预留目录，后续若需要写临时诊断文件或中间文件可直接使用。
    """
    return os.path.join(ensure_wpc_home_dir(), TMP_DIRNAME)


# =============================================================================
# 确保各一级目录存在
# =============================================================================
def ensure_wpc_cache_dir() -> str:
    """
    确保 cache 根目录存在，并返回该路径。
    """
    path = get_wpc_cache_dir()
    os.makedirs(path, exist_ok=True)
    return path


def ensure_wpc_resource_gate_dir() -> str:
    """
    确保 resource gate 根目录存在，并返回该路径。
    """
    path = get_wpc_resource_gate_dir()
    os.makedirs(path, exist_ok=True)
    return path


def ensure_wpc_browser_runtime_root_dir() -> str:
    """
    确保 browser 运行期根目录存在，并返回该路径。
    """
    path = get_wpc_browser_runtime_root_dir()
    os.makedirs(path, exist_ok=True)
    return path


def ensure_wpc_logs_dir() -> str:
    """
    确保日志目录存在，并返回该路径。
    """
    path = get_wpc_logs_dir()
    os.makedirs(path, exist_ok=True)
    return path


def ensure_wpc_tmp_dir() -> str:
    """
    确保通用临时目录存在，并返回该路径。
    """
    path = get_wpc_tmp_dir()
    os.makedirs(path, exist_ok=True)
    return path


def ensure_all_wpc_dirs() -> str:
    """
    一次性确保 WPC 所有一级业务目录都存在。

    会创建：
    - 根目录
    - cache/
    - resource_gate/
    - browser_runtime/
    - logs/
    - tmp/

    返回：
    - WPC 根目录绝对路径

    注意：
    - 这里只创建一级业务目录
    - 各模块内部若有更细的子目录，仍应由各模块自己创建
    """
    home_dir = ensure_wpc_home_dir()
    ensure_wpc_cache_dir()
    ensure_wpc_resource_gate_dir()
    ensure_wpc_browser_runtime_root_dir()
    ensure_wpc_logs_dir()
    ensure_wpc_tmp_dir()
    return home_dir


# =============================================================================
# 调试辅助
# =============================================================================
def describe_wpc_directory_layout() -> dict[str, str]:
    """
    返回当前 WPC 顶层目录布局说明，便于调试或打印。

    返回示例：
    {
        "home_dir": "...",
        "cache_dir": "...",
        "resource_gate_dir": "...",
        "browser_runtime_root_dir": "...",
        "logs_dir": "...",
        "tmp_dir": "..."
    }

    说明：
    - 这里只描述顶层业务目录
    - 不描述各模块内部更细的目录结构
    """
    return {
        'home_dir': get_wpc_home_dir(),
        'cache_dir': get_wpc_cache_dir(),
        'resource_gate_dir': get_wpc_resource_gate_dir(),
        'browser_runtime_root_dir': get_wpc_browser_runtime_root_dir(),
        'logs_dir': get_wpc_logs_dir(),
        'tmp_dir': get_wpc_tmp_dir(),
    }