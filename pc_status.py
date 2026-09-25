#!/usr/bin/env python3
"""PC Status：单一窗口，默认迷你模式，可一键展开/收起完整信息。"""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

import psutil

from pc_status_common import bytes_to_human, get_ip_lines, speed_to_human

try:
    import pc_status_display
except ImportError:  # 没有 PyGObject（非 GNOME 桌面），屏幕相关功能都不可用
    pc_status_display = None

UPDATE_MS = 1000
DISK_PATH = "/"
# 网络没有天然的"满格"概念，这里把 5MB/s 视为满格，仅用来给一个直观的活跃度参考。
NET_BAR_MAX = 5 * 1024 * 1024

MINI_SIZE = (190, 331)
FULL_SIZE = (572, 421)
# 启动位置：贴屏幕右边，从顶部往下约 30% 处
TOP_RATIO = 0.30
# 两块屏都黑着时，每隔多久查一次有没有键盘/鼠标操作
WAKE_POLL_MS = 250
# 点完按钮后要先安静这么久，之后的操作才算"唤醒"，免得点按钮那一下就把屏幕弄亮
WAKE_GRACE_MS = 1000

# 无操作自动关屏的设置（开关 + 秒数），界面上改了就存
SETTINGS_FILE = os.path.expanduser("~/.config/pc_status/settings.json")
DEFAULT_SETTINGS = {"auto_sleep": True, "auto_sleep_seconds": 60}
# 可选的灭屏时间：15 秒（测试用），然后 1 分、2 分……120 分
AUTO_SLEEP_CHOICES = [15] + [m * 60 for m in range(1, 121)]


def sleep_choice_label(seconds):
    return f"{seconds}秒" if seconds < 60 else f"{seconds // 60}分"


def log(msg):
    # 用 systemd-run 启动时输出进 journal：journalctl --user -u 'pc-status-*'
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    # 旧版本存的是分钟数
    if "auto_sleep_minutes" in saved and "auto_sleep_seconds" not in saved:
        saved["auto_sleep_seconds"] = saved["auto_sleep_minutes"] * 60
    saved.pop("auto_sleep_minutes", None)
    settings.update(saved)
    if settings["auto_sleep_seconds"] not in AUTO_SLEEP_CHOICES:
        settings["auto_sleep_seconds"] = DEFAULT_SETTINGS["auto_sleep_seconds"]
    return settings


def save_settings(settings):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f)
    except OSError:
        pass


class WakeWatcher:
    """判断 start() 之后有没有"唤醒操作"。

    装了 pc-status-enterd（见 pc_status_enterd.py）就只认 Enter 键，鼠标和其他键都不算。
    没装的话 Wayland 下拿不到具体按键，只能退回 GNOME IdleMonitor 的"距上次操作多久"：
    这个数字变小就说明有人动了键盘或鼠标——总比屏幕黑了怎么都叫不醒好。
    grace_ms：退回模式下先要安静这么久才开始算，免得点按钮那一下就把屏幕弄亮。
    """

    def __init__(self, grace_ms=0):
        self.grace_ms = grace_ms

    def start(self):
        self.since = time.time()
        self.grace_done = self.grace_ms == 0
        try:
            self.last_idle = pc_status_display.idle_time_ms()
        except pc_status_display.DisplayError:
            self.last_idle = 0

    def triggered(self):
        enter = pc_status_display.last_enter_time()
        if enter is not None:
            if enter > self.since:
                log(f"wake: Enter at {time.strftime('%H:%M:%S', time.localtime(enter))}")
                return True
            return False
        try:
            idle = pc_status_display.idle_time_ms()
        except pc_status_display.DisplayError:
            return False
        woke = self.grace_done and idle < self.last_idle
        if woke:
            log(f"wake: input activity (no enterd), idle {self.last_idle} -> {idle} ms")
        if not self.grace_done:
            self.grace_done = idle >= self.grace_ms
        self.last_idle = idle
        return woke


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

        # 关闭 / 打开 HDMI 外接显示器，同样两种模式共用，见 pc_status_display.py
        self.hdmi_btn = ttk.Button(self.container, command=self.toggle_hdmi)
        self.hdmi_btn.pack(fill="x", padx=8, pady=(4, 0))
        self._hdmi_state = None

        # 关闭 / 打开笔记本屏幕（背光调到 0）。唤醒规则见 _sync_wake()
        self.laptop_btn = ttk.Button(self.container, command=self.toggle_laptop_screen)
        self.laptop_btn.pack(fill="x", padx=8, pady=(4, 0))
        self._laptop_state = None
        self._wake_armed = False
        self._laptop_wake = WakeWatcher(grace_ms=WAKE_GRACE_MS)

        # 无操作一段时间后两块屏一起息屏，按 Enter 唤醒。见 _check_auto_sleep()
        self.settings = load_settings()
        self.auto_sleep_var = tk.BooleanVar(value=self.settings["auto_sleep"])
        self.auto_sleep_time_var = tk.StringVar(
            value=sleep_choice_label(self.settings["auto_sleep_seconds"]))
        auto_row = ttk.Frame(self.container)
        auto_row.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Checkbutton(
            auto_row, text="无操作自动关屏", variable=self.auto_sleep_var,
            command=self._save_auto_sleep,
        ).pack(side="left")
        ttk.Spinbox(
            auto_row, values=[sleep_choice_label(s) for s in AUTO_SLEEP_CHOICES],
            width=5, state="readonly", wrap=False,
            textvariable=self.auto_sleep_time_var, command=self._save_auto_sleep,
        ).pack(side="left", padx=(4, 0))
        # 倒计时 / 当前状态，每秒在 refresh 里更新
        self.countdown_label = ttk.Label(self.container, text="", anchor="w")
        self.countdown_label.pack(fill="x", padx=10, pady=(2, 0))
        self._asleep = False
        self._auto_wake = WakeWatcher()
        # 倒计时从这个时刻算起（启动、唤醒时重置）：不然电脑已经空闲很久时，
        # 一打开 PC Status 就立刻灭屏，连倒计时都看不到
        self._countdown_start = time.monotonic()
        # 两块屏都黑着时屏蔽键盘鼠标，见 _sync_input_block()
        self._input_blocked = False

        self.protocol("WM_DELETE_WINDOW", self.on_close)

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

    def _target_monitor_geometry(self):
        # 笔记本屏黑着、HDMI 开着的时候，窗口放到 HDMI 上：
        # 这时只能点「打开笔记本屏幕」按钮来唤醒，窗口要是留在黑屏上就再也点不到了。
        if self._laptop_state == "off" and self._hdmi_state == "on":
            try:
                geometry = pc_status_display.hdmi_geometry()
            except pc_status_display.DisplayError:
                geometry = None
            if geometry:
                return geometry
        return self._primary_monitor_geometry()

    def _place(self, width, height):
        mon_w, mon_h, mon_x, mon_y = self._target_monitor_geometry()
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

    def _update_hdmi_btn(self):
        try:
            state = pc_status_display.hdmi_status() if pc_status_display else "error"
        except Exception:
            state = "error"
        if state == self._hdmi_state:
            return
        self._hdmi_state = state
        text = {
            "on": "关闭 HDMI",
            "off": "打开 HDMI",
            "absent": "未连接 HDMI",
            "error": "HDMI 控制不可用",
        }[state]
        self.hdmi_btn.config(text=text, state="normal" if state in ("on", "off") else "disabled")

    def toggle_hdmi(self):
        try:
            if self._hdmi_state == "on":
                pc_status_display.hdmi_off()
            elif self._hdmi_state == "off":
                pc_status_display.hdmi_on()
        except pc_status_display.DisplayError as e:
            messagebox.showerror("PC Status", f"切换 HDMI 失败：{e}", parent=self)
        self._update_hdmi_btn()
        self._sync_wake()
        # 屏幕布局变了（主屏坐标会跟着移动），等 Mutter 调整完再把窗口放回右边
        self.after(1500, self._replace)

    def _replace(self):
        self._place(*(MINI_SIZE if self.mode == "mini" else FULL_SIZE))

    def _update_laptop_btn(self):
        try:
            state = pc_status_display.laptop_screen_status() if pc_status_display else "error"
        except Exception:
            state = "error"
        if state == self._laptop_state:
            return
        first = self._laptop_state is None
        self._laptop_state = state
        if not first or state == "off":  # 启动时就是黑的也要挪
            self.after(300, self._replace)
        text = {
            "on": "关闭笔记本屏幕",
            "off": "打开笔记本屏幕",
            "absent": "没有笔记本屏幕",
            "error": "屏幕控制不可用",
        }[state]
        self.laptop_btn.config(text=text, state="normal" if state in ("on", "off") else "disabled")

    def toggle_laptop_screen(self):
        try:
            if self._laptop_state == "on":
                pc_status_display.laptop_screen_off()
            elif self._laptop_state == "off":
                pc_status_display.laptop_screen_on()
        except pc_status_display.DisplayError as e:
            messagebox.showerror("PC Status", f"切换笔记本屏幕失败：{e}", parent=self)
        self._update_laptop_btn()
        self._sync_wake()

    # 笔记本屏幕黑着的时候怎么唤醒，看 HDMI：
    #   - HDMI 开着：人还在用外接屏，鼠标键盘都不能把笔记本屏弄亮，只能再点按钮。
    #   - HDMI 关了 / 没接：两块屏都黑了，按 Enter 就亮（怎么认 Enter 见 WakeWatcher）。
    # 事件都用轮询：IdleMonitor 的信号要 GLib 主循环，Tk 里没有。
    def _sync_wake(self):
        want = self._laptop_state == "off" and self._hdmi_state != "on"
        if want and not self._wake_armed:
            self._wake_armed = True
            self._laptop_wake.start()
            self.after(WAKE_POLL_MS, self._poll_wake)
        elif not want:
            self._wake_armed = False  # 轮询自己会停
        self._sync_input_block()

    def _sync_input_block(self):
        """两块屏都黑着（自动灭屏，或手动关了笔记本屏且 HDMI 没开）时，让 pc-status-enterd
        独占键盘鼠标，摸黑乱按不会误操作；只有 Enter 能解除（enterd 自己会解除）。
        只在状态变化时写：enterd 按"新写入的 1"来独占，反复写会在按了 Enter 之后又锁回去。"""
        want = self._asleep or self._wake_armed
        if want == self._input_blocked or not pc_status_display:
            return
        self._input_blocked = want
        if pc_status_display.set_input_blocked(want):
            log(f"input {'blocked' if want else 'unblocked'}")

    def _poll_wake(self):
        if not self._wake_armed:
            return
        if self._laptop_wake.triggered():
            log("laptop wake: backlight on")
            self._wake_armed = False
            self._sync_input_block()
            try:
                pc_status_display.laptop_screen_on()
            except pc_status_display.DisplayError:
                pass
            self._update_laptop_btn()
            return
        self.after(WAKE_POLL_MS, self._poll_wake)

    def _auto_sleep_seconds(self):
        label = self.auto_sleep_time_var.get()
        for seconds in AUTO_SLEEP_CHOICES:
            if sleep_choice_label(seconds) == label:
                return seconds
        return self.settings["auto_sleep_seconds"]

    def _save_auto_sleep(self):
        self.settings.update(
            auto_sleep=self.auto_sleep_var.get(), auto_sleep_seconds=self._auto_sleep_seconds())
        save_settings(self.settings)

    def _check_auto_sleep(self):
        """每秒在 refresh 里调一次：更新倒计时，空闲够久了就让两块屏一起息屏。"""
        if not pc_status_display:
            self.countdown_label.config(text="")
            return
        if self._asleep:
            enterd = pc_status_display.last_enter_time() is not None
            self.countdown_label.config(text="已灭屏，按 Enter 唤醒" if enterd else "已灭屏，任意操作唤醒")
            return
        # 屏幕实际已经是灭的（比如灭屏期间重启了 PC Status）：接管过来，等 Enter 唤醒
        try:
            actually_asleep = pc_status_display.screens_asleep()
        except pc_status_display.DisplayError:
            actually_asleep = False
        if actually_asleep:
            log("screens found asleep: waiting for Enter")
            self._enter_asleep()
            return
        if not self.auto_sleep_var.get():
            self.countdown_label.config(text="自动关屏已关闭")
            return
        limit_ms = self._auto_sleep_seconds() * 1000
        try:
            idle = min(
                pc_status_display.idle_time_ms(),
                int((time.monotonic() - self._countdown_start) * 1000),
            )
            if pc_status_display.idle_inhibited():
                self.countdown_label.config(text="有程序阻止灭屏（比如在播放视频）")
                return
            if idle < limit_ms:
                remaining = (limit_ms - idle + 999) // 1000
                text = f"{remaining} 秒" if remaining < 60 else f"{remaining // 60}:{remaining % 60:02d}"
                self.countdown_label.config(text=f"灭屏倒计时 {text}")
                return
            pc_status_display.set_screens_asleep(True)
        except pc_status_display.DisplayError:
            self.countdown_label.config(text="")
            return
        log(f"auto sleep: idle {idle} ms, enterd={'yes' if pc_status_display.last_enter_time() is not None else 'no'}")
        self._enter_asleep()

    def _enter_asleep(self):
        self._asleep = True
        try:
            self._asleep_serial = pc_status_display.display_config_serial()
        except pc_status_display.DisplayError:
            self._asleep_serial = None
        enterd = pc_status_display.last_enter_time() is not None
        self.countdown_label.config(text="已灭屏，按 Enter 唤醒" if enterd else "已灭屏，任意操作唤醒")
        self._sync_input_block()
        self._auto_wake.start()
        self.after(WAKE_POLL_MS, self._poll_auto_wake)

    def _screens_woken_elsewhere(self):
        """灭屏期间屏幕是不是被别的方式点亮了：PowerSaveMode 被改回来了，或者显示器配置
        变了（合盖/开盖、插拔显示器——Mutter 重新配置时会直接点亮屏幕，PowerSaveMode 却还是 3）。"""
        try:
            if not pc_status_display.screens_asleep():
                return "power save mode reset"
            if pc_status_display.display_config_serial() != self._asleep_serial:
                return "display config changed (lid / hotplug)"
        except pc_status_display.DisplayError:
            pass
        return None

    def _poll_auto_wake(self):
        if not self._asleep:
            return
        reason = self._screens_woken_elsewhere()
        if reason:
            if pc_status_display.last_enter_time() is not None:
                # 有 enterd：只认 Enter，别的方式点亮的一律重新灭掉
                log(f"screens woken by {reason}, not Enter: back to sleep")
                try:
                    pc_status_display.force_screens_asleep()
                    self._asleep_serial = pc_status_display.display_config_serial()
                except pc_status_display.DisplayError:
                    pass
            else:
                # 没有 enterd 时本来就是任意操作唤醒，跟实际状态同步就好
                log(f"auto wake: screens woken by {reason}")
                try:
                    # 合盖时屏幕亮了但 PowerSaveMode 还是 3，改成 0 跟实际对齐
                    pc_status_display.set_screens_asleep(False)
                except pc_status_display.DisplayError:
                    pass
                self._asleep = False
                self._sync_input_block()
                self._countdown_start = time.monotonic()
                self._check_auto_sleep()
                return
        if self._auto_wake.triggered():
            log("auto wake: screens on")
            try:
                pc_status_display.set_screens_asleep(False)
            except pc_status_display.DisplayError:
                pass
            self._asleep = False
            self._sync_input_block()
            self._countdown_start = time.monotonic()
            self._check_auto_sleep()  # 马上刷新倒计时，不等下一秒
            return
        self.after(WAKE_POLL_MS, self._poll_auto_wake)

    def on_close(self):
        # 关掉 PC Status 时把屏幕都点亮、解除输入屏蔽，不然就没有东西能唤醒它们了
        if self._input_blocked:
            pc_status_display.set_input_blocked(False)
        if self._asleep:
            try:
                pc_status_display.set_screens_asleep(False)
            except pc_status_display.DisplayError:
                pass
        if self._laptop_state == "off" and pc_status_display:
            try:
                pc_status_display.laptop_screen_on()
            except pc_status_display.DisplayError:
                pass
        self.destroy()

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

        # 也可能在别处改了显示设置、插拔了线、调了亮度，每次都跟着刷新
        self._update_hdmi_btn()
        self._update_laptop_btn()
        self._sync_wake()
        self._check_auto_sleep()

        self.after(UPDATE_MS, self.refresh)


def acquire_single_instance_lock():
    # 同时开两个会各自倒计时、各自灭屏唤醒，互相打架；已经有一个在跑就直接退出
    path = os.path.expanduser("~/.cache/pc_status/pc_status.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("PC Status 已经在运行了", file=sys.stderr)
        sys.exit(0)
    return lock  # 进程活着就一直持有，退出时自动释放


if __name__ == "__main__":
    _lock = acquire_single_instance_lock()
    app = PCStatusApp()
    # 被 kill / 关机 / systemctl stop 时也走 on_close：点亮屏幕、解除输入屏蔽
    signal.signal(signal.SIGTERM, lambda *_: app.after(0, app.on_close))
    app.mainloop()
