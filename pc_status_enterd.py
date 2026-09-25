#!/usr/bin/env python3
"""pc-status-enterd：告诉 PC Status "有人按了 Enter"，并在双屏熄灭期间屏蔽其他输入。

Wayland 下普通程序读不到全局按键，要认出 Enter 只能直接读 /dev/input，这需要 root
（或者把用户加进 input 组，但那样用户的任何程序都能读到所有按键）。所以单独跑一个
root 的小服务，只做两件事：

1. 每按一次 Enter（或小键盘 Enter），把当时的时间戳写进 /run/pc-status/enter。
   除此之外不记录、不转发任何按键。PC Status 息屏后看这个时间戳有没有变新来决定唤醒。

2. 屏蔽输入：PC Status 在两块屏都黑着的时候往 /run/pc-status/grab 写 "1"，这里就用
   EVIOCGRAB 独占键盘、鼠标、触摸板——事件只到这个服务，桌面和任何程序都收不到，
   摸黑乱按也不会误操作（顺带 GNOME 也不会被鼠标唤醒）。按 Enter 时这里自己先解除
   独占（这个 Enter 也被吞掉），不依赖 PC Status：就算 PC Status 崩了，Enter 也能恢复。
   之后要 PC Status 再写一次 "1" 才会重新独占。电源键、合盖、音量等系统按键不屏蔽。

/run/pc-status 由 systemd 的 RuntimeDirectory 创建，服务停掉时会被删掉，PC Status 据此
判断服务在不在。grab 文件的属主设成 PC_STATUS_UID（安装时写进 unit），只有这个用户能写。

只用标准库；装在 root 拥有的 /usr/local/lib/pc-status/ 下，见 install-enterd.sh。
"""

import fcntl
import os
import select
import struct
import time

INPUT_DEVICES = "/proc/bus/input/devices"
OUT_DIR = "/run/pc-status"
ENTER_FILE = os.path.join(OUT_DIR, "enter")
GRAB_FILE = os.path.join(OUT_DIR, "grab")

EV_KEY = 1
KEY_ENTER = 28
KEY_KPENTER = 96
KEY_PRESS = 1
# struct input_event（64 位）：timeval(long, long) + type(u16) + code(u16) + value(s32)
EVENT = struct.Struct("llHHi")
EVIOCGRAB = 0x40044590  # _IOW('E', 0x90, int)
RESCAN_SEC = 5  # 隔一会儿重新找一次设备，接上 USB 键盘/鼠标也能认到
POLL_SEC = 0.25  # 多久看一次 grab 文件


def _has_key(bitmap_words, code):
    # /proc 里的 KEY 位图是一串十六进制的 long，高位在前
    words = bitmap_words.split()
    idx, bit = divmod(code, 64)
    if idx >= len(words):
        return False
    return bool(int(words[-1 - idx], 16) >> bit & 1)


def find_devices():
    """返回 (键盘, 指针设备) 两个 /dev/input/eventN 集合。

    键盘 = 带 Enter 键的 kbd 设备（电源键、合盖开关、音量键这些没有 Enter，不会被选中）；
    指针 = 有 mouseN handler 的设备（鼠标、触摸板、触摸屏）。
    """
    keyboards, pointers = set(), set()
    with open(INPUT_DEVICES) as f:
        blocks = f.read().split("\n\n")
    for block in blocks:
        handlers, keys = [], ""
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
            elif line.startswith("B: KEY="):
                keys = line.split("=", 1)[1]
        events = ["/dev/input/" + h for h in handlers if h.startswith("event")]
        if "kbd" in handlers and keys and (_has_key(keys, KEY_ENTER) or _has_key(keys, KEY_KPENTER)):
            keyboards.update(events)
        elif any(h.startswith("mouse") for h in handlers):
            pointers.update(events)
    return keyboards, pointers


def publish_enter():
    tmp = ENTER_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{time.time():.3f}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, ENTER_FILE)  # 原子替换，读的一方不会读到半截


def read_grab_request():
    """grab 文件的 (内容, 修改时间)；PC Status 每写一次修改时间都会变。"""
    try:
        st = os.stat(GRAB_FILE)
        with open(GRAB_FILE) as f:
            return f.read().strip(), st.st_mtime_ns
    except OSError:
        return "0", 0


class Devices:
    def __init__(self):
        self.fds = {}  # path -> fd
        self.keyboards = set()
        self.grabbed = False

    def rescan(self):
        keyboards, pointers = find_devices()
        wanted = keyboards | pointers
        for path in wanted - self.fds.keys():
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            self.fds[path] = fd
            if self.grabbed:
                self._grab_one(fd, True)
        for path in self.fds.keys() - wanted:
            self.close(path)
        self.keyboards = keyboards

    def close(self, path):
        fd = self.fds.pop(path)
        try:
            os.close(fd)  # 关掉 fd 也会自动解除它的独占
        except OSError:
            pass

    @staticmethod
    def _grab_one(fd, on):
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1 if on else 0)
        except OSError:
            pass

    def set_grab(self, on):
        if on == self.grabbed:
            return
        self.grabbed = on
        for fd in self.fds.values():
            self._grab_one(fd, on)

    def read_ready(self, timeout):
        """读掉所有到达的事件；返回这期间有没有按下 Enter。"""
        if not self.fds:
            time.sleep(timeout)
            return False
        ready, _, _ = select.select(list(self.fds.values()), [], [], timeout)
        enter = False
        for fd in ready:
            path = next((p for p, f in self.fds.items() if f == fd), None)
            if path is None:
                continue
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                continue
            except OSError:
                self.close(path)  # 设备被拔掉了，下次扫描再说
                continue
            if path not in self.keyboards:
                continue  # 鼠标、触摸板的事件读掉丢弃（独占时就这样被屏蔽了）
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _sec, _usec, ev_type, code, value = EVENT.unpack_from(data, off)
                if ev_type == EV_KEY and value == KEY_PRESS and code in (KEY_ENTER, KEY_KPENTER):
                    enter = True
        return enter


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(ENTER_FILE, "w") as f:
        f.write("0\n")
    os.chmod(ENTER_FILE, 0o644)
    with open(GRAB_FILE, "w") as f:
        f.write("0\n")
    os.chmod(GRAB_FILE, 0o644)
    uid = os.environ.get("PC_STATUS_UID")
    if uid:
        os.chown(GRAB_FILE, int(uid), -1)  # 只让 PC Status 的用户能写

    devices = Devices()
    next_scan = 0
    last_request = read_grab_request()
    while True:
        now = time.monotonic()
        if now >= next_scan:
            next_scan = now + RESCAN_SEC
            devices.rescan()

        # 只在 PC Status 新写了 "1" 的时候独占：按 Enter 解除后，就算文件还是 "1"
        # （比如 PC Status 崩了）也不会再锁回去
        request = read_grab_request()
        if request != last_request:
            last_request = request
            devices.set_grab(request[0] == "1")

        if devices.read_ready(POLL_SEC):
            devices.set_grab(False)
            publish_enter()


if __name__ == "__main__":
    main()
