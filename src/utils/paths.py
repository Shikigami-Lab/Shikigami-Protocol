import os
import sys

def get_project_root() -> str:
    """
    获取项目根目录绝对路径。
    在源码模式下，返回 src 所在的根目录；
    在 PyInstaller 打包模式下，默认返回 server.exe 所在目录（与 Electron ``process.resourcesPath`` 一致）。

    打包运行时 Electron 会设置 ``SHIKIGAMI_APP_ROOT``，须与此保持一致，否则 ``user_packages/`` 会装在一处、
    主服务进程却在另一处解析，表现为向导显示已安装但 server 内 Kokoro/embedding/Qwen/STT 等全部 import 失败。
    """
    if getattr(sys, 'frozen', False):
        env_root = (os.environ.get('SHIKIGAMI_APP_ROOT') or '').strip()
        if env_root:
            try:
                resolved = os.path.abspath(env_root)
                if os.path.isdir(resolved):
                    return resolved
            except OSError:
                pass
        # PyInstaller 打包模式：sys.executable 是 .exe 的完整路径
        return os.path.dirname(os.path.abspath(sys.executable))
    
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
