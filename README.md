# 中英同传 · 会议与课堂字幕

Windows 中英双向麦克风同传软件。主窗口按普通窗口使用，实时字幕与历史记录共用同一个可滚动的对话区。录制开始后会显示可拖动、可缩放的置顶字幕悬浮窗，适合看 PPT。关闭悬浮窗可继续录制，点击主窗口的「悬浮字幕」可以重新显示。右下角的甘雨头像球控制录制，角标显示播放或暂停。历史、设置和导出都在左侧栏内展开，保留当前窗口的位置、尺寸与透明度。

## 使用

1. 双击 `ZhEnInterpreter.exe` 启动。若从 GitHub 获取源码，请按下方“打包”步骤生成 EXE。
2. 点击右上角「设置」选择麦克风与录制模式。「按 API 优先级」按保存的顺序处理每个语音段，跳过缺少凭据的服务，失败后尝试下一项。点「上移」调整顺序。Kimi 订阅填写 Kimi Code 控制台生成的 Key，DeepSeek 填写 API Key，腾讯云填写 AppID、SecretId、SecretKey。「腾讯云 实时」直接使用腾讯云流式接口。点击「保存设置」将凭据存入当前 Windows 用户的凭据管理器。透明度滑块即时生效。
3. 点击右下角头像开始录制，再点一次暂停。识别结果逐段显示原文、译文和时间。拖动窗口边缘可调节大小，Ctrl + 滚轮可进一步调节字号。
4. 点击左上角「历史」或按 Ctrl+B 展开、收起历史栏。选中一段会话可阅读记录。暂停后点击该会话右下角的头像可接着录制。每次启动默认进入新会话，右上角「＋」也可新建会话。
5. 会话默认名称取开始时间，例如 11.18 19:30。每条历史有独立的「重命名」和「删除」按钮。重命名后按回车，清空名称可恢复默认时间。删除前在同一行确认，正在录制的会话要先暂停。
6. 点击「导出」查看或修改保存文件夹，再点「保存两个文件」，分别导出带时间的原文和译文 TXT。按 Esc 可收起侧栏。

Kimi 路线通过随 EXE 提供的多语言 whisper.cpp 模型在本机识别语音，只将识别文本发送到国内 Kimi Code 订阅端点。使用个人订阅交互录制时，接口能否调用取决于订阅权益、客户端许可及剩余额度。服务返回拒绝或限流时会显示状态码并按顺序尝试下一家。DeepSeek 路线使用其语音识别和文字翻译。已有原文时会复用该段原文。腾讯云接收音频并直接返回双语结果。

## 从源码运行

在 Windows 上安装 Python 3.13，然后运行：

```powershell
python -m pip install -r requirements.txt
python prepare_whisper.py
python ctk_app.py
```

## 打包

```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --icon icon.ico --add-data "icon.ico;." --add-data "icon.png;." --add-data "whisper;whisper" --name ZhEnInterpreter --collect-all sounddevice --collect-all keyring --collect-all customtkinter ctk_app.py
```

打包前先运行上面的 prepare_whisper.py 获取固定版本的 Windows x64 组件和多语言模型。生成文件位于 `dist/ZhEnInterpreter.exe`。

## 服务接口

- [Kimi Code 订阅接口](https://www.kimi.com/code/docs/) 和[使用说明](https://www.kimi.com/en/help/kimi-code/community-guidelines)
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) 和[多语言模型](https://huggingface.co/ggerganov/whisper.cpp)
- [DeepSeek 语音识别](https://api-docs.deepseek.com/guides/asr/) 和 [文字生成](https://api-docs.deepseek.com/api/create-chat-completion/)
- [腾讯云实时语音翻译](https://cloud.tencent.com/document/product/1093/127565)

文字记录保存在当前 Windows 用户的 `%LOCALAPPDATA%\ZhEnInterpreter\history.sqlite3`。软件不保存音频录音。API Key 不写入源码和日志。
