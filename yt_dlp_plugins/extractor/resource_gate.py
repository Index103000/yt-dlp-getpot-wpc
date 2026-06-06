# =============================================================================
# 资源门禁模型说明（以 8C16G 机器为例）
# =============================================================================
# 本模块用于控制“高资源开销任务”的启动节奏，例如：
# - 启动 WPC 浏览器
# - 启动 bgutil 的高开销子进程
# - 其他会明显占用内存的任务
#
# -----------------------------------------------------------------------------
# 一、为什么不能只看“某一瞬间的 available memory”
# -----------------------------------------------------------------------------
# 如果只在启动前读取一次系统内存，例如：
#   available_mb = 3200
# 然后直接判断“3200MB 好像还够”，就存在明显风险：
#
# 1. 这个值只是某一瞬间的快照，可能有抖动；
# 2. 其他进程也可能在几乎同一时刻看到“资源够了”；
# 3. 新任务（例如浏览器）启动后，内存通常不会立刻涨到最终值，
#    而是会在后续几秒内继续增长；
# 4. 因此，仅看单次瞬时值，容易过于乐观，导致多个重任务一起启动，
#    进而把机器内存打满，甚至触发 swap 或 OOM。
#
# 所以这里采用的是：
#   “滑动窗口采样 + reservation（预占位）+ 启动门闩锁”
# 的组合方案。
#
#
# -----------------------------------------------------------------------------
# 二、核心判断逻辑
# -----------------------------------------------------------------------------
# 当前是否允许再启动一个新任务，不是看“此刻 available 是否大于某个阈值”，
# 而是看：
#
#   最近一段采样窗口中的最差内存情况，
#   再减去当前其他待启动任务的预留预算，
#   再减去本次新任务自己的预算之后，
#   系统是否仍然安全。
#
# 当前核心公式为：
#
#   window_min_available_mb
#   - pending_reserved_mb
#   - additional_reserved_mb
#   >= min_free_after_launch_mb
#
# 并且同时要求：
#
#   window_max_used_percent <= max_memory_percent
#
# 其中：
# - window_min_available_mb:
#     最近 N 次采样中，available_mb 的最小值
# - pending_reserved_mb:
#     当前所有“已登记 reservation、但任务可能尚未完全吃满内存”的预留总量
# - additional_reserved_mb:
#     本次准备启动的新任务，还要再额外预留的预算
# - min_free_after_launch_mb:
#     启动该任务后，系统至少还希望保留的可用内存
# - window_max_used_percent:
#     最近 N 次采样中，used_percent 的最大值
# - max_memory_percent:
#     系统允许继续启动新任务的最大内存使用率
#
#
# -----------------------------------------------------------------------------
# 三、为什么要有 reservation（预占位）
# -----------------------------------------------------------------------------
# reservation 的作用是：
#   把“即将到来的内存占用”提前算进去。
#
# 举例：
# - 进程 A 刚刚准备启动浏览器
# - 但浏览器从启动到完全稳定，通常要过几秒，RSS 还会继续上涨
# - 如果在这期间，进程 B 再次检查资源，
#   只看当前 available memory，可能会误以为“资源还够”
# - 然后 B 也去启动浏览器
#
# 这样就会出现：
#   两个浏览器都在“尚未完全体现到系统内存统计里”的阶段，
#   最终总内存被同时吃满。
#
# 为了解决这个问题：
# - 在真正启动重任务前，先登记一个 reservation
# - reservation 表示：“我马上会再消耗大约 X MB”
# - 后续其他进程做判断时，要把这些 reservation 总量一起扣掉
#
# 这样即使某个浏览器还没完全涨到最终 RSS，
# 也已经在准入判断里提前占位了。
#
#
# -----------------------------------------------------------------------------
# 四、为什么还需要启动门闩锁（gate lock）
# -----------------------------------------------------------------------------
# 即使有 reservation，如果多个进程几乎同时做判断，也可能发生：
# - 它们在写 reservation 之前，都认为“资源够”
# - 然后一起进入启动阶段
#
# 所以这里还需要一个短时“启动门闩锁”：
# - 它只保护“登记 reservation + 门内二次判断 + 真正启动”这一小段
# - 不会锁住整个任务运行期
#
# 这样可以避免多个进程在同一时刻同时穿透判断。
#
#
# -----------------------------------------------------------------------------
# 五、以 8C16G 机器为例
# -----------------------------------------------------------------------------
# 假设机器规格：
# - CPU: 8 核
# - 内存: 16GB
#
# 粗略换算后：
# - 总内存约 = 16 * 1024 = 16384 MB
#
# 若当前默认参数如下：
#
#   DEFAULT_RESERVED_MB = 900
#   DEFAULT_MIN_FREE_AFTER_LAUNCH_MB = 1500
#   DEFAULT_MAX_MEMORY_PERCENT = 85.0
#   DEFAULT_MEMORY_SAMPLE_COUNT = 5
#   DEFAULT_MEMORY_SAMPLE_INTERVAL_SECONDS = 0.5
#
# 它们的含义分别是：
#
# 1) DEFAULT_RESERVED_MB = 900
#    表示每启动 1 个新重任务，先按“额外占用 900MB”做保守预算
#    这个值不是最终精确 RSS，而是准入判断里的预算值
#
# 2) DEFAULT_MIN_FREE_AFTER_LAUNCH_MB = 1500
#    表示即使启动这个任务后，系统也至少还要保留 1500MB 可用内存
#
# 3) DEFAULT_MAX_MEMORY_PERCENT = 85.0
#    表示采样窗口内，只要最大内存使用率超过 85%，就不允许再启动新任务
#
#    对 16GB 机器来说：
#      16384 * 0.85 = 13926.4 MB
#    也就是说，当系统已使用内存大约超过 13926MB 时，
#    会因为内存使用率过高而被拦下。
#
# 4) DEFAULT_MEMORY_SAMPLE_COUNT = 5
#    DEFAULT_MEMORY_SAMPLE_INTERVAL_SECONDS = 0.5
#    表示每次判断时，连续采样 5 次，每次间隔 0.5 秒，
#    整个采样窗口约 2 秒。
#
#
# -----------------------------------------------------------------------------
# 六、示例 1：允许启动
# -----------------------------------------------------------------------------
# 假设当前 8C16G 机器的最近一轮采样窗口结果是：
# - 最近 5 次 available_mb 分别为：
#     3200, 3000, 2800, 3100, 2950
#   则：
#     window_min_available_mb = 2800
#
# - 最近 5 次 used_percent 分别为：
#     79.0, 80.5, 82.1, 81.0, 80.8
#   则：
#     window_max_used_percent = 82.1
#
# 同时，当前没有其他 reservation：
#   pending_reserved_mb = 0
#
# 现在本次准备启动 1 个新任务：
#   additional_reserved_mb = 900
#
# 代入公式：
#
#   2800 - 0 - 900 = 1900
#
# 再判断：
# - 1900 >= 1500        -> 成立
# - 82.1 <= 85.0        -> 成立
#
# 因此：
#   允许启动
#
#
# -----------------------------------------------------------------------------
# 七、示例 2：因为已有 reservation 太多而不允许启动
# -----------------------------------------------------------------------------
# 假设当前 8C16G 机器的最近窗口结果是：
# - window_min_available_mb = 4200
# - window_max_used_percent = 78.0
#
# 此时已经有 2 个其他进程登记了 reservation，
# 每个 reservation 预留 900MB：
#
#   pending_reserved_mb = 1800
#
# 现在第 3 个进程也准备再启动 1 个新任务：
#
#   additional_reserved_mb = 900
#
# 代入公式：
#
#   4200 - 1800 - 900 = 1500
#
# 判断：
# - 1500 >= 1500        -> 刚好成立
# - 78.0 <= 85.0        -> 成立
#
# 因此：
#   仍允许启动
#
#
# -----------------------------------------------------------------------------
# 八、示例 3：再多一个就不允许启动
# -----------------------------------------------------------------------------
# 如果在上一个例子基础上，再来第 4 个进程：
#
# 此时已有 3 个 reservation：
#   pending_reserved_mb = 2700
#
# 再代入：
#
#   4200 - 2700 - 900 = 600
#
# 判断：
# - 600 >= 1500         -> 不成立
#
# 因此：
#   不允许启动，需要等待其他任务释放资源
#
#
# -----------------------------------------------------------------------------
# 九、这套模型的目的
# -----------------------------------------------------------------------------
# 这套模型不是为了精确估算某个任务的最终 RSS，
# 而是为了在多进程竞争资源时，提供一个“偏保守、可落地、能明显降低 OOM 风险”的
# 启动准入机制。
#
# 核心思想可以概括为：
# 1. 不信任单次瞬时采样
# 2. 用滑动窗口看最近一段时间内的“最差值”
# 3. 用 reservation 提前计算“即将到来的内存占用”
# 4. 用短时门闩锁避免多个进程同一瞬间一起穿透判断
#
# 如果后续不同任务的实际内存成本不同，只需要调整：
# - reserved_mb
# - min_free_after_launch_mb
# - max_memory_percent
# 等参数即可复用同一套门禁逻辑。
# =============================================================================

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass

import psutil

# =============================================================================
# 默认参数
# =============================================================================
# Windows 平台下，文件锁抢占失败后的轮询等待时间（秒）。
WINDOWS_LOCK_RETRY_INTERVAL_SECONDS = 0.05

# 默认单个“重任务”启动预算（MB）。
# 这里不是精确 RSS，而是资源准入时使用的保守预算值。
DEFAULT_RESERVED_MB = 500

# 默认要求“启动这个重任务之后，系统至少还剩多少可用内存（MB）”。
DEFAULT_MIN_FREE_AFTER_LAUNCH_MB = 2000

# 默认允许的最大内存使用率（百分比）。
DEFAULT_MAX_MEMORY_PERCENT = 80.0

# reservation 默认过期时间（秒）。
# 若进程异常退出，未清理 reservation，则超过该 TTL 后会被视为僵尸记录并删除。
DEFAULT_RESERVATION_TTL_SECONDS = 120

# 默认滑动窗口采样次数。
DEFAULT_MEMORY_SAMPLE_COUNT = 5

# 默认滑动窗口采样间隔（秒）。
DEFAULT_MEMORY_SAMPLE_INTERVAL_SECONDS = 0.5

# 默认在资源不足时，下一轮整体判断前的等待时间（秒）。
DEFAULT_RETRY_INTERVAL_SECONDS = 2.0


# =============================================================================
# 数据结构
# =============================================================================
@dataclass
class MemorySnapshot:
    """
    某一时刻的系统内存快照。

    字段说明：
    - total_bytes:
        系统总内存
    - available_bytes:
        当前系统可用内存
    - used_percent:
        当前系统内存使用率（百分比）
    - swap_total_bytes:
        系统 swap 总量
    - swap_used_bytes:
        当前已使用 swap
    """
    total_bytes: int
    available_bytes: int
    used_percent: float
    swap_total_bytes: int
    swap_used_bytes: int

    @property
    def total_mb(self) -> int:
        return int(self.total_bytes / 1024 / 1024)

    @property
    def available_mb(self) -> int:
        return int(self.available_bytes / 1024 / 1024)

    @property
    def swap_total_mb(self) -> int:
        return int(self.swap_total_bytes / 1024 / 1024)

    @property
    def swap_used_mb(self) -> int:
        return int(self.swap_used_bytes / 1024 / 1024)


@dataclass
class MemoryWindowStats:
    """
    滑动窗口内的内存统计结果。

    字段说明：
    - min_available_mb:
        最近 N 次采样中，available_mb 的最小值
    - max_used_percent:
        最近 N 次采样中，used_percent 的最大值
    - latest_swap_used_mb:
        最近一次采样时的 swap_used_mb
    - sample_count:
        实际采样次数
    """
    min_available_mb: int
    max_used_percent: float
    latest_swap_used_mb: int
    sample_count: int


@dataclass
class LaunchReservation:
    """
    单个“重任务启动预占位”的数据结构。

    字段说明：
    - reservation_id:
        本次 reservation 的唯一标识
    - pid:
        创建 reservation 的进程 PID
    - created_at:
        创建时间（time.time() 的秒级时间戳）
    - reserved_mb:
        本次预留的内存预算（MB）
    """
    reservation_id: str
    pid: int
    created_at: float
    reserved_mb: int

    def to_dict(self) -> dict:
        """
        序列化为可写入 JSON 文件的字典。
        """
        return {
            'reservation_id': self.reservation_id,
            'pid': self.pid,
            'created_at': self.created_at,
            'reserved_mb': self.reserved_mb,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'LaunchReservation':
        """
        从 JSON 字典反序列化为 LaunchReservation。
        """
        return cls(
            reservation_id=str(data['reservation_id']),
            pid=int(data['pid']),
            created_at=float(data['created_at']),
            reserved_mb=int(data['reserved_mb']),
        )


# =============================================================================
# 文件锁
# =============================================================================
class FileLock:
    """
    一个简单的跨进程独占文件锁。

    平台支持：
    - Linux/macOS: fcntl.flock
    - Windows: msvcrt.locking

    设计目标：
    - 不依赖第三方锁库
    - 适合当前插件场景
    - 仅用于短临界区控制

    使用方式：
        with FileLock(lock_path):
            ...
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
# ResourceGate
# =============================================================================
class ResourceGate:
    """
    通用“重任务启动资源门禁”。

    这个类不关心你要启动的是：
    - 浏览器
    - Node / Deno
    - 其他重任务

    它只负责一件事：

        “当前系统资源是否允许再启动一个高开销任务？”

    核心机制包含四部分：
    1. 滑动窗口内存采样
    2. reservation（启动预占位）
    3. 启动门闩锁
    4. 资源准入判断

    为什么需要 reservation：
    - 一个新任务刚开始启动时，系统 available memory 不一定立刻下降到最终水平
    - 如果只看“当前瞬时 available”，会过于乐观
    - reservation 的作用就是把“即将到来的内存占用”提前算进去

    为什么需要启动门闩锁：
    - 多个进程可能在同一时刻都看到“资源足够”
    - 如果没有门闩锁，它们会一起启动，导致瞬时穿透保护
    - 门闩锁只覆盖“登记 reservation + 门内二次判断 + 真正启动”这一小段
    - 不覆盖整个任务运行期，避免把所有重任务全串行化
    """

    def __init__(
        self,
        *,
        gate_name: str,
        base_dir: str,
        reserved_mb: int = DEFAULT_RESERVED_MB,
        min_free_after_launch_mb: int = DEFAULT_MIN_FREE_AFTER_LAUNCH_MB,
        max_memory_percent: float = DEFAULT_MAX_MEMORY_PERCENT,
        reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
        sample_count: int = DEFAULT_MEMORY_SAMPLE_COUNT,
        sample_interval_seconds: float = DEFAULT_MEMORY_SAMPLE_INTERVAL_SECONDS,
        retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    ):
        """
        初始化 ResourceGate。

        参数说明：
        - gate_name:
            当前门禁名称，用于区分不同类型的重任务
            例如：
            - 'wpc_browser_launch'
            - 'bgutil_runtime_launch'
        - base_dir:
            门禁运行目录
            该目录下会自动创建：
            - reservations/
            - gate.lock
        - reserved_mb:
            单个新任务的预留内存预算（MB）
        - min_free_after_launch_mb:
            启动该任务后，系统至少还应剩余多少可用内存（MB）
        - max_memory_percent:
            允许启动时，滑动窗口内最大内存使用率上限
        - reservation_ttl_seconds:
            reservation 文件的过期时间
        - sample_count:
            单次判断时的滑动窗口采样次数
        - sample_interval_seconds:
            滑动窗口采样间隔
        - retry_interval_seconds:
            若当前不允许启动，多久后重试一次
        """
        self.gate_name = gate_name
        self.base_dir = os.path.abspath(base_dir)
        self.reserved_mb = int(reserved_mb)
        self.min_free_after_launch_mb = int(min_free_after_launch_mb)
        self.max_memory_percent = float(max_memory_percent)
        self.reservation_ttl_seconds = int(reservation_ttl_seconds)
        self.sample_count = int(sample_count)
        self.sample_interval_seconds = float(sample_interval_seconds)
        self.retry_interval_seconds = float(retry_interval_seconds)

        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.reservations_dir, exist_ok=True)

    # -------------------------------------------------------------------------
    # 路径属性
    # -------------------------------------------------------------------------
    @property
    def gate_lock_path(self) -> str:
        """
        返回启动门闩锁文件路径。
        """
        return os.path.join(self.base_dir, f'{self.gate_name}.gate.lock')

    @property
    def reservations_dir(self) -> str:
        """
        返回 reservation 目录路径。
        """
        return os.path.join(self.base_dir, f'{self.gate_name}.reservations')

    def get_reservation_file_path(self, reservation_id: str) -> str:
        """
        返回某个 reservation_id 对应的 reservation 文件路径。
        """
        return os.path.join(self.reservations_dir, f'{reservation_id}.json')

    # -------------------------------------------------------------------------
    # 内存采样
    # -------------------------------------------------------------------------
    @staticmethod
    def get_system_memory_snapshot() -> MemorySnapshot:
        """
        获取当前系统内存快照。
        """
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()

        return MemorySnapshot(
            total_bytes=int(vm.total),
            available_bytes=int(vm.available),
            used_percent=float(vm.percent),
            swap_total_bytes=int(sm.total),
            swap_used_bytes=int(sm.used),
        )

    def sample_memory_window(self) -> MemoryWindowStats:
        """
        连续采样一段时间内的系统内存状态，并生成滑动窗口统计结果。

        统计逻辑：
        - min_available_mb:
            最近 N 次采样中 available_mb 的最小值
        - max_used_percent:
            最近 N 次采样中 used_percent 的最大值
        - latest_swap_used_mb:
            最后一次采样时的 swap_used_mb

        为什么这样做：
        - 避免只看单次瞬时值造成误判
        - 用“最差值”进行保守判断，更适合多进程抢资源场景
        """
        snapshots: list[MemorySnapshot] = []

        for idx in range(max(1, self.sample_count)):
            snapshots.append(self.get_system_memory_snapshot())
            if idx != self.sample_count - 1:
                time.sleep(self.sample_interval_seconds)

        return MemoryWindowStats(
            min_available_mb=min(s.available_mb for s in snapshots),
            max_used_percent=max(s.used_percent for s in snapshots),
            latest_swap_used_mb=snapshots[-1].swap_used_mb,
            sample_count=len(snapshots),
        )

    # -------------------------------------------------------------------------
    # reservation 管理
    # -------------------------------------------------------------------------
    def write_reservation(self, reservation: LaunchReservation) -> str:
        """
        写入一个 reservation 文件。

        返回：
        - reservation 文件路径
        """
        reservation_path = self.get_reservation_file_path(reservation.reservation_id)
        temp_path = f'{reservation_path}.tmp.{os.getpid()}.{time.time_ns()}'

        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(reservation.to_dict(), f, ensure_ascii=False, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, reservation_path)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

        return reservation_path

    @staticmethod
    def remove_reservation(reservation_path: str) -> None:
        """
        删除 reservation 文件。

        文件不存在时视为正常情况。
        """
        try:
            if os.path.exists(reservation_path):
                os.remove(reservation_path)
        except OSError:
            pass

    def load_active_reservations(self, *, logger) -> list[LaunchReservation]:
        """
        读取当前所有有效 reservation。

        同时清理：
        1. JSON 损坏的 reservation
        2. 内容非法的 reservation
        3. 超过 TTL 的僵尸 reservation

        返回：
        - 当前仍有效的 reservation 列表
        """
        now_ts = time.time()
        active: list[LaunchReservation] = []

        for name in os.listdir(self.reservations_dir):
            if not name.endswith('.json'):
                continue

            path = os.path.join(self.reservations_dir, name)

            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                reservation = LaunchReservation.from_dict(raw)
            except Exception as e:
                logger.warning(
                    f'Invalid reservation file removed. path={path}, e={e!r}',
                    once=True,
                )
                self.remove_reservation(path)
                continue

            # 清理过期 reservation。
            if now_ts - reservation.created_at > self.reservation_ttl_seconds:
                logger.debug(
                    f'Removed expired reservation. '
                    f'path={path}, age_seconds={now_ts - reservation.created_at:.2f}'
                )
                self.remove_reservation(path)
                continue

            active.append(reservation)

        return active

    def get_total_pending_reserved_mb(self, *, logger) -> int:
        """
        统计当前所有有效 reservation 的总预留内存（MB）。
        """
        return sum(item.reserved_mb for item in self.load_active_reservations(logger=logger))

    # -------------------------------------------------------------------------
    # 准入判断
    # -------------------------------------------------------------------------
    def is_launch_allowed(
        self,
        *,
        memory_window: MemoryWindowStats,
        pending_reserved_mb: int,
        additional_reserved_mb: int,
    ) -> bool:
        """
        根据滑动窗口采样结果 + 当前 pending reservation 总量，判断是否允许启动新任务。

        判定逻辑：
        1. 最近 N 次采样中的最大内存使用率 <= self.max_memory_percent
        2. 最近 N 次采样中的最小可用内存
           - 当前已有 pending reservation 总量
           - 本次准备额外预留的预算
           之后，仍 >= self.min_free_after_launch_mb

        公式：
            memory_window.min_available_mb
            - pending_reserved_mb
            - additional_reserved_mb
            >= self.min_free_after_launch_mb
        """
        if memory_window.max_used_percent > self.max_memory_percent:
            return False

        projected_free_mb = (
            int(memory_window.min_available_mb)
            - int(pending_reserved_mb)
            - int(additional_reserved_mb)
        )

        return projected_free_mb >= self.min_free_after_launch_mb

    def wait_until_allowed(self, *, logger) -> None:
        """
        在门外循环等待，直到当前资源允许再启动一个新任务。

        这里的判断会把：
        - 当前滑动窗口的最差内存值
        - 当前已存在的 reservation 总量
        - 本次任务的 reserved_mb

        一起纳入考虑。
        """
        while True:
            memory_window = self.sample_memory_window()
            pending_reserved_mb = self.get_total_pending_reserved_mb(logger=logger)

            if self.is_launch_allowed(
                memory_window=memory_window,
                pending_reserved_mb=pending_reserved_mb,
                additional_reserved_mb=self.reserved_mb,
            ):
                return

            projected_free_mb = (
                memory_window.min_available_mb
                - pending_reserved_mb
                - self.reserved_mb
            )

            logger.debug(
                f'[{self.gate_name}] launch delayed due to memory pressure. '
                f'min_available_mb={memory_window.min_available_mb}, '
                f'max_used_percent={memory_window.max_used_percent:.2f}, '
                f'pending_reserved_mb={pending_reserved_mb}, '
                f'reserved_mb={self.reserved_mb}, '
                f'projected_free_mb={projected_free_mb}, '
                f'min_free_after_launch_mb={self.min_free_after_launch_mb}, '
                f'latest_swap_used_mb={memory_window.latest_swap_used_mb}. '
                f'Waiting {self.retry_interval_seconds:.2f}s before retry.'
            )
            time.sleep(self.retry_interval_seconds)

    # -------------------------------------------------------------------------
    # 启动许可
    # -------------------------------------------------------------------------
    class _LaunchPermission:
        """
        ResourceGate 内部使用的启动许可上下文。

        生命周期：
        1. 进入上下文时：
           - 获取门闩锁
           - 创建 reservation
           - 在门内再次判断资源
        2. 用户在上下文中执行“真正的启动动作”
        3. 退出上下文时：
           - 删除 reservation
           - 释放门闩锁

        说明：
        - 这里 reservation 覆盖的是“启动阶段”
        - 适合保护“浏览器启动前后那段尚未完全稳定的资源爬升期”
        """

        def __init__(self, gate: 'ResourceGate', logger):
            self.gate = gate
            self.logger = logger
            self.gate_lock = None
            self.reservation = None
            self.reservation_path = None

        def __enter__(self):
            # 先进入门闩锁，避免多个进程同时穿透判断。
            self.gate_lock = FileLock(self.gate.gate_lock_path)
            self.gate_lock.__enter__()

            try:
                # 在门内登记本次 reservation。
                self.reservation = LaunchReservation(
                    reservation_id=str(uuid.uuid4()),
                    pid=os.getpid(),
                    created_at=time.time(),
                    reserved_mb=self.gate.reserved_mb,
                )
                self.reservation_path = self.gate.write_reservation(self.reservation)

                # 门内再判断一次资源。
                # 注意：因为 reservation 已经写入，所以 pending_reserved_mb 已经包含“我自己”的预算。
                while True:
                    memory_window = self.gate.sample_memory_window()
                    pending_reserved_mb = self.gate.get_total_pending_reserved_mb(logger=self.logger)

                    if self.gate.is_launch_allowed(
                        memory_window=memory_window,
                        pending_reserved_mb=pending_reserved_mb,
                        additional_reserved_mb=0,
                    ):
                        break

                    projected_free_mb = (
                        memory_window.min_available_mb
                        - pending_reserved_mb
                    )

                    self.logger.debug(
                        f'[{self.gate.gate_name}] launch still delayed inside gate. '
                        f'min_available_mb={memory_window.min_available_mb}, '
                        f'max_used_percent={memory_window.max_used_percent:.2f}, '
                        f'pending_reserved_mb={pending_reserved_mb}, '
                        f'projected_free_mb={projected_free_mb}, '
                        f'min_free_after_launch_mb={self.gate.min_free_after_launch_mb}, '
                        f'latest_swap_used_mb={memory_window.latest_swap_used_mb}. '
                        f'Waiting {self.gate.retry_interval_seconds:.2f}s before retry.'
                    )
                    time.sleep(self.gate.retry_interval_seconds)

                return self
            except Exception:
                self._cleanup_reservation()
                if self.gate_lock:
                    self.gate_lock.__exit__(None, None, None)
                    self.gate_lock = None
                raise

        def __exit__(self, exc_type, exc, tb):
            try:
                self._cleanup_reservation()
            finally:
                if self.gate_lock:
                    self.gate_lock.__exit__(exc_type, exc, tb)
                    self.gate_lock = None

        def _cleanup_reservation(self):
            """
            删除 reservation 文件。
            """
            if self.reservation_path:
                self.gate.remove_reservation(self.reservation_path)
                self.reservation_path = None

    def acquire_launch_permission(self, *, logger):
        """
        获取“启动许可”。

        推荐用法：

            gate.wait_until_allowed(logger=logger)

            with gate.acquire_launch_permission(logger=logger):
                # 这里执行真正的重任务启动动作
                ...

        为什么分成两步：
        - `wait_until_allowed()` 是门外等待，避免在资源明显不足时还去争抢门闩锁
        - `acquire_launch_permission()` 是门内短临界区，负责：
          1. 门内登记 reservation
          2. 门内二次判断
          3. 为真正启动动作提供受保护的窗口
        """
        return self._LaunchPermission(self, logger)