#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tree.py — 目录树工具
返回 JSON：层级目录结构（限制深度避免刷屏）
用法: python tree.py <path> [--max-depth N] [--max-entries M]
"""
import os, sys, json, argparse

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--max-entries", type=int, default=200)
    ap.add_argument("--ignore", default=".git,node_modules,__pycache__,.venv,venv,target,dist,build,.idea,.vscode,obj,bin")
    args = ap.parse_args()
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(json.dumps({"error": f"目录不存在: {root}"}, ensure_ascii=False))
        sys.exit(0)
    ignore = set(x for x in args.ignore.split(",") if x)

    entries = []
    count = [0]

    def walk(dirpath, depth, prefix):
        if count[0] >= args.max_entries:
            return
        try:
            items = sorted(os.listdir(dirpath), key=lambda x: (not os.path.isdir(os.path.join(dirpath, x)), x.lower()))
        except OSError:
            return
        dirs = [d for d in items if os.path.isdir(os.path.join(dirpath, d)) and d not in ignore]
        files = [f for f in items if not os.path.isdir(os.path.join(dirpath, f)) and f not in ignore]
        # 目录先，文件后
        ordered = [(d, True) for d in dirs] + [(f, False) for f in files]
        for i, (name, is_dir) in enumerate(ordered):
            if count[0] >= args.max_entries:
                break
            last = (i == len(ordered) - 1)
            entries.append({
                "level": depth,
                "name": name,
                "type": "dir" if is_dir else "file",
                "branch": prefix + ("└─ " if last else "├─ "),
            })
            count[0] += 1
            if is_dir and depth < args.max_depth:
                child_prefix = prefix + ("    " if last else "│   ")
                walk(os.path.join(dirpath, name), depth + 1, child_prefix)

    walk(root, 0, "")
    result = {
        "path": root,
        "max_depth": args.max_depth,
        "truncated": count[0] >= args.max_entries,
        "entries": entries,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
