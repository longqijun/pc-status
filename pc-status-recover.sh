#!/bin/bash
# 紧急恢复：结束 PC Status，解除键盘鼠标屏蔽，点亮所有屏幕。
#
# 给 PC Status 出问题时用（屏幕黑了叫不醒、键盘鼠标被锁住……）。可以从别的设备
# 运行：手机上的网页终端、ssh 都行，不需要在桌面会话里。
#
#   pc-status-recover                 # 常规恢复
#   pc-status-recover --stop-enterd   # 另外用 sudo 停掉 pc-status-enterd（最彻底的解锁）
#
# 解除屏蔽、点亮屏幕、恢复背光这几步只用系统自带的 busctl，不依赖 PC Status 的代码，
# 这样就算 PC Status 本身有 bug 也能恢复。只有恢复 HDMI 布局借用 pc_status_display.py，
# 失败了不影响其他步骤。见 DESIGN.md。
set -u

STOP_ENTERD=0
for arg in "$@"; do
    case "$arg" in
        --stop-enterd) STOP_ENTERD=1 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $arg（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

# 从网页终端 / ssh 运行时没有桌面会话的环境变量，补上才能连到 GNOME
uid=$(id -u)
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$uid}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

PROJECT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
MUTTER=(org.gnome.Mutter.DisplayConfig /org/gnome/Mutter/DisplayConfig org.gnome.Mutter.DisplayConfig)
# 匹配 python3 .../pc_status.py 和 ~/bin/pc_status，不会匹配到这个脚本自己（pc-status-recover）
PC_STATUS_PATTERN='python3? .*/pc_status(\.py)?$'

ok()   { echo "  ✓ $*"; }
warn() { echo "  ✗ $*"; }

echo "1. 结束 PC Status"
pids=$(pgrep -f "$PC_STATUS_PATTERN")
if [ -z "$pids" ]; then
    ok "没有在运行"
else
    # 先正常结束：PC Status 收到 SIGTERM 会自己点亮屏幕、解除屏蔽
    kill $pids 2>/dev/null
    for _ in 1 2 3 4; do
        sleep 0.5
        pids=$(pgrep -f "$PC_STATUS_PATTERN") || break
    done
    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null
        sleep 0.5
    fi
    if pgrep -f "$PC_STATUS_PATTERN" >/dev/null; then
        warn "还有进程没结束：$(pgrep -f "$PC_STATUS_PATTERN" | tr '\n' ' ')"
    else
        ok "已结束"
    fi
fi

echo "2. 解除键盘鼠标屏蔽"
if [ -e /run/pc-status/grab ]; then
    if echo 0 > /run/pc-status/grab 2>/dev/null; then
        ok "已通知 pc-status-enterd 解除独占"
    else
        warn "写不了 /run/pc-status/grab，用 --stop-enterd 停掉服务"
    fi
else
    ok "没有 grab 文件（服务没装或是旧版），不需要解除"
fi
if [ "$STOP_ENTERD" = 1 ]; then
    if sudo systemctl stop pc-status-enterd; then
        ok "已停止 pc-status-enterd（开机会自动启动；现在要启动：sudo systemctl start pc-status-enterd）"
    else
        warn "停止 pc-status-enterd 失败"
    fi
fi

echo "3. 点亮屏幕（关闭省电模式）"
if busctl --user set-property "${MUTTER[@]}" PowerSaveMode i 0 2>/dev/null; then
    ok "PowerSaveMode = $(busctl --user get-property "${MUTTER[@]}" PowerSaveMode | awk '{print $2}')（0 = 亮）"
else
    warn "连不上 GNOME（没有登录桌面？）"
fi

echo "4. 恢复笔记本屏背光"
bl_dir=$(ls -d /sys/class/backlight/*/ 2>/dev/null | head -n 1)
if [ -z "$bl_dir" ]; then
    ok "没有背光设备"
else
    bl_name=$(basename "$bl_dir")
    current=$(cat "$bl_dir/brightness")
    max=$(cat "$bl_dir/max_brightness")
    if [ "$current" -gt 0 ]; then
        ok "背光正常（$current / $max）"
    else
        # 优先用 PC Status 关屏前存下的亮度，没有就亮一半
        target=$(sed -n 's/.*"brightness": *\([0-9]*\).*/\1/p' ~/.cache/pc_status/backlight.json 2>/dev/null)
        if [ -z "$target" ] || [ "$target" -le 0 ] || [ "$target" -gt "$max" ]; then
            target=$((max / 2))
        fi
        session=$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1/user/self \
            org.freedesktop.login1.User Display 2>/dev/null | awk '{print $3}' | tr -d '"')
        if [ -n "$session" ] && [ "$session" != "/" ] && \
            busctl call org.freedesktop.login1 "$session" org.freedesktop.login1.Session \
                SetBrightness ssu backlight "$bl_name" "$target" 2>/dev/null; then
            ok "背光 0 → $(cat "$bl_dir/brightness")"
        else
            warn "调背光失败；可以试 sudo sh -c 'echo $target > ${bl_dir}brightness'"
        fi
    fi
fi

echo "5. 恢复 HDMI"
hdmi_state=$(cd "$PROJECT_DIR" && python3 -c 'import pc_status_display as d; print(d.hdmi_status())' 2>/dev/null)
case "$hdmi_state" in
    on) ok "HDMI 开着" ;;
    absent) ok "没接 HDMI" ;;
    off)
        if (cd "$PROJECT_DIR" && python3 -c 'import pc_status_display as d; d.hdmi_on()' 2>/dev/null); then
            ok "HDMI 已重新打开"
        else
            warn "打开 HDMI 失败；可以在「设置 → 显示器」里打开，或者重新插一下 HDMI 线"
        fi
        ;;
    *) warn "查不到 HDMI 状态（GNOME 连不上？）" ;;
esac
rm -rf "$PROJECT_DIR/__pycache__"

echo
echo "完成。重新打开 PC Status：桌面菜单里点 PC Status，或者运行 pc_status"
