import datetime
import random
import secrets
import socket
import string

def generate_random_string(length=20):
    """生成适用于 灵匠浏览器 的随机字符串"""
    characters = string.ascii_letters + string.digits  # 包含大小写字母和数字
    return ''.join(secrets.choice(characters) for _ in range(length))


def generate_common_fingerprint_key():
    """生成通用指纹key"""
    short_rand = format(random.getrandbits(32),
                        '08x')  # 使用 32 位随机数生成 8 位十六进制字符串，8位十六进制数相当于32位随机数，提供约42亿种组合，对于大部分应用场景来说已足够避免冲突
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")  # 获取当前时间字符串，格式为 YYYYMMDDHHMMSS
    return f"{short_rand}-{timestamp}"  # 拼接指纹 key


def is_valid_uint64(s: str) -> bool:
    """判断是否是 uint64"""
    try:
        # 尝试将字符串转换为 uint64
        value = int(s)
        # 检查值是否在 uint64 的范围内
        return 0 <= value <= 0xFFFFFFFFFFFFFFFF
    except ValueError:
        return False


def convert_string_to_uint64(s: str) -> int:
    """将 字符串 转为 uint64"""
    # 如果字符串本身符合 uint64 范围，直接转换
    if is_valid_uint64(s):
        return int(s)

    # 否则，使用 FNV-1a 哈希算法生成 uint64 值
    fnv_offset_basis = 0xcbf29ce484222325
    fnv_prime = 0x100000001b3

    h = fnv_offset_basis
    for ch in s.encode('utf-8'):
        h ^= ch
        h *= fnv_prime
        h &= 0xffffffffffffffff  # 保持在 64 位范围内

    # 如果得到的是负值，需要转换为无符号 uint64（在 Python 中作为有符号 int 返回时，调整为无符号）
    if h < 0:
        return h + 2 ** 64
    return h


def is_valid_uint32(s: str) -> bool:
    """判断是否是 uint32"""
    try:
        value = int(s)
        return 0 <= value <= 0xFFFFFFFF
    except ValueError:
        return False


def convert_string_to_uint32(s: str) -> int:
    """将字符串转为 uint32"""
    # 如果符合 uint32 直接转换
    if is_valid_uint32(s):
        return int(s)

    # 否则，使用 FNV-1a 32 位哈希算法生成 uint32
    fnv_offset_basis = 0x811c9dc5
    fnv_prime = 0x01000193

    h = fnv_offset_basis
    for ch in s.encode('utf-8'):
        h ^= ch
        h = (h * fnv_prime) & 0xffffffff  # 确保32位范围

    return h


def get_available_port():
    """
    获取系统空闲的动态端口
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # 绑定到0表示让系统自动分配一个空闲端口
        s.bind(('', 0))
        return s.getsockname()[1]


