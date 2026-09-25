#!/usr/bin/env python3
"""PC Status：单一窗口，默认迷你模式，可一键展开/收起完整信息。"""

import re
import subprocess
import time
import tkinter as tk
from tkinter import ttk

import psutil

from pc_status_common import bytes_to_human, get_ip_lines, speed_to_human

UPDATE_MS = 1000
DISK_PATH = "/"
# 网络没有天然的"满格"概念，这里把 5MB/s 视为满格，仅用来给一个直观的活跃度参考。
NET_BAR_MAX = 5 * 1024 * 1024

MINI_SIZE = (190, 209)
FULL_SIZE = (572, 299)
# 启动位置：贴屏幕右边，从顶部往下约 30% 处
TOP_RATIO = 0.30


class Gauge(ttk.Frame):
    """完整模式的一行：标题 + 进度条 + 详情文字。"""

    def __init__(self, parent, title):
        super().__init__(parent)
        ttk.Label(self, text=title, width=10, anchor="w").pack(side="left")
        self.bar = ttk.Progressbar(self, length=220, maximum=100)
        self.bar.pack(side="left", padx=8)
        self.detail = ttk.Label(self, text="--", width=28, anchor="w")
        self.detail.pack(side="left")

    def update(self, percent, detail_text):
        self.bar["value"] = max(0, min(100, percent))
        self.detail.config(text=detail_text)


class MiniGauge(ttk.Frame):
    """迷你模式的一行：标题 + 小进度条 + 数值。"""

    def __init__(self, parent, title):
        super().__init__(parent)
        ttk.Label(self, text=title, width=4, anchor="w").pack(side="left")
        self.bar = ttk.Progressbar(self, length=80, maximum=100)
        self.bar.pack(side="left", padx=4)
        self.value = ttk.Label(self, text="--", width=8, anchor="e")
        self.value.pack(side="left")

    def update(self, percent, text):
        self.bar["value"] = max(0, min(100, percent))
        self.value.config(text=text)


class PCStatusApp(tk.Tk):
    def __init__(self):
        super().__init__(className="pc_status")
        self.title("PC Status")
        self.resizable(False, False)
        self.attributes("-topmost", True)

        self._last_net = psutil.net_io_counters()
        self._last_time = time.time()

        self.container = ttk.Frame(self)
        self.container.pack(fill="both", expand=True)

        # 单个固定在顶部的切换按钮，精简/详细两种模式共用同一个控件、同一个位置，
        # 这样来回切换时鼠标完全不用挪动。
        self.toggle_btn = ttk.Button(self.container, command=self.toggle_mode)
        self.toggle_btn.pack(fill="x", padx=8, pady=(8, 0))

        self.body_frame = ttk.Frame(self.container)
        self.body_frame.pack(fill="both", expand=True)

        self._build_mini()
        self._build_full()
        self.mode = "mini"
        self.show_mini()

        self.after(100, self.refresh)

    def _build_mini(self):
        self.mini_frame = ttk.Frame(self.body_frame, padding=8)

        self.mini_cpu = MiniGauge(self.mini_frame, "CPU")
        self.mini_cpu.pack(fill="x", pady=3)
        self.mini_mem = MiniGauge(self.mini_frame, "内存")
        self.mini_mem.pack(fill="x", pady=3)
        self.mini_disk = MiniGauge(self.mini_frame, "SSD")
        self.mini_disk.pack(fill="x", pady=3)
        self.mini_net = MiniGauge(self.mini_frame, "网络")
        self.mini_net.pack(fill="x", pady=3)

    def _build_full(self):
        self.full_frame = ttk.Frame(self.body_frame, padding=14)

        self.full_cpu = Gauge(self.full_frame, "CPU")
        self.full_cpu.pack(fill="x", pady=4)
        self.full_mem = Gauge(self.full_frame, "内存")
        self.full_mem.pack(fill="x", pady=4)
        self.full_disk = Gauge(self.full_frame, "SSD")
        self.full_disk.pack(fill="x", pady=4)

        ttk.Separator(self.full_frame).pack(fill="x", pady=8)

        net_frame = ttk.Frame(self.full_frame)
        net_frame.pack(fill="x", pady=4)
        ttk.Label(net_frame, text="网络", width=10, anchor="w").pack(side="left")
        self.full_net_label = ttk.Label(net_frame, text="--", anchor="w")
        self.full_net_label.pack(side="left")

        ttk.Separator(self.full_frame).pack(fill="x", pady=8)

        ttk.Label(
            self.full_frame, text="IP 地址", anchor="w", font=("", 10, "bold")
        ).pack(fill="x")
        self.full_ip_label = ttk.Label(
            self.full_frame, text="--", anchor="w", justify="left"
        )
        self.full_ip_label.pack(fill="x", pady=(2, 0))

    def _primary_monitor_geometry(self):
        """多显示器时，winfo_screenwidth/height 拿到的是整个虚拟桌面尺寸，
        会导致定位算到别的屏幕上；改用 xrandr 找主显示器的真实几何。"""
        try:
            out = subprocess.run(
                ["xrandr", "--current"], capture_output=True, text=True, timeout=2
            ).stdout
            m = re.search(r"^\S+ connected primary (\d+)x(\d+)\+(\d+)\+(\d+)", out, re.M)
            if not m:
                m = re.search(r"^\S+ connected (\d+)x(\d+)\+(\d+)\+(\d+)", out, re.M)
            if m:
                w, h, x, y = (int(v) for v in m.groups())
                return w, h, x, y
        except Exception:
            pass
        return self.winfo_screenwidth(), self.winfo_screenheight(), 0, 0

    def _place(self, width, height):
        mon_w, mon_h, mon_x, mon_y = self._primary_monitor_geometry()
        x = mon_x + mon_w - width
        y = mon_y + int(mon_h * TOP_RATIO)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def show_mini(self):
        self.full_frame.pack_forget()
        self.mini_frame.pack(fill="both", expand=True)
        self._place(*MINI_SIZE)
        self.mode = "mini"
        self.toggle_btn.config(text="详细模式")

    def show_full(self):
        self.mini_frame.pack_forget()
        self.full_frame.pack(fill="both", expand=True)
        self._place(*FULL_SIZE)
        self.mode = "full"
        self.toggle_btn.config(text="精简模式")

    def toggle_mode(self):
        if self.mode == "mini":
            self.show_full()
        else:
            self.show_mini()

    def refresh(self):
        cpu_percent = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(DISK_PATH)

        now = time.time()
        net = psutil.net_io_counters()
        elapsed = max(now - self._last_time, 1e-6)
        up_speed = (net.bytes_sent - self._last_net.bytes_sent) / elapsed
        down_speed = (net.bytes_recv - self._last_net.bytes_recv) / elapsed
        total_speed = up_speed + down_speed
        self._last_net = net
        self._last_time = now

        self.mini_cpu.update(cpu_percent, f"{cpu_percent:.0f}%")
        self.mini_mem.update(mem.percent, f"{mem.percent:.0f}%")
        self.mini_disk.update(disk.percent, f"{disk.percent:.0f}%")
        self.mini_net.update(
            total_speed / NET_BAR_MAX * 100, speed_to_human(total_speed)
        )

        self.full_cpu.update(cpu_percent, f"{cpu_percent:.1f}%")
        self.full_mem.update(
            mem.percent,
            f"{mem.percent:.1f}%  ({bytes_to_human(mem.used)} / {bytes_to_human(mem.total)})",
        )
        self.full_disk.update(
            disk.percent,
            f"{disk.percent:.1f}%  ({bytes_to_human(disk.used)} / {bytes_to_human(disk.total)})",
        )
        self.full_net_label.config(
            text=(
                f"↑ {speed_to_human(up_speed)}   ↓ {speed_to_human(down_speed)}   "
                f"(累计发送 {bytes_to_human(net.bytes_sent)} / 接收 {bytes_to_human(net.bytes_recv)})"
            )
        )
        ip_lines = get_ip_lines()
        self.full_ip_label.config(
            text="\n".join(ip_lines) if ip_lines else "未检测到网络接口"
        )

        self.after(UPDATE_MS, self.refresh)


if __name__ == "__main__":
    app = PCStatusApp()
    app.mainloop()
