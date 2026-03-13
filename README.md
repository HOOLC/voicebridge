# VoiceBridge

VoiceBridge 是一个跑在 Windows 上的本地语音桥。

它会监听 KOOK 的虚拟声卡输出，把语音转成文字交给本机 `Codex`，再把最终答复转成豆包 TTS 女声送回 KOOK。

## 特点

- Windows 本地运行
- 使用本地 `bridge-home/` 作为 Codex 工作目录
- 播报可被新开口打断
- 支持“再说一遍”本地复述上一条回复
- 回复按短句分段播放，更像电话助手
- 直接运行根目录 `main.py`，不依赖 `uv`

## 目录约定

- 项目根目录：仓库所在目录，例如 `F:\VoiceBridge`
- 主入口：`main.py`
- 主配置文件：`bridge.yaml`
- 环境变量文件：`.env`
- 运行工作目录：`bridge-home/`
- 工作目录模板：`bridge-home.example/`

仓库只提交 `bridge-home.example/`。
真正运行时使用 `bridge-home/`，它已在 `.gitignore` 里。首次运行时如果 `bridge-home/` 不存在，会自动从 `bridge-home.example/` 复制一份出来。

## Python 依赖

项目使用普通 `pip` 依赖管理，依赖列表在 `requirements.txt`：

- `pydantic`
- `python-dotenv`
- `pyyaml`
- `requests`
- `sounddevice`
- `webrtcvad-wheels`

不需要 `uv`。

## 外部依赖

这些不是 Python 包，但运行时必须满足：

- Windows 10 或 Windows 11
- `Python 3.11+`
- 可直接执行的 `codex.cmd`
- 已登录且可正常工作的 Codex CLI
- 豆包 / 火山语音接口凭据
- 一套可用的虚拟声卡，当前推荐 `Voicemeeter Banana`
- 网络可访问 Codex 服务和火山语音接口

## 运行条件

启动前至少要满足这些条件：

1. `python` 命令可用，并且版本不低于 `3.11`
2. `codex.cmd` 在当前终端里可以直接运行
3. `.env` 已填写语音服务凭据
4. `bridge.yaml` 里的 `capture_device` / `playback_device` 和本机设备名一致
5. KOOK 已切到正确的虚拟声卡设备
6. 如果 `bridge.yaml` 里配置了 `extra_search_paths`，这些路径在本机上真实存在

当前仓库里的 `bridge.yaml` 默认包含一条：

```yaml
extra_search_paths:
  - "\\\\wsl.localhost\\Ubuntu\\home\\overlogged\\Quant"
```

如果你的机器上没有这个目录，建议删掉或改成你自己的路径。

## 安装

下面 README 里的命令假设项目目录是：

```text
F:\VoiceBridge
```

### 1. 克隆项目

```powershell
git clone <your-repo-url> F:\VoiceBridge
cd F:\VoiceBridge
```

### 2. 创建虚拟环境

```powershell
python -m venv .venv
```

### 3. 安装依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. 创建环境变量文件

```powershell
Copy-Item .\.env.example .\.env
```

`.env` 至少需要：

```text
DOUBAO_APP_ID=...
DOUBAO_ACCESS_TOKEN=...
```

### 5. 创建主配置文件

```powershell
Copy-Item .\bridge.example.yaml .\bridge.yaml
```

### 6. 按实际环境修改 `bridge.yaml`

至少检查这些字段：

- `capture_device`
- `playback_device`
- `codex_model`
- `codex_timeout_seconds`
- `extra_search_paths`

如果你的项目安装在 `F:\VoiceBridge`，通常至少要确认这些值：

```yaml
assistant_runtime_config_path: "./bridge-home/assistant-runtime.yaml"
assistant_runtime_example_path: "./bridge-home.example/assistant-runtime.yaml"

codex_workspace: "./bridge-home"
codex_session_state_file: ".voicebridge-session.json"
codex_model: "gpt-5.3-codex-spark"
codex_timeout_seconds: 60

capture_device: "Voicemeeter Out B1"
playback_device: "Voicemeeter AUX Input"
```

更复杂的行为不要继续往 `bridge.yaml` 里堆。
如果你需要特殊规则、额外目录、额外技能或特定工作流，直接写进 `bridge-home/AGENTS.md`。

## 虚拟声卡配置

当前推荐使用 `Voicemeeter Banana`。

### 1. 安装 Voicemeeter Banana

1. 下载并安装 `Voicemeeter Banana`
2. 安装过程中所有驱动提示都点允许
3. 安装完成后重启 Windows

### 2. 确认系统里出现这些设备

安装后在 Windows 声音设置里应该能看到：

- `Voicemeeter Input`
- `Voicemeeter AUX Input`
- `Voicemeeter Out B1`
- `Voicemeeter Out B2`

### 3. 打开 Voicemeeter Banana 面板

只保留桥接需要的两条路，不要把它改成系统默认音频设备。

你只需要看中间这两列：

- `Voicemeeter Input`
- `Voicemeeter AUX`

按钮这样设置：

- `Voicemeeter Input` 这一列：只开 `B1`
- `Voicemeeter AUX` 这一列：只开 `B2`
- 左边三个 `Hardware Input` 列：默认都不选设备
- `A1 / A2 / A3`：默认都可以不配

最终面板上只应该保留两颗关键按钮亮着：

- `Voicemeeter Input -> B1`
- `Voicemeeter AUX -> B2`

### 4. 在 KOOK 里指定音频设备

KOOK 设置里使用：

- 扬声器：`Voicemeeter Input`
- 麦克风：`Voicemeeter Out B2`

不要改 Windows 的默认扬声器和默认麦克风，只在 KOOK 里单独指定。

### 5. 在 `bridge.yaml` 里指定设备

- `capture_device: "Voicemeeter Out B1"`
- `playback_device: "Voicemeeter AUX Input"`

### 6. 路由结果

音频链路如下：

- KOOK 来音：`Voicemeeter Input -> B1 -> VoiceBridge`
- VoiceBridge 回话：`Voicemeeter AUX Input -> B2 -> KOOK 麦克风`

如果你不想让机器人声音进你自己的耳机，`A1 / A2 / A3` 保持不配即可。

## 运行

推荐直接运行根目录入口：

```powershell
.\.venv\Scripts\python.exe .\main.py run
```

列出设备：

```powershell
.\.venv\Scripts\python.exe .\main.py devices
```

查看状态：

```powershell
.\.venv\Scripts\python.exe .\main.py status
```

查看会话：

```powershell
.\.venv\Scripts\python.exe .\main.py session
```

重置 Codex 会话：

```powershell
.\.venv\Scripts\python.exe .\main.py reset-session
```

## 首次启动前检查

建议第一次 `run` 前按这个顺序检查：

1. `.\.venv\Scripts\python.exe .\main.py devices`
2. `bridge.yaml` 里的设备名是否和本机一致
3. KOOK 是否已经切到 `Voicemeeter Input / Voicemeeter Out B2`
4. `.env` 是否已经填好豆包凭据
5. `codex.cmd` 是否能在当前终端里直接运行

## 工作目录文件

`bridge-home/` 下最重要的人工维护文件：

- `AGENTS.md`
- `assistant-runtime.yaml`

运行时还会自动生成状态文件，例如 `assistant-state.json`。
如果 `assistant-runtime.yaml` 写坏了，可以直接从 `bridge-home.example/assistant-runtime.yaml` 恢复。

## 行为说明

- `Codex` 单轮超时默认 60 秒
- 开口时默认只停止播报，不取消正在运行的 Codex
- 如果旧回复已经过时，会保存到历史，但不继续播报
- 说“再说一遍”会本地复述上一条已生成的回复
- 如果识别内容太短或像口头语，会直接忽略，不转发给 Codex
