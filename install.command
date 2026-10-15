#!/bin/zsh
cd "${0:A:h}"
if ! command -v python3 >/dev/null; then
  echo "需要 Python 3.9+，请将安装文件交给 GPT 定位内置 Python。"
  read reply
  exit 1
fi
python3 安装迁移skill.py
result=$?
if [ "$result" -ne 0 ]; then echo "安装停止，请查看上方原因。"; fi
read 'reply?按回车关闭此窗口。'
exit "$result"
