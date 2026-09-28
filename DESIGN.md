# PC Status 设计文档

## 1. 需求

一个常驻在屏幕右侧的小窗口：

1. 实时显示 CPU、内存、SSD、网络速度、IP 地址（原有功能）。
2. 一键关闭 / 打开 HDMI 外接显示器，代替拔 HDMI 线（拔插麻烦，也伤接口）。
3. 一键关闭 / 打开笔记本自带屏幕，电脑照常运行，只是屏幕不亮。
4. 无键盘鼠标操作一段时间后，两块屏自动熄灭；时间可调（15 秒测试档，1–120 分），显示倒计时。
5. 两块屏都熄灭时，**只有 Enter 能唤醒**；鼠标、其他按键都不响应，摸黑乱按也不会误操作。
6. PC Status 开着时**合盖不睡眠**（可关），合盖后任务照常跑。

## 2. 运行环境

| 项目 | 本机 |
|---|---|
| 系统 / 桌面 | Ubuntu，GNOME 42.9，**Wayland** 会话 |
| 笔记本屏 | `eDP-1`，背光设备 `intel_backlight`（max 96000） |
| 外接屏 | HP 27m，接 `HDMI-1` |
| 键盘 | 自带键盘 + 2.4G 无线接收器（键盘鼠标套装） |
| 指针 | 两个触摸板、ELECOM OSMOD 7、2.4G 鼠标 |

Wayland 决定了很多设计：普通程序**拿不到全局按键**，也**不能用 xrandr 关输出**，所以屏幕和输入都要通过 GNOME（Mutter）的 D-Bus 接口或内核接口来做。

## 3. 总体架构

```
┌──────────── 用户会话（master）─────────────┐        ┌──────── root ────────┐
│                                            │        │                       │
│  pc_status.py  (tkinter 窗口)              │        │  pc_status_enterd.py  │
│    ├─ 资源监控（psutil）                    │        │  (pc-status-enterd    │
│    ├─ 按钮 / 倒计时 / 唤醒状态机             │        │   .service)           │
│    └─ pc_status_display.py                  │        │                       │
│         ├─ Mutter DisplayConfig  (HDMI、灭屏)│        │  读 /dev/input/event* │
│         ├─ Mutter IdleMonitor    (空闲时间)  │        │  ├─ 认出 Enter        │
│         ├─ GNOME SessionManager  (禁止息屏)  │        │  └─ EVIOCGRAB 独占    │
│         └─ systemd-logind        (背光)      │        │                       │
│                                            │        │                       │
│        读 /run/pc-status/enter  ◀───────────┼────────┤ 写 Enter 时间戳        │
│        写 /run/pc-status/grab   ────────────┼───────▶│ 读 独占请求 (0/1)      │
└────────────────────────────────────────────┘        └───────────────────────┘
```

| 文件 | 作用 |
|---|---|
| `pc_status.py` | 主程序：tkinter 窗口、资源监控、按钮、自动灭屏倒计时、唤醒状态机 |
| `pc_status_common.py` | 格式化工具（字节、速度、IP 列表） |
| `pc_status_display.py` | 所有屏幕 / 输入相关的系统调用，封装成简单函数 |
| `pc_status_enterd.py` | root 小服务：识别 Enter、灭屏期间独占键盘鼠标 |
| `pc-status-enterd.service` | 上面服务的 systemd unit（权限收紧） |
| `install-enterd.sh` | 安装 / 更新服务（需要 sudo） |
| `pc-status-recover.sh` | 紧急恢复：结束 PC Status、解除屏蔽、点亮所有屏幕（§11） |
| `pc_status.desktop` | 桌面菜单项模板 |

## 4. 窗口

- 单一窗口，默认迷你模式，顶部按钮切换详细模式；两种模式共用顶部的按钮，位置固定，来回切换鼠标不用挪。
- 从上到下：模式切换 → 关闭 HDMI → 关闭笔记本屏幕 → ☑ 无操作自动关屏 [时间] → 倒计时 → 资源信息。
- 窗口置顶，贴屏幕右边、从顶部往下 30%。
- **放在哪块屏上**：默认主屏（用 `xrandr` 找 primary）；**笔记本屏熄灭且 HDMI 开着时，自动挪到 HDMI 上**，否则「打开笔记本屏幕」按钮会跟着黑掉、再也点不到（见 §10.1）。HDMI 的位置从 Mutter 读（`hdmi_geometry()`），比 XWayland 的输出名可靠。
- **只允许运行一个**：启动时 `flock ~/.cache/pc_status/pc_status.lock`，拿不到就退出。两个实例会各自倒计时、各自灭屏唤醒，互相打架。
- 每秒 `refresh()` 一次：更新资源信息、两个按钮的状态、倒计时。

## 5. 关闭 / 打开 HDMI

**做法**：通过 Mutter `DisplayConfig.ApplyMonitorsConfig` 把 HDMI 从桌面布局里去掉。显示器收不到信号，效果跟拔线一样，会自己待机。不需要 sudo。

- 用 **临时配置**（method 1），不写 `~/.config/monitors.xml`：重启或重新登录后 HDMI 自动恢复，不会出现开机黑屏。
- 关之前把当前布局（位置、主屏、缩放、分辨率）存到 `~/.cache/pc_status/display_layout.json`，打开时原样恢复；保存的布局失效（换了显示器）时，把 HDMI 用默认分辨率摆到最右边那块屏的右侧、顶端对齐（Mutter 不接受只有对角接触的布局）。
- 布局左上角归一到 (0, 0)，Mutter 要求。
- HDMI 是唯一的屏幕时不允许关。
- 按钮状态：开着 →「关闭 HDMI」，关了 →「打开 HDMI」，没接 →「未连接 HDMI」（灰），非 GNOME →「HDMI 控制不可用」（灰）。

**代价**：HDMI 上的窗口会被 Mutter 挪到笔记本屏上。

**没选的方案**：DDC/CI（`ddcutil setvcp D6 05`）让显示器直接待机——要装软件、配 i2c 权限（sudo），而且有信号时 HP 27m 不一定真的关；HDMI 切换器等硬件方案也可以，但软件方案不用买东西。

## 6. 关闭 / 打开笔记本屏幕

**做法**：背光调到 0。屏幕还在桌面布局里，窗口不会被挪走，只是看不见。

- 写 `/sys/class/backlight/*/brightness` 需要 root；改用 **systemd-logind** 的 `Session.SetBrightness("backlight", 设备, 值)`，当前图形会话的用户可以直接调，不需要 sudo。会话路径从 `login1/user/self` 的 `Display` 属性拿，这样从非图形会话里启动（比如 systemd-run）也能用。
- 多个背光设备时优先 `type=raw`（显卡驱动自己的，如 `intel_backlight`）。
- 关之前把亮度存到 `~/.cache/pc_status/backlight.json`，打开时恢复；找不到就亮一半。
- 状态判断：`brightness == 0` 就是关。

**唤醒规则**（看 HDMI 状态）：

| 状态 | 怎么唤醒 | 原因 |
|---|---|---|
| HDMI 开着 | 只能再点按钮（窗口会挪到 HDMI 上） | 人还在用外接屏，打字按回车不能把笔记本屏弄亮 |
| HDMI 关了 / 没接（两块都黑） | 按 Enter | 两块都黑了，按钮看不见 |

关掉 PC Status 时会把笔记本屏点亮，否则没有东西能唤醒它。

## 7. 无操作自动灭屏

**做法**：Mutter `DisplayConfig` 的 `PowerSaveMode` 属性设成 3（DPMS 关），两块屏一起进入待机——跟 GNOME 自带的息屏一样，不改布局、不动背光，唤醒时两块屏一起亮，窗口位置不变。

- **空闲时间**：Mutter `IdleMonitor.GetIdletime()`（距上次键盘鼠标操作多少毫秒）。
- **倒计时从启动 / 唤醒时刻算起**：取 `min(空闲时间, 距启动或上次唤醒)`。否则电脑已经空闲很久时，一打开 PC Status 就立刻灭屏，连倒计时都看不到（§10.3）。
- **禁止息屏**：`SessionManager.IsInhibited(8)` 为真时（比如全屏播放视频）不灭屏。
- **可选时间**：15 秒（测试用），1 分、2 分……120 分。存在 `~/.config/pc_status/settings.json`（`auto_sleep`、`auto_sleep_seconds`；旧版的 `auto_sleep_minutes` 会自动迁移）。
- **倒计时文字**：

| 状态 | 显示 |
|---|---|
| 倒计时中 | `灭屏倒计时 12 秒`，≥1 分钟时 `灭屏倒计时 1:30` |
| 已灭屏 | `已灭屏，按 Enter 唤醒`（没有 enterd 时 `已灭屏，任意操作唤醒`） |
| 被禁止 | `有程序阻止灭屏（比如在播放视频）` |
| 没勾选 | `自动关屏已关闭` |

- **以实际状态为准**：每秒读一次 `PowerSaveMode`。屏幕实际是灭的而 PC Status 不知道（比如灭屏期间重启了 PC Status、或者手动用命令灭屏），就接管过来，按"已灭屏"处理。

## 8. 只有 Enter 能唤醒 + 灭屏期间屏蔽输入

### 8.1 为什么需要一个 root 服务

Wayland 下普通程序读不到全局按键；GNOME 的 IdleMonitor 只能告诉你"有操作"，分不出是鼠标还是哪个键。要认出 Enter 只能直接读 `/dev/input/event*`，这需要 root 或 `input` 组。

| 方案 | 取舍 |
|---|---|
| **A. 独立的 root 小服务（采用）** | 只把"按了 Enter"的时间戳交出来，其他程序拿不到任何按键 |
| B. 用户加进 `input` 组 | 最简单，但此后用户的**任何程序**都能读到所有按键（包括密码） |

### 8.2 pc-status-enterd 做的两件事

1. **识别 Enter**：监听所有带 Enter 键的键盘，按下 Enter（`KEY_ENTER` 28 或小键盘 `KEY_KPENTER` 96）就把 `time.time()` 原子写入 `/run/pc-status/enter`（写临时文件再 `rename`）。其他按键一概不记录。
2. **屏蔽输入**：PC Status 往 `/run/pc-status/grab` 写 `1` 时，用 `EVIOCGRAB` 独占键盘、鼠标、触摸板——事件只到这个服务，桌面和任何程序都收不到。

**选哪些设备**（解析 `/proc/bus/input/devices`）：

- 键盘：handler 里有 `kbd` 且 KEY 位图包含 Enter。电源键、睡眠键、合盖开关、音量键（Consumer Control）都没有 Enter，自然不会被选中，照常可用。
- 指针：handler 里有 `mouseN`（鼠标、触摸板、触摸屏）。
- 每 5 秒重新扫描一次，插上 USB 键盘鼠标也能认到；独占期间新插的设备也会被独占。

### 8.3 独占的安全设计（不能把人锁在外面）

- **Enter 一定能解除**：服务在独占期间读到 Enter，先自己解除独占，再写时间戳。这个 Enter 也被吞掉，不会送进任何程序。不依赖 PC Status——PC Status 崩了也能恢复。
- **只在"新写入的 1"时独占**：服务比较 `grab` 文件的（内容, 修改时间），变了才处理。按 Enter 解除后，就算文件里还是 `1`（PC Status 崩了没来得及改），也不会再锁回去；PC Status 要再写一次 `1` 才会重新独占。
- PC Status 也只在状态**变化**时写 `grab`，反复写同一个 `1` 会在按了 Enter 之后又锁回去。
- PC Status 正常关闭、被 kill（SIGTERM）时，都会先点亮屏幕、写 `0` 再退出。
- 最后的逃生口：从别的设备（ssh / 网页终端）运行 `pc-status-recover`（§11）；还不行就 `pc-status-recover --stop-enterd`，服务一停，fd 关闭，独占自动解除。

### 8.4 服务的权限收紧

- 代码装到 root 拥有的 `/usr/local/lib/pc-status/`，**不从用户可写的项目目录直接以 root 运行**（否则用户进程改了代码就等于拿到 root）。
- `/run/pc-status` 由 `RuntimeDirectory=` 创建，服务停止时删除；PC Status 看 `enter` 文件在不在来判断服务在不在。
- `grab` 文件的属主设成 `PC_STATUS_UID`（安装脚本用 `$SUDO_UID` 填进 unit），只有这个用户能写；为此只保留 `CAP_CHOWN` 一项能力。
- `DevicePolicy=closed` + `DeviceAllow=char-input r`、`PrivateNetwork=yes`、`ProtectSystem=strict`、`ProtectHome=yes`、`NoNewPrivileges=yes`、`RestrictAddressFamilies=AF_UNIX`、`MemoryDenyWriteExecute=yes` 等。
- 只用 Python 标准库，没有第三方依赖。

### 8.5 PC Status 这边的唤醒判断（WakeWatcher）

- 有 enterd（`/run/pc-status/enter` 存在）：只认 **Enter 时间戳比开始等待的时刻新**。
- 没有 enterd：退回"任意键盘鼠标操作"——看 IdleMonitor 的空闲时间有没有变小（总比屏幕黑了怎么都叫不醒好）。手动关笔记本屏时有 1 秒宽限期，免得点按钮那一下就把屏幕弄亮。
- 用轮询（250 ms）而不是 IdleMonitor 的 `WatchFired` 信号：信号需要 GLib 主循环，tkinter 里没有。

### 8.6 灭屏期间被别的方式点亮

| 情况 | 现象 | 处理 |
|---|---|---|
| 鼠标（没装 enterd 或旧版 enterd） | GNOME 把 `PowerSaveMode` 改回 0 | 装了新版 enterd 后鼠标事件根本到不了 GNOME，不会再发生 |
| **合盖 / 开盖、插拔显示器** | Mutter 重新配置显示器时直接点亮屏幕，但 `PowerSaveMode` **仍然是 3** | 灭屏时记下配置编号（`GetCurrentState` 的 serial），编号变了就强制重新灭屏 |
| `PowerSaveMode` 被改回 0 | — | 有 enterd 时重新灭屏（只认 Enter）；没有 enterd 时接受唤醒 |

"强制重新灭屏"要先设 0 再设 3：`PowerSaveMode` 已经是 3 时再设 3 会被当成没变化忽略掉。已确认设置 `PowerSaveMode` 不会改变 serial，不会自己触发死循环。

### 8.7 什么时候屏蔽输入

`屏蔽 = 自动灭屏中 或 (笔记本屏手动关了 且 HDMI 没开)`，也就是"两块屏都黑着"。只关笔记本屏、HDMI 还开着时不屏蔽——人还在用外接屏。

### 8.8 合盖不睡眠

**问题**：系统默认 `HandleLidSwitch=suspend`，没接外接屏时合盖整台机器睡眠，跑着的任务都停了。接着外接屏时 logind 视为 docked（`HandleLidSwitchDocked=ignore`），本来就不睡眠。

**做法**：勾选「合盖不睡眠」（设置 `lid_no_suspend`，默认开）时，调 logind `Manager.Inhibit("handle-lid-switch", …, "block")` 拿一个锁 fd 并一直持有。

- `handle-lid-switch` 是底层锁，**不受 `LidSwitchIgnoreInhibited=yes` 影响**，总是生效；GNOME 的 gsd-power 接外接屏时也是用它。
- 本地活动会话的用户申请不需要 sudo（polkit 默认允许）。拿不到就弹错误、取消勾选。
- 锁的生命周期跟 fd 一样：取消勾选时 `close`；PC Status 退出、崩溃、被 kill 都会自动释放，系统恢复合盖睡眠。不改 `/etc/systemd/logind.conf`，只在 PC Status 开着时生效。

**合盖 / 开盖时做什么**（每秒读 logind `Manager.LidClosed`，灭屏期间 250 ms 读一次）：

| 事件 | 处理 |
|---|---|
| 合盖，HDMI 没开 | 灭屏 + 屏蔽输入，跟自动灭屏一样只认 Enter。否则只剩笔记本屏时 Mutter 会让它在盖子里一直亮着 |
| 合盖，HDMI 开着 | 不管（合盖用外接屏） |
| 开盖 | 唤醒：灭着的屏点亮；手动关掉的笔记本屏背光也恢复。开盖是明确想用电脑，不算"误操作" |

开盖时 Mutter 也会重新配置显示器（serial 变了），`_poll_auto_wake` 先查开盖再查 serial，不然会被 §8.6 当成"不是 Enter"又灭掉。

**代价**：合盖不锁屏，开盖直接回到桌面。

## 9. 状态与文件

| 路径 | 谁写 | 内容 |
|---|---|---|
| `~/.config/pc_status/settings.json` | PC Status | 自动灭屏开关、时间（秒）、合盖不睡眠开关 |
| `~/.cache/pc_status/display_layout.json` | PC Status | 关 HDMI 前的布局 |
| `~/.cache/pc_status/backlight.json` | PC Status | 关笔记本屏前的亮度 |
| `~/.cache/pc_status/pc_status.lock` | PC Status | 单实例锁 |
| `/run/pc-status/enter` | enterd（root） | 最近一次 Enter 的时间戳 |
| `/run/pc-status/grab` | PC Status（属主是用户） | `1` 独占 / `0` 解除 |

日志：PC Status 把灭屏、唤醒、屏蔽等事件打印到 stdout；用 `systemd-run --user` 启动时进 journal（`journalctl --user -u 'pc-status-*'`）。enterd 的日志 `journalctl -u pc-status-enterd`。

## 10. 过程中踩过的坑（事后补充）

### 10.1 关了笔记本屏就打不开了

HDMI 开着时关掉笔记本屏，按规则只能点按钮打开；但窗口放在主屏（笔记本屏）上，跟着一起黑了，按钮点不到，Enter 按规则也不唤醒。
**修复**：笔记本屏熄灭、HDMI 开着时，窗口自动挪到 HDMI 上（§4）。

### 10.2 显示"已灭屏"，屏幕其实是亮的

当时 enterd 还是旧版（没有输入屏蔽），灭屏后动鼠标，GNOME 自己把屏幕点亮了；PC Status 只认 Enter，不知道屏幕已经亮了，一直显示"已灭屏，按 Enter 唤醒"。
**修复**：以 `PowerSaveMode` 的实际值为准，两个方向都同步（§7、§8.6）。

### 10.3 一启动就灭屏，看不到倒计时

电脑已经空闲超过设定时间时，一打开 PC Status 立刻灭屏。
**修复**：倒计时从启动 / 唤醒时刻算起（§7）。同时出现过菜单里又开了一个实例、两个一起跑的情况，于是加了单实例锁（§4）。

### 10.4 合盖把屏幕点亮了

合盖时 Mutter 停用笔记本屏、重新配置，直接把 HDMI 点亮，`PowerSaveMode` 却还是 3，PC Status 没察觉。
**修复**：灭屏期间监视配置编号，变了就强制重新灭屏（§8.6）。

### 10.5 验证"鼠标会不会唤醒 GNOME 的 DPMS"

最初用户实测：直接设 `PowerSaveMode=3` 后只动鼠标，屏幕不会亮。后来实际使用中又出现过鼠标唤醒。原因没有完全查清（可能跟显示器待机后的重新识别有关），但装了带输入屏蔽的 enterd 之后，鼠标事件根本到不了 GNOME，这个问题不再相关。

## 11. 紧急恢复与手动命令

### 11.1 pc-status-recover

万一 PC Status 出问题（屏幕黑了叫不醒、键盘鼠标被锁住），从别的设备（手机网页终端、ssh）运行：

```sh
pc-status-recover                 # 常规恢复
pc-status-recover --stop-enterd   # 另外用 sudo 停掉 pc-status-enterd（最彻底的解锁）
```

`~/bin/pc-status-recover` 链接到项目里的 `pc-status-recover.sh`。依次：

1. 结束所有 PC Status 实例：先 SIGTERM（PC Status 会自己点亮屏幕、解除屏蔽），2 秒没退出再 SIGKILL。
2. `/run/pc-status/grab` 写 `0`，enterd 解除独占；`--stop-enterd` 时再 `sudo systemctl stop`。
3. `PowerSaveMode` 设 0，点亮屏幕。
4. 笔记本背光是 0 就恢复成关屏前存的亮度（没有就亮一半），走 logind。
5. HDMI 被停用了就重新打开（借用 `pc_status_display.hdmi_on()`）。

设计要点：

- 第 2–4 步只用系统自带的 `busctl`，**不依赖 PC Status 的代码**——PC Status 本身有 bug 时也能恢复。只有第 5 步（恢复 HDMI 布局，太复杂）借用 PC Status 的模块，失败了也不影响前面几步。
- 从网页终端 / ssh 运行时没有桌面会话的环境变量，脚本自己补上 `XDG_RUNTIME_DIR`、`DBUS_SESSION_BUS_ADDRESS`。
- 进程匹配 `python3 .../pc_status(.py)`，脚本自己叫 `pc-status-recover`（连字符），不会误杀自己。
- 实测过：在"两块屏灭屏 + 背光 0 + 输入被独占"的状态下，用几乎为空的环境变量运行，各步骤都恢复成功（PC Status 开着和没开两种情况）。

### 11.2 手动命令

```sh
# 两块屏一起灭屏（PC Status 开着时会在 1 秒内接管：屏蔽输入、只认 Enter）
busctl --user set-property org.gnome.Mutter.DisplayConfig /org/gnome/Mutter/DisplayConfig \
    org.gnome.Mutter.DisplayConfig PowerSaveMode i 3
# 查看当前状态：i 0 亮，i 3 灭
busctl --user get-property org.gnome.Mutter.DisplayConfig /org/gnome/Mutter/DisplayConfig \
    org.gnome.Mutter.DisplayConfig PowerSaveMode
```

注意：PC Status 处于灭屏状态时，用命令设 `i 0` 点亮会被当成"不是 Enter"，0.25 秒内重新灭掉（见 §12）。

## 12. 已知问题 / 待定

- **命令点亮会被重新灭掉**：输入已经被屏蔽，鼠标不会再改 `PowerSaveMode`，现在还会改它的基本只剩手动命令。可以改成"`PowerSaveMode` 被改回 0 时当作正常唤醒接受，只有配置变化（合盖、插拔）才强制重新灭屏"——待用户决定。
- 从执行灭屏命令到 PC Status 接管最多有 1 秒空档，这期间输入还没被屏蔽。
- 按 Enter 唤醒时这个 Enter 被吞掉；但没装 enterd 时（退回模式），唤醒用的按键会照常送给当前窗口。
- 手动"两块都黑"（HDMI 关 + 笔记本屏背光 0）期间合盖 / 开盖，背光会不会被系统改动，没有实测。
- 只支持 GNOME（Mutter 的 D-Bus 接口）。其他桌面上屏幕相关按钮会显示"不可用"，资源监控照常。
- 真机上还没完整走过一遍：自动灭屏 → 乱按 / 动鼠标 / 合盖 → 屏幕保持黑 → Enter 唤醒。
- 合盖不睡眠（§8.8）已实测：合盖不睡眠、屏幕熄灭。窗口高度为新增的复选框按估算加了 27px，可能要微调。
