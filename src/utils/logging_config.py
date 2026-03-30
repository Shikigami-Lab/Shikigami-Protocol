"""应用级日志配置：level、format、第三方库降噪；终端输出同时写入 debug.log。"""
import logging
import os
from typing import Any

_DEBUG_LOG = "data/logs/debug.log"


def setup_app_logging(config: Any) -> None:
    """根据 config 设置 logging 根 level 与 format，并压低第三方库的请求日志。
    终端输出（logger.info 等）同时追加到 data/logs/debug.log，与 log_debug() 的 JSON 行混在同一文件。"""
    level = getattr(logging, (getattr(config, "log_level", "info") or "info").upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
    )
    # 终端输出同时写入 debug.log
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG), exist_ok=True)
        file_handler = logging.FileHandler(_DEBUG_LOG, mode="a", encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(file_handler)
    except Exception:
        pass
    # 将 httpx/openai/httpcore 设为 WARNING，避免每条 HTTP 请求都打 INFO 到 stderr（否则 Electron 里会显示成 [server:err]）
    for _name in ("openai", "httpx", "httpcore"):
        logging.getLogger(_name).setLevel(logging.WARNING)
