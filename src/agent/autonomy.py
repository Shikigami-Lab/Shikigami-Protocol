"""Reflection admission and background consumption of the existing Harness loop.

Only the selected single-chat character may start new work. Already admitted
work keeps its captured identity after a UI switch. No human messages are made.
"""
import asyncio
import logging
import uuid

from src.agent.capabilities import capability_context

logger = logging.getLogger(__name__)


class AutonomyRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.tasks = set()
        self.group_view = False

    def enabled(self, session):
        runtime = self.runtime
        if self.group_view or runtime._closed or not runtime.autonomy_allowed(session.profile_id):
            return False
        if session.id != session.profile_id:
            return False
        manager = getattr(runtime.app.state, 'session_manager', None)
        current = manager.get_current() if manager else None
        return current is not None and current.id == session.id and current.profile_id == session.profile_id

    def _build_messages(self, session, goal):
        from src.config.effective_config import get_effective_max_history_turns
        from src.prompt.pipeline import build_messages
        runtime = self.runtime
        preset = runtime.app.state.config.get_active_llm_preset()
        return build_messages(session, goal, session.conversation_store,
            n_history_turns=get_effective_max_history_turns(runtime.app, session.profile_id),
            preset=preset, app=runtime.app,
            extra_system=capability_context(runtime.access_for(session.profile_id), origin='reflection'))

    async def start(self, session, goal, cycle_id, *, from_tool_results=False):
        runtime = self.runtime
        if (from_tool_results or not isinstance(goal, str) or not 0 < len(goal.strip()) <= 1000
                or not self.enabled(session)):
            return False
        if session.id in getattr(runtime.app.state, 'chat_streaming_sessions', set()):
            return False
        # Do not compete with another task of this character, or start several
        # autonomous workers that monopolize the global foreground slots.
        if any(not r.terminal and (r.profile_id == session.profile_id
                or r.envelope.get('origin') == 'reflection') for r in runtime._runs.values()):
            return False
        if sum(not r.terminal for r in runtime._runs.values()) >= 2:
            return False
        settings = runtime.settings
        admission = str(uuid.uuid5(uuid.NAMESPACE_URL, session.profile_id + ':reflection:' + str(cycle_id)))
        allowance = runtime.store.claim_autonomy(admission, session.profile_id, goal,
            settings['autonomy_interval_seconds'], settings['autonomy_max_requests_per_hour'],
            settings['max_model_requests'])
        if not allowance:
            return False
        run = None
        try:
            messages = self._build_messages(session, goal.strip())
            # Build is synchronous: no UI switch can interleave admission and
            # prepare. Execution subsequently remains bound to this session.
            run = runtime.prepare(session, messages,
                runtime.app.state.config.get_active_llm_preset(), request_id=admission, origin='reflection')
            run.gateway.max_requests = allowance
            task = asyncio.create_task(self._consume(run), name='reflection-action-' + run.id)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return True
        except Exception:
            if run is not None:
                await runtime.cancel(run.id, run.profile_id)
            # An uncertain admission is not automatically refunded/replayed.
            logger.warning('Autonomous task could not start for captured character')
            return False

    async def _consume(self, run):
        try:
            async for _ in self.runtime.stream(run):
                pass  # Task events use the existing broadcast/durable drawer.
        except Exception:
            logger.warning('Autonomous task stream ended unexpectedly')
            if not run.terminal:
                await self.runtime.cancel(run.id, run.profile_id)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

