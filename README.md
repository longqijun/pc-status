# PC Status

设计说明（为什么这样做、踩过的坑、已知问题）见 [DESIGN.md](DESIGN.md)。

一个常驻在屏幕右侧的小窗口，实时显示 CPU、内存、SSD、网络速度和 IP 地址。

- 默认是迷你模式（CPU / 内存 / SSD / 网络四条进度条），点顶部按钮展开详细模式，可以看到占用量、累计流量和各网卡 IP。
- 窗口始终置顶，贴在主显示器右边、从顶部往下约 30% 的位置；多显示器时通过 `xrandr` 找主显示器。
- 每秒刷新一次。
- 「关闭 HDMI / 打开 HDMI」按钮：把 HDMI 外接显示器从桌面布局里去掉，效果跟拔掉 HDMI 线一样，显示器没信号会自己待机；再点一次按原来的位置恢复。只在 GNOME（Wayland 或 X11）下可用，用的是 Mutter 的 DisplayConfig D-Bus 接口，不需要 sudo。关闭是临时的，重启或重新登录后 HDMI 会恢复。
- 「关闭笔记本屏幕 / 打开笔记本屏幕」按钮：把笔记本屏的背光调到 0，电脑照常运行，窗口也不会被挪走。通过 systemd-logind 调背光，不需要 sudo。唤醒规则：
  - HDMI 开着（还在用外接屏）：只能再点按钮打开，键盘鼠标都不会把它弄亮。
  - HDMI 关了或没接（两块屏都黑）：按 Enter 就亮，鼠标和其他键都不会唤醒（需要 pc-status-enterd，见下）。
  - 关掉 PC Status 时会自动把笔记本屏幕点亮。
- 「无操作自动关屏 [时间]」：勾上后，一段时间（15 秒测试档，或 1–120 分，默认 1 分）没有键盘鼠标操作，两块屏一起进入待机（GNOME 的 DPMS，跟系统自带息屏一样，窗口不动），下面一行显示倒计时。灭屏期间键盘鼠标都被屏蔽，只有 Enter 能唤醒（需要 pc-status-enterd）；合盖、插拔显示器把屏幕点亮时会自动重新灭掉。有程序禁止息屏时（比如全屏看视频）不会关。设置存在 `~/.config/pc_status/settings.json`。
- 「合盖不睡眠」（默认勾上）：PC Status 开着时合上盖子，电脑不睡眠，任务、网络、ssh 照常跑。没接 HDMI 时合盖会灭屏、屏蔽键盘鼠标（跟自动关屏一样，只有 Enter 能唤醒）；接着 HDMI 合盖就是正常的外接屏用法，不灭屏。开盖自动点亮屏幕。取消勾选或关掉 PC Status 后恢复系统默认（合盖睡眠）。
  - 原理：向 systemd-logind 申请 `handle-lid-switch` 锁，不需要 sudo，不改系统配置；PC Status 崩了锁也会自动释放。
  - 注意：合盖不会锁屏，开盖直接回到桌面。放包里时散热差，长时间合盖跑任务最好插电、别闷着。

## 依赖

- Python 3，带 tkinter（Ubuntu：`sudo apt install python3-tk`）
- `pip install -r requirements.txt`（psutil）
- `xrandr`（用来定位主显示器，没有的话退回整个桌面的尺寸）
- HDMI 开关需要 PyGObject（Ubuntu 自带 `python3-gi`）和 GNOME 桌面；没有的话按钮显示"HDMI 控制不可用"，其他功能不受影响

## Enter 键唤醒 + 灭屏期间屏蔽输入：pc-status-enterd

Wayland 下普通程序读不到全局按键，要认出 Enter 只能直接读 `/dev/input`，这需要 root。
所以单独装一个 root 的小服务 `pc_status_enterd.py`，只做两件事：

- 监听 Enter（含小键盘 Enter），按一次就把时间戳写进 `/run/pc-status/enter`，其他按键一概不记录。
- 两块屏都黑着时（PC Status 往 `/run/pc-status/grab` 写 `1`），独占键盘、鼠标、触摸板：
  事件到不了桌面和任何程序，摸黑乱按不会误操作，GNOME 也不会被鼠标唤醒。
  按 Enter 时服务自己先解除独占（这个 Enter 也被吞掉），不依赖 PC Status，PC Status 崩了也能恢复。
  电源键、合盖、音量等系统按键不屏蔽。

服务权限尽量收紧（不能联网、只能访问输入设备、只保留 CAP_CHOWN）。`grab` 文件只有安装时执行 sudo 的用户能写。

## 紧急恢复

万一 PC Status 出问题（屏幕黑了叫不醒、键盘鼠标被锁住、按 Enter 也没反应），从别的设备（手机网页终端、ssh）运行：

```
pc-status-recover                 # 结束 PC Status、解除键盘鼠标屏蔽、点亮所有屏幕、恢复背光和 HDMI
pc-status-recover --stop-enterd   # 另外用 sudo 停掉 pc-status-enterd（最彻底的解锁）
```

安装：`ln -s "$PWD/pc-status-recover.sh" ~/bin/pc-status-recover`

```
sudo ./install-enterd.sh
```

脚本把服务代码复制到 root 拥有的 `/usr/local/lib/pc-status/`（不从用户可写的项目目录直接以 root 运行），
注册并启动 `pc-status-enterd.service`。改了 `pc_status_enterd.py` 之后重新执行一次即可。

没装这个服务时，PC Status 退回"任意键盘鼠标操作都唤醒"，免得屏幕黑了叫不醒。

注意：按 Enter 唤醒时，这个 Enter 也会照常送给当前窗口（比如终端里会执行当前命令行）。

## 运行

```
python3 pc_status.py
```

## 安装到桌面菜单

本机的做法：`~/bin/pc_status` 链接到 `pc_status.py`，方便在命令行直接运行；
桌面菜单项从 `pc_status.desktop` 复制过去，把 `Exec` 改成实际路径：

```
ln -s "$PWD/pc_status.py" ~/bin/pc_status
sed "s#/path/to/pc-status#$PWD#" pc_status.desktop > ~/.local/share/applications/pc_status.desktop
```
