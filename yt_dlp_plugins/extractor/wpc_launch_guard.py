# yt_dlp_plugins/extractor/wpc_launch_guard.py
from __future__ import annotations

import json
import os
import pathlib
import time
import uuid
from dataclasses import dataclass

from yt_dlp_plugins.extractor.wpc_exceptions import WPCRejectedRequest
from yt_dlp_plugins.extractor.wpc_paths import get_wpc_browser_runtime_root_dir
from yt_dlp_plugins.extractor.wpc_runtime_config import WPC_RUNTIME_CONFIG


@dataclass
class MemorySnapshot:
    """
    当前系统内存快照。

    字段说明：
    - total_mb:
        系统总内存，单位 MB。
    - available_mb:
        系统当前可用内存，单位 MB。
        注意：Linux 下 available 比 free 更适合作为可用内存判断依据。
    - used_percent:
        当前内存使用百分比。
    """

    total_mb: int
    available_mb: int
    used_percent: float


@dataclass
class WPCLaunchSlot:
    """
    WPC 浏览器启动槽位。

    字段说明：
    - slot_path:
        当前进程持有的槽位文件路径。
    - token:
        当前进程写入槽位文件的唯一 token。

    为什么需要 token：
    - release 时会校验 token；
    - 只有 token 匹配时才删除 slot 文件；
    - 避免误删其他进程新建的槽位文件。
    """

    slot_path: str
    token: str


def _now() -> float:
    """
    返回单调时间。

    为什么不用 time.time():
    - time.time() 可能受系统时间调整影响；
    - 这里用于超时、stale 判断，更适合用 monotonic。
    """
    return time.monotonic()


def _get_launch_guard_dir() -> str:
    """
    返回 WPC 启动保护目录。

    当前放在 browser_runtime 根目录下面：

        ~/.cache/yt-dlp-getpot-wpc/browser_runtime/launch_guard

    说明：
    - 这是跨进程共享目录；
    - 用于保存并发槽位文件；
    - 与每次浏览器运行的 profile/ext 目录分开。
    """
    root = pathlib.Path(get_wpc_browser_runtime_root_dir()) / "launch_guard"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _read_memory_snapshot() -> MemorySnapshot | None:
    """
    读取系统内存快照。

    优先使用 psutil：
    - psutil.virtual_memory().available
    - psutil.virtual_memory().percent

    如果 psutil 不存在：
    - 返回 None；
    - 调用方可以选择跳过内存检查；
    - 但仍然执行跨进程并发槽位保护。

    为什么不强依赖 psutil：
    - 插件环境可能没有显式安装 psutil；
    - 当前 WPC 的关键硬保护除了内存，还有跨进程并发槽位；
    - psutil 缺失不应该导致 provider 直接不可用。
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return None

    mem = psutil.virtual_memory()

    return MemorySnapshot(
        total_mb=int(mem.total / 1024 / 1024),
        available_mb=int(mem.available / 1024 / 1024),
        used_percent=float(mem.percent),
    )


def _check_memory_before_launch(logger) -> None:
    """
    WPC 浏览器启动前内存 fail-fast 检查。

    检查目标：
    - 避免在系统已经内存紧张时继续启动 Chromium；
    - 尤其是 bgutil 大面积失败后，WPC 作为兜底 provider 被高并发触发时。

    配置项：
    - WPC_LAUNCH_GUARD_MIN_AVAILABLE_MB
        启动前要求系统至少有多少 available memory。
    - WPC_LAUNCH_GUARD_RESERVED_MB
        预估一个 WPC 浏览器实例需要占用多少内存。
    - WPC_LAUNCH_GUARD_MIN_FREE_AFTER_LAUNCH_MB
        假设启动后，还希望系统至少剩余多少 available memory。
    - WPC_LAUNCH_GUARD_MAX_MEMORY_PERCENT
        当前系统内存使用率超过该值时拒绝启动。

    触发拒绝时：
    - 抛 WPCRejectedRequest；
    - 由 getpot_wpc.py 映射为 PoTokenProviderRejectedRequest；
    - 让 POT 框架安静跳过 WPC，尝试下一个 provider；
    - 不启动浏览器。
    """
    cfg = WPC_RUNTIME_CONFIG.launch_guard

    if not cfg.enabled:
        return

    if not cfg.memory_check_enabled:
        return

    snapshot = _read_memory_snapshot()

    if snapshot is None:
        logger.warning(
            "[wpc-launch-guard] psutil is not available; "
            "skip memory precheck, but concurrency slot guard still works"
        )
        return

    required_available_mb = max(
        cfg.min_available_mb,
        cfg.reserved_mb + cfg.min_free_after_launch_mb,
    )

    if snapshot.available_mb < required_available_mb:
        raise WPCRejectedRequest(
            "[wpc-launch-guard] reject launching browser because available memory is too low; "
            f"available_mb={snapshot.available_mb}; "
            f"required_available_mb={required_available_mb}; "
            f"total_mb={snapshot.total_mb}; "
            f"used_percent={snapshot.used_percent:.2f}; "
            f"reserved_mb={cfg.reserved_mb}; "
            f"min_free_after_launch_mb={cfg.min_free_after_launch_mb}; "
            f"min_available_mb={cfg.min_available_mb}"
        )

    if snapshot.used_percent >= cfg.max_memory_percent:
        raise WPCRejectedRequest(
            "[wpc-launch-guard] reject launching browser because memory usage is too high; "
            f"used_percent={snapshot.used_percent:.2f}; "
            f"max_memory_percent={cfg.max_memory_percent:.2f}; "
            f"available_mb={snapshot.available_mb}; "
            f"total_mb={snapshot.total_mb}"
        )


def _slot_file_is_stale(path: pathlib.Path, stale_seconds: float) -> bool:
    """
    判断槽位文件是否已经 stale。

    说明：
    - WPC 进程可能被 kill，导致 slot 文件没有释放；
    - 通过文件 mtime 判断是否 stale；
    - 超过 stale_seconds 后允许清理。

    注意：
    - 这里不强行校验 pid 是否存活；
    - 跨平台 pid 检测复杂，而且 pid 可能复用；
    - mtime stale 对这里的用途足够。
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False

    return time.time() - stat.st_mtime > stale_seconds


def _cleanup_stale_slots(
    *,
    guard_dir: str,
    logger,
) -> None:
    """
    清理陈旧的 WPC 启动槽位文件。

    为什么需要：
    - 浏览器启动过程中，进程可能被 yt-dlp / systemd / 外层 timeout kill；
    - 这会导致 slot 文件残留；
    - 若不清理，后续 WPC 会一直认为槽位满了。

    注意：
    - 清理失败只记录 warning；
    - 不因为某个 stale 文件清理失败而中断整个 provider。
    """
    cfg = WPC_RUNTIME_CONFIG.launch_guard

    if cfg.slot_stale_seconds <= 0:
        return

    guard_path = pathlib.Path(guard_dir)

    for path in guard_path.glob("slot-*.json"):
        try:
            if _slot_file_is_stale(path, cfg.slot_stale_seconds):
                path.unlink(missing_ok=True)
                logger.debug(
                    f"[wpc-launch-guard] removed stale slot file: {path}"
                )
        except Exception as e:
            logger.warning(
                "[wpc-launch-guard] failed to remove stale slot file: "
                f"path={path}, error={e!r}"
            )


def _try_acquire_slot(logger) -> WPCLaunchSlot | None:
    """
    尝试获取一个跨进程 WPC 浏览器启动槽位。

    实现方式：
    - 在 launch_guard 目录下创建 slot-N.json；
    - 使用 os.O_CREAT | os.O_EXCL 原子创建；
    - 创建成功表示获得槽位；
    - 如果所有 slot 都已存在，表示并发已满。

    为什么不用 asyncio.Semaphore:
    - yt-dlp 高并发通常是多进程；
    - 进程内 semaphore 只能限制当前进程；
    - 文件槽位可以跨进程生效。

    返回：
    - WPCLaunchSlot：获取成功；
    - None：槽位已满。
    """
    cfg = WPC_RUNTIME_CONFIG.launch_guard

    if not cfg.enabled:
        return WPCLaunchSlot(slot_path="", token="disabled")

    if cfg.max_concurrent <= 0:
        return WPCLaunchSlot(slot_path="", token="unlimited")

    guard_dir = _get_launch_guard_dir()
    _cleanup_stale_slots(guard_dir=guard_dir, logger=logger)

    token = uuid.uuid4().hex
    pid = os.getpid()

    for slot_index in range(cfg.max_concurrent):
        slot_path = pathlib.Path(guard_dir) / f"slot-{slot_index}.json"

        payload = {
            "token": token,
            "pid": pid,
            "created_at": time.time(),
            "monotonic_created_at": _now(),
            "slot_index": slot_index,
        }

        try:
            fd = os.open(
                str(slot_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except FileExistsError:
            continue

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()

                try:
                    os.fsync(f.fileno())
                except Exception:
                    # 某些文件系统上 fsync 可能不可用；
                    # 这里不影响槽位语义。
                    pass

        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass

            try:
                slot_path.unlink(missing_ok=True)
            except Exception:
                pass

            raise

        logger.debug(
            "[wpc-launch-guard] acquired launch slot. "
            f"slot_index={slot_index}; "
            f"max_concurrent={cfg.max_concurrent}; "
            f"path={slot_path}"
        )

        return WPCLaunchSlot(slot_path=str(slot_path), token=token)

    return None


def _release_slot(slot: WPCLaunchSlot, logger) -> None:
    """
    释放 WPC 浏览器启动槽位。

    释放时会校验 token：
    - 如果文件里的 token 和当前 slot.token 一致，才删除；
    - 如果不一致，说明该 slot 文件可能已经被其他进程重建，不删除。

    对 disabled / unlimited 模式：
    - slot_path 为空，直接返回。
    """
    if not slot.slot_path:
        return

    path = pathlib.Path(slot.slot_path)

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return
    except Exception:
        data = {}

    if data.get("token") != slot.token:
        logger.warning(
            "[wpc-launch-guard] skip releasing slot because token mismatch. "
            f"path={slot.slot_path}"
        )
        return

    try:
        path.unlink(missing_ok=True)
        logger.debug(
            f"[wpc-launch-guard] released launch slot. path={slot.slot_path}"
        )
    except Exception as e:
        logger.warning(
            "[wpc-launch-guard] failed to release launch slot. "
            f"path={slot.slot_path}, error={e!r}"
        )


class WPCLaunchGuard:
    """
    WPC 浏览器启动前硬保护。

    保护内容：
    1. 内存 fail-fast 检查；
    2. 跨进程并发槽位限制；
    3. stale slot 清理；
    4. slot 自动释放。

    与 ResourceGate 的区别：
    - ResourceGate 更像“等待资源后再启动”；
    - LaunchGuard 是“资源不足或并发已满就直接拒绝”；
    - LaunchGuard 不会长时间等待；
    - 适合 WPC 作为 bgutil 兜底时，防止高并发瞬间打爆机器。

    典型配置：
    - WPC_LAUNCH_GUARD_ENABLED=1
    - WPC_LAUNCH_GUARD_MAX_CONCURRENT=2
    - WPC_LAUNCH_GUARD_RESERVED_MB=700
    - WPC_LAUNCH_GUARD_MIN_FREE_AFTER_LAUNCH_MB=1024
    - WPC_LAUNCH_GUARD_MAX_MEMORY_PERCENT=85

    抛出的异常：
    - WPCRejectedRequest：
        当前资源条件不允许启动浏览器；
        上层应映射为 PoTokenProviderRejectedRequest；
        这样 yt-dlp POT 框架会安静跳过当前 provider。
    """

    def __init__(self, logger):
        self._logger = logger
        self._slot: WPCLaunchSlot | None = None

    def __enter__(self):
        cfg = WPC_RUNTIME_CONFIG.launch_guard

        if not cfg.enabled:
            return self

        _check_memory_before_launch(self._logger)

        slot = _try_acquire_slot(self._logger)

        if slot is None:
            raise WPCRejectedRequest(
                "[wpc-launch-guard] reject launching browser because WPC launch slots are full; "
                f"max_concurrent={cfg.max_concurrent}; "
                f"slot_stale_seconds={cfg.slot_stale_seconds}"
            )

        self._slot = slot
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._slot:
            _release_slot(self._slot, self._logger)
            self._slot = None

        # 不吞异常。
        return False