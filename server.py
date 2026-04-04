import asyncio
import logging
import os
import sys
import warnings
from pathlib import Path

# ── CI 构建后冒烟测试入口 ────────────────────────────────────────────────────
# 在任何第三方/项目 import 之前处理，确保缺模块时能给出清晰错误而非随机崩溃。
# 由 release.yml 的 "Smoke-test built server binary" 步骤调用。
if '--self-check' in sys.argv:
    import importlib as _ilib
    _SMOKE = [
        # 已知 PyInstaller 容易因动态 import 而漏掉的 stdlib
        'timeit', 'pickletools', 'profile', 'pstats', 'cProfile',
        'doctest', 'dis', 'tracemalloc', 'multiprocessing',
        'pickle', 'pprint', 'inspect', 'ast', 'csv', 'sqlite3',
        # 核心第三方依赖
        'fastapi', 'uvicorn', 'pydantic', 'httpx', 'openai',
        'edge_tts', 'websockets', 'dotenv', 'yaml', 'ruamel',
        'soundfile', 'anyio', 'starlette', 'click',
    ]
    _fail = []
    for _m in _SMOKE:
        try:
            _ilib.import_module(_m)
        except Exception as _e:
            _fail.append(f'{_m}: {_e}')
    if _fail:
        print('[self-check] FAILED — missing modules:', file=sys.stderr)
        for _f in _fail:
            print(f'  {_f}', file=sys.stderr)
        sys.exit(1)
    print('[self-check] OK')
    sys.exit(0)
# ─────────────────────────────────────────────────────────────────────────────

# pydub 在未安装 ffmpeg 时发出 RuntimeWarning，写入 stderr 被 Electron 误标为 err。
# SenseVoice/funasr 使用 torchaudio 加载音频，不依赖 ffmpeg，可安全忽略。
warnings.filterwarnings("ignore", message=".*ffmpeg.*", category=RuntimeWarning)
warnings.filterwarnings("ignore", message=".*avconv.*", category=RuntimeWarning)
# sox Python 包在未安装 SoX 可执行文件时发出警告，我们用 soundfile 后端，无需 SoX。
warnings.filterwarnings("ignore", message=".*SoX could not be found.*")
warnings.filterwarnings("ignore", message=".*sox.*not.*found.*")
# transformers 在未安装 flash-attn 时输出 WARNING 日志；设为 error 只保留真正的错误。
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

# 保证项目根目录在 sys.path 首位，Electron 等子进程启动时能正确解析 src 包
if not getattr(sys, 'frozen', False):
    _root = os.path.dirname(os.path.abspath(__file__))
    if _root not in sys.path:
        sys.path.insert(0, _root)

from src.utils.paths import get_project_root, get_resource_path

# 打包：可选依赖装在 user_packages/；须与 get_project_root()（含 SHIKIGAMI_APP_ROOT）一致，并处理 pip --target 生成的 .pth
if getattr(sys, 'frozen', False):
    _user_pkg = os.path.join(get_project_root(), 'user_packages')
    if os.path.isdir(_user_pkg):
        try:
            import site
            if _user_pkg not in sys.path:
                sys.path.insert(0, _user_pkg)
            # addsitedir：执行目录内 .pth，否则部分 wheel 的子路径不会被加入 sys.path
            site.addsitedir(_user_pkg)
        except Exception:
            if _user_pkg not in sys.path:
                sys.path.insert(0, _user_pkg)

# 固定工作目录为项目根，确保所有相对路径（profiles/, groups/, lorebooks/ 等）在打包 exe 下也能正确解析
os.chdir(get_project_root())

# 启动器子进程只输出 JSON / SETUP_EVENT，禁止混入 SPLASH 行（否则 Electron 无法 parse JSON）
_WIZARD_ONLY_STDOUT = any(
    x in sys.argv
    for x in (
        "--setup-status-json",
        "--wizard-download",
        "--wizard-apply-stt",
        "--wizard-launch-gptsovits",
        "--wizard-pip-uninstall",
        "--wizard-save-gptsovits-dir",
    )
)

# 启动器「卸载 torch」子进程若先执行 embedding 预热，会 import transformers → 映射 torch DLL；
# 随后在同进程内 rmtree user_packages/torch 时 Windows 报 WinError 5（非「主程序未退出」）。
_SKIP_EMBEDDING_BOOTSTRAP_FOR_UNINSTALL = "--wizard-pip-uninstall" in sys.argv

# 尽早向 Electron 发送进度（模块导入前），避免启动界面长时间停在 0%
if not _WIZARD_ONLY_STDOUT:
    sys.stdout.write("SPLASH:33:Loading Python modules...\n")
    sys.stdout.flush()

from dotenv import load_dotenv
load_dotenv()  # 加载 .env（GOOGLE_API_KEY 等）到 os.environ

# 在首次 import 任何会拉取 huggingface_hub 的模块之前设好镜像；否则库在 import 时缓存默认 endpoint，后续改 env 无效
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# embedding 模块在 import 时会先读 config 设好离线 env，再导出；此处仅做 PreTrainedModel 补丁
if not _SKIP_EMBEDDING_BOOTSTRAP_FOR_UNINSTALL:
    try:
        from src.memory.embedding import _ensure_pretrained_model_on_transformers
        _ensure_pretrained_model_on_transformers()
    except Exception:
        pass

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from src.config.app_config import AppConfig
from src.core.session_manager import SessionManager
from src.api.chat import router as chat_router
from src.api.sessions import router as sessions_router
from src.api.settings import router as settings_router
from src.api.settings_ext import router as settings_ext_router
from src.api.ws_tts import router as ws_tts_router
from src.api.history import router as history_router
from src.api.events import router as events_router
from src.api.status import router as status_router
from src.api.memory import router as memory_router
from src.tools.timer.api import router as timers_router
from src.tools.todo.api import router as todos_router
from src.api.vlm import router as vlm_router
from src.api.stt import router as stt_router
try:
    from src.api.debug import router as debug_router
    _debug_router_loaded = True
except ImportError:
    _debug_router_loaded = False  # not present in public tree — silently skip
from src.api.groups import router as groups_router
from src.api.profiles_import import router as profiles_import_router
from src.api.lorebooks import router as lorebooks_router
from src.api.preferences import router as preferences_router
from src.api.setup_guide import router as setup_guide_router
from src.api.tools import router as tools_router
from src.utils.debug_logger import log_server_start

logger = logging.getLogger(__name__)

_STARTUP_PROGRESS_FILE = "startup_progress.json"


def _write_startup_progress(pct: int, msg: str) -> None:
    """向 Electron 发送启动进度：stdout 实时推送 + 文件写入（双保险）。"""
    # stdout 方式：Electron 监听 serverProcess.stdout，零延迟，无轮询
    sys.stdout.write(f"SPLASH:{pct}:{msg}\n")
    sys.stdout.flush()
    # 文件方式：兜底，用于 Electron 尚未连接 stdout 的极端情况
    try:
        import json
        _sp_path = os.path.join(os.getcwd(), _STARTUP_PROGRESS_FILE)
        with open(_sp_path, "w", encoding="utf-8") as f:
            json.dump({"pct": pct, "msg": msg}, f, ensure_ascii=False)
    except Exception:
        pass


def _load_version_from_package_json() -> str:
    """从 package.json 读取 version，作为全项目唯一版本来源。"""
    try:
        import json
        pkg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package.json")
        with open(pkg_path, "r", encoding="utf-8") as f:
            return json.load(f).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    _write_startup_progress(35, "Loading configuration...")
    config = AppConfig.load()
    app.state.config = config
    app.state.version = _load_version_from_package_json()
    app.state.public_mode = os.getenv("PUBLIC_MODE", "false").lower() == "true"

    from src.utils.logging_config import setup_app_logging
    setup_app_logging(config)

    _write_startup_progress(40, "Initializing session...")
    sm = SessionManager(config)
    await sm.init()
    app.state.session_manager = sm

    # ── P6 记忆管理器注册表（懒创建，key=profile_id）──────────────────────────
    app.state.memory_managers = {}

    # ── 群组管理（方案 B：群聊会话实体）──────────────────────────────────────
    from src.core.group_manager import GroupManager
    app.state.group_manager = GroupManager(config)

    # ── P7 用户回复节奏追踪器 ──────────────────────────────────────────────────
    from src.context.cadence_tracker import CadenceTracker
    app.state.cadence_tracker = CadenceTracker()

    # ── 全局最后用户消息时间（用于跨 session 沉默检测）─────────────────────────
    # 任意 session 收到用户消息时更新，ASE 使用 max(session, global) 避免误触发
    app.state.last_user_message_time = 0.0

    # ── 辅助引擎健康警告（key→警告文本，前端轮询展示）────────────────────────
    app.state.engine_warnings = {}

    log_server_start(
        host=config.host,
        port=config.port,
        default_llm=config.default_llm,
        default_tts=config.default_tts,
    )

    # 启动日志：列出所有已加载的 profile-session
    for s in sm.list_sessions():
        logger.info("[server] profile-session 已加载: id=%s name=%s root=%s",
                    s.id, s.display_name, s.storage_root)

    from src.memory import log_startup_config as log_memory_config
    log_memory_config(config)

    _write_startup_progress(50, "Initializing engines...")
    # ── P4 引擎初始化 ────────────────────────────────────────────────────────
    from src.engines.emotion_engine import EmotionEngine
    from src.engines.affinity_engine import AffinityEngine
    from src.engines.energy_refresh_task import start_energy_refresh_from_config

    app.state.emotion_engine = EmotionEngine()
    app.state.affinity_engine = AffinityEngine()

    start_energy_refresh_from_config(sm, app.state.emotion_engine, app, config)

    from src.llm.registry import ensure_analysis_provider
    ensure_analysis_provider(config)

    _write_startup_progress(60, "Registering tools and timers...")
    # ── 工具注册 ────────────────────────────────────────────────────────────────
    from src.tools.todo import register as register_todo
    from src.tools.timer import register as register_timer_tool
    from src.tools.websearch import register as register_websearch
    from src.tools.trends import register as register_trend
    from src.tools.weather import register as register_weather
    register_todo(app)
    register_timer_tool(app)
    register_websearch(app)
    register_trend(app)
    register_weather(app)

    # ── 计时器到期：后端直接触发 system_trigger ─────────────────────────────────
    from src.tools.timer.manager import _timer_manager
    from src.tools.timer.expire_handler import make_expire_callback
    from src.utils.debug_logger import log_debug

    _loop = asyncio.get_event_loop()
    _timer_manager.register_expire_callback(make_expire_callback(app, _loop))
    log_debug("timer_expire_callbacks_registered", count=len(_timer_manager._expire_callbacks))

    _write_startup_progress(70, "Starting reflection and proactive speaking...")
    # ── ReflectionEngine + AseEngine ────────────────────────────────────────
    from src.core.reflection import ReflectionEngine
    from src.core.ase import AseEngine

    reflection_engine = ReflectionEngine(app)
    ase_engine = AseEngine(app)
    app.state.reflection_engine = reflection_engine
    app.state.ase_engine = ase_engine

    ref_cfg = config.get_reflection_config()
    ase_cfg = config.get_ase_config()

    if ref_cfg.get("enabled"):
        await reflection_engine.start()
    else:
        logger.info("[server] ReflectionEngine 已禁用 (reflection.enabled=false)")

    if ase_cfg.get("enabled"):
        await ase_engine.start()
    else:
        logger.info("[server] AseEngine 已禁用 (ase.enabled=false)")

    _write_startup_progress(85, "Starting daily memory tasks...")
    # ── 每日记忆/遗忘任务（先 day summary 再 forgetting，只跑已加载且昨日有对话）────
    from src.core.daily_memory_job import run_daily_memory_loop
    daily_memory_task = asyncio.create_task(run_daily_memory_loop(app))
    app.state.daily_memory_task = daily_memory_task
    logger.info("[server] 每日记忆/遗忘循环已启动")

    # ── 趋势感知 + 天气 后台抓取循环 ─────────────────────────────────────────
    from src.tools.trends.fetcher import trend_fetcher_loop
    from src.tools.weather.fetcher import weather_fetcher_loop
    app.state.trend_fetcher_task = asyncio.create_task(trend_fetcher_loop(app))
    app.state.weather_fetcher_task = asyncio.create_task(weather_fetcher_loop(app))
    logger.info("[server] 趋势感知/天气抓取循环已启动")

    # ── Qwen3-TTS 预热（后台任务，不阻塞启动）────────────────────────────────
    if config.default_tts == "qwen3_tts":
        async def _prewarm_qwen3():
            try:
                import asyncio as _asyncio
                from src.tts.qwen3_tts_provider import _get_model, _resolve_model_path, _DEFAULT_MODEL_IDS, _HF_REPO_TO_LOCAL, _PROJECT_ROOT as _Q_ROOT
                import os as _os
                qcfg = config.tts_config.get("qwen3_tts") or {}
                mode     = qcfg.get("mode", "custom_voice")
                model_id = (qcfg.get("model_id") or "").strip()
                device   = qcfg.get("device", "cuda:0")
                dtype    = qcfg.get("dtype", "bfloat16")
                attn     = qcfg.get("attn_implementation", "eager")
                compile_ = bool(qcfg.get("use_torch_compile", False))
                if not model_id:
                    model_id = _DEFAULT_MODEL_IDS.get(mode, _DEFAULT_MODEL_IDS["custom_voice"])
                if model_id in _HF_REPO_TO_LOCAL:
                    local = _HF_REPO_TO_LOCAL[model_id]
                    if _os.path.isdir(_os.path.join(_Q_ROOT, local)):
                        model_id = local
                resolved = str(_resolve_model_path(model_id))
                logger.info("[server] Qwen3-TTS 预热中 (model=%s device=%s)…", resolved, device)
                await _asyncio.to_thread(_get_model, resolved, device, dtype, attn, compile_)
                logger.info("[server] Qwen3-TTS 预热完成")
            except Exception as _e:
                logger.warning("[server] Qwen3-TTS 预热失败（不影响正常运行）: %s", _e)
        asyncio.create_task(_prewarm_qwen3())

    _write_startup_progress(100, "Opening window...")
    logger.info("[server] ╔══════════════════════════════════════╗")
    logger.info("[server] ║      Shikigami Protocol  v%s        ║", app.state.version)
    logger.info("[server] ╚══════════════════════════════════════╝")
    logger.info("[server] 监听地址: http://%s:%s", config.host, config.port)
    yield

    for _task_attr in ("daily_memory_task", "trend_fetcher_task", "weather_fetcher_task"):
        _t = getattr(app.state, _task_attr, None)
        if _t:
            _t.cancel()
            try:
                await _t
            except asyncio.CancelledError:
                pass
    await reflection_engine.stop()
    await ase_engine.stop()
    await sm.cleanup()


app = FastAPI(title="Shikigami Protocol", lifespan=lifespan)


@app.get("/version")
def get_version(request: Request):
    """返回应用版本（来自 package.json），供 UI 等使用。"""
    return {"version": getattr(request.app.state, "version", "0.0.0")}


# 与仓库根 server.py、sync_public PUBLIC_SYNC_DOC_PATHS 一致；须在 StaticFiles(/) 之前注册，否则会被静态路由吞掉 404。
_USER_DOCS_STEMS = frozenset(
    {
        "GETTING_STARTED",
        "ARCHITECTURE_REFERENCE",
        "SETUP_FIRST_RUN",
        "profile_prompts",
    }
)
_USER_DOCS_PHYSICAL = frozenset(f"{stem}.{loc}.md" for stem in _USER_DOCS_STEMS for loc in ("zh", "en"))
_USER_DOCS_LEGACY_TO_ZH = {
    "GETTING_STARTED.md": "GETTING_STARTED.zh.md",
    "ARCHITECTURE_REFERENCE.md": "ARCHITECTURE_REFERENCE.zh.md",
    "SETUP_FIRST_RUN.md": "SETUP_FIRST_RUN.zh.md",
    "profile_prompts.md": "profile_prompts.zh.md",
}
_USER_DOCS_URL_ALLOW = _USER_DOCS_PHYSICAL | frozenset(_USER_DOCS_LEGACY_TO_ZH.keys())


@app.get("/user-docs/{doc_name}", response_class=PlainTextResponse)
def serve_user_doc(doc_name: str):
    """在浏览器中查看 docs/ 下白名单 Markdown（text/markdown）。"""
    if "/" in doc_name or doc_name.startswith("."):
        raise HTTPException(status_code=404, detail="Unknown document")
    if doc_name in _USER_DOCS_LEGACY_TO_ZH:
        doc_name = _USER_DOCS_LEGACY_TO_ZH[doc_name]
    elif doc_name not in _USER_DOCS_URL_ALLOW:
        raise HTTPException(status_code=404, detail="Unknown document")
    
    # 始终基于项目根目录查找，无论是否打包
    docs_dir = Path(get_resource_path("docs")).resolve()
    path = (docs_dir / doc_name).resolve()
    try:
        path.relative_to(docs_dir)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid path")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    text = path.read_text(encoding="utf-8")
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(sessions_router)
app.include_router(settings_router)
app.include_router(settings_ext_router)
app.include_router(ws_tts_router)
app.include_router(history_router)
app.include_router(events_router)
app.include_router(status_router)
app.include_router(memory_router)
app.include_router(timers_router)
app.include_router(todos_router)
app.include_router(vlm_router)
app.include_router(stt_router)
if _debug_router_loaded:
    app.include_router(debug_router)
app.include_router(groups_router)
app.include_router(profiles_import_router)
app.include_router(lorebooks_router)
app.include_router(preferences_router)
app.include_router(setup_guide_router)
app.include_router(tools_router)

# Serve frontend — must be mounted LAST (catches all remaining routes)
static_dir = get_resource_path("static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


if __name__ == "__main__":
    # ── Electron 启动器：在常驻服务启动前跑一次性子任务（与主进程隔离，减轻 Windows 文件锁问题）

    def _wizard_stdout_utf8_line(line: str) -> None:
        try:
            sys.stdout.buffer.write(line.encode("utf-8"))
            sys.stdout.buffer.flush()
        except (AttributeError, OSError, BrokenPipeError, ValueError):
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except Exception:
                pass

    if "--setup-status-json" in sys.argv:
        import json as _json

        config = AppConfig.load()
        from src.api.setup_guide import build_setup_status_dict

        # Windows 打包 exe 下 stdout 常为系统代码页；写 buffer 保证 UTF-8，与前端读静态资源一致
        _line = _json.dumps(build_setup_status_dict(config), ensure_ascii=False) + "\n"
        try:
            sys.stdout.buffer.write(_line.encode("utf-8"))
            sys.stdout.buffer.flush()
        except (AttributeError, OSError, BrokenPipeError, ValueError):
            print(_line, end="")
        raise SystemExit(0)
    if "--wizard-download" in sys.argv:
        _wi = sys.argv.index("--wizard-download")
        _bid = sys.argv[_wi + 1] if len(sys.argv) > _wi + 1 else ""
        _src = sys.argv[_wi + 2] if len(sys.argv) > _wi + 2 else "auto"
        from src.api.setup_guide import run_wizard_download_cli

        raise SystemExit(run_wizard_download_cli(_bid, _src))
    if "--wizard-apply-stt" in sys.argv:
        _wi = sys.argv.index("--wizard-apply-stt")
        _mp = sys.argv[_wi + 1] if len(sys.argv) > _wi + 1 else ""
        from src.api.setup_guide import wizard_apply_stt_model_cli

        raise SystemExit(wizard_apply_stt_model_cli(_mp))
    if "--wizard-launch-gptsovits" in sys.argv:
        config = AppConfig.load()
        from src.api.setup_guide import wizard_launch_gptsovits_cli

        raise SystemExit(wizard_launch_gptsovits_cli(config))
    if "--wizard-pip-uninstall" in sys.argv:
        import json as _json

        from src.api.setup_guide import run_wizard_pip_uninstall_cli

        _raw = sys.stdin.read() or "{}"
        try:
            _body = _json.loads(_raw)
        except _json.JSONDecodeError:
            _wizard_stdout_utf8_line("SETUP_RESULT:" + _json.dumps({"ok": False, "errors": ["invalid json stdin"]}) + "\n")
            raise SystemExit(1)
        raise SystemExit(
            run_wizard_pip_uninstall_cli(_body.get("packages") or [], _body.get("dirs") or [])
        )
    if "--wizard-save-gptsovits-dir" in sys.argv:
        import json as _json

        from src.api.settings_ext import _load_yaml, _save_yaml

        _raw = sys.stdin.read() or "{}"
        try:
            _body = _json.loads(_raw)
        except _json.JSONDecodeError:
            _wizard_stdout_utf8_line("SETUP_RESULT:" + _json.dumps({"ok": False, "error": "invalid json stdin"}) + "\n")
            raise SystemExit(1)
        _dir = (_body.get("dir") or "").strip()
        y, data = _load_yaml()
        if "tts" not in data or not isinstance(data.get("tts"), dict):
            data["tts"] = {}
        if "gpt_sovits" not in data["tts"] or not isinstance(data["tts"].get("gpt_sovits"), dict):
            data["tts"]["gpt_sovits"] = {}
        data["tts"]["gpt_sovits"]["dir"] = _dir
        _save_yaml(y, data)
        _wizard_stdout_utf8_line("SETUP_RESULT:" + _json.dumps({"ok": True}, ensure_ascii=False) + "\n")
        raise SystemExit(0)

    config = AppConfig.load()
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        reload=False,
        log_level=config.log_level,
    )
