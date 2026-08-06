#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stats.py — 目录/文件统计工具
返回 JSON：文件数、目录数、总大小、按扩展名分布、最大的文件、最近修改
用法: python stats.py <path> [--max-depth N]
"""
import os, sys, json, argparse
from collections import Counter

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--max-depth", type=int, default=4)
    ap.add_argument("--ignore", default=".git,node_modules,__pycache__,.venv,venv,target,dist,build,.idea,.vscode")
    args = ap.parse_args()
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(json.dumps({"error": f"目录不存在: {root}"}, ensure_ascii=False))
        sys.exit(0)
    ignore = set(x for x in args.ignore.split(",") if x)

    files = 0
    dirs = 0
    total_bytes = 0
    ext_counter = Counter()
    big_files = []
    recent = []

    for dirpath, dirnames, filenames in os.walk(root):
        # 过滤忽略目录
        dirnames[:] = [d for d in dirnames if d not in ignore]
        depth = dirpath[len(root):].count(os.sep)
        if depth >= args.max_depth:
            dirnames[:] = []
        dirs += 1
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            files += 1
            total_bytes += st.st_size
            ext = os.path.splitext(fn)[1].lower() or "(无扩展名)"
            ext_counter[ext] += 1
            if len(big_files) < 10:
                big_files.append((st.st_size, fp))
                big_files.sort(reverse=True)
                big_files = big_files[:10]
            if len(recent) < 10:
                recent.append((st.st_mtime, fp))
                recent.sort(reverse=True)
                recent = recent[:10]

    def fmt(n):
        for unit in ['B','KB','MB','GB']:
            if n < 1024 or unit == 'GB':
                return f"{n:.1f} {unit}" if unit != 'B' else f"{n} B"
            n /= 1024

    result = {
        "path": root,
        "files": files,
        "dirs": dirs,
        "total_size": total_bytes,
        "total_size_human": fmt(total_bytes),
        "by_extension": [{"ext": k, "count": v} for k, v in ext_counter.most_common(15)],
        "largest_files": [{"size": s, "size_human": fmt(s), "path": p} for s, p in big_files],
        "recently_modified": [{"time": __import__('datetime').datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M'), "path": p} for t, p in recent],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
