import os
import sys


def _frozen_exe_dir() -> str:
    """PyInstaller onefile/onedir：可执行文件所在目录（realpath 消解符号链接）。"""
    try:
        return os.path.dirname(os.path.realpath(os.path.abspath(sys.executable)))
    except OSError:
        return os.path.dirname(os.path.abspath(sys.executable))


def get_project_root() -> str:
    """
    获取项目根目录绝对路径。
    在源码模式下，返回 src 所在的根目录；
    在 PyInstaller 打包模式下，默认返回 server.exe 所在目录（与 Electron ``process.resourcesPath`` 一致）。

    打包运行时 Electron 会设置 ``SHIKIGAMI_APP_ROOT``，须与此保持一致，否则 ``user_packages/`` 会装在一处、
    主服务进程却在另一处解析，表现为向导显示已安装但 server 内 Kokoro/embedding/Qwen/STT 等全部 import 失败。

    若环境变量指向了存在但错误的目录（无 ``config/``），会回退到 ``sys.executable`` 所在目录，减轻误配。
    """
    if getattr(sys, 'frozen', False):
        exe_root = _frozen_exe_dir()
        candidates: list = []
        env_root = (os.environ.get('SHIKIGAMI_APP_ROOT') or '').strip()
        if env_root:
            try:
                er = os.path.realpath(os.path.abspath(env_root))
                if os.path.isdir(er):
                    candidates.append(er)
            except OSError:
                pass
        if exe_root not in candidates:
            candidates.append(exe_root)
        for root in candidates:
            try:
                if os.path.isdir(os.path.join(root, "config")):
                    return root
            except OSError:
                continue
        return candidates[0] if candidates else exe_root
    
    # 源码模式：基于此文件位置向上推 2 级 (src/utils/paths.py -> src/utils -> src -> root)
    # 注意：如果 server.py 在根目录，此处应推 2 级
    # __file__ 是 .../src/utils/paths.py
    current_file = os.path.abspath(__file__)
    src_utils_dir = os.path.dirname(current_file)
    src_dir = os.path.dirname(src_utils_dir)
    return os.path.dirname(src_dir)

def get_resource_path(relative_path: str) -> str:
    """
    获取资源文件的绝对路径。
    注意：此函数始终返回相对于「应用执行根目录」的路径。
    """
    return os.path.join(get_project_root(), relative_path)


def get_models_root() -> str:
    """模型根目录：环境变量 SHIKIGAMI_MODELS_ROOT（若存在且为目录）否则 <项目根>/models。

    注意：用户若在系统环境变量里误设 ``SHIKIGAMI_MODELS_ROOT``，会导致下载与运行时都去该目录，
    与安装目录下 ``resources/models`` 不一致；打包版 Electron 启动时会清除该变量。
    """
    env = (os.environ.get("SHIKIGAMI_MODELS_ROOT") or "").strip()
    if env:
        try:
            resolved = os.path.realpath(os.path.abspath(env))
            if os.path.isdir(resolved):
                return resolved
        except OSError:
            pass
    return os.path.abspath(os.path.join(get_project_root(), "models"))


def sense_voice_model_files_ok(model_dir: str) -> bool:
    """SenseVoice 目录是否含 tokens.txt 与 model.int8.onnx / model.onnx。"""
    if not model_dir or not os.path.isdir(model_dir):
        return False
    if not os.path.isfile(os.path.join(model_dir, "tokens.txt")):
        return False
    return os.path.isfile(os.path.join(model_dir, "model.int8.onnx")) or os.path.isfile(
        os.path.join(model_dir, "model.onnx")
    )


def resolve_sense_voice_model_dir(cfg_model_path: str = "") -> str:
    """与 model_bundles 中 sherpa_sense_voice 的 local_subdir 一致；兼容 HF/MS 多一层子目录。

    勿依赖 os.getcwd()：打包或子进程 cwd 不稳定时会导致「已下载但 STT 找不到模型」。
    """
    raw = (cfg_model_path or "").strip()
    if raw:
        ap = os.path.abspath(raw)
        if sense_voice_model_files_ok(ap):
            return ap
    primary = os.path.join(get_models_root(), "sherpa-onnx-sense-voice-zh-en-ja-ko-yue")
    if sense_voice_model_files_ok(primary):
        return os.path.abspath(primary)
    if os.path.isdir(primary):
        try:
            for name in sorted(os.listdir(primary)):
                sub = os.path.join(primary, name)
                if os.path.isdir(sub) and sense_voice_model_files_ok(sub):
                    return os.path.abspath(sub)
        except OSError:
            pass
    return ""
