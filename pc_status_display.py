"""关闭 / 打开 HDMI 外接显示器和笔记本屏幕（GNOME Wayland）。

HDMI：

通过 Mutter 的 DisplayConfig D-Bus 接口把 HDMI 从当前布局里去掉：显示器收不到信号，
效果跟拔掉 HDMI 线一样，显示器会自己进入待机。不需要 sudo，也不依赖 DDC/CI。

用的是"临时"配置（不写进 ~/.config/monitors.xml），重启或重新登录后 HDMI 会恢复。
关闭前把当前布局存到 ~/.cache/pc_status/display_layout.json，打开时按原样恢复
（位置、主屏幕、缩放、分辨率），这样来回开关不会把两块屏幕的上下左右关系弄乱。

笔记本屏幕：
把背光调到 0（通过 systemd-logind 的 SetBrightness，不需要 sudo）。屏幕还在桌面布局里，
窗口不会被挪走，只是看不见。关之前的亮度存到 ~/.cache/pc_status/backlight.json，
程序中途退出/崩溃后也能恢复。什么时候唤醒由 pc_status.py 决定。
"""

import json
import os

from gi.repository import Gio, GLib

_BUS_NAME = "org.gnome.Mutter.DisplayConfig"
_OBJ_PATH = "/org/gnome/Mutter/DisplayConfig"
_IFACE = "org.gnome.Mutter.DisplayConfig"
# ApplyMonitorsConfig 的 method：0 只校验，1 临时生效，2 持久保存
_METHOD_TEMPORARY = 1

_LAYOUT_FILE = os.path.expanduser("~/.cache/pc_status/display_layout.json")


class DisplayError(Exception):
    pass


def _call(method, params=None, reply_type=None):
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return bus.call_sync(
            _BUS_NAME, _OBJ_PATH, _IFACE, method, params,
            GLib.VariantType(reply_type) if reply_type else None,
            Gio.DBusCallFlags.NONE, 3000, None,
        )
    except GLib.Error as e:
        raise DisplayError(e.message) from e


def _state():
    serial, monitors, logical, _props = _call("GetCurrentState").unpack()
    return serial, monitors, logical


def _is_hdmi(connector):
    return connector.startswith("HDMI")


def _current_mode(modes):
    for mode in modes:
        if mode[6].get("is-current"):
            return mode[0]
    return None


def _preferred_mode(modes):
    for mode in modes:
        if mode[6].get("is-preferred"):
            return mode[0]
    return modes[0][0] if modes else None


def _snapshot(monitors, logical):
    """把当前布局转成可以直接交给 ApplyMonitorsConfig 的形式。"""
    mode_of = {spec[0]: _current_mode(modes) for spec, modes, _ in monitors}
    return [
        {
            "x": x, "y": y, "scale": scale, "transform": transform, "primary": primary,
            "monitors": [[spec[0], mode_of[spec[0]]] for spec in specs],
        }
        for x, y, scale, transform, primary, specs, _props in logical
    ]


def _apply(serial, layout):
    # 最左上角对齐到 (0, 0)，Mutter 要求布局从原点开始
    min_x = min(l["x"] for l in layout)
    min_y = min(l["y"] for l in layout)
    if not any(l["primary"] for l in layout):
        layout[0]["primary"] = True
    logical = [
        (
            l["x"] - min_x, l["y"] - min_y, l["scale"], l["transform"], l["primary"],
            [(connector, mode, {}) for connector, mode in l["monitors"]],
        )
        for l in layout
    ]
    _call(
        "ApplyMonitorsConfig",
        GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, _METHOD_TEMPORARY, logical, {})),
    )


def hdmi_status():
    """返回 "on" / "off" / "absent"（没有接 HDMI 显示器）。"""
    _serial, monitors, logical = _state()
    if not any(_is_hdmi(spec[0]) for spec, _modes, _props in monitors):
        return "absent"
    for *_head, specs, _props in logical:
        if any(_is_hdmi(spec[0]) for spec in specs):
            return "on"
    return "off"


def hdmi_off():
    serial, monitors, logical = _state()
    layout = _snapshot(monitors, logical)
    remaining = [l for l in layout if not any(_is_hdmi(c) for c, _m in l["monitors"])]
    if len(remaining) == len(layout):
        return  # 已经是关的
    if not remaining:
        raise DisplayError("HDMI 是唯一的显示器，不能关闭")

    os.makedirs(os.path.dirname(_LAYOUT_FILE), exist_ok=True)
    with open(_LAYOUT_FILE, "w") as f:
        json.dump(layout, f)
    _apply(serial, remaining)


def hdmi_on():
    serial, monitors, logical = _state()
    available = {spec[0]: {m[0] for m in modes} for spec, modes, _ in monitors}

    saved = None
    try:
        with open(_LAYOUT_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        pass
    # 保存的布局里的显示器和分辨率现在都还在，才按它恢复（期间可能换过显示器）
    if saved and all(
        c in available and m in available[c] for l in saved for c, m in l["monitors"]
    ):
        _apply(serial, saved)
        return

    # 没有可用的保存布局：把 HDMI 用默认分辨率摆到现有屏幕的右边
    layout = _snapshot(monitors, logical)
    # 跟最右边那块屏顶端对齐，保证两块屏是挨着的（Mutter 不接受只有对角接触的布局）
    rightmost = max(layout, key=lambda l: l["x"] + _width(monitors, l))
    right = rightmost["x"] + _width(monitors, rightmost)
    for spec, modes, _props in monitors:
        if _is_hdmi(spec[0]) and not any(spec[0] == c for l in layout for c, _m in l["monitors"]):
            layout.append({
                "x": right, "y": rightmost["y"], "scale": 1.0, "transform": 0, "primary": False,
                "monitors": [[spec[0], _preferred_mode(modes)]],
            })
            break
    _apply(serial, layout)


def _width(monitors, logical_monitor):
    connector, mode_id = logical_monitor["monitors"][0]
    for spec, modes, _props in monitors:
        if spec[0] == connector:
            for mode in modes:
                if mode[0] == mode_id:
                    return int(mode[1] / logical_monitor["scale"])
    return 0


# ---------- 笔记本屏幕（背光） ----------

_BACKLIGHT_DIR = "/sys/class/backlight"
_BACKLIGHT_FILE = os.path.expanduser("~/.cache/pc_status/backlight.json")


def _backlight():
    """返回 (设备名, 当前亮度, 最大亮度)；没有背光设备返回 None。"""
    try:
        names = sorted(os.listdir(_BACKLIGHT_DIR))
    except OSError:
        return None
    if not names:
        return None
    # 同时有好几个时优先用显卡驱动自己的（intel_backlight 之类，type=raw）
    def rank(name):
        try:
            with open(os.path.join(_BACKLIGHT_DIR, name, "type")) as f:
                return {"raw": 0, "platform": 1, "firmware": 2}.get(f.read().strip(), 3)
        except OSError:
            return 3
    name = min(names, key=rank)
    base = os.path.join(_BACKLIGHT_DIR, name)
    with open(os.path.join(base, "brightness")) as f:
        current = int(f.read())
    with open(os.path.join(base, "max_brightness")) as f:
        maximum = int(f.read())
    return name, current, maximum


def _set_brightness(name, value):
    # 写 /sys 需要 root；logind 允许当前图形会话的用户直接调自己屏幕的背光
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        display = bus.call_sync(
            "org.freedesktop.login1", "/org/freedesktop/login1/user/self",
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.freedesktop.login1.User", "Display")),
            None, Gio.DBusCallFlags.NONE, 3000, None,
        ).unpack()[0]
        session_path = display[1]
        if session_path == "/":
            raise DisplayError("找不到图形会话")
        bus.call_sync(
            "org.freedesktop.login1", session_path, "org.freedesktop.login1.Session",
            "SetBrightness", GLib.Variant("(ssu)", ("backlight", name, value)),
            None, Gio.DBusCallFlags.NONE, 3000, None,
        )
    except GLib.Error as e:
        raise DisplayError(e.message) from e


def laptop_screen_status():
    """返回 "on" / "off" / "absent"（没有可调的背光，比如台式机）。"""
    bl = _backlight()
    if not bl:
        return "absent"
    return "off" if bl[1] == 0 else "on"


def laptop_screen_off():
    bl = _backlight()
    if not bl:
        raise DisplayError("没有找到笔记本屏幕的背光")
    name, current, _maximum = bl
    if current == 0:
        return
    os.makedirs(os.path.dirname(_BACKLIGHT_FILE), exist_ok=True)
    with open(_BACKLIGHT_FILE, "w") as f:
        json.dump({"device": name, "brightness": current}, f)
    _set_brightness(name, 0)


def laptop_screen_on():
    bl = _backlight()
    if not bl:
        raise DisplayError("没有找到笔记本屏幕的背光")
    name, current, maximum = bl
    if current > 0:
        return
    value = maximum // 2  # 找不到保存的亮度就先亮一半
    try:
        with open(_BACKLIGHT_FILE) as f:
            saved = json.load(f)
        if saved.get("device") == name and 0 < saved.get("brightness", 0) <= maximum:
            value = saved["brightness"]
    except (OSError, ValueError):
        pass
    _set_brightness(name, value)


def idle_time_ms():
    """距离上一次键盘/鼠标操作过了多少毫秒（GNOME 的 IdleMonitor）。"""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return bus.call_sync(
            "org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
            "org.gnome.Mutter.IdleMonitor", "GetIdletime", None,
            GLib.VariantType("(t)"), Gio.DBusCallFlags.NONE, 3000, None,
        ).unpack()[0]
    except GLib.Error as e:
        raise DisplayError(e.message) from e


# ---------- 无操作自动关屏（DPMS） ----------
#
# 用 Mutter 的 PowerSaveMode 让所有显示器进入待机（跟 GNOME 自己的"息屏"一样），
# 不改桌面布局、不动背光，唤醒时两块屏一起亮，窗口位置也不变。

_POWER_SAVE_ON = 0
_POWER_SAVE_OFF = 3


def set_screens_asleep(asleep):
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            _BUS_NAME, _OBJ_PATH, "org.freedesktop.DBus.Properties", "Set",
            GLib.Variant("(ssv)", (_IFACE, "PowerSaveMode",
                                   GLib.Variant("i", _POWER_SAVE_OFF if asleep else _POWER_SAVE_ON))),
            None, Gio.DBusCallFlags.NONE, 3000, None,
        )
    except GLib.Error as e:
        raise DisplayError(e.message) from e


def display_config_serial():
    """显示器配置的编号：合盖/开盖、插拔显示器、改布局时都会变。"""
    return _state()[0]


def force_screens_asleep():
    """重新让屏幕休眠。显示器配置变了（比如合盖）时 Mutter 会重新点亮屏幕，但 PowerSaveMode
    还是 3；再设一次 3 会被当成没变化忽略掉，所以先设回 0 再马上设 3，强制它重新下发。"""
    set_screens_asleep(False)
    set_screens_asleep(True)


def screens_asleep():
    """屏幕实际是不是处于省电（熄灭）状态——不管是谁关的、谁开的。"""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        mode = bus.call_sync(
            _BUS_NAME, _OBJ_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (_IFACE, "PowerSaveMode")),
            None, Gio.DBusCallFlags.NONE, 3000, None,
        ).unpack()[0]
    except GLib.Error as e:
        raise DisplayError(e.message) from e
    return mode != _POWER_SAVE_ON


def idle_inhibited():
    """有程序要求不要息屏（比如全屏看视频）时返回 True。"""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return bus.call_sync(
            "org.gnome.SessionManager", "/org/gnome/SessionManager",
            "org.gnome.SessionManager", "IsInhibited", GLib.Variant("(u)", (8,)),  # 8 = idle
            GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 3000, None,
        ).unpack()[0]
    except GLib.Error:
        return False


# ---------- Enter 键唤醒（pc-status-enterd） ----------

_ENTER_FILE = "/run/pc-status/enter"


def last_enter_time():
    """最近一次按 Enter 的时间戳（time.time()）；pc-status-enterd 没在跑就返回 None。"""
    try:
        with open(_ENTER_FILE) as f:
            return float(f.read().strip() or 0)
    except (OSError, ValueError):
        return None


def hdmi_geometry():
    """HDMI 显示器在桌面上的 (宽, 高, x, y)；HDMI 没开返回 None。"""
    _serial, monitors, logical = _state()
    size_of = {}
    for spec, modes, _props in monitors:
        for mode in modes:
            if mode[6].get("is-current"):
                size_of[spec[0]] = (mode[1], mode[2])
    for x, y, scale, _transform, _primary, specs, _props in logical:
        for spec in specs:
            if _is_hdmi(spec[0]) and spec[0] in size_of:
                w, h = size_of[spec[0]]
                return int(w / scale), int(h / scale), x, y
    return None


_GRAB_FILE = "/run/pc-status/grab"


def set_input_blocked(blocked):
    """双屏熄灭期间让 pc-status-enterd 独占键盘鼠标（只有 Enter 能解除）。

    服务没装或版本太旧（没有 grab 文件）时返回 False，什么也不做。
    """
    try:
        with open(_GRAB_FILE, "w") as f:
            f.write("1\n" if blocked else "0\n")
        return True
    except OSError:
        return False
