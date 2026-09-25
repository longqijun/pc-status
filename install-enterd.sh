#!/bin/sh
# 安装 / 更新 pc-status-enterd（让 PC Status 息屏后只认 Enter 唤醒、屏蔽其他输入）。需要 sudo。
#   sudo ./install-enterd.sh
# 卸载：
#   sudo systemctl disable --now pc-status-enterd && sudo rm -rf /usr/local/lib/pc-status /etc/systemd/system/pc-status-enterd.service
set -e
cd "$(dirname "$0")"
# 允许控制输入屏蔽的用户：执行 sudo 的那个人
uid="${SUDO_UID:?请用 sudo 从自己的账号运行}"
# 以 root 运行的代码放到 root 拥有的目录，不直接从用户可写的项目目录运行
install -d -m 755 /usr/local/lib/pc-status
install -m 644 pc_status_enterd.py /usr/local/lib/pc-status/pc_status_enterd.py
sed "s/@UID@/$uid/" pc-status-enterd.service > /etc/systemd/system/pc-status-enterd.service
chmod 644 /etc/systemd/system/pc-status-enterd.service
systemctl daemon-reload
systemctl enable pc-status-enterd
systemctl restart pc-status-enterd
sleep 1
systemctl --no-pager --lines=0 status pc-status-enterd
