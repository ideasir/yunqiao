#!/usr/bin/env python3
"""对比 Windows 下载版本与仓库 HEAD 的差异"""
import difflib, subprocess, sys

REPO = "/home/admin/.openclaw/workspace/yunqiao"
SYNC = "/tmp/yunqiao-sync"

for f in ["ui.html", "agent.py", "desktop.py"]:
    head = subprocess.run(["git", "-C", REPO, "show", f"HEAD:client/{f}"],
                          capture_output=True, text=True).stdout
    win = open(f"{SYNC}/{f}", encoding="utf-8", errors="replace").read()
    if head == win:
        print(f"===== {f}: IDENTICAL to HEAD =====")
        continue
    print(f"===== {f}: DIFFERS (HEAD {len(head)}B vs Windows {len(win)}B) =====")
    diff = list(difflib.unified_diff(head.splitlines(), win.splitlines(),
                                     fromfile="HEAD", tofile="WINDOWS", n=1))
    # 限制输出
    if len(diff) > 80:
        print("\n".join(diff[:80]))
        print(f"... ({len(diff)} diff lines total)")
    else:
        print("\n".join(diff))
    print()
