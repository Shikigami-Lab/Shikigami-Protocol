# 环境安装与首次运行

> 想看「一共几步、设置里各 Tab 干什么」？请读 [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md)。
> **English:** [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md)

本文负责：**Python / Node 安装、`init.bat` / `init.sh` 用法、Docker 部署、常见闪退排查**。

---

## 快速路径

**已不再发布预编译桌面安装包（.exe / .dmg / .AppImage）。** 请使用下方 **源码运行**，或 **Docker**。[Releases](https://github.com/Shikigami-Lab/Shikigami-Protocol/releases) 仅作版本标签与说明，附件为 GitHub 默认提供的**源码压缩包**；桌面端打包与发行形态**正在开发中**。

### 源码运行

#### Windows
```bat
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
init.bat
launch.bat
```

`init.bat` 会自动：检测 Python → 创建/修复 `.venv` → `pip install -r requirements.txt` (核心依赖) → （有 Node 时）`npm install` → 复制 `.env.example` 为 `.env`。

> **AI 增强组件**：ChromaDB、ModelScope、Qwen3-TTS 等较重依赖库不再默认安装，请在启动后的 **设置 → 入门** 页面点击一键安装。

### macOS / Linux

```bash
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
bash init.sh
bash launch.sh
```

### Docker（服务器 / NAS / 无 Electron）

```bash
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
cp .env.example .env        # 填写 API Key
docker compose up -d
# 浏览器打开 http://localhost:7788
```

`profiles/`、`models/`、`Voices/`、`config/` 已挂载为 volume，数据不随容器销毁。GPU 可选：取消注释 `docker-compose.yml` 中的 `deploy.resources`（需 nvidia-container-toolkit）。

常用命令：`docker compose up -d` / `down` / `logs -f` / `up -d --build`

---

## 前置依赖

### Python 3.10+

**作用**：运行 `server.py` 及全部后端逻辑。

**Windows 安装（任选其一）**：
- 官网安装包：[python.org/downloads](https://www.python.org/downloads/) — 安装时**勾选 "Add Python to PATH"**
- Microsoft Store：搜索 "Python 3.12" 安装
- winget：`winget install Python.Python.3.12`

**macOS**：`brew install python@3.12` 或官网 pkg
**Linux**：`sudo apt install python3.12 python3.12-venv` 或发行版等价命令

**验证**（新开终端）：`python --version` 或 `py -3 --version` → 应显示 3.10+

### Node.js 18+（仅 Electron 桌面版需要）

若只用**浏览器模式**（`python server.py` + 浏览器访问 `http://localhost:7788`），可跳过此项。

**Windows**：[nodejs.org](https://nodejs.org/) LTS，或 [nvm-windows](https://github.com/coreybutler/nvm-windows) → `nvm install lts` → `nvm use lts`
**macOS / Linux**：`brew install node` 或 `nvm install --lts`

**验证**：`node -v`、`npm -v` 有版本号即可。

---

## 手动步骤（不想用 init 脚本时）

```bash
# 1. 创建虚拟环境（仅首次）
python -m venv .venv

# 2. 激活并安装依赖
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt

# 3. 桌面版：安装 Node 依赖（仅首次）
npm install

# 4. 复制环境变量模板（仅首次）
cp .env.example .env
# 编辑 .env，填入至少一个 LLM API Key

# 5. 启动
# 桌面版：
npm start
# 或仅后端 + 浏览器：
python server.py
# 浏览器访问 http://localhost:7788
```

---

## 配置 API Key

**推荐**：启动应用后，在 **设置 → 模型** 里直接填写 API Key，自动保存。

也可在启动前编辑项目根目录的 `.env`（适合 Docker / 无界面部署）：

```env
GEMINI_API_KEY=你的密钥
# 或本地 Ollama（无需 Key）：
# OLLAMA_LOCAL_API_KEY=ollama
```

---

## 常见问题与排查

### 闪退 / exit code 103

**现象**：双击 `launch.bat` 或启动 Electron 后立即关闭，终端显示：
```
[server:err] No Python at 'C:\Users\...\Python312\python.exe'
[server] exited unexpectedly (code=103)
```

**原因**：项目的 `.venv` 是从其他电脑复制来的，其 `pyvenv.cfg` 指向了原机器上的 Python 路径，本机不存在该路径。

**解决**：在项目根目录运行 `init.bat`（会自动检测并重建 `.venv`），或手动删除 `.venv` 后重新执行 `pip install -r requirements.txt`。

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

### 只需后端 / 不用 Electron

可以完全跳过 Node.js：

```bash
python server.py
# 然后浏览器访问 http://localhost:7788
```

---

### 启动后 7788 一片空白

等待数秒（首次启动需加载模型）；若超过 30 秒，查看终端报错信息。

---

### init.bat 提示"找不到 Python"

确认 Python 已加入 PATH：新开 CMD 执行 `python --version`。若仍提示找不到，重新安装 Python 时勾选 **"Add Python to PATH"**。

---

**下一步**：完成安装后，阅读 [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md) 了解如何配置模型与角色、开始第一次对话。
