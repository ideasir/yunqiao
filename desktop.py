#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云桥桌面客户端启动入口（根目录版）
自动定位到 client/desktop.py 并启动，避免"找不到文件"。
用法: python desktop.py   （在项目根目录或任何位置）
"""
import os
import sys

def main():
    # 定位 client/desktop.py（相对本文件）
    here = os.path.dirname(os.path.abspath(__file__))
    client_entry = os.path.join(here, 'client', 'desktop.py')
    if not os.path.isfile(client_entry):
        print(f"❌ 找不到客户端入口: {client_entry}")
        print("   请确认项目结构完整（应包含 client/ 目录）")
        sys.exit(1)
    # 把 client 目录加入 sys.path，并切换到该目录执行
    client_dir = os.path.dirname(client_entry)
    sys.path.insert(0, client_dir)
    os.chdir(client_dir)
    # 以模块方式执行 client/desktop.py
    import runpy
    runpy.run_path(client_entry, run_name="__main__")

if __name__ == "__main__":
    main()