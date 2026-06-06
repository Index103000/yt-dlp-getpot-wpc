from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


# =============================================================================
# 缓存相关默认常量
# =============================================================================
# 单条缓存文件的后缀名。
DEFAULT_CACHE_ENTRY_SUFFIX = '.json'

# 单条锁文件的后缀名。
DEFAULT_CACHE_LOCK_SUFFIX = '.lock'

# token 默认缓存时长（单位：小时）。
# 当前与 bgutil 默认值保持一致，便于后续按 bgutil 的变更同步调整。
DEFAULT_TOKEN_TTL_HOURS = 6

# Windows 平台抢锁失败后的轮询间隔（秒）。
WINDOWS_LOCK_RETRY_INTERVAL_SECONDS = 0.05


# =============================================================================
# 数据结构：YoutubeSessionData
# =============================================================================
@dataclass
class YoutubeSessionData:
    """
    表示一条 YouTube 会话缓存数据。

    字段说明：
    - po_token:
        已生成的 PO Token
    - content_binding:
        当前缓存对应的 content binding
    - expires_at:
        当前缓存的过期时间，内部统一按 UTC datetime 表示

    说明：
    - 这里的字段语义和 bgutil 的单条缓存 entry 保持一致
    - 区别仅在于：
      bgutil 当前是“多条 entry 放在一个 cache.json”
      这里改成“每条 entry 一个 json 文件”
    """
    po_token: str
    content_binding: str
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """
        将当前对象序列化为可写入单条缓存文件的 dict。

        输出字段说明：
        - poToken:
            token 内容
        - contentBinding:
            当前缓存对应的 content binding
        - expiresAt:
            ISO 8601 格式的过期时间字符串
        """
        return {
            'poToken': self.po_token,
            'contentBinding': self.content_binding,
            'expiresAt': self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'YoutubeSessionData':
        """
        从单条缓存文件中的字典反序列化为 YoutubeSessionData。

        兼容处理：
        - expiresAt 必须能被 datetime.fromisoformat 解析
        - 若 expiresAt 不带时区，则自动按 UTC 处理
        """
        expires_raw = data['expiresAt']
        expires_at = datetime.fromisoformat(expires_raw)

        # 如果时间字符串不带 tzinfo，则默认按 UTC 处理。
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        return cls(
            po_token=data['poToken'],
            content_binding=data['contentBinding'],
            expires_at=expires_at,
        )


def _utcnow() -> datetime:
    """
    返回当前 UTC 时间。

    说明：
    - 统一使用 UTC，避免服务器本地时区差异影响过期判断
    """
    return datetime.now(timezone.utc)


# =============================================================================
# 路径相关
# =============================================================================
def resolve_cachedir(cachedir: str) -> str:
    """
    解析 cache 根目录。

    参数：
    - cachedir:
        外部显式传入的 cache 根目录

    返回：
    - cache 根目录绝对路径

    设计说明：
    - cache 模块不再决定“默认缓存目录应该放哪里”
    - 这类策略应由调用方决定，例如：
        * WPC 调用方可传 ~/.cache/yt-dlp-getpot-wpc/cache
        * 其他插件也可传自己的 cache 根目录
    - cache 模块只负责在该根目录下维护自己的内部结构
    """
    if not cachedir:
        raise ValueError('cachedir is required')
    return os.path.abspath(cachedir)


def get_cache_entries_dir(cachedir: str) -> str:
    """
    返回缓存条目目录路径。

    目录用途：
    - 存放每个 content_binding 对应的单条缓存文件

    当前内部目录结构：
    - <cachedir>/entries/
    """
    return os.path.join(cachedir, 'entries')


def get_cache_locks_dir(cachedir: str) -> str:
    """
    返回锁文件目录路径。

    目录用途：
    - 存放每个 content_binding 对应的单独锁文件

    当前内部目录结构：
    - <cachedir>/locks/
    """
    return os.path.join(cachedir, 'locks')


def ensure_cachedir(cachedir: str) -> str:
    """
    确保缓存根目录以及内部子目录存在。

    参数：
    - cachedir:
        cache 根目录

    返回：
    - 规范化后的 cache 根目录绝对路径

    当前会确保以下目录存在：
    - <cachedir>/
    - <cachedir>/entries/
    - <cachedir>/locks/

    说明：
    - entries / locks 属于 cache 模块的内部实现细节
    - 不应由外部路径模块关心
    """
    cachedir = resolve_cachedir(cachedir)
    os.makedirs(get_cache_entries_dir(cachedir), exist_ok=True)
    os.makedirs(get_cache_locks_dir(cachedir), exist_ok=True)
    return cachedir


def make_cache_key(content_binding: str) -> str:
    """
    根据 content_binding 生成稳定、安全的文件 key。

    为什么不直接用 content_binding 做文件名：
    - content_binding 可能包含不适合作为文件名的字符
    - 直接暴露原始值不利于路径安全与可移植性

    当前做法：
    - 使用 sha256(content_binding) 的十六进制摘要作为文件 key
    """
    return hashlib.sha256(content_binding.encode('utf-8')).hexdigest()


def get_cache_entry_path(cachedir: str, content_binding: str) -> str:
    """
    返回某个 content_binding 对应的缓存文件路径。

    文件路径格式：
    - <cachedir>/entries/<sha256(content_binding)>.json
    """
    key = make_cache_key(content_binding)
    return os.path.join(get_cache_entries_dir(cachedir), f'{key}{DEFAULT_CACHE_ENTRY_SUFFIX}')


def get_cache_lock_path(cachedir: str, content_binding: str) -> str:
    """
    返回某个 content_binding 对应的锁文件路径。

    锁文件路径格式：
    - <cachedir>/locks/<sha256(content_binding)>.lock
    """
    key = make_cache_key(content_binding)
    return os.path.join(get_cache_locks_dir(cachedir), f'{key}{DEFAULT_CACHE_LOCK_SUFFIX}')


# =============================================================================
# 单 key 文件锁
# =============================================================================
class CacheEntryLock:
    """
    单个 content_binding 对应的跨进程独占锁。

    设计目标：
    - 不同 content_binding 完全并发
    - 相同 content_binding 串行执行
    - 不引入第三方依赖
    - Linux/macOS 使用 fcntl.flock
    - Windows 使用 msvcrt.locking

    使用方式：
        with CacheEntryLock(lock_path):
            ...  # 对该 content_binding 的缓存读 / mint / 写全过程

    说明：
    - 这是“单 key 锁”，不是全局锁
    - 非常适合高并发、不同 content_binding 居多的场景
    """

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fp = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        self._fp = open(self.lock_path, 'a+b')
        self._lock()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._unlock()
        finally:
            if self._fp:
                self._fp.close()
                self._fp = None

    def _lock(self) -> None:
        """
        获取独占锁。
        """
        if self._fp is None:
            raise RuntimeError('lock file is not opened')

        if os.name == 'nt':
            import msvcrt

            self._fp.seek(0)
            while True:
                try:
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(WINDOWS_LOCK_RETRY_INTERVAL_SECONDS)
        else:
            import fcntl

            fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX)

    def _unlock(self) -> None:
        """
        释放独占锁。
        """
        if self._fp is None:
            return

        if os.name == 'nt':
            import msvcrt

            self._fp.seek(0)
            msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)


# =============================================================================
# 单条缓存文件：无锁读写
# =============================================================================
def _load_youtube_session_data_unlocked(
    *,
    logger,
    cachedir: str,
    content_binding: str,
) -> YoutubeSessionData | None:
    """
    读取某个 content_binding 对应的单条缓存文件。

    这是无锁版本。
    调用方必须确保自己已经拿到了该 key 对应的锁。

    参数：
    - logger:
        日志对象
    - cachedir:
        cache 根目录
    - content_binding:
        当前查询的 key

    返回：
    - 命中并解析成功：YoutubeSessionData
    - 文件不存在 / 文件损坏 / 内容不合法：None

    额外校验：
    - 文件内的 contentBinding 必须与当前查询的 content_binding 一致
    - 若不一致，则视为异常条目并忽略
    """
    cachedir = resolve_cachedir(cachedir)
    entry_path = get_cache_entry_path(cachedir, content_binding)
    if not os.path.exists(entry_path):
        return None

    try:
        with open(entry_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f'Error parsing cache entry. path={entry_path}, e={e}', once=True)
        return None

    if not isinstance(raw, dict):
        logger.warning(f'Ignored cache entry because root object is not a dict: {entry_path}', once=True)
        return None

    try:
        entry = YoutubeSessionData.from_dict(raw)
    except Exception as e:
        logger.warning(f'Ignored invalid cache entry. path={entry_path}, e={e}', once=True)
        return None

    if entry.content_binding != content_binding:
        logger.warning(
            'Ignored cache entry because contentBinding mismatched. '
            f'expected={content_binding}, got={entry.content_binding}, path={entry_path}',
            once=True,
        )
        return None

    return entry


def _set_youtube_session_data_unlocked(
    *,
    logger,
    cachedir: str,
    youtube_session_data: YoutubeSessionData,
) -> None:
    """
    将单条 YoutubeSessionData 写入对应缓存文件。

    这是无锁版本。
    调用方必须确保自己已经拿到了该 key 对应的锁。

    参数：
    - logger:
        日志对象
    - cachedir:
        cache 根目录
    - youtube_session_data:
        待写入的缓存对象

    写入策略：
    1. 写入唯一临时文件
    2. flush + fsync
    3. os.replace 原子替换目标文件

    这样做可以降低半写文件、截断文件的风险。
    """
    cachedir = ensure_cachedir(cachedir)

    entry_path = get_cache_entry_path(cachedir, youtube_session_data.content_binding)
    temp_path = f'{entry_path}.tmp.{os.getpid()}.{time.time_ns()}'

    raw_data = youtube_session_data.to_dict()

    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(raw_data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, entry_path)
        logger.debug(f'Cache entry saved: {entry_path}')
    finally:
        # 正常 replace 后 temp_path 通常已不存在；
        # 若中途异常，这里尽量清理遗留临时文件。
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _remove_youtube_session_data_unlocked(
    *,
    logger,
    cachedir: str,
    content_binding: str,
) -> None:
    """
    删除某个 content_binding 对应的缓存文件。

    这是无锁版本。
    调用方必须确保自己已经拿到了该 key 对应的锁。

    参数：
    - logger:
        日志对象
    - cachedir:
        cache 根目录
    - content_binding:
        当前查询的 key

    说明：
    - 文件不存在时视为正常情况
    """
    cachedir = resolve_cachedir(cachedir)
    entry_path = get_cache_entry_path(cachedir, content_binding)

    try:
        if os.path.exists(entry_path):
            os.remove(entry_path)
            logger.debug(f'Cache entry removed: {entry_path}')
    except OSError as e:
        logger.warning(f'Failed to remove cache entry. path={entry_path}, e={e!r}', once=True)


# =============================================================================
# 单条缓存：纯内存操作
# =============================================================================
def is_youtube_session_data_expired(
    youtube_session_data: YoutubeSessionData,
    *,
    now: datetime | None = None,
) -> bool:
    """
    判断单条缓存是否已过期。

    过期条件：
    - now > expires_at
    """
    now = now or _utcnow()
    return now > youtube_session_data.expires_at


def put_youtube_session_data(
    *,
    content_binding: str,
    po_token: str,
    token_ttl_hours: int = DEFAULT_TOKEN_TTL_HOURS,
    now: datetime | None = None,
) -> YoutubeSessionData:
    """
    根据给定参数构造一条新的 YoutubeSessionData。

    参数：
    - content_binding:
        当前缓存的 key
    - po_token:
        新生成的 token
    - token_ttl_hours:
        TTL 小时数
    - now:
        当前时间，未传则使用当前 UTC 时间

    返回：
    - 新构造的 YoutubeSessionData
    """
    now = now or _utcnow()

    return YoutubeSessionData(
        po_token=po_token,
        content_binding=content_binding,
        expires_at=now + timedelta(hours=int(token_ttl_hours)),
    )


# =============================================================================
# 对外：单 key 带锁访问
# =============================================================================
def get_youtube_session_data(
    *,
    logger,
    cachedir: str,
    content_binding: str,
    cleanup: bool = True,
) -> YoutubeSessionData | None:
    """
    读取某个 content_binding 的缓存。

    参数：
    - logger:
        日志对象
    - cachedir:
        cache 根目录
    - content_binding:
        当前查询的 key
    - cleanup:
        若为 True，则发现已过期时会在锁内顺手删除对应缓存文件

    返回：
    - 命中且未过期：YoutubeSessionData
    - 未命中或已过期：None

    设计说明：
    - 读取全过程在该 key 的锁内完成
    - 相同 key 串行，不同 key 并发
    """
    cachedir = ensure_cachedir(cachedir)
    lock_path = get_cache_lock_path(cachedir, content_binding)

    with CacheEntryLock(lock_path):
        entry = _load_youtube_session_data_unlocked(
            logger=logger,
            cachedir=cachedir,
            content_binding=content_binding,
        )

        if entry is None:
            logger.debug(f'Cache miss for content binding: {content_binding}')
            return None

        if is_youtube_session_data_expired(entry):
            logger.debug(f'Cache expired for content binding: {content_binding}')
            if cleanup:
                _remove_youtube_session_data_unlocked(
                    logger=logger,
                    cachedir=cachedir,
                    content_binding=content_binding,
                )
            return None

        logger.debug(f'Cache hit for content binding: {content_binding}')
        return entry


def get_youtube_session_data_locked(
    *,
    logger,
    cachedir: str,
    content_binding: str,
    cleanup: bool = True,
) -> YoutubeSessionData | None:
    """
    在“调用方已持有该 key 的锁”的前提下，读取某个 content_binding 的缓存。

    参数：
    - logger:
        日志对象
    - cachedir:
        cache 根目录
    - content_binding:
        当前查询的 key
    - cleanup:
        若为 True，则发现已过期时会顺手删除缓存文件

    返回：
    - 命中且未过期：YoutubeSessionData
    - 未命中或已过期：None

    使用场景：
    - 当外层已经拿到该 content_binding 的锁时，避免再次重复加锁
    - 例如：
        1. 先拿 key 锁
        2. 锁内再查缓存
        3. miss 才 mint
        4. 锁内再写回

    注意：
    - 该方法本身不加锁
    - 调用方必须自行保证串行访问
    """
    cachedir = resolve_cachedir(cachedir)

    entry = _load_youtube_session_data_unlocked(
        logger=logger,
        cachedir=cachedir,
        content_binding=content_binding,
    )

    if entry is None:
        logger.debug(f'Cache miss for content binding: {content_binding}')
        return None

    if is_youtube_session_data_expired(entry):
        logger.debug(f'Cache expired for content binding: {content_binding}')
        if cleanup:
            _remove_youtube_session_data_unlocked(
                logger=logger,
                cachedir=cachedir,
                content_binding=content_binding,
            )
        return None

    logger.debug(f'Cache hit for content binding: {content_binding}')
    return entry


def set_youtube_session_data(
    *,
    logger,
    cachedir: str,
    youtube_session_data: YoutubeSessionData,
) -> None:
    """
    将单条 YoutubeSessionData 写入缓存文件。

    这是带锁版本：
    - 会先获取该 content_binding 对应的锁
    - 再写入缓存文件

    参数：
    - logger:
        日志对象
    - cachedir:
        cache 根目录
    - youtube_session_data:
        待写入的缓存对象

    适合：
    - 外部只想写入一条缓存，不关心锁细节的场景
    """
    cachedir = ensure_cachedir(cachedir)
    lock_path = get_cache_lock_path(cachedir, youtube_session_data.content_binding)

    with CacheEntryLock(lock_path):
        _set_youtube_session_data_unlocked(
            logger=logger,
            cachedir=cachedir,
            youtube_session_data=youtube_session_data,
        )


def set_youtube_session_data_locked(
    *,
    logger,
    cachedir: str,
    youtube_session_data: YoutubeSessionData,
) -> None:
    """
    在“调用方已持有该 key 的锁”的前提下，将单条 YoutubeSessionData 写入缓存文件。

    参数：
    - logger:
        日志对象
    - cachedir:
        cache 根目录
    - youtube_session_data:
        待写入的缓存对象

    使用场景：
    - 当外层已经锁住该 content_binding，且希望在同一锁内完成：
        1. 再次检查缓存
        2. mint
        3. 写回

    注意：
    - 该方法本身不加锁
    - 调用方必须自行保证对同一 key 的串行访问
    """
    _set_youtube_session_data_unlocked(
        logger=logger,
        cachedir=cachedir,
        youtube_session_data=youtube_session_data,
    )


def get_cache_entry_lock(
    content_binding: str,
    cachedir: str,
) -> CacheEntryLock:
    """
    返回某个 content_binding 对应的锁对象。

    参数：
    - content_binding:
        当前查询的 key
    - cachedir:
        cache 根目录

    使用场景：
    - provider 希望自己控制锁的范围时使用
    - 例如在同一把锁内完成：
        1. 再查缓存
        2. 若 miss 则 mint
        3. 写回缓存

    返回：
    - CacheEntryLock 实例
    """
    cachedir = ensure_cachedir(cachedir)
    return CacheEntryLock(get_cache_lock_path(cachedir, content_binding))