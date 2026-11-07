/* Task events deliberately live outside chat messages, history and TTS. */
(function (root) {
  'use strict';
  const terminal = state => ['completed', 'cancelled', 'failed'].includes(state);
  const timerAction = name => {
    const actions = { timer_create: 'create', timer_schedule: 'schedule', timer_list: 'list', timer_cancel: 'cancel' };
    const nativeName = String(name || '').replace(/^mcp__shikigami__/, '');
    return Object.prototype.hasOwnProperty.call(actions, nativeName) ? actions[nativeName] : '';
  };
  const capabilityKeys = ['workspace_enabled', 'code_enabled', 'web_enabled', 'history_enabled', 'memory_enabled', 'todo_enabled'];
  const workspaceTabs = ['files', 'plans', 'reminders', 'tasks'];
  const workspaceWidthKey = 'agentWorkspaceWidth';
  const minWorkspaceWidth = 320;
  const maxWorkspaceWidth = 720;
  const minWorkspaceChatWidth = 520;
  const fileCategories = ['note', 'work', 'material', 'other'];
  const textOf = value => typeof value === 'string' ? value : (value == null ? '' : JSON.stringify(value, null, 2));
  const keyOf = run => JSON.stringify([run.profile_id, run.session_id, run.run_id || run.id]);
  const visibleRuns = (runs, profile, session) => Object.values(runs)
    .filter(run => run.profile_id === profile && run.session_id === session)
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
  function reduceEvent(runs, event) {
    if (!event || !event.run_id || !event.profile_id || !event.session_id || !Number.isInteger(event.sequence)) return runs;
    if (!['started', 'status', 'tool_call', 'tool_result', 'approval_requested', 'completed', 'cancelled', 'failed'].includes(event.type)) return runs;
    const key = keyOf(event);
    const old = runs[key];
    if (old && (terminal(old.state) || event.sequence <= old.sequence)) return runs;
    const run = { id: event.run_id, run_id: event.run_id, profile_id: event.profile_id,
      session_id: event.session_id, created_at: event.created_at || new Date().toISOString(),
      state: 'running', events: [], approval: null, ...old, sequence: event.sequence,
      request_id: old?.request_id || event.request_id };
    if (['reflection', 'proactive', 'user'].includes(event.origin)) run.origin = event.origin;
    run.events = [...run.events, event];
    if (typeof event.workspace_warning === 'string') run.workspace_warning = event.workspace_warning;
    if (Array.isArray(event.artifacts)) run.artifacts = event.artifacts;
    if (terminal(event.type)) {
      run.state = event.type;
      run.approval = null;
      run.cancelling = false;
      run.error = event.type === 'failed' ? (event.error || old?.error || '') : '';
    } else if (event.type === 'approval_requested' && event.approval_id) {
      run.approval = event;
      run.state = 'approval_requested';
    } else if (event.type === 'status') {
      run.state = event.status || 'running';
      if (run.state !== 'approval_requested') run.approval = null;
      if (terminal(run.state)) { run.cancelling = false; run.error = run.state === 'failed' ? (event.error || old?.error || '') : ''; }
    }
    return { ...runs, [key]: run };
  }
  const mixin = {
    data() { return { agentRuns: {}, agentRunErrors: {}, agentPollBusy: false,
      agentPollTimer: null, agentViewVersion: 0, agentChatContext: null, agentPendingChatReplies: {},
      agentContinueRunId: '', agentHistorySequence: 0, agentCapabilities: capabilityKeys,
      agentWorkspaceOpen: false, agentWorkspaceFiles: [], agentWorkspaceInventory: {}, agentWorkspaceSelected: null,
      agentWorkspaceTab: 'files', agentDrawerNarrow: false,
      agentWorkspaceWidth: 400, agentWorkspacePreferredWidth: 400,
      agentWorkspaceMinWidth: minWorkspaceWidth, agentWorkspaceMaxWidth: maxWorkspaceWidth,
      agentWorkspaceResize: null,
      agentArtifactOpenSequence: 0,
      agentWorkspaceVersions: [], agentWorkspaceText: '', agentWorkspaceImage: '',
      agentWorkspaceCategories: fileCategories, agentWorkspaceMetadata: { description: '', category: 'other', pinned: false },
      agentWorkspaceError: '', agentWorkspaceVersionError: '', agentWorkspacePreviewHint: '',
      agentWorkspaceLoading: false, agentWorkspacePreviewBusy: false, agentWorkspaceMutationBusy: false,
      agentWorkspaceEpoch: 0, agentWorkspaceListSequence: 0, agentWorkspaceFileSequence: 0,
      agentRunsKnown: {}, agentCancelRequests: {},
      agentTaskQuery: '', agentTaskSearch: null, agentTaskSearchBusy: false,
      agentTaskSearchError: '', agentTaskSearchSequence: 0, agentTaskRevision: null }; },
    computed: {
      agentActiveTaskCount() { return this.currentAgentRuns.filter(run => !terminal(run.state)).length; },
      agentMessageTasks() {
        const groups = new Map();
        for (const run of this.currentAgentRuns) {
          const owner = this.agentMessageOwner(run);
          if (owner) groups.set(owner, [...(groups.get(owner) || []), run]);
        }
        return groups;
      },
      agentWorkspaceInventoryHint() {
        const hints = [];
        if (this.agentWorkspaceInventory.truncated === true) hints.push(this.t('agentWorkspaceListPartial'));
        if (this.agentWorkspaceInventory.over_byte_limit === true) hints.push(this.t('agentWorkspaceOverLimit'));
        return hints.join(' ');
      },
      agentWorkspaceMarkdown() {
        const file = this.agentWorkspaceSelected;
        if (!file || !this.agentWorkspaceText ||
            !(/\.(md|markdown)$/i.test(file.path) || /^text\/markdown(?:;|$)/i.test(file.mime || ''))) return '';
        try {
          // Keep workspace content on a separate renderer. No HTML or embedded resources.
          const parser = new root.marked.Marked({ breaks: true, gfm: true, renderer: {
            html() { return ''; },
            image({ tokens }) { return this.parser.parseInline(tokens); },
            link({ href, tokens }) {
              const label = this.parser.parseInline(tokens);
              if (!/^https?:\/\//i.test(href)) return label;
              let url;
              try { url = new URL(href); } catch (_) { return label; }
              if (url.username || url.password || url.origin === root.getBaseUrl?.() || url.origin === root.location?.origin) return label;
              const escaped = url.href.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
              return `<a href="${escaped}" target="_blank" rel="noopener noreferrer">${label}</a>`;
            },
          } });
          return parser.parse(this.agentWorkspaceText);
        } catch (_) {
          return ''; // The template falls back to escaped source text.
        }
      },
      agentOwnTaskActive() {
        return !this.currentGroupId && Object.values(this.agentRuns).some(run =>
          run.profile_id === this.currentSessionId && run.session_id === this.currentSessionId && !terminal(run.state));
      },
      agentChatReplyPending() {
        return Object.values(this.agentPendingChatReplies).includes(this.currentSessionId);
      },
      agentCanDetach() {
        const run = this.agentRuns[this.agentChatContext?.runKey];
        return !!(run && !terminal(run.state) && this.agentRunIsAttached(run));
      },
      agentWorkspaceLocked() {
        return this.agentWorkspaceMutationBusy || this.agentOwnTaskActive || !this.agentRunsKnown[this.currentSessionId] ||
          !!this.agentRunErrors[this.currentSessionId];
      },
      agentRemindersVisible() {
        return this.agentWorkspaceOpen && this.agentWorkspaceTab === 'reminders' && !!this.currentSessionId && !this.currentGroupId;
      },
      currentAgentRuns() {
        // Presentation only: retain every run in agentRuns for stop ownership/status.
        return this.currentGroupId ? [] : visibleRuns(this.agentRuns, this.currentSessionId, this.currentSessionId)
          .filter(run => ['reflection', 'proactive'].includes(run.origin) || (!terminal(run.state) && !this.agentRunIsAttached(run)) || run.state === 'failed' || (run.state === 'cancelled' && run.user_message_id != null) || run.artifacts?.length || run.parent_run_id || run.workspace_warning || run.error || (run.events || [])
            .some(event => ['tool_call', 'approval_requested', 'failed'].includes(event.type)));
      },
    },
    watch: {
      currentSessionId() { this.syncAgentView(true); this.agentViewChanged(); },
      currentGroupId() { this.syncAgentView(true); this.agentViewChanged(); },
    },
    mounted() {
      if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
        this._agentDrawerMedia = window.matchMedia('(max-width: 1000px)');
        this._agentDrawerMediaChanged = event => {
          this.agentDrawerNarrow = event.matches;
          if (event.matches) this.finishAgentWorkspaceResize();
          else this.syncAgentWorkspaceWidth();
          if (event.matches && this.agentWorkspaceOpen) this.$nextTick(() => this.$refs?.agentWorkspaceClose?.focus?.());
        };
        this.agentDrawerNarrow = this._agentDrawerMedia.matches;
        this._agentDrawerMedia.addEventListener('change', this._agentDrawerMediaChanged);
        this.loadAgentWorkspaceWidth();
        if (typeof ResizeObserver === 'function') {
          const app = document.getElementById('app');
          const sidebar = app?.querySelector(':scope > .sidebar');
          this._agentWorkspaceResizeObserver = new ResizeObserver(() => this.syncAgentWorkspaceWidth());
          if (app) this._agentWorkspaceResizeObserver.observe(app);
          if (sidebar) this._agentWorkspaceResizeObserver.observe(sidebar);
        }
      }
      this.syncAgentView(true);
      this.refreshAgentRuns();
      this.agentPollTimer = setInterval(() => { this.syncAgentView(); this.refreshAgentRuns(); }, 2500);
    },
    unmounted() {
      if (this._agentViewSync) this._agentViewSync.closed = true;
      clearInterval(this.agentPollTimer);
      this._agentDrawerMedia?.removeEventListener('change', this._agentDrawerMediaChanged);
      this._agentWorkspaceResizeObserver?.disconnect();
      this.closeAgentWorkspace({ restoreFocus: false });
    },
    methods: {
      loadAgentWorkspaceWidth() {
        try {
          const width = Number(localStorage.getItem(workspaceWidthKey));
          if (width >= minWorkspaceWidth && width <= maxWorkspaceWidth) this.agentWorkspacePreferredWidth = width;
        } catch (_) { /* Storage can be unavailable. */ }
        this.syncAgentWorkspaceWidth();
      },
      syncAgentWorkspaceWidth() {
        if (this.agentDrawerNarrow || typeof document === 'undefined') return;
        const app = document.getElementById('app');
        if (!app || !app.clientWidth) return;
        const sidebar = app.querySelector(':scope > .sidebar');
        const leftWidth = sidebar?.getBoundingClientRect().width || 0;
        this.agentWorkspaceMaxWidth = Math.max(minWorkspaceWidth, Math.min(maxWorkspaceWidth,
          Math.floor(app.clientWidth - leftWidth - minWorkspaceChatWidth)));
        this.agentWorkspaceWidth = Math.min(this.agentWorkspacePreferredWidth, this.agentWorkspaceMaxWidth);
      },
      setAgentWorkspaceWidth(width) {
        const next = Math.max(minWorkspaceWidth, Math.min(this.agentWorkspaceMaxWidth, Math.round(width)));
        if (next === this.agentWorkspaceWidth) return false;
        this.agentWorkspaceWidth = this.agentWorkspacePreferredWidth = next;
        return true;
      },
      rememberAgentWorkspaceWidth() {
        try { localStorage.setItem(workspaceWidthKey, String(this.agentWorkspacePreferredWidth)); }
        catch (_) { /* The new width still applies to the current window. */ }
      },
      startAgentWorkspaceResize(event) {
        if (event.button !== 0 || this.agentDrawerNarrow || this.agentWorkspaceResize) return;
        this.syncAgentWorkspaceWidth();
        event.preventDefault();
        event.currentTarget.setPointerCapture(event.pointerId);
        this.agentWorkspaceResize = { id: event.pointerId, x: event.clientX, width: this.agentWorkspaceWidth, changed: false };
      },
      moveAgentWorkspaceResize(event) {
        const drag = this.agentWorkspaceResize;
        if (drag?.id !== event.pointerId) return;
        if (this.setAgentWorkspaceWidth(drag.width + drag.x - event.clientX)) drag.changed = true;
      },
      finishAgentWorkspaceResize(event) {
        const drag = this.agentWorkspaceResize;
        if (!drag || (event && drag.id !== event.pointerId)) return;
        this.agentWorkspaceResize = null;
        if (drag.changed) this.rememberAgentWorkspaceWidth();
      },
      resizeAgentWorkspaceWithKey(event) {
        if (this.agentDrawerNarrow || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        this.syncAgentWorkspaceWidth();
        const width = { Home: minWorkspaceWidth, End: this.agentWorkspaceMaxWidth,
          ArrowLeft: this.agentWorkspaceWidth + 20, ArrowRight: this.agentWorkspaceWidth - 20 }[event.key];
        if (this.setAgentWorkspaceWidth(width)) this.rememberAgentWorkspaceWidth();
      },
      agentTerminal: terminal,
      agentRunKey: keyOf,
      async syncAgentView(force = false) {
        const state = this._agentViewSync || (this._agentViewSync = {
          desired: null, confirmed: null, busy: false, version: 0, closed: false,
        });
        state.desired = !!this.currentGroupId;
        // app.js restores groups asynchronously after loading the server profile.
        // Its group-exit/invalid-group paths clear this persisted selection.
        if (!state.desired) {
          try { state.desired = !!localStorage.getItem('currentGroupId'); } catch (_) { /* storage may be unavailable */ }
        }
        state.version++;
        if (state.closed || state.busy) return;
        if (force) state.confirmed = null;
        if (state.confirmed === state.desired) return;
        state.busy = true;
        try {
          // One POST at a time: an old response cannot overwrite a newer view.
          while (!state.closed && state.confirmed !== state.desired) {
            const groupChat = state.desired, version = state.version;
            // A failed response may still have changed the server's gate.
            state.confirmed = null;
            try {
              await this.agentFetch(API_PATHS.agentView(), { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ group_chat: groupChat }) });
              state.confirmed = groupChat;
            } catch (_) {
              // Retry only for a newer view/poll trigger; otherwise wait for one.
              if (state.version === version) break;
            }
          }
        } finally { state.busy = false; }
      },
      agentMessageOwner(run) {
        if (['reflection', 'proactive'].includes(run.origin) || this.currentGroupId || run.profile_id !== this.currentSessionId || run.session_id !== this.currentSessionId) return null;
        const id = run.run_id || run.id;
        const messages = this.messages.filter(message => message.session_id === run.profile_id);
        const assistant = (run.assistant_message_id != null && messages.find(message =>
          message.role === 'assistant' && message.id === run.assistant_message_id)) ||
          messages.filter(message => message.role === 'assistant' && message.agent_run_id === id).at(-1);
        if (assistant) return assistant;
        return messages.find(message => message.role === 'user' &&
          ((run.user_message_id != null && message.id === run.user_message_id) ||
           (message.agent_run_id && message.agent_run_id === id) ||
           (run.client_request_id && message.request_id === run.client_request_id))) || null;
      },
      messageAgentRuns(message) {
        if (this.currentGroupId || !message || message.session_id !== this.currentSessionId) return [];
        return this.agentMessageTasks.get(message) || [];
      },
      messageAgentArtifacts(message) {
        if (message?.role !== 'assistant') return [];
        const paths = new Set();
        for (const run of this.messageAgentRuns(message)) {
          if (!terminal(run.state) || !Array.isArray(run.artifacts)) continue;
          for (const artifact of run.artifacts) {
            const path = this.agentArtifactPath(artifact);
            if (path) paths.add(path);
          }
        }
        return [...paths];
      },
      agentViewChanged() {
        this.agentViewVersion++;
        this.clearAgentTaskSearch();
        this.agentTaskRevision = null;
        this.agentContinueRunId = '';
        this.closeAgentWorkspace({ restoreFocus: false });
        // A detached request may finish, but cannot alter the newly selected chat.
        if (this.agentChatContext && !this.agentChatIsVisible(this.agentChatContext)) {
          this.isStreaming = false;
          this.streamingText = '';
          this._chatAbortController = null;
        }
        this._aseStreaming = false;
        this._aseStreamBuf = '';
        this.timerList = [];
        if (this.currentSessionId && !this.currentGroupId) this.refreshTimers();
        this.refreshAgentRuns();
      },
      agentChatIsVisible(context) {
        // Vue wraps assigned objects in reactive proxies; raw object identity
        // is not a stable request identity across an async send.
        return !context?.detached && typeof context?.requestId === 'string' && context.requestId === this.agentChatContext?.requestId && context.viewVersion === this.agentViewVersion &&
          context.sessionId === this.currentSessionId && !this.currentGroupId;
      },
      agentRunIsAttached(run) {
        const context = this.agentChatContext;
        // A detached run needs its own Stop control, even before its first tool call.
        return !!(!['reflection', 'proactive'].includes(run.origin) && this.isStreaming && context && this.agentChatIsVisible(context) &&
          ((context.boundRunKey === keyOf(run) && context.boundRequestId === context.requestId) ||
            (typeof run.request_id === 'string' && run.request_id === context.requestId)) &&
          run.session_id === context.sessionId && run.profile_id === context.sessionId);
      },
      reconcileChatMessage(message, authoritative = false) {
        if (message.session_id !== this.currentSessionId || this.currentGroupId) return { message: null, added: false };
        const existing = this.messages.find(item => item.role === message.role && (
          (message.id != null && item.id === message.id) ||
          (message.request_id && item.request_id === message.request_id)));
        const patch = { ...message };
        if (patch.id == null) delete patch.id;
        for (const key of Object.keys(patch)) if (patch[key] === undefined) delete patch[key];
        if (existing) {
          // A later local prefix must never overwrite the saved server reply.
          if (authoritative || !existing._serverConfirmed) Object.assign(existing, patch);
          if (authoritative) existing._serverConfirmed = true;
          return { message: existing, added: false };
        }
        this.messages.push({ ...patch, _serverConfirmed: authoritative });
        return { message: this.messages[this.messages.length - 1], added: true };
      },
      preserveChatReply(context, { id, partial = true } = {}) {
        if (!this.agentChatIsVisible(context)) return null;
        const saved = context.serverReply;
        const reply = saved || { id, request_id: context.requestId, session_id: context.sessionId,
          role: 'assistant', content: this.streamingText, partial,
          agent_run_id: this.agentRuns[context.runKey]?.run_id || this.agentRuns[context.runKey]?.id,
          generation_status: this.agentRuns[context.runKey]?.state && terminal(this.agentRuns[context.runKey].state) ? this.agentRuns[context.runKey].state : (partial ? undefined : 'completed'), timestamp: this._now(),
          sender: this.sessions.find(s => s.id === context.sessionId)?.display_name || '' };
        const run = this.agentRuns[context.runKey];
        if (!reply.agent_run_id && run) reply.agent_run_id = run.run_id || run.id;
        if (!reply.generation_status && terminal(run?.state)) reply.generation_status = run.state;
        if (!reply.content?.trim()) return null;
        return this.reconcileChatMessage(reply, !!saved).message;
      },
      receiveAgentEvent(event, context) {
        // Never infer an event's ownership from the currently selected profile.
        if (context && (event.session_id !== context.sessionId || event.profile_id !== context.sessionId)) return;
        const updatedRuns = reduceEvent(this.agentRuns, event);
        const key = keyOf(event);
        if (updatedRuns !== this.agentRuns && this.agentRemindersVisible &&
            event.session_id === this.currentSessionId && event.profile_id === this.currentSessionId &&
            event.type === 'tool_result' && event.is_error === false) {
          const call = updatedRuns[key]?.events.find(item => item.type === 'tool_call' && item.call_id === event.call_id);
          if (timerAction(call?.tool_name) === 'schedule') this.refreshTimers();
        }
        this.agentRuns = updatedRuns;
        if (context && event.run_id && !['reflection', 'proactive'].includes(this.agentRuns[keyOf(event)]?.origin)) {
          context.runKey = keyOf(event);
          // Only a context from the owning HTTP stream has a client nonce.
          // Native event request_id may instead be the server's run UUID.
          if (typeof context.requestId === 'string') {
            context.boundRunKey = keyOf(event);
            context.boundRequestId = context.requestId;
            const run = this.agentRuns[context.runKey];
            if (run && (!run.client_request_id || run.client_request_id === context.requestId)) {
              this.updateAgentRun(context.runKey, { client_request_id: context.requestId });
            }
          }
        }
        this.syncAgentStatus(keyOf(event));
      },
      agentHasTimerEvents(run) {
        return (run?.events || []).some(event => ['tool_call', 'tool_result'].includes(event.type) && !!timerAction(event.tool_name));
      },
      agentRunHadToolError(run) {
        return (run?.events || []).some(event => event.type === 'tool_result' && event.is_error === true);
      },
      agentStateLabel(run) {
        if (run.state === 'completed' && this.agentRunHadToolError(run)) return this.t('agentCompletedWithToolErrors');
        const state = !terminal(run.state) && run.cancelling ? 'cancelling' : run.state;
        const key = 'agentState_' + state;
        return this.t(key) === key ? state : this.t(key);
      },
      agentRunTime(timestamp) {
        if (timestamp == null || timestamp === '') return '';
        const numeric = typeof timestamp === 'number' || /^\d+(\.\d+)?$/.test(timestamp);
        const value = numeric ? Number(timestamp) : Date.parse(timestamp) / 1000;
        if (!Number.isFinite(value) || !Number.isFinite(new Date(value > 1e12 ? value : value * 1000).getTime())) return '';
        return this.formatMsgTime(value);
      },
      agentToolLabel(name) {
        const action = timerAction(name);
        if (action) return this.t('agentTimerTool_' + action);
        const native = String(name || '').replace(/^mcp__shikigami__/, '');
        const key = 'agentTool_' + native;
        return this.t(key) === key ? (name || '') : this.t(key);
      },
      agentRunLabel(run) {
        if (run.origin === 'proactive') return this.t('agentProactiveTask') + ' · ' + this.agentStateLabel(run);
        if (run.origin === 'reflection') return this.t('agentReflectionTask') + ' · ' + this.agentStateLabel(run);
        const fallback = this.agentStateLabel(run);
        if (run.state !== 'completed' || this.agentRunHadToolError(run)) return fallback;
        const events = run.events || [];
        const callIndex = events.map(event => event.type).lastIndexOf('tool_call');
        if (callIndex < 0) return fallback;
        const call = events[callIndex], action = timerAction(call.tool_name);
        if (!action || !call.call_id) return fallback;
        // Only summarize the final tool call, backed by its latest matching result.
        const results = events.slice(callIndex + 1).filter(event => event.type === 'tool_result' && event.call_id === call.call_id);
        const result = results[results.length - 1];
        if (!result || result.is_error !== false) return fallback;
        if (action === 'create') {
          try {
            const args = typeof call.arguments === 'string' ? JSON.parse(call.arguments) : call.arguments;
            const seconds = Number(args?.seconds);
            if (Number.isFinite(seconds) && seconds > 0) {
              const minutes = seconds % 60 === 0;
              return this.t(minutes ? 'agentTimerCreatedMinutes' : 'agentTimerCreatedSeconds').replace('{count}', minutes ? seconds / 60 : seconds);
            }
          } catch (_) { /* A successful result still proves creation, but not a duration. */ }
        }
        return this.t('agentTimerDone_' + action);
      },
      agentEventLabel(event, run) {
        if (event.type === 'completed' && run) return this.agentStateLabel({ ...run, state: 'completed' });
        if (event.type === 'tool_call') return this.t('agentToolCall') + ': ' + this.agentToolLabel(event.tool_name);
        if (event.type === 'tool_result') return this.t(event.is_error === true ? 'agentToolError' : (event.is_error === false ? 'agentToolSuccess' : 'agentToolResult'));
        return this.agentStateLabel({ state: event.type === 'status' ? event.status : event.type });
      },
      agentEventText(event) {
        if (event.type === 'tool_call') return textOf(event.arguments);
        if (event.type === 'tool_result') return Array.isArray(event.content) ? event.content.filter(c => c.type === 'text').map(c => textOf(c.text)).join('\n') : textOf(event.content ?? event.result);
        if (event.type === 'completed' && event.reason?.kind === 'completed' && !event.error && !event.detail && !event.message) return '';
        return textOf(event.error || event.detail || event.reason || event.message);
      },
      updateAgentRun(key, patch) {
        const run = this.agentRuns[key];
        if (run) this.agentRuns = { ...this.agentRuns, [key]: { ...run, ...patch } };
        this.syncAgentStatus(key);
      },
      syncAgentStatus(key) {
        const context = this.agentChatContext;
        if (context && context.runKey === key && this.agentChatIsVisible(context) && this.agentRuns[key]) {
          const run = this.agentRuns[key];
          this.statusText = run.error ? `${this.agentStateLabel(run)}: ${run.error}` : this.agentStateLabel(run);
        }
      },
      async agentFetch(path, options) {
        const response = await fetch(getBaseUrl() + path, options);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
        return data;
      },
      async refreshAgentRuns() {
        if (this.agentPollBusy) return;
        const profiles = new Set(Object.values(this.agentRuns).filter(r => !terminal(r.state)).map(r => r.profile_id));
        if (this.currentSessionId && !this.currentGroupId) profiles.add(this.currentSessionId);
        if (!profiles.size) return;
        this.agentPollBusy = true;
        try {
          await Promise.all([...profiles].map(async profile => {
            const before = this.agentRuns;
            try {
              const data = await this.agentFetch(API_PATHS.agentRuns(profile));
              if (profile === this.currentSessionId && !this.currentGroupId && Number.isInteger(data.revision)) {
                if (this.agentTaskSearch?.revision < data.revision) this.clearAgentTaskSearch();
                this.agentTaskRevision = Math.max(this.agentTaskRevision ?? 0, data.revision);
              }
              if (!Array.isArray(data.runs)) throw new Error('Invalid run list response');
              const listedKeys = new Set();
              for (const snapshot of data.runs) {
                if (snapshot.profile_id !== profile || !snapshot.session_id) continue;
                const normalized = { ...snapshot, run_id: snapshot.run_id || snapshot.id };
                if (!normalized.run_id) continue;
                const key = keyOf(normalized);
                listedKeys.add(key);
                const unchanged = this.agentRuns[key] === before[key];
                for (const event of [...(snapshot.events || [])].sort((a, b) => a.sequence - b.sequence)) {
                  if (event.profile_id === profile && event.session_id === snapshot.session_id && event.run_id === normalized.run_id) this.receiveAgentEvent(event);
                }
                const current = this.agentRuns[key];
                if (!current) {
                  this.agentRuns = { ...this.agentRuns, [key]: { ...normalized, events: [], approval: null, sequence: -1 } };
                } else if (unchanged && !terminal(current.state)) {
                  this.updateAgentRun(key, { id: snapshot.id || current.id, state: snapshot.state,
                    ...(terminal(snapshot.state) ? { approval: null, cancelling: false, error: snapshot.state === 'failed' ? (snapshot.error || current.error || '') : '' } : {}) });
                }
              }
              // Missing entries are invalidated server records, but a snapshot
              // cannot delete runs added or updated while its request was in flight.
              const missing = Object.keys(before).filter(key => before[key].profile_id === profile &&
                !listedKeys.has(key) && this.agentRuns[key] === before[key]);
              if (missing.length) {
                const next = { ...this.agentRuns };
                for (const key of missing) delete next[key];
                this.agentRuns = next;
              }
              for (const snapshot of data.runs) {
                if (snapshot.profile_id !== profile || !snapshot.session_id) continue;
                const key = keyOf(snapshot);
                if (!this.agentRuns[key]) continue;
                const metadata = {};
                for (const field of ['origin', 'user_message_id', 'assistant_message_id', 'parent_run_id', 'artifacts', 'request_id', 'workspace_warning', 'error']) {
                  if (snapshot[field] != null) metadata[field] = snapshot[field];
                }
                this.updateAgentRun(key, metadata);
              }
              this.agentRunsKnown = { ...this.agentRunsKnown, [profile]: true };
              this.agentRunErrors = { ...this.agentRunErrors, [profile]: '' };
            } catch (error) {
              this.agentRunErrors = { ...this.agentRunErrors, [profile]: error.message };
            }
          }));
        } finally { this.agentPollBusy = false; }
      },
      async cancelAgentRun(run, controller, { abortAttached = true } = {}) {
        const key = keyOf(run);
        if (abortAttached && !controller && this.agentChatContext?.runKey === key && this.agentChatIsVisible(this.agentChatContext)) controller = this._chatAbortController;
        if (terminal(run.state)) { if (controller) controller.abort(); return true; }
        let pending = this.agentCancelRequests[key];
        if (!pending && run.cancelBusy) return false;
        if (!pending && run.cancelling) { if (controller) controller.abort(); return true; }
        if (!pending) {
          this.updateAgentRun(key, { cancelling: true, cancelBusy: true, error: '' });
          pending = (async () => {
            try {
              const data = await this.agentFetch(API_PATHS.agentRunCancel(run.id || run.run_id), {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ profile_id: run.profile_id }),
              });
              if (!data.accepted && !terminal(data.state)) throw new Error(this.t('agentCancelRejected'));
              const current = this.agentRuns[key];
              if (current && !terminal(current.state) && terminal(data.state)) this.updateAgentRun(key, { state: data.state, approval: null, cancelling: false });
              this.refreshAgentRuns();
              return true;
            } catch (error) {
              if (terminal(this.agentRuns[key]?.state)) return true;
              if (this.agentRuns[key]) this.updateAgentRun(key, { cancelling: false, error: error.message });
              return false;
            } finally { this.updateAgentRun(key, { cancelBusy: false }); }
          })();
          this.agentCancelRequests[key] = pending;
        }
        // Share the control acknowledgement with task-specific and global Stop.
        // Accepted means cancellation was requested, not that the run has ended.
        const accepted = await pending;
        if (this.agentCancelRequests[key] === pending) delete this.agentCancelRequests[key];
        if (accepted && controller) controller.abort();
        return accepted;
      },
      async answerAgentApproval(run, outcome) {
        if (!run.approval || run.approvalBusy || terminal(run.state) || run.cancelling) return;
        const key = keyOf(run), approvalId = run.approval.approval_id;
        this.updateAgentRun(key, { approvalBusy: true, error: '' });
        try {
          const data = await this.agentFetch(API_PATHS.agentRunApproval(run.id || run.run_id), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile_id: run.profile_id, approval_id: approvalId, outcome }),
          });
          if (!data.accepted) throw new Error(this.t('agentApprovalRejected'));
          const current = this.agentRuns[key];
          if (current && !terminal(current.state) && current.approval?.approval_id === approvalId) this.updateAgentRun(key, { approval: null, state: 'running' });
          this.refreshAgentRuns();
        } catch (error) { this.updateAgentRun(key, { error: error.message }); }
        finally { this.updateAgentRun(key, { approvalBusy: false }); }
      },
      continueAgentMessage(message) {
        if (!message.agent_run_id || !terminal(message.generation_status) || message.session_id !== this.currentSessionId) return;
        this.selectAgentContinuation({ id: message.agent_run_id, profile_id: message.session_id,
          session_id: message.session_id, state: message.generation_status });
      },
      clearAgentTaskSearch() {
        this.agentTaskSearchSequence++;
        this.agentTaskQuery = '';
        this.agentTaskSearch = null;
        this.agentTaskSearchBusy = false;
        this.agentTaskSearchError = '';
      },
      async searchAgentTasks() {
        const profile = this.currentSessionId;
        const query = this.agentTaskQuery.trim();
        if (!query) { this.clearAgentTaskSearch(); return; }
        if (!profile || this.currentGroupId) return;
        const sequence = ++this.agentTaskSearchSequence;
        const current = () => sequence === this.agentTaskSearchSequence &&
          profile === this.currentSessionId && !this.currentGroupId;
        this.agentTaskSearch = null;
        this.agentTaskSearchError = '';
        this.agentTaskSearchBusy = true;
        try {
          const result = await this.agentFetch(API_PATHS.agentRuns(profile, query));
          if (current() && !(result.revision < this.agentTaskRevision)) this.agentTaskSearch = result;
        } catch (error) {
          if (current()) this.agentTaskSearchError = error.message;
        } finally {
          if (current()) this.agentTaskSearchBusy = false;
        }
      },
      agentCanContinueRun(run) {
        const stored = this.agentRuns[keyOf(run)];
        // Until polling confirms the origin, a terminal event might be autonomous.
        return (stored?.origin || run.origin) === 'user' && run.origin !== 'reflection' && !this.currentGroupId &&
          !this.isStreaming && !this.agentOwnTaskActive && !this.agentChatReplyPending && terminal(run.state) &&
          run.profile_id === this.currentSessionId && run.session_id === this.currentSessionId;
      },
      selectAgentContinuation(run) {
        if (!this.agentCanContinueRun(run)) return;
        this.agentContinueRunId = run.run_id || run.id || '';
        if (this.agentWorkspaceOpen) this.closeAgentWorkspace({ restoreFocus: false });
        this.$nextTick(() => this.$refs?.chatTextarea?.focus());
      },
      detachAgentChat() {
        if (!this.agentCanDetach) return;
        if (this.agentWorkspaceOpen && this.agentDrawerNarrow) this.closeAgentWorkspace({ restoreFocus: false });
        const context = this.agentChatContext;
        this.preserveChatReply(context);
        context.detachedText = this.streamingText;
        context.detached = true;
        // Keep the original fetch/controller alive in sendMessage's closure.
        this._chatAbortController = null;
        this.isStreaming = false;
        this.streamingText = '';
        this.statusText = this.t('agentBackgroundHint');
        this.$nextTick(() => this.$refs?.chatTextarea?.focus());
      },
      receiveDetachedChatFrame(context, data) {
        if (!context.detached || context.replyDone) return;
        if (data.token) context.detachedText = (context.detachedText || '') + data.token;
        if (!data.done && !data.error) return;
        context.replyDone = true;
        if (context.sessionId !== this.currentSessionId || this.currentGroupId || context.viewVersion !== this.agentViewVersion) return;
        if (data.discard_partial) {
          this.messages = this.messages.filter(message => !(message.role === 'assistant' && message.partial && message.request_id === context.requestId));
          return;
        }
        const run = this.agentRuns[context.runKey];
        const state = data.generation_status || (data.error ? 'failed' :
          (data.cancelled || run?.cancelling ? 'cancelled' : (terminal(run?.state) ? run.state : 'completed')));
        const reply = context.serverReply || { id: data.message_id, request_id: context.requestId, session_id: context.sessionId,
          role: 'assistant', content: context.detachedText || '', timestamp: this._now(),
          agent_run_id: data.agent_run_id || run?.run_id || run?.id, generation_status: state,
          partial: !!(data.partial || state === 'cancelled' || state === 'failed'),
          sender: this.sessions.find(s => s.id === context.sessionId)?.display_name || '' };
        if (!reply.agent_run_id && run) reply.agent_run_id = run.run_id || run.id;
        if (!reply.generation_status) reply.generation_status = state;
        if (reply.content?.trim()) this.reconcileChatMessage(reply, !!context.serverReply);
      },
      agentWorkspaceFileHint(file) {
        if (file?.oversized === true) return this.t('agentWorkspaceOversized');
        if (file?.previewable === false) return this.t('agentWorkspacePreviewUnavailable');
        return '';
      },
      agentWorkspaceVersionHint(version) {
        const count = version?.snapshot_omitted_count;
        const hasCount = Number.isSafeInteger(count) && count > 0;
        if (version?.snapshot_complete !== false && version?.snapshot_inventory_complete !== false && !hasCount) return '';
        return this.t(hasCount ? 'agentWorkspaceSnapshotPartialCount' : 'agentWorkspaceSnapshotPartial').replace('{count}', count);
      },
      agentWorkspaceContext() {
        return { profile: this.currentSessionId, epoch: this.agentWorkspaceEpoch };
      },
      agentWorkspaceIsCurrent(context) {
        return this.agentWorkspaceOpen && !this.currentGroupId && context.profile === this.currentSessionId &&
          context.epoch === this.agentWorkspaceEpoch;
      },
      clearAgentWorkspacePreview() {
        if (this.agentWorkspaceImage) URL.revokeObjectURL(this.agentWorkspaceImage);
        this.agentWorkspaceImage = '';
        this.agentWorkspaceText = '';
        this.agentWorkspacePreviewHint = '';
        this.agentWorkspaceVersions = [];
        this.agentWorkspaceVersionError = '';
        this.agentWorkspaceMetadata = { description: '', category: 'other', pinned: false };
      },
      closeAgentWorkspace(options = {}) {
        this.finishAgentWorkspaceResize();
        const wasOpen = this.agentWorkspaceOpen;
        this.agentArtifactOpenSequence++;
        this.agentWorkspaceEpoch++;
        this.agentWorkspaceOpen = false;
        this.agentWorkspaceFiles = [];
        this.agentWorkspaceInventory = {};
        this.agentWorkspaceSelected = null;
        this.agentWorkspaceError = '';
        this.agentWorkspaceLoading = false;
        this.agentWorkspacePreviewBusy = false;
        this.clearAgentWorkspacePreview();
        if (wasOpen && options.restoreFocus !== false) {
          const opener = this._agentDrawerOpener;
          this.$nextTick(() => {
            if (opener?.isConnected && typeof opener.focus === 'function') opener.focus();
            else this.$refs?.agentWorkspaceOpener?.focus?.();
          });
        }
        this._agentDrawerOpener = null;
      },
      toggleAgentWorkspace() {
        if (this.agentWorkspaceOpen) { this.closeAgentWorkspace(); return; }
        return this.openAgentWorkspace('files');
      },
      openAgentWorkspace(tab = 'files') {
        if (!this.currentSessionId || this.currentGroupId) return;
        this.$refs?.sidebarGroups?.hidePopover?.();
        if (!this.agentWorkspaceOpen) this._agentDrawerOpener = typeof document === 'undefined' ? null : document.activeElement;
        this.agentWorkspaceTab = workspaceTabs.includes(tab) ? tab : 'files';
        this.agentWorkspaceOpen = true;
        this.refreshAgentRuns();
        this.$nextTick(() => this.$refs?.agentWorkspaceClose?.focus?.());
        if (this.agentWorkspaceTab === 'plans') return this.refreshTodos();
        if (this.agentWorkspaceTab === 'reminders') return this.refreshTimers();
        return this.refreshAgentWorkspace();
      },
      selectAgentWorkspaceTab(tab) {
        if (!workspaceTabs.includes(tab) || this.currentGroupId || !this.currentSessionId) return;
        if (!this.agentWorkspaceOpen) return this.openAgentWorkspace(tab);
        if (tab === 'files' && this.agentWorkspaceTab === 'files' && this.agentWorkspaceLoading) return;
        if (tab !== 'files') this.agentArtifactOpenSequence++;
        this.agentWorkspaceTab = tab;
        if (tab === 'plans') return this.refreshTodos();
        if (tab === 'reminders') return this.refreshTimers();
        if (tab === 'files') return this.refreshAgentWorkspace();
      },
      navigateAgentWorkspaceTabs(event) {
        if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key) || this.currentGroupId || !this.agentWorkspaceOpen) return;
        event.preventDefault();
        const index = event.key === 'Home' ? 0 : event.key === 'End' ? workspaceTabs.length - 1 :
          (workspaceTabs.indexOf(this.agentWorkspaceTab) + (event.key === 'ArrowRight' ? 1 : -1) + workspaceTabs.length) % workspaceTabs.length;
        this.selectAgentWorkspaceTab(workspaceTabs[index]);
        event.currentTarget.querySelectorAll('[role="tab"]')[index]?.focus();
      },
      trapAgentWorkspaceFocus(event) {
        if (!this.agentWorkspaceOpen || !this.agentDrawerNarrow || event.key !== 'Tab') return;
        const drawer = this.$refs?.agentWorkspaceDrawer;
        if (!drawer) return;
        const controls = [...drawer.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex]:not([tabindex="-1"])')]
          .filter(element => {
            if (!element.getClientRects().length || element.tabIndex < 0) return false;
            // Closed details can retain layout boxes in Chromium. Their hidden
            // restore buttons must not become a keyboard-wrap destination.
            for (let ancestor = element.parentElement; ancestor && ancestor !== drawer; ancestor = ancestor.parentElement) {
              if (ancestor.tagName === 'DETAILS' && !ancestor.open &&
                  !ancestor.querySelector(':scope > summary')?.contains(element)) return false;
            }
            return true;
          });
        const first = controls[0], last = controls.at(-1);
        if (!first) { event.preventDefault(); drawer.focus(); return; }
        const active = document.activeElement;
        if (event.shiftKey && (active === first || !drawer.contains(active))) {
          event.preventDefault(); last.focus();
        } else if (!event.shiftKey && (active === last || !drawer.contains(active))) {
          event.preventDefault(); first.focus();
        }
      },
      agentArtifactPath(artifact) {
        const value = typeof artifact === 'string' ? artifact : artifact?.path;
        if (typeof value !== 'string' || !value || /[\\:\x00-\x1f]/.test(value) ||
            value.split('/').some(part => !part || part === '.' || part === '..')) return '';
        return value;
      },
      async openAgentArtifact(artifact) {
        const path = this.agentArtifactPath(artifact);
        if (!path || !this.currentSessionId || this.currentGroupId) return;
        const sequence = ++this.agentArtifactOpenSequence;
        const profile = this.currentSessionId;
        const opener = typeof document === 'undefined' ? null : document.activeElement;
        if (this.agentWorkspaceSelected) this.clearAgentWorkspaceSelection();
        const loading = this.openAgentWorkspace('files');
        const context = this.agentWorkspaceContext();
        await loading;
        if (sequence !== this.agentArtifactOpenSequence || profile !== this.currentSessionId ||
            this.agentWorkspaceTab !== 'files' || !this.agentWorkspaceIsCurrent(context)) return;
        const file = this.agentWorkspaceFiles.find(file => file.path === path);
        if (file && !this.agentWorkspaceError) {
          const active = typeof document === 'undefined' ? null : document.activeElement;
          const focus = active === opener || active === this.$refs?.agentWorkspaceClose;
          await this.selectAgentWorkspaceFile(file, { invalidateArtifact: false, focus });
        } else {
          this.clearAgentWorkspaceSelection();
          if (!this.agentWorkspaceError) this.agentWorkspaceError = this.t('agentWorkspaceFileGone');
        }
      },
      clearAgentWorkspaceSelection() {
        this.agentWorkspaceFileSequence++;
        this.agentWorkspaceSelected = null;
        this.agentWorkspacePreviewBusy = false;
        this.clearAgentWorkspacePreview();
      },
      returnToAgentWorkspaceFiles() {
        this.agentArtifactOpenSequence++;
        this.clearAgentWorkspaceSelection();
        this.agentWorkspaceError = '';
        const context = this.agentWorkspaceContext();
        this.$nextTick(() => {
          if (this.agentWorkspaceIsCurrent(context) && this.agentWorkspaceTab === 'files' && !this.agentWorkspaceSelected) {
            this.$refs?.agentWorkspaceFileList?.focus?.();
          }
        });
      },
      async refreshAgentWorkspace() {
        const context = this.agentWorkspaceContext(), sequence = ++this.agentWorkspaceListSequence;
        if (!this.agentWorkspaceIsCurrent(context)) return;
        this.agentWorkspaceLoading = true;
        this.agentWorkspaceError = '';
        try {
          const data = await this.agentFetch(API_PATHS.agentWorkspaceFiles(context.profile));
          if (!this.agentWorkspaceIsCurrent(context) || sequence !== this.agentWorkspaceListSequence) return;
          if (!Array.isArray(data.files)) throw new Error(this.t('agentWorkspaceInvalid'));
          this.agentWorkspaceFiles = data.files.filter(file => this.agentArtifactPath(file?.path));
          this.agentWorkspaceInventory = { truncated: data.truncated === true, over_byte_limit: data.over_byte_limit === true };
          if (this.agentWorkspaceSelected) {
            const selected = this.agentWorkspaceSelected;
            const refreshed = this.agentWorkspaceFiles.find(file => file.path === selected.path);
            if (!refreshed) {
              this.clearAgentWorkspaceSelection();
              this.agentWorkspaceError = this.t('agentWorkspaceFileGone');
            } else if (['oversized', 'previewable', 'size', 'modified', 'mime'].some(key => refreshed[key] !== selected[key])) {
              // A smaller replacement must clear the old read-limit state.
              await this.selectAgentWorkspaceFile(refreshed, { invalidateArtifact: false });
            } else {
              const previous = this.agentWorkspaceFileMetadata(selected);
              const pristine = Object.keys(previous).every(key => this.agentWorkspaceMetadata[key] === previous[key]);
              this.agentWorkspaceSelected = refreshed;
              if (pristine && !this.agentWorkspaceMutationBusy) this.agentWorkspaceMetadata = this.agentWorkspaceFileMetadata(refreshed);
            }
          }
        } catch (error) {
          if (this.agentWorkspaceIsCurrent(context) && sequence === this.agentWorkspaceListSequence) this.agentWorkspaceError = error.message;
        } finally {
          if (this.agentWorkspaceIsCurrent(context) && sequence === this.agentWorkspaceListSequence) this.agentWorkspaceLoading = false;
        }
      },
      async agentWorkspaceResponse(path, options) {
        // Same host as the rest of the app; never use a URL supplied by file metadata.
        const response = await fetch(getBaseUrl() + path, options);
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(textOf(data.detail || data.error) || `HTTP ${response.status}`);
        }
        return response;
      },
      async selectAgentWorkspaceFile(file, { invalidateArtifact = true, focus = false } = {}) {
        if (!this.agentArtifactPath(file?.path)) return;
        if (invalidateArtifact) this.agentArtifactOpenSequence++;
        const context = this.agentWorkspaceContext(), sequence = ++this.agentWorkspaceFileSequence;
        if (!this.agentWorkspaceIsCurrent(context)) return;
        const current = () => this.agentWorkspaceIsCurrent(context) && sequence === this.agentWorkspaceFileSequence;
        this.clearAgentWorkspacePreview();
        this.agentWorkspaceSelected = file;
        this.agentWorkspaceMetadata = this.agentWorkspaceFileMetadata(file);
        this.agentWorkspacePreviewBusy = true;
        this.agentWorkspaceVersions = null;
        this.agentWorkspaceError = '';
        if (focus) {
          const active = typeof document === 'undefined' ? null : document.activeElement;
          this.$nextTick(() => {
            const focusStayed = typeof document === 'undefined' ||
              document.activeElement === active || (active && !active.isConnected && document.activeElement === document.body);
            if (focusStayed && current() && this.agentWorkspaceTab === 'files') this.$refs?.agentWorkspaceBack?.focus?.();
          });
        }
        const versions = (async () => {
          try {
            const data = await this.agentFetch(API_PATHS.agentWorkspaceVersions(context.profile, file.path));
            if (!Array.isArray(data.versions)) throw new Error(this.t('agentWorkspaceInvalid'));
            if (current()) this.agentWorkspaceVersions = data.versions.filter(v => typeof v?.id === 'string');
          } catch (error) {
            if (current()) {
              this.agentWorkspaceVersions = [];
              this.agentWorkspaceVersionError = error.message;
            }
          }
        })();
        try {
          const unavailable = this.agentWorkspaceFileHint(file);
          if (unavailable) { this.agentWorkspacePreviewHint = unavailable; return; }
          const limit = 1024 * 1024;
          if (Number(file.size) > limit) { this.agentWorkspacePreviewHint = this.t('agentWorkspaceTooLarge'); return; }
          const response = await this.agentWorkspaceResponse(API_PATHS.agentWorkspaceFile(context.profile, file.path));
          const blob = await response.blob();
          if (!current()) return;
          if (blob.size > limit) { this.agentWorkspacePreviewHint = this.t('agentWorkspaceTooLarge'); return; }
          const mime = (blob.type || file.mime || '').split(';')[0].toLowerCase();
          if (/^image\/(png|jpeg|gif|webp|avif|bmp)$/.test(mime)) {
            this.agentWorkspaceImage = URL.createObjectURL(blob);
          } else if (/^(text\/|application\/(json|xml|javascript)|image\/svg\+xml)/.test(mime) ||
              /\.(txt|md|markdown|csv|tsv|json|jsonl|py|js|ts|jsx|tsx|css|html|xml|svg|yaml|yml|toml|sh|sql|log)$/i.test(file.path)) {
            const text = await blob.text();
            if (current()) this.agentWorkspaceText = text;
          } else this.agentWorkspacePreviewHint = this.t('agentWorkspaceDownloadOnly');
        } catch (error) { if (current()) this.agentWorkspaceError = error.message; }
        finally {
          if (current()) this.agentWorkspacePreviewBusy = false;
          await versions;
        }
      },
      async downloadAgentWorkspaceFile() {
        const file = this.agentWorkspaceSelected, context = this.agentWorkspaceContext();
        const sequence = this.agentWorkspaceFileSequence;
        const current = () => this.agentWorkspaceIsCurrent(context) && sequence === this.agentWorkspaceFileSequence;
        if (!this.agentArtifactPath(file?.path) || !current()) return;
        if (file.oversized === true) { this.agentWorkspacePreviewHint = this.agentWorkspaceFileHint(file); return; }
        try {
          const response = await this.agentWorkspaceResponse(API_PATHS.agentWorkspaceFile(context.profile, file.path, true));
          const blob = await response.blob();
          if (!current()) return;
          const url = URL.createObjectURL(blob), link = document.createElement('a');
          link.href = url;
          link.download = file.path.split(/[\\/]/).pop();
          document.body.appendChild(link); link.click(); link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) { if (current()) this.agentWorkspaceError = error.message; }
      },
      agentWorkspaceFileMetadata(file) {
        return { description: typeof file?.description === 'string' ? file.description : '',
          category: fileCategories.includes(file?.category) ? file.category : 'other', pinned: file?.pinned === true };
      },
      saveAgentWorkspaceMetadata() {
        const value = { ...this.agentWorkspaceMetadata };
        if (this.agentWorkspaceLocked || !this.agentWorkspaceSelected) return;
        if (typeof value.description !== 'string' || [...value.description].length > 200 ||
            !fileCategories.includes(value.category) || typeof value.pinned !== 'boolean') {
          this.agentWorkspaceError = this.t('agentWorkspaceMetadataInvalid'); return;
        }
        return this.mutateAgentWorkspace('metadata', value);
      },
      async mutateAgentWorkspace(kind, value) {
        const context = this.agentWorkspaceContext();
        if (this.agentWorkspaceMutationBusy || !this.agentWorkspaceIsCurrent(context)) return;
        const path = this.agentWorkspaceSelected?.path;
        const fileSequence = this.agentWorkspaceFileSequence;
        const roleContext = kind === 'metadata' ? this.workspaceProfileContext() : null;
        const selectionIsCurrent = () => this.agentWorkspaceSelected?.path === path && this.agentWorkspaceFileSequence === fileSequence;
        const current = () => this.agentWorkspaceIsCurrent(context) && (kind === 'upload' || selectionIsCurrent()) &&
          (kind !== 'metadata' || this.workspaceProfileIsCurrent(roleContext));
        this.agentWorkspaceMutationBusy = true;
        this.agentWorkspaceError = '';
        try {
          // Fresh preflight supplements the UI lock; the backend must enforce the lock atomically.
          const data = await this.agentFetch(API_PATHS.agentRuns(context.profile));
          if (!current()) return;
          if (!Array.isArray(data.runs)) throw new Error(this.t('agentWorkspaceInvalid'));
          if (data.runs.some(run => run.profile_id === context.profile && !terminal(run.state))) throw new Error(this.t('agentWorkspaceLocked'));
          if (kind === 'metadata') {
            if (!this.agentArtifactPath(path)) return;
            const result = await this.agentFetch(API_PATHS.agentWorkspaceMetadata(), { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ profile_id: context.profile, path, ...value }) });
            if (!current()) return;
            const file = result.file;
            if (result.ok !== true || file?.path !== path || typeof file.description !== 'string' ||
                [...file.description].length > 200 || !fileCategories.includes(file.category) || typeof file.pinned !== 'boolean') {
              throw new Error(this.t('agentWorkspaceInvalid'));
            }
            const metadata = this.agentWorkspaceFileMetadata(file);
            this.agentWorkspaceFiles = this.agentWorkspaceFiles.map(row => row.path === path ? { ...row, ...metadata } : row);
            this.agentWorkspaceSelected = { ...this.agentWorkspaceSelected, ...metadata };
            this.agentWorkspaceMetadata = metadata;
            return;
          } else if (kind === 'upload') {
            const body = new FormData(); body.append('profile_id', context.profile); body.append('file', value);
            await this.agentWorkspaceResponse(API_PATHS.agentWorkspaceUpload(), { method: 'POST', body });
          } else {
            if (!this.agentArtifactPath(path) || !value?.id) return;
            await this.agentFetch(API_PATHS.agentWorkspaceRestore(), { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ profile_id: context.profile, path, version_id: value.id }) });
          }
          if (!this.agentWorkspaceIsCurrent(context)) return;
          await this.refreshAgentWorkspace();
          // Do not reopen a file the user changed while the operation was in flight.
          if (kind === 'restore' && current()) {
            await this.selectAgentWorkspaceFile(this.agentWorkspaceFiles.find(file => file.path === path) || this.agentWorkspaceSelected);
          }
        } catch (error) { if (current()) this.agentWorkspaceError = error.message; }
        finally { this.agentWorkspaceMutationBusy = false; }
      },
      async uploadAgentWorkspaceFile(event) {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (file) await this.mutateAgentWorkspace('upload', file);
      },
      restoreAgentWorkspaceVersion(version) { return this.mutateAgentWorkspace('restore', version); },
      openAgentTimers() {
        if (!this.currentSessionId || this.currentGroupId) return;
        if (!this.agentWorkspaceOpen) return this.openAgentWorkspace('reminders');
        const loading = this.selectAgentWorkspaceTab('reminders');
        this.$nextTick(() => this.$refs?.agentWorkspaceRemindersTab?.focus?.());
        return loading;
      },
    },
  };
  root.AgentUIMixin = mixin;
  if (typeof module !== 'undefined' && module.exports) module.exports = { terminal, keyOf, visibleRuns, reduceEvent, mixin };
})(typeof window === 'undefined' ? globalThis : window);
