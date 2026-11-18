# 中英同传 · 会议与课堂字幕

Windows 麦克风同传软件。浅色主界面展示整场对话与会话历史，独立的半透明字幕窗可悬浮在 PPT 上方。支持 DeepSeek 近实时模式与腾讯云实时模式，可自动中英互译。

## 使用

1. 双击 `ZhEnInterpreter.exe` 启动。若从 GitHub 获取源码，请按下方“打包”步骤生成 EXE。
2. 点击“密钥设置”。DeepSeek 模式填 DeepSeek API Key；腾讯云模式填 AppID、SecretId、SecretKey。凭据只保存在当前 Windows 用户的凭据管理器中。
3. 选择麦克风和服务，点击“开始”。主界面会按时间显示整场对话；独立字幕窗始终置顶，可用滑块调整透明度。两个窗口都能拖动边缘改变大小，字幕会自动缩放；也可以用 `−`、`+` 或 Ctrl + 鼠标滚轮调节字号。
4. 点击“历史”查看按句保存的原文和译文。每次重新打开软件默认是新会话；在历史里选择“继续此会话”后，主界面会加载完整对话，继续录制时才向旧会话追加。可把选中的会话分别导出为原文和译文两个 TXT 文件。

DeepSeek 语音识别需要完整音频片段，短句结束后才出现翻译。腾讯云模式使用实时音频流。两种模式都需要网络，服务商按其计费规则收费。

## 从源码运行

在 Windows 上安装 Python 3.13，然后运行：

```powershell
python -m pip install -r requirements.txt
python interpreter.py
```

## 打包

```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --icon icon.ico --add-data "icon.ico;." --name ZhEnInterpreter --collect-all sounddevice --collect-all keyring interpreter.py
```

生成文件位于 `dist/ZhEnInterpreter.exe`。

## 服务接口

- [DeepSeek 语音识别](https://api-docs.deepseek.com/guides/asr/) 和 [文字生成](https://api-docs.deepseek.com/api/create-chat-completion/)
- [腾讯云实时语音翻译](https://cloud.tencent.com/document/product/1093/127565)

文字记录保存在当前 Windows 用户的 `%LOCALAPPDATA%\ZhEnInterpreter\history.sqlite3`。软件不保存音频录音。API Key 不写入源码和日志。
