"""EnergyRefreshTask — 后台 asyncio 任务，定期对所有已加载 session 做能量时间恢复。"""
import asyncio
import logging

logger = logging.getLogger(__name__)


def start_energy_refresh_from_config(session_manager, emotion_engine, app, config) -> None:
    """从 config 读取 energy 引擎配置并启动后台刷新任务。"""
    energy_cfg = config.get_engine_config("energy") if hasattr(config, "get_engine_config") else {}
    interval = energy_cfg.get("refresh_interval", 300)
    skip_if_active_secs = energy_cfg.get("recovery_skip_if_active_secs", 300)
    asyncio.create_task(
        start_energy_refresh(
            session_manager, emotion_engine, app,
            interval=interval,
            skip_if_active_secs=skip_if_active_secs,
        ),
        name="energy_refresh",
    )


async def start_energy_refresh(
    session_manager,
    emotion_engine,
    app,
    interval: int = 300,
    skip_if_active_secs: float = 300.0,
) -> None:
    """每 interval 秒对所有已加载 session 做时间恢复计算（轻量，无 LLM）。
    每个 session 使用其人格的 energy.refresh_interval 决定是否执行恢复。
    """
    logger.info("[energy_refresh] started, interval=%ds skip_if_active=%ds",
                interval, skip_if_active_secs)
    from src.config.effective_config import get_effective_engine_config
    while True:
        # Re-read global interval each cycle so UI changes take effect without restart
        try:
            energy_cfg_global = app.state.config.get_engine_config("energy") if hasattr(app.state.config, "get_engine_config") else {}
            interval = energy_cfg_global.get("refresh_interval", interval)
        except Exception:
            pass
        await asyncio.sleep(interval)
        sessions = session_manager.list_sessions()
        for session in sessions:
            try:
                energy_cfg = get_effective_engine_config(app, session.profile_id, "energy")
                refresh_interval = energy_cfg.get("refresh_interval", 300)
                recovery_gain = energy_cfg.get("recovery_gain_per_300s")
                if recovery_gain is not None:
                    try:
                        recovery_gain = float(recovery_gain)
                    except (TypeError, ValueError):
                        recovery_gain = None
                full_secs = energy_cfg.get("full_recovery_secs")
                if full_secs is not None:
                    try:
                        full_secs = float(full_secs)
                    except (TypeError, ValueError):
                        full_secs = None
                emotion_engine.apply_time_recovery(
                    session,
                    skip_if_active_secs=skip_if_active_secs,
                    refresh_interval=refresh_interval,
                    recovery_gain_per_300s=recovery_gain,
                    full_recovery_secs=full_secs,
                )
            except Exception as e:
                logger.warning("[energy_refresh] failed for %s: %s", session.id, e)
