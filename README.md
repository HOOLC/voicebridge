# VoiceBridge

VoiceBridge 是一个本地运行的 AI 语音桥接项目。

它监听 Windows 上的虚拟声卡音频，把语音转成文字后发给本机 `Codex CLI`，再把 AI 的最终回复转成语音播回语音频道。当前也支持接入飞书机器人和基于 cron 的定时任务。

## 项目定位

- 电话语音、飞书私聊、定时任务共用一条主链路
- 电话输出必须适合 TTS 播放
- 飞书输出可以更自由，也支持结构化消息 JSON
- 电话、飞书、定时任务共享一个主会话
- 运行目录使用仓库下的 `bridge-home/`

当前内置两种飞书结构化输出约定：

- `report_card`：适合结论、分组、卡点、待决策
- `table_card`：适合巡检、状态、日报、session 汇总

## 当前平台

只支持 Windows。

当前实现依赖这些 Windows 前提：

- 默认命令是 `codex.cmd`
- 音频设备命名和路由按 Windows 虚拟声卡设计
- README 默认围绕 `Voicemeeter Banana`
- 进程取消逻辑使用了 Windows `taskkill`

## 目录结构

- 入口文件：`main.py`
- 主配置：`bridge.yaml`
- 运行目录：`bridge-home/`
- Python 源码：`src/voicebridge/`

`bridge-home/` 是真实运行目录，并且默认不纳入 Git，适合放每台机器自己的助手行为配置、个人定时任务和本地记忆。首次执行 `status`、`check`、`run` 等命令时，如果这些运行文件缺失，程序会直接用代码内嵌默认值补齐 `bridge-home/`。

## 配置分层

### `bridge.yaml`

安装级 / 机器级配置，适合放：

- 音频设备
- API 凭据入口
- 工作目录路径
- Codex CLI 命令和会话状态文件
- 飞书基础参数

### `bridge-home/assistant-runtime.yaml`

运行级 / 高频调优配置，适合放：

- 音色和语速
- 确认词
- 打断和识别阈值
- 口头控制词
- 定时任务

这里适合放你自己的本地任务和个性化助手行为；仓库不再保留单独模板目录，避免和正式工作目录混淆。

更细的回复口径、飞书格式偏好、巡检汇报规则，统一放在 `bridge-home/AGENTS.md`。

### `bridge-home/MEMORY.md`

长期记忆文件，放稳定偏好和跨天上下文。

### `bridge-home/memory/YYYY-MM-DD.md`

每日记忆文件，放当天临时记录和短期上下文。

## 依赖

- `Python 3.11+`
- 可直接执行的 `codex.cmd`
- 已登录并可正常使用的 `Codex CLI`
- 火山引擎 / 豆包语音服务凭据
- 一套可用的 Windows 虚拟声卡
- 可访问 Codex 服务和火山语音接口的网络

Python 依赖通过 `requirements.txt` 安装。
默认直接使用系统 `python`，不依赖仓库内虚拟环境。

## 安装

```powershell
git clone <your-repo-url> F:\VoiceBridge
cd F:\VoiceBridge
python -m pip install -U pip
python -m pip install -r requirements.txt
Copy-Item .\.env.example .\.env
Copy-Item .\bridge.example.yaml .\bridge.yaml
```

`.env` 不是必须文件。如果你想把语音服务凭据保存在项目目录里，再复制：

```powershell
Copy-Item .\.env.example .\.env
```

如果你已经在系统环境变量里配置了这些值，可以不创建 `.env`。

安装阶段不需要手动复制 `bridge-home/` 或 `bridge-home/assistant-runtime.yaml`。如果这些运行目录和文件缺失，执行 `python .\main.py run`、`python .\main.py status` 或 `python .\main.py session` 时，程序会自动补齐运行目录。

`.env` 里常见的是：

```text
DOUBAO_APP_ID=...
DOUBAO_ACCESS_TOKEN=...
```

如果要启用飞书机器人，还需要：

```text
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
FEISHU_USER_ID=...
```

## 示例配置

`bridge.yaml` 里至少确认这些字段：

- `capture_device`
- `playback_device`
- `assistant_cli_provider`
- `codex_model`
- `codex_timeout_seconds`
- `codex_auto_compact_enabled`
- `feishu_enabled`

示例：

```yaml
assistant_cli_provider: "codex"
kimi_command: "kimi.exe"
codex_workspace: "./bridge-home"
codex_command: "codex.cmd"
codex_model: "gpt-5.3-codex-spark"
codex_auto_compact_enabled: true
codex_auto_compact_max_context_chars: 260000
codex_auto_compact_buffer_chars: 120000

feishu_enabled: true
feishu_user_id_type: "user_id"

capture_device: "Voicemeeter Out B1"
playback_device: "Voicemeeter AUX Input"
```

如果要切到 Kimi CLI，把 `assistant_cli_provider` 改成 `"kimi"` 即可；`codex_use_yolo: true` 会继续给两种 CLI 都加上 `--yolo`。

然后先执行一次：

```powershell
python .\main.py check
```

这会自动补齐 `bridge-home/assistant-runtime.yaml`、`bridge-home/MEMORY.md`、`bridge-home/AGENTS.md`、`bridge-home/memory/` 等运行目录文件。

再按需要修改 `bridge-home/assistant-runtime.yaml`。当前推荐结构：

- `voice`
- `ack`
- `interaction`
- `commands`
- `schedule`

本地可选音色列表放在 `bridge-home/tts-voices.yaml`。`voice.tts_speaker` 可以直接填 speaker id，也可以填这个列表里的名称或别名。

`schedule.tasks` 现在支持两种执行方式：

- `type: "agent"`：和原来一样，把 `prompt` 投进共享主会话
- `type: "background_process"`：直接在指定 `cwd` 里拉起后台进程，不占用共享主会话

示例：

```yaml
schedule:
  check_interval_seconds: 15
  tasks:
    - name: "巡检"
      cron: "*/30 * * * *"
      type: "agent"
      prompt: "/boss 巡检"
      enabled: true
    - name: "独立工作区检查"
      cron: "0 * * * *"
      type: "background_process"
      cwd: "F:\\Lab\\other-workspace"
      use_agent_cli: true
      cli_provider: "configured"
      prompt: "检查当前工作区状态并输出简短摘要"
      enabled: false
```

`background_process` 任务支持这些字段：

- `cwd`：工作目录；相对路径按 `assistant-runtime.yaml` 所在目录解析
- `command` + `args`：直接启动指定后台命令
- `stdin`：启动后写入标准输入，适合 `codex exec -` 这类命令
- `use_agent_cli: true`：直接启动 CLI 一次性执行，不走共享主会话
- `cli_provider`：`configured` / `codex` / `kimi`，默认跟随安装配置
- `use_yolo`：覆盖机器级 `codex_use_yolo`

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

## 命令

列出本机音频设备：

```powershell
python .\main.py devices
```

运行只读自检：

```powershell
python .\main.py check
```

查看当前安装配置、运行配置摘要和运行状态：

```powershell
python .\main.py status
```

启动桥接：

```powershell
python .\main.py run
```

查看当前共享 CLI 会话：

```powershell
python .\main.py session
```

如果共享会话累计历史过长，系统会先自动生成一份续接摘要，再重开共享会话继续执行。默认同时看两条阈值：

- `codex_auto_compact_max_context_chars - codex_auto_compact_buffer_chars`
- `codex_auto_compact_turn_threshold`

当前默认值比较保守，会故意留出更大的 buffer，避免在上下文已经顶满后才 compact。

查看本地可选音色列表：

```powershell
python .\main.py voices
```

从官方列表刷新本地音色目录：

```powershell
python .\main.py voices --refresh
```

重置共享 Codex 会话：

```powershell
python .\main.py reset-session
```

## 当前行为

- 识别到有效语音后，会先播一个简短确认词
- 用户新开口时，可以中断当前播报
- 正式回复会整段一次合成并播放
- 说“再说一遍”会复述上一条回复
- 太短或无意义的口头语会被忽略，不转发给 Codex
- 飞书只接收指定用户的私聊文本消息
- 电话输入转写会同步到飞书，并带前缀“这是电话输入：”
- 电话只播放由语音输入触发的正式回复
- 定时任务使用标准 5 段 cron 表达式；`agent` 任务共享主会话，`background_process` 任务直接启动后台进程
- 共享 Codex 会话在累计历史过长时会自动 compact：先总结旧会话，再新开会话并注入摘要续接
- `assistant-state.json` 只保存运行状态快照，不再承担配置镜像

## 重要文件

- `bridge-home/AGENTS.md`：运行中的本地助手指令
- `bridge-home/assistant-runtime.yaml`：运行级行为配置
- `bridge-home/tts-voices.yaml`：本地音色目录
- `bridge-home/MEMORY.md`：长期记忆
- `bridge-home/memory/`：每日记忆
- `.voicebridge-session.json`：共享 Codex 会话状态
- `bridge-home/assistant-state.json`：运行状态快照
