# VoiceBridge

VoiceBridge 2.0 是一个只保留电话桥接能力的本地服务。

当前主链路已经收缩为：

- Windows 本地采集电话语音
- 本地做语音识别
- 通过 WSL `codex-feishu-agent phone-send` 把文本发给后端
- 通过 WSL `codex-feishu-agent phone-recv` 取最终稳定回复
- 用 MiniMax TTS HD 合成语音并播回电话链路

仓库里不再承载这些旧能力：

- Codex CLI 会话管理
- agent / session 巡检
- 飞书消息桥
- cron 定时任务
- `AGENTS.md` / `MEMORY.md` 运行时默认模板

## 目录

- 入口：`main.py`
- 源码：`src/voicebridge/`
- 安装级配置：`bridge.yaml`
- 运行级配置：`bridge-home/assistant-runtime.yaml`
- 运行状态：`bridge-home/assistant-state.json`

## 配置分层

### `bridge.yaml`

安装级 / 机器级配置，适合放：

- 音频设备
- 语音识别凭据入口
- MiniMax API 基础地址
- WSL 电话后端命令名
- 采集参数

### `bridge-home/assistant-runtime.yaml`

运行级 / 高频调优配置，适合放：

- MiniMax TTS 模型和音色
- 确认词
- VAD 阈值
- 口头控制词
- 电话后端的超时和轮询参数

## 当前依赖

- Windows
- Python 3.11+
- `codex-feishu-agent`
- 可用的 WSL
- MiniMax API key
- 一套可用的音频输入输出设备

当前语音链路分成两段：

- ASR：继续使用火山 / 豆包识别接口
- TTS：改成 MiniMax TTS

这意味着 `.env.example` 现在只保留 `MINIMAX_API_KEY`。如果你还要继续沿用现有 ASR，需要在 `bridge.yaml` 里填 `volcengine_app_id` 和 `volcengine_access_key`，或者改成本地别的识别实现。

## 安装

```powershell
git clone <your-repo-url> F:\VoiceBridge
cd F:\VoiceBridge
python -m pip install -U pip
python -m pip install -r requirements.txt
Copy-Item .\.env.example .\.env
Copy-Item .\bridge.example.yaml .\bridge.yaml
```

## `.env`

```text
MINIMAX_API_KEY=...
```

## `bridge.yaml` 示例

```yaml
assistant_runtime_config_path: "./bridge-home/assistant-runtime.yaml"
assistant_state_path: "./bridge-home/assistant-state.json"

volcengine_app_id:
volcengine_access_key:
volcengine_asr_resource_id: "volc.bigasr.auc_turbo"

minimax_api_base: "https://api.minimaxi.com"
phone_bridge_command: "codex-feishu-agent"

capture_device: "Voicemeeter Out B1"
playback_device: "Voicemeeter AUX Input"
```

## `assistant-runtime.yaml` 示例

```yaml
version: 1

voice:
  tts_model: "speech-2.8-hd"
  tts_voice_id: "Chinese (Mandarin)_Warm_Bestie"
  format: "wav"
  sample_rate: 32000
  speed: 1.0
  volume: 1.0
  pitch: 0
  language_boost: "Chinese"

ack:
  default: "收到"
  variants:
    - "收到"
    - "好，收到"
    - "嗯，收到"

interaction:
  interrupt_playback: true
  vad_rms_threshold: 900

commands:
  transcript_min_chars: 2
  ignore_phrases: ["嗯", "啊", "呃", "额", "哦", "喂"]
  repeat_aliases: ["再说一遍", "再说一次", "重复一下", "把刚才说的再说一遍", "刚才说什么"]
  stop_aliases: ["停一下", "别说了", "先别说", "闭嘴"]

phone:
  from_name: "voicebridge"
  reply_source: "final_answer"
  reply_timeout_seconds: 180
  recv_poll_interval_seconds: 1.0
```

## 命令

列出本机音频设备：

```powershell
python .\main.py devices
```

运行只读自检：

```powershell
python .\main.py check
```

查看当前电话桥配置和状态：

```powershell
python .\main.py status
```

启动电话桥：

```powershell
python .\main.py run
```

## 当前行为

- 识别到有效语音后，会先播一个简短确认词
- 用户新开口时，可以中断当前播报
- 说“再说一遍”会复述上一条已播报回复
- 太短或无意义的口头语会被忽略，不会发给 `phone-send`
- 回复读取默认只消费 `final_answer`
- 进入 TTS 前会做一次电话播报清洗，不会把符号和工具痕迹直接念出来

## 关键文件

- `bridge.yaml`
- `bridge.example.yaml`
- `bridge-home/assistant-runtime.yaml`
- `bridge-home/assistant-state.json`
- `src/voicebridge/phone_bridge.py`
- `src/voicebridge/speech_client.py`
