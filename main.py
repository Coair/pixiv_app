# -*- coding: utf-8 -*-
"""
Pixiv Downloader - 主程序入口
"""
import sys
import os

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台 GBK 下打印 emoji 会崩溃，统一 UTF-8 容错
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from cli import main

if __name__ == '__main__':
    sys.exit(main())
