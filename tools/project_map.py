#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project_map.py — 项目地图工具
返回 JSON：项目根、巨型文件、源码目录、测试位置、Git 状态、依赖清单、语言分布
用法: python project_map.py <path>
"""
import os, sys, json, argparse

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--max-depth", type=int, default=3)
    args = ap.parse_args()
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(json.dumps({"error": f"目录不存在: {root}"}, ensure_ascii=False))
        sys.exit(0)
    ignore = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'target', 'dist', 'build', '.idea', '.vscode', 'obj', 'bin'}

    # 巨型文件（前 20 大源文件）
    src_exts = {'.rs','.py','.js','.ts','.tsx','.jsx','.go','.java','.cpp','.c','.h','.hpp','.cs','.rb','.php','.kt','.swift','.vue','.mjs','.cjs'}
    big_files = []
    src_counts = {}
    test_dirs = []
    total_src = 0
    has_git = os.path.isdir(os.path.join(root, '.git'))

    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        dirnames[:] = [d for d in dirnames if d not in ignore]
        rel = dirpath[len(root)+1:] if len(dirpath) > len(root) else ''
        rel_l = rel.lower()
        if 'test' in rel_l or 'tests' in rel_l or rel_l.endswith('_test') or rel_l == 'test' or rel_l == 'tests' or rel_l.endswith('/test') or rel_l.endswith('/tests'):
            if rel and rel not in test_dirs:
                test_dirs.append(rel)
        if depth >= args.max_depth:
            dirnames[:] = []
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext in src_exts:
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                total_src += 1
                src_counts[ext] = src_counts.get(ext, 0) + 1
                big_files.append((st.st_size, fp))
                if len(big_files) > 20:
                    big_files.sort(reverse=True)
                    big_files = big_files[:20]

    big_files.sort(reverse=True)

    def fmt(n):
        for unit in ['B','KB','MB','GB']:
            if n < 1024 or unit == 'GB':
                return f"{n:.1f} {unit}" if unit != 'B' else f"{n} B"
            n /= 1024

    # 依赖清单（常见文件）
    deps = {}
    for df in ['package.json', 'Cargo.toml', 'requirements.txt', 'pyproject.toml', 'go.mod', 'pom.xml', 'build.gradle', 'composer.json', 'Gemfile']:
        p = os.path.join(root, df)
        if os.path.isfile(p):
            deps[df] = '存在'

    result = {
        "project_root": root,
        "git": has_git,
        "total_source_files": total_src,
        "language_distribution": [{"ext": k, "files": v} for k, v in sorted(src_counts.items(), key=lambda x: -x[1])],
        "largest_files": [{"size": fmt(s), "bytes": s, "path": p} for s, p in big_files[:10]],
        "test_dirs": test_dirs[:20],
        "dependency_files": deps,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
