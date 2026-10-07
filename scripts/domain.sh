#!/bin/sh
# 当前公网入口域名（cpolar free 每次重启都会换，改了就要同步天猫精灵后台）
~/dev/fly-bridge/armv7/box-run.sh 'grep -ohE "https://[a-z0-9.-]+\.cpolar\.[a-z]+" /var/log/cpolar/genie.log.master.log.* 2>/dev/null | tail -1' | tail -1
