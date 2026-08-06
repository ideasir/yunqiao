import sys, platform
name = sys.argv[1] if len(sys.argv) > 1 else "世界"
print(f"你好, {name}!")
print(f"来自: {platform.node()} ({platform.system()})")
print(f"参数: {sys.argv[1:]}")
print(f"Python: {sys.version.split()[0]}")
