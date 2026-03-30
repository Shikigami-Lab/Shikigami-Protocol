"""engine_warnings.py — 辅助引擎健康状态跟踪

用法：
    from src.utils.engine_warnings import set_warning, clear_warning

当辅助引擎（analysis/reflection）连接失败时 set_warning；
成功恢复时 clear_warning；
前端通过 GET /api/engine_warnings 轮询并在状态栏显示。
"""


def set_warning(app, key: str, msg: str) -> None:
    """设置引擎警告（幂等，内容不变时不更新）。"""
    warnings: dict = getattr(app.state, "engine_warnings", None)
    if warnings is None:
        app.state.engine_warnings = {}
        warnings = app.state.engine_warnings
    if warnings.get(key) != msg:
        warnings[key] = msg


def clear_warning(app, key: str) -> None:
    """清除引擎警告（已清除时无操作）。"""
    warnings: dict = getattr(app.state, "engine_warnings", None)
    if warnings:
        warnings.pop(key, None)
