#!/bin/sh
# MiGPT 触发器在盒子上的启动方式（systemd 的 ExecStart 就是它 / 或直接 node ask.mjs --serve）
cd "$(dirname "$0")" || exit 1
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  export "$line" 2>/dev/null
done < migpt.env
exec node ask.mjs --serve
