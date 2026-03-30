# 上手路线图

> 环境安装（Python、Node、`init.bat`）见 [SETUP_FIRST_RUN.zh.md](./SETUP_FIRST_RUN.zh.md)。
> 人物卡 JSON 字段见 [profile_prompts.zh.md](./profile_prompts.zh.md)。
> **English:** [GETTING_STARTED.en.md](./GETTING_STARTED.en.md)

**本文假设你已完成环境安装，后端正在运行（浏览器能打开 `http://localhost:7788`）。**

---

## 最少几步能聊上天？

| 步骤 | 做什么 | 在哪里 |
|:---:|:---|:---|
| **1** | 配置至少**一个对话模型**（云端 Key 或本地 Base URL） | 设置 → **模型** |
| **2** | 至少有一张**人格卡**（可用 AI 向导从描述生成整张卡） | 设置 → **人格** |

完成 **1 + 2** 即可在主界面开始对话。

> 还没装好环境？先读 [SETUP_FIRST_RUN.zh.md](./SETUP_FIRST_RUN.zh.md)。
> 所在网络无法直连 Gemini / OpenAI？先看下方「代理」一节。

---

## 代理（大陆或受限网络）

后端是 **Python 进程**，系统里「浏览器能翻墙」不等于 Python 已走代理。

**推荐做法**：

1. 打开 **设置 → 系统**。
2. 勾选 **使用代理**，填写：`http://127.0.0.1:端口`（端口见你的代理软件 HTTP / Mixed Port）。
3. **保存**后**重启**应用（使环境变量生效）。

若不确定端口：在代理软件界面查「本地 HTTP 端口」；或在 CMD 执行 `netstat -ano | findstr LISTENING` 找 `127.0.0.1` 上的常见端口（如 `7890`、`10808`）。

也可直接编辑 **`.env`** 设置 `HTTP_PROXY` / `HTTPS_PROXY`（与设置页效果一致）。

---

## 设置页各 Tab 一览

| Tab | 用途 |
|:---|:---|
| **入门** | **分步引导 (推荐)**：网络环境配置、AI 记忆依赖与模型、TTS / STT 依赖与模型。 |
| **人格** | 角色卡列表、编辑、**AI 向导**生成卡、侧栏排序、记忆 / 引擎等人格级覆盖 |
| **模型** | LLM 预设管理、API Key、本地 OpenAI 兼容地址、辅助模型（情感 / 好感等） |
| **记忆** | 长期事实、向量记忆开关、嵌入模型（云端 Gemini 或本地 BGE 等）、日记与遗忘预览 |
| **TTS** | 朗读引擎：Edge / GPT-SoVITS / Kokoro / Qwen3-TTS |
| **自省 / ASE** | 角色内心独白、主动发言配置 |
| **VLM** | 屏幕 / 视觉理解：模型与截图策略 |
| **工具** | 待办、计时器、天气、趋势、网页搜索等工具插件的开关与配置 |
| **系统** | 主题、代理、HF 镜像、离线开关 |

人格卡里还可配置情绪/能量/好感文案、记忆策略、TTS 参考音等——字段说明见 [profile_prompts.zh.md](./profile_prompts.zh.md)。

---

## 可选增强

### 记忆与向量检索

- 使用 **Gemini 等云端嵌入**：在「模型」里配置 Key 即可，向量记忆走云端。
- 无云端 Key、想用**本地向量检索**：在「入门」页用 HF 镜像 / ModelScope 下载 BGE-small 等到 `models/`，再到「记忆」将嵌入提供方改为本地并填写路径。
- 设置了 `HF_HUB_OFFLINE=1` 时，在线下载会失败，需先关闭离线或自备模型。

### 语音输入（STT）

在「入门」页的 STT 区选择引擎并下载模型：
- **SenseVoice**（ONNX）：多语言、速度较快，适合多数场景。
- **Whisper / faster-whisper**：更重、更准，按需下载（可指定 `stt.model_path` 使用本地文件）。

### 语音朗读（TTS）

- **Edge TTS**：免装模型，需联网，100+ 音色。
- **Kokoro**：本地 ONNX，~200ms 延迟，支持 zh / ja / en。
- **GPT-SoVITS / Qwen3-TTS**：按「TTS」页说明安装依赖或下载权重。Qwen3-TTS 依赖 PyTorch，见下方。

### Qwen3-TTS 与 PyTorch（仅使用 Qwen3-TTS 时）

`requirements-ai.txt` 中不含 GPU 版 PyTorch（避免强制下载 GB 级文件）。需单独安装：

```bat
# NVIDIA GPU（CUDA，版本按显卡驱动调整）
.venv\Scripts\python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

# 仅 CPU
.venv\Scripts\python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
```

更多组合见 [PyTorch 官网](https://pytorch.org/)。若报错 `Torch not compiled with CUDA enabled`，说明安装的是 CPU 版，按上方换 cu 版或在 TTS 设置里改设备为 `cpu`。

### 屏幕 / 视觉（VLM）

在「VLM」中配置多模态模型后，可在支持场景下使用截图理解能力（依赖所选 API 或本地多模态服务）。

### 环境感知工具（天气 / 趋势）

在「工具」页启用并配置天气、趋势（RSS / API）等工具；启用后会注入 prompt 并影响主动发言内容。

---

## 文档分工

| 文档 | 何时读 |
|:---|:---|
| **本文 GETTING_STARTED** | UI 已运行，想知道「几步、去哪点、能开什么」 |
| **SETUP_FIRST_RUN** | 从零装 Python / Node、跑 `init.bat`、Docker、闪退排查 |
| **profile_prompts** | 手写或精调 JSON 人物卡时查字段；也是 AI 向导的生成依据 |
| **ARCHITECTURE_REFERENCE** | 贡献者 / 架构师查阅模块边界与最低文件要求 |

在应用内 **设置 → 入门** 顶部可打开上述文档（随界面语言打开 `*.zh.md` / `*.en.md`）。
