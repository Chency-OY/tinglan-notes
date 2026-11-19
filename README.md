# 中英同传 · 会议与课堂字幕

Windows 中英双向麦克风同传软件。主窗口按普通窗口使用，实时字幕与历史记录共用同一个可滚动的对话区。甘雨头像球默认在主窗口所在屏幕的工作区右下角，避开任务栏，可拖动。单击悬浮球开始或暂停录制，角标会随状态切换。主窗口最小化时仍可使用悬浮球。在设置中取消「显示桌面悬浮球」可将其隐藏。右下角的甘雨头像球控制录制，角标显示播放或暂停。左侧栏管理会话和项目，设置与单会话导出在同一个窗口右侧打开，保留当前窗口的位置与尺寸。主窗口不透明；桌面悬浮球可以单独调整不透明度，拖动后会记住位置。悬浮球下方显示最新译文，放不下的长句会滚动。

## 使用

1. 双击 `ZhEnInterpreter.exe` 启动。若从 GitHub 获取源码，请按下方“打包”步骤生成 EXE。
2. 点击齿轮或侧栏底部「设置」。在「录制」选择麦克风，在「翻译服务」填写密钥和调整顺序，在「外观」调整字幕字号、悬浮球显示和不透明度。录制时根据停顿切段，并按保存的 API 顺序处理每个语音段，跳过缺少凭据的服务，失败后尝试下一项。点「上移」调整顺序。Kimi 订阅填写 Kimi Code 控制台生成的 Key，DeepSeek 填写 API Key。点击「保存设置」将凭据存入当前 Windows 用户的凭据管理器。字号和悬浮球不透明度即时生效，并记住调整结果。
3. 点击右下角头像开始录制。麦克风在后台启动，启动时再次点击可取消，录制中点击可暂停。检测到讲话后连续停顿约 1 秒即提交；连续讲话达到 60 秒时也会提交。收音、语音切段和网络请求分别运行。上一段上传时继续收录下一段，停顿附近的低声内容也会保留。点击暂停立即停麦，后台继续翻译已录内容，此时可直接点击头像续录。关闭窗口会等待剩余译文保存。DeepSeek 和 Kimi 会参考本会话上一段的原文和译文，以衔接指代和术语。原文与译文中的英文比中文大一号，并连同录制时间保存。拖动窗口边缘可调节大小，Ctrl + 滚轮可进一步调节字号。
4. 左侧栏显示项目与尚未归类的最近会话，点文件夹直接展开项目里的会话。点击侧栏图标或按 Ctrl+B 可以收起或展开侧栏。打开历史会话后可点击头像续录，每次启动默认进入新会话。
5. 会话名称默认为开始时间，例如 11.18 19:30。鼠标移到会话上，点右侧「⋯」可重命名、移入项目、导出、归档或删除。清空名称后按回车可恢复默认名称。打开「设置 → 已归档」查看、打开或恢复归档内容。
6. 点击「导出」打开右侧保存页，选择位置后点击「保存 TXT」。默认保存到桌面，原文和译文分开，保留会话及每段字幕的时间。
7. 在左侧「项目」标题旁点「创建项目」。项目右侧「⋯」可以在项目内新建会话、导出或归档整个项目。恢复项目时，原先单独归档的会话会继续留在「已归档」。把现有会话移入项目后，会话显示在项目文件夹下。
8. 点项目右侧「⋯ → 导出项目 ZIP」可将整个项目保存为 ZIP，默认位置为桌面。每个会话各有一个文件夹，里面是带时间戳的「原文.txt」和「译文.txt」。压缩包包含项目内的归档会话，正在录制的会话会导出当时已保存的段落。

Kimi 路线通过随 EXE 提供的多语言 whisper.cpp 模型在本机识别语音，只将识别文本发送到国内 Kimi Code 订阅端点。使用个人订阅交互录制时，接口能否调用取决于订阅权益、客户端许可及剩余额度。服务返回拒绝或限流时会显示状态码并按顺序尝试下一家。DeepSeek 路线使用其语音识别和文字翻译。已有原文时会复用该段原文。

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
python -m PyInstaller --onefile --windowed --icon icon.ico --add-data "icon.ico;." --add-data "icon.png;." --add-data "whisper;whisper" --add-data "third_party;third_party" --name ZhEnInterpreter --collect-all sounddevice --collect-all keyring --collect-all customtkinter ctk_app.py
```

打包前先运行上面的 prepare_whisper.py 获取固定版本的 Windows x64 组件和多语言模型。生成文件位于 `dist/ZhEnInterpreter.exe`。

## 服务接口

- [Kimi Code 订阅接口](https://www.kimi.com/code/docs/) 和[使用说明](https://www.kimi.com/en/help/kimi-code/community-guidelines)
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) 和[多语言模型](https://huggingface.co/ggerganov/whisper.cpp)
- [DeepSeek 语音识别](https://api-docs.deepseek.com/guides/asr/) 和 [文字生成](https://api-docs.deepseek.com/api/create-chat-completion/)

文字记录保存在当前 Windows 用户的 `%LOCALAPPDATA%\ZhEnInterpreter\history.sqlite3`。软件不保存音频录音。API Key 不写入源码和日志。
