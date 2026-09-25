"""pc_status 用到的工具函数。"""

import socket

import psutil


def bytes_to_human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def speed_to_human(bytes_per_sec):
    return bytes_to_human(bytes_per_sec) + "/s"


def get_ip_lines():
    lines = []
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                lines.append(f"{iface}: {addr.address}")
    return lines
