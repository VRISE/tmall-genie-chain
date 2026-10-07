#!/usr/bin/env bash
# 把 genie/ 推到盒子并重启服务。改一行代码就走这条路，别整目录覆盖（答案库在盒子上会自己长大，覆盖等于把它抹平）。
#
#   BOX=root@192.168.5.42 ./scripts/deploy.sh              # 只推代码
#   BOX=root@192.168.5.42 ./scripts/deploy.sh --with-db    # 明确要连答案库一起覆盖才加这个开关
set -eu
BOX=${BOX:?用法: BOX=root@<盒子IP> ./scripts/deploy.sh [--with-db]}
HERE=$(cd "$(dirname "$0")/.." && pwd)
REMOTE_DIR=${REMOTE_DIR:-/opt/genie42}
UNIT=${UNIT:-genied42}
FILES=(genie/genied.py genie/genielib/plan.py genie/genielib/targets.py genie/genielib/aligenie.py genie/genielib/__init__.py)

for f in "${FILES[@]}"; do
  echo "推 $f"
  scp -q "$HERE/$f" "$BOX:$REMOTE_DIR/${f#genie/}"
done
if [ "${1:-}" = "--with-db" ]; then
  echo "⚠ 覆盖盒子上的答案库（这台机器上的库会自己长大，覆盖前自己确认过没有）"
  scp -q "$HERE/genie/offline/answers.json" "$BOX:/var/lib/genie42/offline/answers.json"
fi
ssh "$BOX" "systemctl restart $UNIT && sleep 1 && systemctl is-active $UNIT && curl -s -m 3 localhost:8092/health"
