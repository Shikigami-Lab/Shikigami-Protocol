import os
import sys

def get_project_root() -> str:
    """
    获取项目根目录绝对路径。
    在源码模式下，返回 src 所在的根目录；
    在 PyInstaller 打包模式下，返回 .exe 所在的目录。
    """
    if getattr(sys, 'frozen', False):
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
