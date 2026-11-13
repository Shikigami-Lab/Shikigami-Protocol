# 环境安装与首次运行

> 想看「一共几步、设置里各 Tab 干什么」？请读 [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md)。
> **English:** [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md)

本文负责：**Python / Node 安装、`init.bat` / `init.sh` 用法、Docker 部署、常见闪退排查**。

---

## 快速路径

**v0.12.0 Public Beta 正在 `dev` 准备，尚未正式发布。** 下方命令明确选择 `dev`；默认 `main` 与旧 [Release 压缩包](https://github.com/Shikigami-Lab/Shikigami-Protocol/releases) 不包含这些更新。源码可运行 Electron 或浏览器模式，Docker 用于主应用 Web UI。目前没有 v0.12.0 预编译桌面安装包。本文说明运行要求，不代表所有平台、CPU 架构及可选组件均已验收。

### 源码运行

#### Windows（PowerShell）
```powershell
git clone --branch dev https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
.\init.bat
.\launch.bat
```

`init.bat` 会检测 Python、准备 `.venv`、安装核心依赖，并在有 Node 时安装 Node 依赖，复制缺失的配置模板。若提示环境无效或过旧，先保留或重命名旧 `.venv`，再重新运行。

> **AI 增强组件**：ChromaDB、ModelScope、Qwen3-TTS 等较重依赖库不再默认安装，请在启动后的 **设置 → 新手引导** 页面点击一键安装。

### macOS / Linux

```bash
git clone --branch dev https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
bash init.sh
bash launch.sh
```

### Docker（服务器 / NAS / 无 Electron）

```bash
git clone --branch dev https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
cp -n .env.example .env
cp -n config/app.yaml.example config/app.yaml
# 在宿主机编辑 config/app.yaml 的 llm_presets/default_llm 与 .env 密钥
docker compose up -d
# 浏览器打开 http://localhost:7788
```

上方为 macOS/Linux shell 语法，并保留已有配置。PowerShell 使用 `Copy-Item .env.example .env` 和 `Copy-Item config/app.yaml.example config/app.yaml`，CMD 使用对应 `copy` 命令，**仅复制尚不存在的目标文件**。按下方「配置 API Key」在宿主机配置预设与密钥，修改后执行 `docker compose restart shikigami`。

`data/`、`profiles/`、`models/`、`Voices/`、`config/` 已挂载为 volume；`.env` 为只读挂载，API Key 需在宿主机编辑。旧容器若仍有只存在 `/app/data` 中的数据，须按[升级步骤](#upgrade-v0110)先迁出再重建。GPU 可选：取消注释 `docker-compose.yml` 中的 `deploy.resources`（需 nvidia-container-toolkit）。

这是**主应用 Docker 部署**，与可选的 **Harness 任务容器**分开。现有主镜像没有 Docker CLI，Compose 未挂 Docker 套接字，单独 `docker compose up` 无法提供 Harness 访问。使用 Harness 请在宿主机运行源码，并按下文安装。

常用命令：`docker compose up -d` / `down` / `logs -f` / `up -d --build`

---

## 前置依赖

### Python 3.12（推荐）

**作用**：运行 `server.py` 及全部后端逻辑。当前安装**推荐 Python 3.12**，源码安装测试以此为基础。项目标注 3.10+，不代表最新 Python 3.14/3.15 的重型可选 AI 依赖已兼容；创建虚拟环境时请选 3.12。

**Windows 安装（任选其一）**：
- 官网安装包：[python.org/downloads](https://www.python.org/downloads/) — 安装时**勾选 "Add Python to PATH"**
- Microsoft Store：搜索 "Python 3.12" 安装
- winget：`winget install Python.Python.3.12`

**macOS**：`brew install python@3.12` 或官网 pkg
**Linux**：`sudo apt install python3.12 python3.12-venv` 或发行版等价命令

**验证**（新开终端）：Windows 用 `py -3.12 --version`，macOS/Linux 用 `python3.12 --version`，应显示 3.12.x。

留意 init 脚本实际选择的 Python；若选到其他版本，改用下方「手动步骤」中明确指定 3.12 的命令。

### Node.js 18+（仅 Electron 桌面版需要）

若只用**浏览器模式**，可跳过 Node。在项目根目录，Windows 执行 `.\.venv\Scripts\python.exe server.py`，macOS/Linux 执行 `.venv/bin/python server.py`，再访问 `http://localhost:7788`。

**Windows**：[nodejs.org](https://nodejs.org/) LTS，或 [nvm-windows](https://github.com/coreybutler/nvm-windows) → `nvm install lts` → `nvm use lts`
**macOS / Linux**：`brew install node` 或 `nvm install --lts`

**验证**：`node -v`、`npm -v` 有版本号即可。

---

## 手动步骤（不想用 init 脚本时）

**Windows（CMD）**

```bat
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
copy config\app.yaml.example config\app.yaml
.venv\Scripts\python.exe server.py
```

**macOS / Linux**

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
cp config/app.yaml.example config/app.yaml
.venv/bin/python server.py
```

**仅新安装时**复制模板，升级时保留原配置。浏览器访问 `http://localhost:7788`。使用 Electron 时另装 Node，执行 `npm install`，再用对应平台的启动脚本，不要同时另起 `server.py`。PowerShell 中 Windows 的 `.venv` 命令前加 `.\`，`copy` 改用 `Copy-Item`。

---

## 配置 API Key

**源码运行**：打开**设置 → 模型**，选择或新建预设，填写提供方、模型 ID、Base URL 和 API Key，保存并测试连接，再选择用于聊天的预设。本地模型需先运行接口，并填写实际已安装的模型名。

**Docker**：在宿主机 `config/app.yaml` 配置 `llm_presets` / `default_llm`，并在宿主机 `.env` 填对应密钥（如 `Gemini` → `GEMINI_API_KEY`）。仅当 `app.yaml` 不存在时从 `config/app.yaml.example` 复制，保留已有配置。UI 创建或保存预设也会联动写入 `.env`，只读挂载可能使整次保存失败。请通过宿主文件修改预设，保存后执行 `docker compose restart shikigami`。

也可在启动前编辑项目根目录的 `.env`（适合 Docker / 无界面部署）：

```env
GEMINI_API_KEY=你的密钥
# 或本地 Ollama（无需 Key）：
# OLLAMA_LOCAL_API_KEY=ollama
```

---

<a id="harness-setup"></a>

## AI 工具（Harness，可选）

让角色真正执行任务：读写其工作区文件、运行代码、搜索网页、查找历史消息、操作待办与计时器等。**所有工具能力默认关闭**——不打算用工具的话，本节可以完全跳过，普通聊天不受影响。

> **Harness 无需"启动"**：它不是常驻服务。每次任务由应用自动拉起一个一次性 Docker 容器，跑完自动销毁。完成下面的准备后，日常使用只需保持 **Docker 运行 + 打开应用**，没有任何额外的启动步骤。

### 第 1 步：安装并启动 Docker

Harness 任务要求宿主应用可访问 **Docker Linux 容器引擎**。代码无直接网络访问，容器根文件系统只读，授权后角色工作区可写；模型和受控网页请求经宿主桥接。各平台要求：

- **Windows**：安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（需要 WSL2，安装向导会引导）。**使用工具期间保持 Docker Desktop 运行**；可在其设置里勾选 "Start Docker Desktop when you sign in" 免每次手动开。
- **macOS**：可使用 [Docker Desktop](https://www.docker.com/products/docker-desktop/) 等 Linux 容器引擎，应用进程须能访问引擎及其绑定挂载。Intel 与 Apple Silicon 各自需要原生镜像构建及运行检查；只有 `docker` 命令不代表已可用。
- **Linux**：`sudo apt install docker.io`（或发行版等价命令）并启动服务；把用户加入 docker 组免 sudo。

**验证**（新开终端）：`docker version` 须同时显示 Client 和 Server；`docker info --format "{{.OSType}}"` 应返回 `linux`。应用进程也须能访问该引擎；仍需按本机 CPU 架构检查构建与运行。

### 第 2 步：构建 Harness 镜像（一次性，需联网）

在仓库根目录执行：

```bash
docker build -t shikigami-harness:workspace-0.2.1rc2 integrations/deepseek-harness
```

### 第 3 步：在应用里启用

1. **设置 → 模型 → 工具执行模型**：选一个已保存的模型预设（留空则跟随当前聊天模型）。
2. **设置 → 工具**：打开「默认启用 AI 工具」，按需开启 workspace / code / web / history / memory / todo 等能力（code 需要先开 workspace）。**ready** 仅说明本地前置条件与有效权限就绪；请另在「模型」测试连接，再试一个小任务。
3. **设置 → 人格 → 工具配置**（可选）：各项可开启、关闭或继承；全局关闭时也可只给某角色开启。计时器与待办还有独立权限。先检查普通工具使用，再按需开启自省发起任务。

更细的能力说明见 [integrations/deepseek-harness/README.md](../integrations/deepseek-harness/README.md)。

### 工具报错对照表

| 报错提示 | 含义与处理 |
| --- | --- |
| `Docker is not installed or not on PATH` | 未安装 Docker 或未加入 PATH → 回到第 1 步 |
| `Docker is installed but its engine is not running` | Docker 装了但引擎没启动 → 打开 Docker Desktop（或启动 docker 服务），等它就绪后重试 |
| `the Harness image is missing` | 镜像还没构建 → 执行第 2 步的 `docker build` |
| `Install the pinned project requirements` | 后端依赖不完整 → 激活 `.venv` 后 `pip install -r requirements.txt` |
| `AI tools are disabled for this character` | 该角色没开工具 → 设置 → 工具 或 人格 → 工具配置 中启用 |

---

<a id="upgrade-v0110"></a>

## 从 v0.11.0 升级

1. 退出应用及工具任务，备份配套旧源码、依赖版本，以及 **`.env`、整个 `config/`、`profiles/`、`data/`**。包括角色工作区 `profiles/<id>/workspace/`、聊天与记忆数据库、`data/agent/` 中的任务记录和工作区版本。若有 SQLite `-wal` / `-shm` 文件，一并保留；应用内工作区快照不等于完整备份。使用本地模型或语音资源时，也保留 `models/`、`Voices/`。
2. **旧 Docker 部署**：若 `/app/data` 尚未绑定挂载，先停止旧容器，在重建或删除前复制出该目录。备份后将保留的内容放入新版 Compose 挂载的宿主机 `data/`。新的空挂载只会遮住旧容器数据，不会自动迁移。
3. 更新源码时保留这些配置和数据，勿用模板覆盖或用空压缩包替换整个安装目录。在应用环境重装依赖：Windows 用 `.\.venv\Scripts\python -m pip install -r requirements.txt`，macOS/Linux 用 `.venv/bin/python -m pip install -r requirements.txt`。使用 Electron 时执行 `npm install` 更新依赖；使用 Harness 时按上方命令重建镜像。主应用 Docker 在数据迁移后执行 `docker compose up -d --build`。
4. 先关闭 AI 工具启动，核对模型配置、原有聊天、记忆与工作区文件，再逐项开启权限。更新可能新增数据库列，回退时需停机并成套恢复旧源码、依赖/镜像和备份数据，先另存更新后产生的成果。

---

## 常见问题与排查

### 闪退 / exit code 103

**现象**：双击 `launch.bat` 或启动 Electron 后立即关闭，终端显示：
```
[server:err] No Python at 'C:\Users\...\Python312\python.exe'
[server] exited unexpectedly (code=103)
```

**原因**：项目的 `.venv` 是从其他电脑复制来的，其 `pyvenv.cfg` 指向了原机器上的 Python 路径，本机不存在该路径。

**解决**：退出应用，先保留或重命名旧 `.venv`（如 `.venv-old`），再在项目根目录运行 `init.bat` 创建新环境。保留旧环境与配置、数据备份以便回退。

---

### npm install 失败 / ECONNRESET

**国内用户**：`init.bat` 会自动设置 Electron 镜像（npmmirror），通常无需手动配置。若单独执行 `npm install` 时网络报错：

```bat
set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
npm install
```

长期使用：`npm config set electron_mirror https://npmmirror.com/mirrors/electron/`

若出现 **`EPERM: operation not permitted`**：关闭占用目录的进程（IDE、杀毒软件），重启后删除 `node_modules` 再重装。

---

### Ubuntu：Electron 提示 `chrome-sandbox` 所有者 / 4755 错误

仅当 Electron 启动明确提示 `chrome-sandbox` 需由 root 所有且权限为 `4755` 时，在**受信任的项目根目录**对 npm 安装的官方 Electron helper 执行：

```bash
sudo chown root:root node_modules/electron/dist/chrome-sandbox
sudo chmod 4755 node_modules/electron/dist/chrome-sandbox
```

然后按原启动脚本重试，保持 Electron 沙箱开启。此 helper 用于 Electron 桌面窗口；下方的后端 + 浏览器模式不需要它。

---

### 只需后端 / 不用 Electron

可以完全跳过 Node.js。完成 init 后，在项目根目录使用项目虚拟环境：

**Windows（PowerShell）**

```powershell
.\.venv\Scripts\python.exe server.py
```

**macOS / Linux**

```bash
.venv/bin/python server.py
```

然后访问 `http://localhost:7788`。只有已激活本项目虚拟环境时，才使用裸 `python server.py`。

---

### 启动后 7788 一片空白

等待数秒（首次启动需加载模型）；若超过 30 秒，查看终端报错信息。

---

### init.bat 提示"找不到 Python"

确认 Python 已加入 PATH：新开 CMD 执行 `python --version`。若仍提示找不到，重新安装 Python 时勾选 **"Add Python to PATH"**。

---

**下一步**：完成安装后，阅读 [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md) 了解如何配置模型与角色、开始第一次对话。
