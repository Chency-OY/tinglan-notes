# 中英同传 · 会议与课堂字幕

Windows 中英双向麦克风同传软件。浅冰蓝色的半透明窗口可以置于 PPT 上方，实时字幕与历史记录共用同一个可滚动的对话区。右下角的甘雨头像球控制录制，角标显示播放或暂停。历史、设置和导出都在左侧栏内展开，保留当前窗口的位置、尺寸与透明度。

## 使用

1. 双击 `ZhEnInterpreter.exe` 启动。若从 GitHub 获取源码，请按下方“打包”步骤生成 EXE。
2. 点击右上角「设置」选择麦克风与翻译服务。DeepSeek 模式填写 API Key，腾讯云模式填写 AppID、SecretId、SecretKey。点击「保存设置」将凭据存入当前 Windows 用户的凭据管理器。透明度滑块即时生效。
3. 点击右下角头像开始录制，再点一次暂停。识别结果逐段显示原文、译文和时间。拖动窗口边缘可调节大小，Ctrl + 滚轮可进一步调节字号。
4. 点击左上角「历史」或按 Ctrl+B 展开、收起历史栏。选中一段会话可阅读记录。暂停后点击该会话右下角的头像可接着录制。每次启动默认进入新会话，右上角「＋」也可新建会话。
5. 点击「导出」查看或修改保存文件夹，再点「保存两个文件」，分别导出带时间的原文和译文 TXT。按 Esc 可收起侧栏。

DeepSeek 语音识别需要完整音频片段，短句结束后才出现翻译。腾讯云模式使用实时音频流。两种模式都需要网络，服务商按其计费规则收费。

## 从源码运行

在 Windows 上安装 Python 3.13，然后运行：

```powershell
python -m pip install -r requirements.txt
python ctk_app.py
```

## 打包

```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --icon icon.ico --add-data "icon.ico;." --add-data "icon.png;." --name ZhEnInterpreter --collect-all sounddevice --collect-all keyring --collect-all customtkinter ctk_app.py
```

生成文件位于 `dist/ZhEnInterpreter.exe`。

## 服务接口

- [DeepSeek 语音识别](https://api-docs.deepseek.com/guides/asr/) 和 [文字生成](https://api-docs.deepseek.com/api/create-chat-completion/)
- [腾讯云实时语音翻译](https://cloud.tencent.com/document/product/1093/127565)

文字记录保存在当前 Windows 用户的 `%LOCALAPPDATA%\ZhEnInterpreter\history.sqlite3`。软件不保存音频录音。API Key 不写入源码和日志。
