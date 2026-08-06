#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grep_code.py — 代码搜索工具
返回 JSON：匹配行 + 文件 + 行号
用法: python grep_code.py <pattern> <path> [--glob '*.rs'] [--max-matches N] [--ignore-case]
"""
import os, sys, json, argparse, re

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", help="搜索的正则或字符串")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--glob", default=None, help="文件过滤，如 '*.rs' 或 '*.py'")
    ap.add_argument("--max-matches", type=int, default=100)
    ap.add_argument("--ignore-case", action="store_true")
    ap.add_argument("--ignore", default=".git,node_modules,__pycache__,.venv,venv,target,dist,build,.idea,.vscode,obj,bin")
    args = ap.parse_args()
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(json.dumps({"error": f"目录不存在: {root}"}, ensure_ascii=False))
        sys.exit(0)
    ignore = set(x for x in args.ignore.split(",") if x)
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        rx = re.compile(args.pattern, flags)
    except re.error as e:
        print(json.dumps({"error": f"正则错误: {e}"}, ensure_ascii=False))
        sys.exit(0)
    glob_ext = None
    if args.glob and '*' in args.glob:
        glob_ext = args.glob.split('.')[-1].rstrip('*').lower()

    matches = []
    scanned = 0

    def match_file(fp):
        nonlocal scanned
        if glob_ext and not fp.lower().endswith('.' + glob_ext):
            return
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                for lineno, line in enumerate(f, 1):
                    if rx.search(line):
                        matches.append({
                            "file": fp,
                            "line": lineno,
                            "text": line.rstrip('\n')[:200],
                        })
                        if len(matches) >= args.max_matches:
                            return True
            scanned += 1
        except OSError:
            pass
        return False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for fn in filenames:
            if match_file(os.path.join(dirpath, fn)):
                break
        if len(matches) >= args.max_matches:
            break

    result = {
        "pattern": args.pattern,
        "path": root,
        "matches": len(matches),
        "scanned_files": scanned,
        "truncated": len(matches) >= args.max_matches,
        "results": matches,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
