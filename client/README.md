# 云桥 MCP 客户端

基于 pywebview 的桌面应用，HTML 前端 + Python 后端。

## 快速启动

```bash
pip install -r requirements.txt
python main.py
```

## 打包成 exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "云桥MCP" main.py
```

会在 `dist/云桥MCP.exe` 生成单文件 exe。

## 使用

1. 启动后点击 ⚙ 设置中转地址和 PSK
2. 配对码显示在界面中，发给智能体
3. 智能体用配对码连接后即可远程操作
