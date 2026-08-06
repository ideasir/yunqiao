"""va-status: VeryAgent 项目状态命令
用法: run_custom va-status [分支名]
"""
import sys, subprocess, os

PROJECT = r"E:\AIcode\github\VeryAgent"

def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=PROJECT, timeout=30, encoding='utf-8', errors='replace')
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    print(f"=== VeryAgent 项目状态 ===")
    print(f"目录: {PROJECT}")
    
    out, _ = run("git branch --show-current")
    print(f"当前分支: {out or '(未知)'}")
    
    out, _ = run("git log --oneline -3")
    print("最近提交:")
    print(out or "(无)")

    out, _ = run("git status --porcelain | wc -l")
    print(f"未提交改动: {out.strip()} 个文件")

    if target:
        out, rc = run(f"git checkout {target} 2>&1")
        print(f"切换分支 {target}: {'成功' if rc == 0 else out}")
        out, _ = run("git branch --show-current")
        print(f"当前分支: {out}")
    
    # 构建产物标记
    exe = os.path.join(PROJECT, "src-tauri", "target", "release", "veryagent.exe")
    print(f"构建产物: {'存在' if os.path.exists(exe) else '未构建'}")

if __name__ == "__main__":
    main()
