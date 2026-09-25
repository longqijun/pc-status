# PC Status

一个常驻在屏幕右侧的小窗口，实时显示 CPU、内存、SSD、网络速度和 IP 地址。

- 默认是迷你模式（CPU / 内存 / SSD / 网络四条进度条），点顶部按钮展开详细模式，可以看到占用量、累计流量和各网卡 IP。
- 窗口始终置顶，贴在主显示器右边、从顶部往下约 30% 的位置；多显示器时通过 `xrandr` 找主显示器。
- 每秒刷新一次。

## 依赖

- Python 3，带 tkinter（Ubuntu：`sudo apt install python3-tk`）
- `pip install -r requirements.txt`（psutil）
- `xrandr`（用来定位主显示器，没有的话退回整个桌面的尺寸）

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
