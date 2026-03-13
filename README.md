# VoiceBridge

VoiceBridge 是一个本地运行的 AI 语音桥接项目。

它监听 Windows 上的虚拟声卡音频，把语音转成文字后发给本机 `Codex CLI`，再把 AI 的最终回复转成语音播回语音频道。当前实现面向 KOOK + Windows + Voicemeeter 这一套链路。

## 项目定位

- 这是一个 AI 项目，不是传统的固定话术机器人
- 语音理解由火山引擎 / 豆包语音接口完成
- 对话和推理由本机 `Codex CLI` 完成
- 回复会按整段文本一次合成、一次播放
- 运行时工作目录使用仓库下的 `bridge-home/`

## 当前平台

只支持 Windows。

原因不是文档层面的限制，而是当前实现本身绑定了 Windows 的运行方式：

- 默认命令是 `codex.cmd`
- 音频设备命名和路由按 Windows 虚拟声卡设计
- README 和配置默认围绕 `Voicemeeter Banana`
- 进程取消逻辑使用了 Windows 的 `taskkill`

如果你要直接运行，默认前提就是 Windows 10 / 11。

## 目录结构

- 入口文件：`main.py`
- 主配置：`bridge.yaml`
- 环境变量：`.env`
- 运行目录：`bridge-home/`
- 运行目录模板：`bridge-home.example/`
- Python 源码：`src/voicebridge/`

`bridge-home/` 是真实运行目录，首次启动时会从 `bridge-home.example/` 自动补齐缺失文件。

## 依赖

运行前需要这些条件：

- `Python 3.11+`
- 可直接执行的 `codex.cmd`
- 已登录并可正常使用的 `Codex CLI`
- 火山引擎 / 豆包语音服务凭据
- 一套可用的 Windows 虚拟声卡
- 可访问 Codex 服务和火山语音接口的网络

Python 依赖通过 `requirements.txt` 安装，不依赖 `uv`。

## 安装

```powershell
git clone <your-repo-url> F:\VoiceBridge
cd F:\VoiceBridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
```

如果你不用虚拟环境，也可以直接用当前系统里的 `python` 执行后续命令；README 不再假设解释器路径固定在 `.venv\Scripts\python.exe`。

安装后需要先准备 `bridge.yaml`，这是启动前唯一必须存在的本地配置文件。通常直接从示例复制：

```powershell
Copy-Item .\bridge.example.yaml .\bridge.yaml
```

`.env` 不是必须文件。如果你想把语音服务凭据保存在项目目录里，再复制：

```powershell
Copy-Item .\.env.example .\.env
```

如果你已经在系统环境变量里配置了这些值，可以不创建 `.env`。

安装阶段不需要手动复制 `bridge-home/`、`bridge-home.example/` 或 `bridge-home/assistant-runtime.yaml`。如果这些运行目录和文件缺失，执行 `python .\main.py run`、`python .\main.py status` 或 `python .\main.py session` 时会自动从模板补齐。

`.env` 里常见的是：

```text
DOUBAO_APP_ID=...
DOUBAO_ACCESS_TOKEN=...
```

## 配置

先准备好 `bridge.yaml`，至少确认这些字段：

- `capture_device`
- `playback_device`
- `codex_model`
- `codex_timeout_seconds`
- `extra_search_paths`

示例：

```yaml
codex_workspace: "./bridge-home"
codex_command: "codex.cmd"
codex_model: "gpt-5.3-codex-spark"
codex_timeout_seconds: 60

capture_device: "Voicemeeter Out B1"
playback_device: "Voicemeeter AUX Input"
```

然后执行一次初始化命令，让运行目录和运行时文件自动补齐：

```powershell
python .\main.py status
```

这一步之后，如果 `bridge-home/` 不存在，程序会自动从 `bridge-home.example/` 补一份；`bridge-home/assistant-runtime.yaml` 也会随之可用。

再修改 `bridge-home/assistant-runtime.yaml`，这里放运行时风格配置，比如：

- 语音人设
- 确认词
- 回复风格
- 语速 `speed_ratio`

当前默认语速已经调到略快。

## 音频链路

当前推荐 `Voicemeeter Banana`。

建议的 KOOK / VoiceBridge 路由是：

- KOOK 扬声器：`Voicemeeter Input`
- KOOK 麦克风：`Voicemeeter Out B2`
- `capture_device`: `Voicemeeter Out B1`
- `playback_device`: `Voicemeeter AUX Input`

对应链路：

- KOOK 来音 -> `Voicemeeter Input -> B1 -> VoiceBridge`
- AI 回话 -> `VoiceBridge -> Voicemeeter AUX Input -> B2 -> KOOK`

## 运行

列出本机音频设备：

```powershell
python .\main.py devices
```

启动桥接：

```powershell
python .\main.py run
```

查看运行状态：

```powershell
python .\main.py status
```

查看当前 Codex 会话：

```powershell
python .\main.py session
```

重置保存的 Codex 会话：

```powershell
python .\main.py reset-session
```

## 当前行为

- 识别到有效语音后，会先播一个简短确认词
- 用户新开口时，可以中断当前播报
- 正式回复会整段一次合成并播放
- 说“再说一遍”会复述上一条回复
- 太短或无意义的口头语会被忽略，不转发给 Codex
- Codex 会话会持久化，`session` 命令可以看到恢复信息

## 重要文件

- `bridge-home/AGENTS.md`：给 Codex 的本地工作指令
- `bridge-home/assistant-runtime.yaml`：语音和交互运行配置
- `.voicebridge-session.json`：保存的 Codex 会话状态
- `bridge-home/assistant-state.json`：运行状态快照

## macOS 迁移需要做什么

如果要迁移到 macOS，核心不是改一两个命令，而是把平台相关层拆开。至少要处理这些工作：

1. 把 `codex_command` 从 `codex.cmd` 切到 macOS 可执行形式，通常是 `codex`
2. 重写进程终止逻辑，去掉 `taskkill`，改成 macOS / POSIX 兼容实现
3. 重新验证 `sounddevice`、输入输出设备枚举和设备名匹配逻辑
4. 替换当前基于 `Voicemeeter Banana` 的虚拟声卡方案，改成 macOS 可用链路，例如 BlackHole、Loopback 或同类工具
5. 重写 README 里的音频路由说明，因为 KOOK 的设备名和虚拟声卡名称会完全不同
6. 重新测试 `bridge.yaml` 默认值，尤其是音频设备、路径格式和额外目录配置
7. 完整回归测试一遍打断播放、会话恢复、TTS 播放和设备切换

如果只是“理论可迁移”，现在已经够了；如果要“开箱即用支持 macOS”，需要单独做一轮平台适配。
