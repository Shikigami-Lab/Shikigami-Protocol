// Shikigami Protocol — Frontend App (Vue 3 Options API, no build step)
// P2: SSE streaming chat + WebSocket TTS audio
// P3: Electron detection + screenshot IPC
// P5: History loading + SSE broadcast sync (multi-client / LAN)

// 每次请求时按当前页 location 计算，避免缓存或加载顺序导致 HTTPS 页仍用 http/ws（内网穿透必用）
function getBaseUrl() {
  const o = window.location.origin;
  const p = window.location.protocol || 'http:';
  const h = window.location.hostname || '127.0.0.1';
  const port = window.location.port || '7788';
  if (o && o !== 'null' && p !== 'file:') return o;
  return (p === 'file:' ? 'http:' : p) + '//' + h + ':' + port;
}
function getWsUrl() {
  const p = window.location.protocol || 'http:';
  const h = window.location.hostname || '127.0.0.1';
  const port = window.location.port || '7788';
  // 页面为 HTTPS 或通过隧道域名访问时必须用 wss，否则浏览器会拦截
  const pageIsHttps = (p === 'https:') || (typeof window.location.href === 'string' && window.location.href.indexOf('https://') === 0);
  const isTunnelHost = h && !/^localhost$|^127\.0\.0\.1$|^\[::1\]$/.test(h) && (/\.(cpolar|ngrok|natapp|frp|sakura|loophole)\./i.test(h) || /\.[a-z]{2,}$/.test(h));
  const isSecure = pageIsHttps || isTunnelHost;
  const scheme = isSecure ? 'wss:' : 'ws:';
  const portPart = (isSecure && !window.location.port) ? '' : (':' + (window.location.port || port));
  return scheme + '//' + h + portPart;
}
window.getBaseUrl = getBaseUrl;
window.getWsUrl = getWsUrl;

// Detect Electron runtime (preload.js sets window.electronAPI)
const IS_ELECTRON = !!(window.electronAPI && window.electronAPI.isElectron);

/** 首次访问且无服务端/本地已保存语言时：浏览器任意语言标签以 zh 开头 → 中文界面，否则英文。 */
function inferLocaleFromNavigator() {
  try {
    const list = (typeof navigator !== 'undefined' && navigator.languages && navigator.languages.length)
      ? navigator.languages.slice()
      : [];
    if (typeof navigator !== 'undefined' && navigator.language) {
      list.push(navigator.language);
    }
    for (const tag of list) {
      const low = String(tag).toLowerCase();
      if (low.startsWith('zh')) return 'zh';
    }
    return 'en';
  } catch (_) {
    return 'zh';
  }
}

// Unique ID for this browser tab — used to suppress duplicate messages from our own sends
const CLIENT_ID = ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
  (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));

// Configure marked
marked.setOptions({ breaks: true, gfm: true });

// ── UI debug logger ───────────────────────────────────────────────────────────
// Mirrors console output to data/logs/ui_debug.log via POST /debug/ui_log.
// Fire-and-forget; never throws; never blocks the caller.
function uiLog(prefix, msg, data) {
  console.debug(`[${prefix}]`, msg, ...(data !== undefined ? [data] : []));
  fetch(getBaseUrl() + API_PATHS.debugUiLog(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prefix,
      level: 'debug',
      msg: String(msg),
      data: data !== undefined ? { extra: String(data) } : {},
    }),
  }).catch(() => {});
}
function uiWarn(prefix, msg, data) {
  console.warn(`[${prefix}]`, msg, ...(data !== undefined ? [data] : []));
  fetch(getBaseUrl() + API_PATHS.debugUiLog(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prefix,
      level: 'warn',
      msg: String(msg),
      data: data !== undefined ? { extra: String(data) } : {},
    }),
  }).catch(() => {});
}

const App = {
  mixins: [SettingsMixin],
  data() {
    return {
      // Chat state
      messages: [],
      inputText: '',
      isStreaming: false,
      streamingText: '',

      // Session state
      sessions: [],
      sessionDragging: false,
      sessionDragOverId: null,
      sessionDragActiveId: null,   // 触屏拖拽：当前被拖动的 session id
      supportsHtml5Drag: true,     // 是否启用 draggable（移动端通常不可靠）
      currentSessionId: null,
      currentSessionName: '...',
      currentSessionAvatar: '',
      outOfSyncWithServer: false,  // 本端显示的人物与服务器当前人物不一致时为 true

      // 群聊（步骤 4b）
      groups: [],
      pinnedGroupIds: [],  // 置顶群组 id 列表，顺序即显示顺序，存 localStorage
      currentGroupId: null,
      currentGroup: null,
      showCreateGroupModal: false,
      createGroupName: '',
      createGroupSelectedIds: [],
      createGroupProfiles: [],
      groupChatStreamingSender: '',  // 当前正在流式回复的 AI 名字
      _groupChatAbortController: null,  // 群聊流式请求的中断控制器
      _chatAbortController: null,       // 单聊流式请求的中断控制器
      showGroupSettingsModal: false,
      confirmingDeleteGroup: false,  // 内联二次确认，与设置页 confirm-bar 风格一致
      groupSettingsName: '',
      groupSettingsParticipants: [],
      groupSettingsInviteProfiles: [],
      groupSettingsMaxReplies: 0,  // 0 = 不限制（步骤6）
      groupSettingsOrchestrator: 'random',  // random | fixed
      groupSettingsSystemHint: '',
      groupChatSenderFilter: '',   // 主聊天视图：按 sender 筛选，空=全部
      groupChatSearchQuery: '',    // 主聊天视图：搜索群内消息
      groupSearchResults: [],      // 主聊天视图：搜索结果（可选）
      // 群组设置弹窗内「记录编辑」独立状态（仅影响弹窗内列表，不影响主视图）
      groupSettingsRecordFilter: '',
      groupSettingsRecordSearchQuery: '',
      groupSettingsRecordSearchResults: [],
      groupSettingsRecordList: [],       // 最近记录（打开设置自动加载 + 加载更多）
      groupSettingsRecordTotal: 0,
      groupSettingsRecordOffset: 0,
      groupSettingsRecordLimit: 50,
      groupSettingsRecordLoading: false,

      // Locale（首访实际默认值在 mounted 里按 navigator 推断，此处仅作占位）
      locale: localStorage.getItem('locale') || 'zh',

      // UI state
      statusText: '',
      ttsEnabled: false,
      isElectron: IS_ELECTRON,
      sidebarOpen: window.innerWidth >= 769,
      appVersion: '…',   // 从 GET /version 拉取，设置页显示

      // TTS WebSocket
      _ttsWs: null,
      _audioQueue: [],
      isPlayingAudio: false,
      _audioCtx: null,
      _audioUnlockAttached: false,
      _audioUnlockRef: null,       // ref for removing unlock listener on unmount
      _currentAudioSource: null,   // P5: track playing source for interrupt

      // TTS audio cache — key=message content, value=ArrayBuffer[] (raw chunks)
      // Only committed to _ttsCache on natural playback completion (not on interrupt).
      _ttsCache: {},
      _ttsCachingKey: null,         // content key being accumulated during current playback
      _ttsCacheAccumulator: [],     // temporary chunks for the in-progress stream
      _ttsCacheOrder: [],           // insertion order for LRU eviction (max 10 entries)
      _ttsStopRequested: false,     // true after stop: discard any further segments (e.g. GPT-SoVITS) until next sendToTTS
      _ttsPendingReplayText: null,  // { text, profileId } when replay (no cache) needs to send after reconnect

      // SSE broadcast subscription (multi-client sync)
      _sseSource: null,
      _sseSessionId: null,  // 当前已订阅的 sessionId，用于幂等：同一 session 不重复建连

      // ASE proactive speech streaming state (tokens arrive via SSE broadcast)
      _aseStreaming: false,
      _aseStreamBuf: '',

      // Screen capture — persistent stream for browser (non-Electron) ASE screenshots
      _screenStream: null,
      screenShareActive: false,

      // VLM — pending screenshot captured by Electron IPC (base64 data URL)
      pendingScreenshot: null,
      pendingImageType: 'screenshot',  // 'screenshot' | 'upload' — 随 pendingScreenshot 一起设置，供 VLM 用不同 prompt

      // P4 — Status panel（群聊下用人格选择器选中的 profile 查状态）
      showStatusPanel: false,
      selectedStatusProfileId: null,  // 群聊时「查看谁的状态」
      currentStatus: { emotion_state: null, affinity_state: null, available_emotions: [] },

      // 频率面板（与状态面板并列；单聊=当前人格，群聊可切换人格）
      showCadencePanel: false,
      selectedCadenceProfileId: null,
      userActivity: {},
      editEnergy: 80,
      editEmotion: '',
      editAffinityDelta: 0,
      _statusPollTimer: null,

      // ASE / Reflection panel
      showAsePanel: false,
      aseStatus: {},
      _asePollTimer: null,

      // Pagination — only render the most recent N messages
      msgDisplayCount: 60,

      // P5-B — Bubble operations
      _historyOffset: 0,   // messages skipped by limit=200 loading (for store-index calc)

      // P5-C — Toolbar panel toggles
      showTodosPanel: false,
      showTimerPanel: false,
      showCommandsPanel: false,
      showToolbarConfig: false,

      // Toolbar button visibility config (P5-C)
      toolbarConfig: {
        tts:         { label: 'TTS',   icon: '🔊', enabled: true },
        voiceInput:  { label: '语音输入', icon: '🎤', enabled: true },
        convMode:    { label: '对话模式', icon: '💬', enabled: true, sttRequired: true },
        screenshot:  { label: '截图',  icon: '📷', enabled: true, electronOnly: true },
        screenShare: { label: '屏幕共享', icon: '🖥️', enabled: true, webOnly: true },
        uploadImage: { label: '上传图片', icon: '📁', enabled: true },
        todos:       { label: '待办',  icon: '📋', enabled: true },
        timer:       { label: '计时',  icon: '⏱', enabled: true },
        commands:    { label: '命令',  icon: '⌘', enabled: true },
      },

      // Todos (localStorage, no backend in P4-yet)
      todos: [],
      newTodoText: '',

      // Timers panel
      timerList: [],
      newTimerLabel: '',
      newTimerSeconds: 60,
      _timerRefreshId: null,

      // STT 语音输入（方案 B：服务端 faster-whisper）
      sttConfig: { enabled: true, language: 'zh' },
      voiceRecording: false,
      _mediaRecorder: null,
      _voiceStream: null,
      _voiceChunks: [],
      _voiceVadSilenceStart: null,
      _voiceRecordStartTime: 0,
      _voiceVadIntervalId: null,
      _voiceConversationTimerId: null,  // 语音对话模式：回复结束后自动再次开始录音的定时器
      voiceInputResultMode: 'fill',  // 'fill' | 'send'，从 localStorage 恢复
      voiceConversationMode: false,  // 语音对话模式：识别后立即发送，回复结束后自动再次录音
      voiceConversationResumeDelayMs: 3500,  // 回复结束或未说话后多少毫秒自动开始下一轮录音（TTS 禁用时生效）
      voiceInputLang: 'zh',
      // 对话模式 barge-in 监听器（TTS 播放中检测到说话 → 立即打断并开始录音）
      _bargeInMonitorId: null,
      _bargeInStream: null,
      _bargeInCtx: null,
      sttNoSpeechThreshold: 0.9,
      sttLogProbThreshold: -2.0,
      voiceSilenceTimeoutMs: 1500,
      voiceMaxDurationMs: 30000,
      voiceMinDurationMs: 500,
      voiceVadEnabled: true,
    };
  },

  async mounted() {
    // Locale 优先级：服务端 ui_prefs > localStorage > 浏览器语言推断（仅当两者皆无有效值）
    let prefs = {};
    try {
      const prefsRes = await fetch(getBaseUrl() + '/api/preferences');
      if (prefsRes.ok) {
        prefs = await prefsRes.json();
        if (prefs.locale === 'zh' || prefs.locale === 'en') {
          this.locale = prefs.locale;
          localStorage.setItem('locale', prefs.locale);
          document.documentElement.lang = prefs.locale;
        }
        if (Object.prototype.hasOwnProperty.call(prefs, 'theme') && typeof prefs.theme === 'string') {
          document.documentElement.setAttribute('data-theme', prefs.theme);
          localStorage.setItem('theme', prefs.theme);
        }
      }
    } catch (_) {}

    if (!Object.prototype.hasOwnProperty.call(prefs, 'theme') || typeof prefs.theme !== 'string') {
      const ts = localStorage.getItem('theme');
      if (ts !== null) {
        document.documentElement.setAttribute('data-theme', ts);
        try {
          await fetch(getBaseUrl() + '/api/preferences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ theme: ts }),
          });
        } catch (_) {}
      }
    }

    if (prefs.locale !== 'zh' && prefs.locale !== 'en') {
      const stored = localStorage.getItem('locale');
      if (stored === 'zh' || stored === 'en') {
        this.locale = stored;
        document.documentElement.lang = stored;
      } else {
        const inferred = inferLocaleFromNavigator();
        this.locale = inferred;
        localStorage.setItem('locale', inferred);
        document.documentElement.lang = inferred;
        try {
          await fetch(getBaseUrl() + '/api/preferences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ locale: inferred }),
          });
        } catch (_) {}
      }
    }

    if (typeof this.loadEmotionStandardKeys === 'function')
      await this.loadEmotionStandardKeys();

    // Set initial statusText with correct locale
    this.statusText = this.t('statusConnecting');

    // 移动端/触屏环境：HTML5 draggable 通常不可用 → 用 Pointer Events 自己做拖拽排序
    try {
      const hoverNone = window.matchMedia && window.matchMedia('(hover: none)').matches;
      const hasPointer = typeof window.PointerEvent === 'function';
      this.supportsHtml5Drag = !(hoverNone && hasPointer);
    } catch (_) {
      this.supportsHtml5Drag = true;
    }

    // Restore TTS state before doing anything (so watch fires if true)
    const savedTts = localStorage.getItem('ttsEnabled');
    if (savedTts === 'true') this.ttsEnabled = true;
    const savedVoiceMode = localStorage.getItem('voiceInputResultMode');
    if (savedVoiceMode === 'send' || savedVoiceMode === 'fill') this.voiceInputResultMode = savedVoiceMode;
    this.voiceConversationMode = localStorage.getItem('voiceConversationMode') === 'true';
    const resumeDelay = localStorage.getItem('voiceConversationResumeDelayMs');
    if (resumeDelay != null) this.voiceConversationResumeDelayMs = Math.max(2000, Math.min(30000, parseInt(resumeDelay, 10) || 3500));
    const v = localStorage.getItem('voiceInputLang'); if (v === 'zh' || v === 'en') this.voiceInputLang = v;
    const ns = localStorage.getItem('sttNoSpeechThreshold'); if (ns != null) this.sttNoSpeechThreshold = Math.max(0.5, Math.min(0.95, parseFloat(ns) || 0.9));
    const lp = localStorage.getItem('sttLogProbThreshold'); if (lp != null) this.sttLogProbThreshold = Math.max(-3, Math.min(0, parseFloat(lp) || -2));
    const t = localStorage.getItem('voiceSilenceTimeoutMs'); if (t != null) this.voiceSilenceTimeoutMs = Math.max(500, parseInt(t, 10) || 1500);
    const m = localStorage.getItem('voiceMaxDurationMs'); if (m != null) this.voiceMaxDurationMs = Math.max(5000, parseInt(m, 10) || 30000);
    const n = localStorage.getItem('voiceMinDurationMs'); if (n != null) this.voiceMinDurationMs = Math.max(200, parseInt(n, 10) || 500);
    this.voiceVadEnabled = localStorage.getItem('voiceVadEnabled') !== 'false';

    fetch(getBaseUrl() + API_PATHS.version()).then(r => r.json()).then(d => {
      this.appVersion = 'v' + (d.version || '0.0.0');
    }).catch(() => { this.appVersion = 'v0.0.0'; });

    if (window.electronAPI && window.electronAPI.onUpdateStatus) {
      window.electronAPI.onUpdateStatus(({ status, version, percent }) => {
        this.updateStatus = status;
        if (version)  this.updateVersion = version;
        if (percent !== undefined) this.updatePercent = percent;
      });
    }

    await this.loadSessions();
    await this.loadGroups();
    try {
      const pinned = localStorage.getItem('pinnedGroupIds');
      if (pinned) {
        const arr = JSON.parse(pinned);
        if (Array.isArray(arr)) this.pinnedGroupIds = arr;
      }
    } catch (_) {}
    if (window.innerWidth >= 769) {
      this.sidebarOpen = true;
      const appEl = document.getElementById('app');
      if (appEl) appEl.setAttribute('data-sidebar-closed', 'false');
    }
    let restoredGroup = false;
    try {
      const savedGroupId = localStorage.getItem('currentGroupId');
      if (savedGroupId && this.groups && this.groups.length) {
        const g = this.groups.find(gr => gr.group_id === savedGroupId);
        if (g) {
          await this.switchToGroup(g);
          restoredGroup = true;
        } else {
          localStorage.removeItem('currentGroupId');
        }
      }
    } catch (_) {}
    if (!restoredGroup && this.currentSessionId) {
      await this.loadHistory(this.currentSessionId);
      this.subscribeEvents(this.currentSessionId);
      this.fetchStatus();
      this._statusPollTimer = setInterval(() => this.fetchStatus(), 30000);
    }
    this.statusText = this.t('statusReady');
    this.loadSystemConfig();
    this.loadSttConfig();
    this._loadToolbarConfig();
    if (this.currentSessionId) await this.refreshTodos();

    // 辅助引擎健康检查（启动时 + 每60s轮询）
    this.loadEngineWarnings();
    this._engineWarnTimer = setInterval(() => this.loadEngineWarnings(), 60000);

    // 启动时检测配置是否完整，用于显示设置按钮红点
    this.loadSetupGuide();

    // Close toolbar panels when clicking outside
    this._outsideClickHandler = (e) => {
      if (!e.target.closest('.toolbar-panel-wrap') && !e.target.closest('.toolbar-btn.toolbar-gear')) {
        this.showTodosPanel = false;
        this.showTimerPanel = false;
        this.showCommandsPanel = false;
        this.showToolbarConfig = false;
      }
    };
    document.addEventListener('click', this._outsideClickHandler);

    // 多端对齐：切回本页时以服务端当前会话为准（群聊时不做切换，避免最小化恢复后回到单聊）
    this._visibilityHandler = () => {
      if (document.visibilityState !== 'visible') return;
      if (this.currentGroupId) return;
      const prevId = this.currentSessionId;
      this.loadSessions().then(() => {
        if (this.currentSessionId && this.currentSessionId !== prevId) {
          this.loadHistory(this.currentSessionId);
          this.subscribeEvents(this.currentSessionId);
          this.fetchStatus();
          this.fetchAseStatus();
          this.refreshTodos();
        }
      });
    };
    document.addEventListener('visibilitychange', this._visibilityHandler);

    // 周期性检测与服务器当前人物是否一致（仅单聊；群聊下不比较，不显示「与服务器人物不一致」）
    this._sessionSyncTimer = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      if (this.currentGroupId) return;
      fetch(getBaseUrl() + API_PATHS.sessionsCurrent())
        .then(r => r.ok ? r.json() : {})
        .then(data => {
          const serverCurrent = data.current_id || null;
          if (serverCurrent != null && this.currentSessionId !== serverCurrent) {
            this.outOfSyncWithServer = true;
          }
        })
        .catch(() => {});
    }, 20000);

    // Fix iOS/iPadOS Safari: virtual keyboard causes two problems:
    //   1. height shrinks when keyboard appears (visualViewport.height decreases)
    //   2. browser scrolls layout viewport so input is visible → visualViewport.offsetTop > 0
    //      → #app (position:fixed; top:0) drifts above the visible area
    //
    // Two-track solution:
    //   TRACK A — keyboard appear: visualViewport resize/scroll → apply vv.height + offsetTop
    //   TRACK B — keyboard dismiss: focusout → after 300ms animation, if no input is focused,
    //             clear inline styles so CSS (height:100dvh; top:0) takes over cleanly.
    //   This avoids relying on iPad's unreliable vp events for the "restore" path.
    const appEl = document.getElementById('app');
    if (window.visualViewport && appEl) {
      const clearVpStyles = () => {
        appEl.style.height = '';
        appEl.style.top    = '';
        window.scrollTo(0, 0);
        document.documentElement.scrollTop = 0;
        document.body.scrollTop = 0;
      };

      this._vpResizeHandler = () => {
        const vv = window.visualViewport;
        // 仅在大屏（iPad/桌面）做“键盘已关”判断，避免手机端误清导致键盘弹起时看不到输入框
        const isWide = window.innerWidth >= 768;
        const threshold = window.innerHeight * 0.7;
        if (isWide && vv.height >= threshold) {
          clearVpStyles();
          return;
        }
        appEl.style.height = vv.height + 'px';
        appEl.style.top    = vv.offsetTop + 'px';
      };
      window.visualViewport.addEventListener('resize', this._vpResizeHandler);
      window.visualViewport.addEventListener('scroll', this._vpResizeHandler);
      this._vpResizeHandler();

      // TRACK B: on focusout, wait for keyboard animation then reset if no input focused
      this._vpFocusOutHandler = () => {
        setTimeout(() => {
          const a = document.activeElement;
          if (!a || (a.tagName !== 'INPUT' && a.tagName !== 'TEXTAREA')) {
            clearVpStyles();
          }
        }, 300);
      };
      document.addEventListener('focusout', this._vpFocusOutHandler);
    }
  },

  unmounted() {
    if (this._captureErrorResetTimer) clearTimeout(this._captureErrorResetTimer);
    if (this._audioUnlockRef) {
      document.removeEventListener('click', this._audioUnlockRef);
      document.removeEventListener('touchstart', this._audioUnlockRef);
      document.removeEventListener('keydown', this._audioUnlockRef);
      this._audioUnlockRef = null;
    }
    if (this._sseSource) this._sseSource.close();
    if (this._screenStream) {
      this._screenStream.getTracks().forEach(t => t.stop());
      this._screenStream = null;
    }
    if (this._statusPollTimer) clearInterval(this._statusPollTimer);
    if (this._asePollTimer) clearInterval(this._asePollTimer);
    if (this._timerRefreshId) clearInterval(this._timerRefreshId);
    if (this._engineWarnTimer) clearInterval(this._engineWarnTimer);
    if (this._outsideClickHandler) document.removeEventListener('click', this._outsideClickHandler);
    if (this._visibilityHandler) document.removeEventListener('visibilitychange', this._visibilityHandler);
    if (this._sessionSyncTimer) clearInterval(this._sessionSyncTimer);
    if (this._vpResizeHandler) {
      window.visualViewport?.removeEventListener('resize', this._vpResizeHandler);
      window.visualViewport?.removeEventListener('scroll', this._vpResizeHandler);
    }
    if (this._vpFocusOutHandler) document.removeEventListener('focusout', this._vpFocusOutHandler);
  },

  methods: {
    /* ─────────────────── i18n ─────────────────── */

    t(key) {
      return (window.LOCALES && window.LOCALES[this.locale] && window.LOCALES[this.locale][key]) || key;
    },
    setLocale(lang) {
      this.locale = lang;
      localStorage.setItem('locale', lang);
      document.documentElement.lang = lang;
      // Persist to server so all browser sessions (phone, desktop) share the same locale
      fetch(getBaseUrl() + '/api/preferences', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ locale: lang })
      }).catch(() => {});
      // Update statusText to reflect new locale if it is the "ready" state
      if (this.statusText === this.t('statusReady') ||
          (lang === 'zh' && this.statusText === 'Ready') ||
          (lang === 'en' && this.statusText === '就绪')) {
        this.statusText = (window.LOCALES[lang] && window.LOCALES[lang].statusReady) || '就绪';
      }
    },

    /* ─────────────────── Sessions ─────────────────── */

    closeSidebar() {
      this.sidebarOpen = false;
      const appEl = document.getElementById('app');
      if (appEl) appEl.setAttribute('data-sidebar-closed', 'true');
    },
    openSidebar() {
      this.sidebarOpen = true;
      const appEl = document.getElementById('app');
      if (appEl) appEl.setAttribute('data-sidebar-closed', 'false');
    },
    toggleSidebar() {
      this.sidebarOpen = !this.sidebarOpen;
      const appEl = document.getElementById('app');
      if (appEl) appEl.setAttribute('data-sidebar-closed', this.sidebarOpen ? 'false' : 'true');
    },

    async loadSessions() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessions());
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this.sessions = data.sessions || [];
        // 以服务端 current_id 为准，多端对齐：谁后切换/谁后刷新，当前会话都一致
        const serverCurrentId = data.current_id || null;
        const current = serverCurrentId
          ? this.sessions.find(s => s.id === serverCurrentId)
          : this.sessions.find(s => s.is_current);
        if (current) {
          this.currentSessionId = current.id;
          this.currentSessionName = current.display_name;
          this.currentSessionAvatar = current.avatar || '';
        } else if (serverCurrentId && this.sessions.length) {
          this.currentSessionId = serverCurrentId;
          this.currentSessionName = serverCurrentId;
          this.currentSessionAvatar = '';
        } else {
          this.currentSessionId = null;
          this.currentSessionName = '...';
          this.currentSessionAvatar = '';
          this.messages = [];
          this.streamingText = '';
        }
        this.outOfSyncWithServer = false;
      } catch (e) {
        this.statusText = `${this.t('statusCannotConnect')}: ${e.message}`;
      }
    },

    /** 手动与服务器同步当前人物（手机端切了人物后，电脑端点此刷新） */
    async syncSessionsFromServer() {
      const prevId = this.currentSessionId;
      await this.loadSessions();
      this.outOfSyncWithServer = false;
      if (this.currentSessionId && this.currentSessionId !== prevId) {
        await this.loadHistory(this.currentSessionId);
        this.subscribeEvents(this.currentSessionId);
        this.fetchStatus();
        this.fetchAseStatus();
        if (this.currentSessionId) await this.refreshTodos();
        this.statusText = `${this.t('statusSyncedTo').replace('{name}', this.currentSessionName)}`;
      } else {
        this.statusText = this.t('statusSynced');
      }
    },

    sessionDragStart(ev, sessionId) {
      if (!this.supportsHtml5Drag) return;
      this.sessionDragging = true;
      this.sessionDragActiveId = sessionId;
      ev.dataTransfer.setData('text/plain', sessionId);
      ev.dataTransfer.effectAllowed = 'move';
    },
    sessionDragEnd() {
      this.sessionDragging = false;
      this.sessionDragOverId = null;
      this.sessionDragActiveId = null;
    },

    sessionPointerDown(ev, sessionId) {
      // Desktop: HTML5 draggable handles it; only handle touch/pointer here
      if (this.supportsHtml5Drag) return;

      ev.preventDefault(); // 触屏：避免滚动/选中文本；桌面不能 prevent，否则手柄无法启动原生 drag
      this.sessionDragging = true;
      this.sessionDragActiveId = sessionId;
      this.sessionDragOverId = null;

      const startY = ev.clientY;
      let hasMoved = false;

      const onMove = (e) => {
        if (!hasMoved && Math.abs(e.clientY - startY) > 6) hasMoved = true;
        if (!hasMoved) return;
        const el = document.elementFromPoint(e.clientX, e.clientY);
        const item = el && el.closest('[data-session-id]');
        const overId = item ? item.getAttribute('data-session-id') : null;
        this.sessionDragOverId = (overId && overId !== sessionId) ? overId : null;
      };

      const onUp = async () => {
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
        document.removeEventListener('pointercancel', onUp);

        const targetId = this.sessionDragOverId;
        this.sessionDragging = false;
        this.sessionDragOverId = null;
        this.sessionDragActiveId = null;

        if (hasMoved && targetId && targetId !== sessionId) {
          const fromIdx = this.sessions.findIndex(s => s.id === sessionId);
          const toIdx = this.sessions.findIndex(s => s.id === targetId);
          if (fromIdx !== -1 && toIdx !== -1) {
            const copy = this.sessions.slice();
            const [moved] = copy.splice(fromIdx, 1);
            copy.splice(toIdx, 0, moved);
            this.sessions = copy;
            await this.saveSessionOrder();
          }
        }
      };

      document.addEventListener('pointermove', onMove);
      document.addEventListener('pointerup', onUp);
      document.addEventListener('pointercancel', onUp);
    },
    sessionDragOver(ev, sessionId) {
      ev.dataTransfer.dropEffect = 'move';
      this.sessionDragOverId = sessionId;
    },
    async sessionDrop(ev, dropTargetId) {
      this.sessionDragOverId = null;
      const draggedId = ev.dataTransfer.getData('text/plain');
      if (!draggedId || draggedId === dropTargetId) return;
      const fromIdx = this.sessions.findIndex(s => s.id === draggedId);
      const toIdx = this.sessions.findIndex(s => s.id === dropTargetId);
      if (fromIdx === -1 || toIdx === -1) return;
      const copy = this.sessions.slice();
      const [item] = copy.splice(fromIdx, 1);
      copy.splice(toIdx, 0, item);
      this.sessions = copy;
      await this.saveSessionOrder();
    },

    async saveSessionOrder() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profilesOrder(), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile_ids: this.sessions.map(s => s.id) }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.ok) {
          this.statusText = this.t('statusOrderSaved');
        } else {
          this.statusText = res.ok ? this.t('statusOrderSaveFailed') : `${this.t('statusOrderSaveFailed')}: HTTP ${res.status}`;
        }
      } catch (e) {
        this.statusText = `${this.t('statusOrderSaveFailed')}: ${e.message}`;
      }
    },

    async moveSessionUp(sessionId) {
      const idx = this.sessions.findIndex(s => s.id === sessionId);
      if (idx <= 0) return;
      const copy = this.sessions.slice();
      [copy[idx - 1], copy[idx]] = [copy[idx], copy[idx - 1]];
      this.sessions = copy;
      await this.saveSessionOrder();
    },
    async moveSessionDown(sessionId) {
      const idx = this.sessions.findIndex(s => s.id === sessionId);
      if (idx < 0 || idx >= this.sessions.length - 1) return;
      const copy = this.sessions.slice();
      [copy[idx], copy[idx + 1]] = [copy[idx + 1], copy[idx]];
      this.sessions = copy;
      await this.saveSessionOrder();
    },

    async switchSession(sessionId) {
      if (sessionId === this.currentSessionId || this.isStreaming) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessionSwitch(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId }),
        });
        const data = await res.json();
        if (data.ok) {
          this.currentGroupId = null;
          this.currentGroup = null;
          try { localStorage.removeItem('currentGroupId'); } catch (_) {}
          this.currentSessionId = sessionId;
          this.currentSessionName = data.display_name;
          this.currentSessionAvatar = this.sessions.find(s => s.id === sessionId)?.avatar || '';
          this.outOfSyncWithServer = false;
          this.messages = [];
          this.streamingText = '';
          this.msgDisplayCount = 60;
          this.statusText = this.t('statusSwitchedTo').replace('{name}', data.display_name);
          this.sessions = this.sessions.map(s => ({
            ...s,
            is_current: s.id === sessionId,
          }));
          await this.loadHistory(sessionId);
          this.subscribeEvents(sessionId);
          this.fetchStatus();
          this.fetchAseStatus();
          this.sidebarOpen = false;
        }
      } catch (e) {
        this.statusText = `${this.t('statusSwitchFailed')}: ${e.message}`;
      }
    },

    async loadGroups() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.groups());
        if (!res.ok) return;
        const data = await res.json();
        this.groups = data.groups || [];
      } catch (e) {
        console.warn('[group] loadGroups failed', e);
      }
    },
    isGroupPinned(groupId) {
      return (this.pinnedGroupIds || []).indexOf(groupId) >= 0;
    },
    toggleGroupPin(groupId) {
      let ids = (this.pinnedGroupIds || []).slice();
      const i = ids.indexOf(groupId);
      if (i >= 0) ids.splice(i, 1);
      else ids.push(groupId);
      this.pinnedGroupIds = ids;
      try { localStorage.setItem('pinnedGroupIds', JSON.stringify(ids)); } catch (_) {}
    },
    async switchToGroup(group) {
      if (!group || this.isStreaming) return;
      this.currentSessionId = null;
      this.currentGroupId = group.group_id;
      this.currentGroup = group;
      this.messages = [];
      this.streamingText = '';
      this.groupChatStreamingSender = '';
      this.msgDisplayCount = 60;
      this.outOfSyncWithServer = false;
      this.showStatusPanel = false;
      this.showAsePanel = false;
      try { localStorage.setItem('currentGroupId', group.group_id); } catch (_) {}
      this.statusText = this.t('statusGroupEntered').replace('{name}', group.display_name || group.group_id);
      this.selectedStatusProfileId = (group.participants && group.participants[0]) ? group.participants[0].profile_id : null;
      this.selectedCadenceProfileId = (group.participants && group.participants[0]) ? group.participants[0].profile_id : null;
      await this.loadGroupHistory(group.group_id);
      this.subscribeEvents('group:' + group.group_id);
      this.sidebarOpen = false;
      if (this.selectedStatusProfileId) this.fetchStatus();
      if (this.selectedCadenceProfileId) this.fetchUserActivity();
    },
    async loadGroupHistory(groupId) {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.groupHistory(groupId, 200));
        if (!res.ok) return;
        const data = await res.json();
        const msgs = (data.messages || [])
          .filter(m => m.role === 'user' || m.role === 'assistant')
          .map(m => ({ role: m.role, content: m.content, timestamp: m.timestamp, sender: m.sender || '' }));
        this._historyOffset = Math.max(0, (data.total || msgs.length) - msgs.length);
        this.msgDisplayCount = 60;
        this.messages = msgs;
        await this.$nextTick();
        requestAnimationFrame(() => this.scrollToBottom());
      } catch (e) {
        console.warn('[group] loadGroupHistory failed', e);
      }
    },
    async leaveGroupChat() {
      this.currentGroupId = null;
      this.currentGroup = null;
      this.selectedStatusProfileId = null;
      this.selectedCadenceProfileId = null;
      this.messages = [];
      this.streamingText = '';
      this.msgDisplayCount = 60;
      try { localStorage.removeItem('currentGroupId'); } catch (_) {}
      await this.loadSessions();
      if (this.currentSessionId) {
        await this.loadHistory(this.currentSessionId);
        this.subscribeEvents(this.currentSessionId);
        this.fetchStatus();
      }
      this.statusText = this.t('statusGroupLeft');
    },
    async openCreateGroupModal() {
      this.showCreateGroupModal = true;
      this.createGroupName = '';
      this.createGroupSelectedIds = [];
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profiles());
        if (res.ok) {
          const data = await res.json();
          this.createGroupProfiles = data.profiles || [];
        }
      } catch (e) {
        console.warn('[group] load profiles failed', e);
        this.createGroupProfiles = [];
      }
    },
    toggleCreateGroupParticipant(profileId) {
      const i = this.createGroupSelectedIds.indexOf(profileId);
      if (i >= 0) {
        this.createGroupSelectedIds = this.createGroupSelectedIds.filter(id => id !== profileId);
      } else {
        this.createGroupSelectedIds = this.createGroupSelectedIds.slice();
        this.createGroupSelectedIds.push(profileId);
      }
    },
    async openGroupSettings() {
      if (!this.currentGroup || !this.currentGroupId) return;
      this.confirmingDeleteGroup = false;
      this.showGroupSettingsModal = true;
      this.groupSettingsName = this.currentGroup.display_name || '';
      this.groupSettingsParticipants = (this.currentGroup.participants || []).map(p => ({ ...p, muted: !!p.muted }));
      this.groupSettingsMaxReplies = Math.max(0, parseInt(this.currentGroup.max_replies_per_turn, 10) || 0);
      this.groupSettingsOrchestrator = (this.currentGroup.orchestrator || 'random').trim() || 'random';
      this.groupSettingsSystemHint = (this.currentGroup.system_hint || '').trim();
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profiles());
        const data = res.ok ? await res.json() : { profiles: [] };
        const all = data.profiles || [];
        const inGroup = new Set((this.currentGroup.participants || []).map(p => p.profile_id));
        this.groupSettingsInviteProfiles = all.filter(p => !inGroup.has(p.profile_id));
      } catch (e) {
        this.groupSettingsInviteProfiles = [];
      }
      this.groupSettingsRecordSearchQuery = '';
      this.groupSettingsRecordSearchResults = [];
      this.loadGroupSettingsRecords(true);
    },
    /** 打开设置时加载最近记录；reset=true 清空并拉第一页，false 追加下一页 */
    async loadGroupSettingsRecords(reset) {
      const gid = this.currentGroupId;
      if (!gid) return;
      if (reset) {
        this.groupSettingsRecordOffset = 0;
        this.groupSettingsRecordList = [];
      }
      this.groupSettingsRecordLoading = true;
      try {
        const limit = this.groupSettingsRecordLimit || 50;
        const offset = reset ? 0 : this.groupSettingsRecordOffset;
        const url = getBaseUrl() + API_PATHS.groupHistory(gid, limit, offset);
        const res = await fetch(url);
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        const msgs = data.messages || [];
        if (reset) this.groupSettingsRecordList = msgs;
        else this.groupSettingsRecordList.push(...msgs);
        this.groupSettingsRecordTotal = data.total ?? 0;
        this.groupSettingsRecordOffset = this.groupSettingsRecordList.length;
      } catch (e) {
        this.showToast('加载记录失败: ' + (e.message || '未知错误'), 'error');
      } finally {
        this.groupSettingsRecordLoading = false;
      }
    },
    loadGroupSettingsRecordsMore() {
      if (this.groupSettingsRecordList.length >= this.groupSettingsRecordTotal || this.groupSettingsRecordLoading) return;
      this.loadGroupSettingsRecords(false);
    },
    /** 群组设置弹窗内：仅搜索本群记录，结果只显示在弹窗内，不影响主聊天视图 */
    async runGroupSettingsRecordSearch() {
      const gid = this.currentGroupId;
      const q = (this.groupSettingsRecordSearchQuery || '').trim();
      if (!gid) return;
      if (!q) {
        this.groupSettingsRecordSearchResults = [];
        return;
      }
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.groupMessagesSearch(gid, q, 100));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this.groupSettingsRecordSearchResults = data.items || [];
      } catch (e) {
        this.groupSettingsRecordSearchResults = [];
        this.showToast('搜索失败: ' + (e.message || '未知错误'), 'error');
      }
    },
    deleteCurrentGroup() {
      if (!this.currentGroupId) return;
      this.confirmingDeleteGroup = true;
    },
    cancelDeleteGroup() {
      this.confirmingDeleteGroup = false;
    },
    async confirmDeleteGroup() {
      if (!this.currentGroupId) return;
      const groupId = this.currentGroupId;
      this.confirmingDeleteGroup = false;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.group(groupId), { method: 'DELETE' });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
        this.showGroupSettingsModal = false;
        this.groups = (this.groups || []).filter(g => g.group_id !== groupId);
        if (this.currentGroupId === groupId) await this.leaveGroupChat();
        this.statusText = this.t('statusGroupDeleted');
      } catch (e) {
        this.statusText = `${this.t('statusGroupDeleteFailed')}: ${e.message}`;
      }
    },
    async saveGroupSettings() {
      if (!this.currentGroupId) return;
      const name = (this.groupSettingsName || '').trim() || '未命名群组';
      const participants = this.groupSettingsParticipants.map(p => ({
        profile_id: p.profile_id,
        type: 'ai',
        name: (p.name || '').trim() || this.sessions.find(s => s.id === p.profile_id)?.display_name || p.profile_id,
        muted: !!p.muted,
      }));
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.group(this.currentGroupId), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            display_name: name,
            participants,
            max_replies_per_turn: Math.max(0, parseInt(this.groupSettingsMaxReplies, 10) || 0),
            orchestrator: (this.groupSettingsOrchestrator || 'random').trim() || 'random',
            system_hint: (this.groupSettingsSystemHint || '').trim(),
          }),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
        const updated = await res.json();
        this.currentGroup = updated;
        await this.loadGroups();
        this.showGroupSettingsModal = false;
        this.statusText = this.t('statusGroupSettingsSaved');
      } catch (e) {
        this.statusText = `${this.t('statusSaveFailed')}: ${e.message}`;
      }
    },
    removeGroupParticipant(profileId) {
      const removed = this.groupSettingsParticipants.find(p => p.profile_id === profileId);
      this.groupSettingsParticipants = this.groupSettingsParticipants.filter(p => p.profile_id !== profileId);
      const displayName = (removed && removed.name) || this.sessions.find(s => s.id === profileId)?.display_name || profileId;
      this.groupSettingsInviteProfiles = [...(this.groupSettingsInviteProfiles || []), { profile_id: profileId, display_name: displayName }];
    },
    async clearGroupHistory() {
      if (!this.currentGroupId) return;
      if (!confirm('确定清空本群全部聊天记录？无法恢复。')) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.groupMessagesClear(this.currentGroupId), { method: 'DELETE' });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
        this.showGroupSettingsModal = false;
        this.messages = [];
        this.statusText = this.t('statusGroupHistoryCleared');
      } catch (e) {
        this.statusText = `${this.t('statusGroupClearFailed')}: ${e.message}`;
      }
    },
    async exportGroupHistory(format) {
      if (!this.currentGroupId) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.groupHistoryExport(this.currentGroupId, format));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `group_${this.currentGroupId}_${format || 'json'}.${format === 'txt' ? 'txt' : format === 'json' ? 'json' : 'txt'}`;
        a.click();
        URL.revokeObjectURL(url);
        this.statusText = this.t('statusGroupHistoryExported');
      } catch (e) {
        this.statusText = `${this.t('statusGroupExportFailed')}: ${e.message}`;
      }
    },
    groupChatMessageVisible(msg) {
      if (this.groupChatSenderFilter) {
        if (this.groupChatSenderFilter === '用户') {
          if (msg.role !== 'user') return false;
        } else if ((msg.sender || '').trim() !== this.groupChatSenderFilter) return false;
      }
      const q = (this.groupChatSearchQuery || '').trim().toLowerCase();
      if (q && !(msg.content || '').toLowerCase().includes(q)) return false;
      return true;
    },
    async runGroupSearch() {
      const q = (this.groupChatSearchQuery || '').trim();
      if (!q || !this.currentGroupId) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.groupMessagesSearch(this.currentGroupId, q, 100));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this.groupSearchResults = data.items || [];
        this.statusText = this.groupSearchResults.length ? this.t('statusSearchResults').replace('{n}', this.groupSearchResults.length) : this.t('statusNoMatch');
      } catch (e) {
        this.groupSearchResults = [];
        this.statusText = `${this.t('statusSearchFailed')}: ${e.message}`;
      }
    },
    addGroupParticipant(profile) {
      this.groupSettingsParticipants = [...this.groupSettingsParticipants, { profile_id: profile.profile_id, type: 'ai', name: profile.display_name || profile.profile_id, muted: false }];
      this.groupSettingsInviteProfiles = this.groupSettingsInviteProfiles.filter(p => p.profile_id !== profile.profile_id);
    },
    /** 参与者列表顺序即「按列表顺序」时的发言顺序；上移/下移后需点保存生效 */
    moveGroupParticipantUp(idx) {
      if (idx <= 0 || !this.groupSettingsParticipants.length) return;
      const copy = this.groupSettingsParticipants.slice();
      [copy[idx - 1], copy[idx]] = [copy[idx], copy[idx - 1]];
      this.groupSettingsParticipants = copy;
    },
    moveGroupParticipantDown(idx) {
      if (idx < 0 || idx >= this.groupSettingsParticipants.length - 1) return;
      const copy = this.groupSettingsParticipants.slice();
      [copy[idx], copy[idx + 1]] = [copy[idx + 1], copy[idx]];
      this.groupSettingsParticipants = copy;
    },
    async createGroupSubmit() {
      const name = (this.createGroupName || '').trim() || '未命名群组';
      if (this.createGroupSelectedIds.length === 0) {
        this.statusText = this.t('statusSelectParticipant');
        return;
      }
      const profiles = this.createGroupProfiles.filter(p => this.createGroupSelectedIds.includes(p.profile_id));
      const participants = profiles.map(p => ({
        profile_id: p.profile_id,
        type: 'ai',
        name: (p.display_name || p.profile_id).trim(),
      }));
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.groups(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: name, participants }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          this.statusText = err.detail || `${this.t('statusGroupCreateFailed')}: HTTP ${res.status}`;
          return;
        }
        const group = await res.json();
        this.showCreateGroupModal = false;
        await this.loadGroups();
        this.switchToGroup(group);
        this.statusText = this.t('statusGroupCreated').replace('{name}', name);
      } catch (e) {
        this.statusText = `${this.t('statusGroupCreateFailed')}: ${e.message}`;
      }
    },

    /* ─────────────────── History & Live Sync ─────────────────── */

    async loadHistory(sessionId) {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessionHistory(sessionId) + '?limit=200');
        if (!res.ok) return;
        const data = await res.json();
        const msgs = (data.messages || [])
          .filter(m => m.role === 'user' || m.role === 'assistant')
          .map(m => ({ role: m.role, content: m.content, timestamp: m.timestamp, sender: m.sender || '' }));
        // If history is truncated, prepend a system notice
        const total = data.total || msgs.length;
        // Store the offset so bubble-delete ops can compute the correct store index
        this._historyOffset = Math.max(0, total - msgs.length);
        // Always reset display count so we start from the bottom
        this.msgDisplayCount = 60;
        if (total > msgs.length) {
          this.messages = [
            { role: 'system_notice', content: `── 仅显示最近 ${msgs.length} 条，共 ${total} 条历史记录 ──` },
            ...msgs,
          ];
        } else {
          this.messages = msgs;
        }
        // Double-tick + rAF to ensure browser has painted before scrolling
        await this.$nextTick();
        await this.$nextTick();
        requestAnimationFrame(() => this.scrollToBottom());
      } catch (e) {
        // Non-fatal — continue with empty history
      }
    },

    subscribeEvents(sessionId) {
      // 幂等：本端已订阅该 session 且连接未关闭则不再建连，避免同一页面产生两个 SSE（多设备各建各的，不受影响）
      if (this._sseSource && this._sseSessionId === sessionId && this._sseSource.readyState !== 2) return;

      if (this._sseSource) {
        this._sseSource.close();
        this._sseSource = null;
      }
      this._sseSessionId = sessionId;
      const url = getBaseUrl() + API_PATHS.events(sessionId);
      const source = new EventSource(url);
      this._sseSource = source;

      source.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);

          // ── ASE proactive token stream ─────────────────────────────────────
          // Normal chat tokens come via HTTP response; only ASE/heartbeat tokens
          // arrive via SSE broadcast. Handle them here to stream + trigger TTS.
          if ('token' in data) {
            if (data.done) {
              // Stream finished — finalize bubble and fire TTS
              if (this._aseStreaming) {
                const last = this.messages[this.messages.length - 1];
                if (last && last._streaming) delete last._streaming;
                if (last) last.sender = this.sessions.find(s => s.id === this.currentSessionId)?.display_name || '';
                if (this.ttsEnabled && this._aseStreamBuf.trim()) {
                  this.sendToTTS(this._aseStreamBuf);
                }
                this._aseStreaming = false;
                this._aseStreamBuf = '';
                this.$nextTick(() => this.scrollToBottom());
                uiLog('heartbeat', 'token stream done, TTS fired');
              }
            } else if (data.token) {
              // Accumulate token into streaming bubble
              this._aseStreamBuf += data.token;
              if (!this._aseStreaming) {
                this._aseStreaming = true;
                const displayName = this.sessions.find(s => s.id === this.currentSessionId)?.display_name || '';
                this.messages.push({ role: 'assistant', content: data.token, _streaming: true, timestamp: this._now(), sender: displayName });
              } else {
                const last = this.messages[this.messages.length - 1];
                if (last && last._streaming) last.content += data.token;
              }
              this.$nextTick(() => this.scrollToBottom());
            }
            return;
          }

          // ── Regular broadcast events ───────────────────────────────────────
          if (data.type === 'new_message') {
            if (data.client_id === CLIENT_ID) return; /* 本端已从请求流添加，避免群聊重复 */
            // Skip ASE new_message if we already rendered it token-by-token
            if (this._aseStreaming && data.client_id === '') return;
            this.messages.push({ role: data.role, content: data.content, timestamp: this._now(), sender: data.sender || '' });
            // TTS for server-originated messages (system_trigger, etc.) not streamed as tokens
            if (data.role === 'assistant' && data.client_id === '' && this.ttsEnabled && data.content?.trim()) {
              this.sendToTTS(data.content);
            }
            this.$nextTick(() => this.scrollToBottom());
          } else if (data.type === 'timer_expired') {
            this.showToast(`⏱ 计时器「${data.label}」到时了！`);
            if (this.showTimerPanel) this.refreshTimers();
            // system_trigger 已由后端直接处理，前端只负责 UI 刷新
          } else if (data.type === 'heartbeat_screenshot_request') {
            uiLog('heartbeat', 'screenshot_request received, capturing...');
            this._sendHeartbeatScreenshot();
          }
        } catch (_) { /* ignore malformed */ }
      };

      source.onerror = () => {
        // EventSource auto-reconnects; no manual action needed
      };
    },

    /* ─────────────────── Chat ─────────────────── */

    async sendMessage() {
      const text = (this.inputText || '').trim();
      const hasImage = !!this.pendingScreenshot;
      if ((!text && !hasImage) || this.isStreaming) return;

      if (this.currentGroupId) {
        await this.sendGroupMessage(text || '请根据当前画面回复', hasImage);
        return;
      }

      const displayText = text || '请根据当前画面回复';
      this.inputText = '';
      this.isStreaming = true;
      this.streamingText = '';
      this.statusText = this.t('statusThinking');
      this._chatAbortController = new AbortController();

      // Show user bubble immediately (sender 与后端一致，单聊为「用户」)
      this.messages.push({ role: 'user', content: displayText, timestamp: this._now(), is_command: displayText.startsWith('/'), sender: '用户' });
      await this.$nextTick();
      this.scrollToBottom();

      // VLM: if screenshot/image is pending, describe it and append context to the message
      let messageToSend = displayText;
      if (this.pendingScreenshot) {
        const screenshotB64 = this.pendingScreenshot.split(',')[1]; // strip data: prefix
        const imageType = this.pendingImageType || 'screenshot';
        this.pendingScreenshot = null;
        this.pendingImageType = 'screenshot';
        try {
          uiLog('VLM', 'image pending, requesting description...', { imageType });
          const vlmResp = await fetch(getBaseUrl() + API_PATHS.vlm(), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              session_id: this.currentSessionId,
              image_b64: screenshotB64,
              source: 'chat',
              image_type: imageType,
            }),
          });
          if (vlmResp.ok) {
            const vlmData = await vlmResp.json();
            if (vlmData.description) {
              messageToSend = `${messageToSend}\n[当前屏幕：${vlmData.description}]`;
              uiLog('VLM', 'description received', vlmData.description.slice(0, 80));
            }
          }
        } catch (e) {
          uiLog('VLM', 'description request failed', e.message);
        }
      }

      try {
        const response = await fetch(getBaseUrl() + API_PATHS.chat(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: messageToSend,
            client_id: CLIENT_ID,
          }),
          signal: this._chatAbortController.signal,
        });

        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop(); // last incomplete line stays in buffer

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const data = JSON.parse(line.slice(6));

              if (data.error) {
                this.statusText = `${this.t('statusError')}: ${data.error}`;
                this.isStreaming = false;
                this.streamingText = '';
                return;
              }

              if (data.token) {
                this.streamingText += data.token;
                await this.$nextTick();
                this.scrollToBottom();
              }

              if (data.done) {
                const finalText = this.streamingText;
                const displayName = this.sessions.find(s => s.id === this.currentSessionId)?.display_name || '';
                this.messages.push({ role: 'assistant', content: finalText, timestamp: this._now(), sender: displayName });
                this.streamingText = '';
                this.isStreaming = false;
                this.statusText = this.t('statusReady');

                if (this.ttsEnabled && finalText.trim()) {
                  this.sendToTTS(finalText);
                }

                // Refresh status panel after reply (engines may have updated state)
                setTimeout(() => { this.fetchStatus(); if (this.showCadencePanel) this.fetchUserActivity(); }, 1500);
              }
            } catch (_) { /* ignore malformed JSON */ }
          }
        }
      } catch (e) {
        if (e && e.name === 'AbortError') {
          this.statusText = this.t('statusAborted');
          // 对话模式下用户取消回复：TTS 不会播放，_playNext 不会触发，需手动调度下一轮
          if (this.voiceConversationMode && !this.voiceRecording) {
            this._scheduleVoiceConversationNext();
          }
        } else {
          this.statusText = `${this.t('statusSendFailed')}: ${e.message}`;
        }
        this.isStreaming = false;
        this.streamingText = '';
      } finally {
        this._chatAbortController = null;
      }
    },

    abortGroupChat() {
      if (this._groupChatAbortController) {
        this._groupChatAbortController.abort();
      }
    },

    abortChat() {
      if (this._chatAbortController) {
        this._chatAbortController.abort();
      }
    },

    async sendGroupMessage(displayText, hasImage) {
      if (!this.currentGroupId || this.isStreaming) return;
      const userSender = (this.currentGroup && this.currentGroup.user_name) || '用户';
      this.inputText = '';
      this.isStreaming = true;
      this.streamingText = '';
      this.groupChatStreamingSender = '';
      this._groupChatAbortController = new AbortController();
      this.statusText = this.t('statusGroupReplying');
      this.messages.push({ role: 'user', content: displayText, timestamp: this._now(), sender: userSender });
      await this.$nextTick();
      this.scrollToBottom();

      let messageToSend = displayText;
      if (hasImage && this.pendingScreenshot) {
        const screenshotB64 = this.pendingScreenshot.split(',')[1];
        this.pendingScreenshot = null;
        try {
          const vlmResp = await fetch(getBaseUrl() + API_PATHS.vlm(), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              session_id: this.currentSessionId || '',
              image_b64: screenshotB64,
              source: 'chat',
              image_type: this.pendingImageType || 'screenshot',
            }),
          });
          if (vlmResp.ok) {
            const vlmData = await vlmResp.json();
            if (vlmData.description) messageToSend = `${messageToSend}\n[当前屏幕：${vlmData.description}]`;
          }
        } catch (e) { console.warn('[group] VLM failed', e); }
      }

      try {
        const response = await fetch(getBaseUrl() + API_PATHS.groupChat(this.currentGroupId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: messageToSend, client_id: CLIENT_ID }),
          signal: this._groupChatAbortController.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const data = JSON.parse(line.slice(6));
              if (data.error) {
                this.statusText = `${this.t('statusError')}: ${data.error}`;
                this.isStreaming = false;
                this.streamingText = '';
                this.groupChatStreamingSender = '';
                return;
              }
              // 仅 sender：后端在每条 AI 回复开始前先发，用于立即显示「正在说话的 AI」头像
              if (data.sender && data.token === undefined && !data.done) {
                this.groupChatStreamingSender = data.sender;
                await this.$nextTick();
                this.scrollToBottom();
              }
              if (data.token !== undefined) {
                this.streamingText += data.token || '';
                if (data.sender) this.groupChatStreamingSender = data.sender;
                await this.$nextTick();
                this.scrollToBottom();
              }
              if (data.done) {
                if (this.streamingText.trim() || data.sender) {
                  this.messages.push({
                    role: 'assistant',
                    content: this.streamingText.trim(),
                    timestamp: this._now(),
                    sender: data.sender || this.groupChatStreamingSender || '',
                  });
                  this.streamingText = '';
                  this.groupChatStreamingSender = data.sender || '';
                  await this.$nextTick();
                  this.scrollToBottom();
                }
                if (!data.sender) {
                  this.isStreaming = false;
                  this.statusText = this.t('statusReady');
                }
              }
            } catch (_) { /* ignore */ }
          }
        }
        this.isStreaming = false;
        this.streamingText = '';
        this.groupChatStreamingSender = '';
        this.statusText = this.t('statusReady');
      } catch (e) {
        if (e.name === 'AbortError') {
          this.statusText = this.t('statusAborted');
        } else {
          this.statusText = `${this.t('statusGroupSendFailed')}: ${e.message}`;
        }
        this.isStreaming = false;
        this.streamingText = '';
        this.groupChatStreamingSender = '';
      } finally {
        this._groupChatAbortController = null;
      }
    },

    /* ─────────────────── System trigger (timer expiry, etc.) ─────────────────── */

    async _sendSystemTrigger(label) {
      if (!this.currentSessionId) return;
      // Insert a timer_notice bubble (frontend-only, not saved to history)
      this.messages.push({ role: 'timer_notice', content: `⏱ 计时器「${label}」时间到了！`, timestamp: this._now() });
      await this.$nextTick();
      this.scrollToBottom();

      try {
        const response = await fetch(getBaseUrl() + API_PATHS.sessionSystemTrigger(this.currentSessionId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: `计时器「${label}」到时了，请提醒用户。` }),
        });
        if (!response.ok) return;

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let streamText = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const data = JSON.parse(line.slice(6));
              if (data.token) {
                streamText += data.token;
                // Update streaming bubble (last assistant entry or create one)
                const last = this.messages[this.messages.length - 1];
                if (last && last.role === 'assistant' && last._streaming) {
                  last.content = streamText;
                } else {
                  const displayName = this.sessions.find(s => s.id === this.currentSessionId)?.display_name || '';
                  this.messages.push({ role: 'assistant', content: streamText, timestamp: this._now(), _streaming: true, sender: displayName });
                }
                await this.$nextTick();
                this.scrollToBottom();
              }
              if (data.done) {
                // Finalize streaming bubble
                const last = this.messages[this.messages.length - 1];
                if (last && last._streaming) delete last._streaming;
                if (last) last.sender = this.sessions.find(s => s.id === this.currentSessionId)?.display_name || '';
                if (this.ttsEnabled && streamText.trim()) this.sendToTTS(streamText);
                setTimeout(() => { this.fetchStatus(); if (this.showCadencePanel) this.fetchUserActivity(); }, 1500);
              }
            } catch (_) {}
          }
        }
      } catch (_) {}
    },

    /* ─────────────────── VLM / Heartbeat ─────────────────── */

    async _sendHeartbeatScreenshot() {
      // Called when ASE broadcasts "heartbeat_screenshot_request" via SSE.
      // Tries Electron IPC first, then falls back to a pre-authorized browser stream.
      if (!this.currentSessionId) return;

      let b64 = null;

      if (IS_ELECTRON && window.electronAPI) {
        // Electron path — native screen capture via IPC
        try {
          const dataUrl = await window.electronAPI.captureScreen();
          if (dataUrl) b64 = dataUrl.split(',')[1];
          uiLog('heartbeat', 'Electron screenshot captured');
        } catch (e) {
          uiLog('heartbeat', 'Electron capture failed', e.message);
          this._setCaptureErrorStatus('主动发言截图失败：屏幕捕获不可用。可尝试更新显卡驱动或使用工具栏「上传图片」。');
        }
      } else if (this._screenStream) {
        // Browser path — use pre-authorized getDisplayMedia stream
        b64 = await this._grabFrameFromStream();
        if (b64) uiLog('heartbeat', 'browser screenshot captured');
      }

      if (!b64) {
        uiLog('heartbeat', 'no screenshot available (enable screen share in toolbar)');
        this._setCaptureErrorStatus('主动发言截图失败：屏幕捕获不可用。可尝试更新显卡驱动或使用工具栏「上传图片」。');
        return;
      }

      try {
        await fetch(getBaseUrl() + API_PATHS.sessionScreenshot(this.currentSessionId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image_b64: b64, triggered_by: 'ase' }),
        });
        uiLog('heartbeat', 'screenshot uploaded');
      } catch (e) {
        uiLog('heartbeat', 'screenshot upload error', e.message);
      }
    },

    async toggleScreenCapture() {
      // Toggle persistent screen capture stream for browser-based ASE screenshots.
      // getDisplayMedia shows a one-time permission dialog; subsequent grabs are silent.
      if (this._screenStream) {
        this._screenStream.getTracks().forEach(t => t.stop());
        this._screenStream = null;
        this.screenShareActive = false;
        uiLog('VLM', 'screen capture stopped');
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getDisplayMedia({
          video: { cursor: 'always' },
          audio: false,
        });
        this._screenStream = stream;
        this.screenShareActive = true;
        // Auto-reset when user ends share via browser UI
        stream.getVideoTracks()[0].addEventListener('ended', () => {
          this._screenStream = null;
          this.screenShareActive = false;
          uiLog('VLM', 'screen share ended by user');
        });
        uiLog('VLM', 'screen capture started');
      } catch (e) {
        this.screenShareActive = false;
        uiLog('VLM', 'screen capture failed', e.message);
        this._setCaptureErrorStatus('屏幕共享失败：无法获取画面。可尝试更新显卡驱动、关闭多余显示器，或使用「上传图片」代替。');
      }
    },

    async _grabFrameFromStream() {
      // Capture a single JPEG frame from the active screen share stream.
      if (!this._screenStream) return null;
      try {
        const track = this._screenStream.getVideoTracks()[0];
        if (!track || track.readyState !== 'live') return null;

        if (typeof ImageCapture !== 'undefined') {
          const capture = new ImageCapture(track);
          const bitmap = await capture.grabFrame();
          const canvas = document.createElement('canvas');
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
          canvas.getContext('2d').drawImage(bitmap, 0, 0);
          return canvas.toDataURL('image/jpeg', 0.85).split(',')[1];
        }

        // Fallback: draw via video element (Firefox / older browsers)
        return await new Promise((resolve) => {
          const video = document.createElement('video');
          video.muted = true;
          video.srcObject = new MediaStream([track]);
          video.onloadedmetadata = () => {
            video.play().then(() => {
              const canvas = document.createElement('canvas');
              canvas.width = video.videoWidth;
              canvas.height = video.videoHeight;
              canvas.getContext('2d').drawImage(video, 0, 0);
              video.pause();
              video.srcObject = null;
              resolve(canvas.toDataURL('image/jpeg', 0.85).split(',')[1] || null);
            }).catch(() => resolve(null));
          };
          video.onerror = () => resolve(null);
          setTimeout(() => resolve(null), 4000);
        });
      } catch (e) {
        uiLog('VLM', 'grabFrame failed', e.message);
        return null;
      }
    },

    /* ─────────────────── TTS ─────────────────── */

    connectTTS() {
      if (this._ttsWs && this._ttsWs.readyState === WebSocket.OPEN) return;

      // Create AudioContext if needed; do NOT resume here — page may have loaded without user gesture (e.g. HTTPS tunnel).
      // Resume on first user interaction so autoplay policy doesn't block (see _attachAudioUnlock).
      if (!this._audioCtx) {
        this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        this._attachAudioUnlock();
      }

      try {
        this._ttsWs = new WebSocket(`${getWsUrl()}/ws/tts`);
        this._ttsWs.binaryType = 'arraybuffer';

        this._ttsWs.onopen = () => {
          this.statusText = 'TTS 已连接';
          const pending = this._ttsPendingReplayText;
          if (pending) {
            this._ttsPendingReplayText = null;
            this.sendToTTS(pending.text, 0, pending.profileId);
          }
        };

        this._ttsWs.onmessage = (event) => {
          if (this._ttsStopRequested) return; // user clicked stop — discard all in-flight segments (e.g. GPT-SoVITS)
          if (event.data && event.data.byteLength > 0) {
            this._audioQueue.push(event.data);
            // Accumulate a copy for the cache (slice avoids shared-buffer issues)
            if (this._ttsCachingKey) {
              this._ttsCacheAccumulator.push(event.data.slice(0));
            }
            if (!this.isPlayingAudio) this._playNext();
          }
        };

        this._ttsWs.onerror = () => {
          this.statusText = this.locale === 'en' ? 'TTS connection failed' : 'TTS 连接失败';
          this.ttsEnabled = false;
        };

        this._ttsWs.onclose = () => {
          if (this.ttsEnabled) this.statusText = this.locale === 'en' ? 'TTS disconnected' : 'TTS 已断开';
        };
      } catch (e) {
        this.statusText = `TTS WebSocket 错误: ${e.message}`;
      }
    },

    _attachAudioUnlock() {
      if (this._audioUnlockAttached || !this._audioCtx) return;
      this._audioUnlockAttached = true;
      const unlock = () => {
        if (this._audioCtx && this._audioCtx.state === 'suspended') {
          this._audioCtx.resume();
        }
        document.removeEventListener('click', this._audioUnlockRef);
        document.removeEventListener('touchstart', this._audioUnlockRef);
        document.removeEventListener('keydown', this._audioUnlockRef);
      };
      this._audioUnlockRef = unlock;
      document.addEventListener('click', unlock);
      document.addEventListener('touchstart', unlock);
      document.addEventListener('keydown', unlock);
    },

    disconnectTTS() {
      if (this._ttsWs) {
        this._ttsWs.close();
        this._ttsWs = null;
      }
      this._audioQueue = [];
      this.isPlayingAudio = false;
      if (this._audioCtx) {
        this._audioCtx.close();
        this._audioCtx = null;
      }
      this._audioUnlockAttached = false;
      if (this._audioUnlockRef) {
        document.removeEventListener('click', this._audioUnlockRef);
        document.removeEventListener('touchstart', this._audioUnlockRef);
        document.removeEventListener('keydown', this._audioUnlockRef);
        this._audioUnlockRef = null;
      }
    },

    stopTTSPlayback() {
      this._ttsStopRequested = true;
      // Clear queue and stop current audio source immediately
      this._audioQueue = [];
      if (this._currentAudioSource) {
        try { this._currentAudioSource.stop(); } catch (_) {}
        this._currentAudioSource = null;
      }
      this.isPlayingAudio = false;
      // Discard any partial cache accumulation on interrupt
      this._ttsCachingKey = null;
      this._ttsCacheAccumulator = [];
    },

    /** 群聊下根据消息的 sender 显示名解析出 profile_id，供 TTS 按人选音色。 */
    _getProfileIdForSender(senderDisplayName) {
      if (!this.currentGroupId || !this.currentGroup || !this.currentGroup.participants) return null;
      const p = this.currentGroup.participants.find(p => {
        const name = (p.name || '').trim() || this.sessions.find(s => s.id === p.profile_id)?.display_name || p.profile_id;
        return name === senderDisplayName;
      });
      return p ? p.profile_id : null;
    },

    sendToTTS(text, retryCount, profileId) {
      // 群聊下仅当带 profile_id（播放键）时才播；单聊无 profile_id 照常播
      if (this.currentGroupId && profileId == null) return;
      if (retryCount === undefined) retryCount = 0;
      const maxRetries = 5;
      if (!this._ttsWs || this._ttsWs.readyState !== WebSocket.OPEN) {
        if (retryCount >= maxRetries) {
          this.statusText = this.t('statusTTSFailed');
          return;
        }
        this.connectTTS();
        setTimeout(() => this.sendToTTS(text, retryCount + 1, profileId), 600);
        return;
      }
      // Interrupt any currently playing audio before starting new speech
      this.stopTTSPlayback();
      this._ttsStopRequested = false; // allow new segments to be queued
      // Begin caching for this text
      this._ttsCachingKey = text.trim();
      this._ttsCacheAccumulator = [];

      // ── Pre-strip multi-line constructs BEFORE sentence splitting ──
      // Code fences (```...```) span multiple lines; if we split first,
      // each line becomes its own sentence and the fence regex never matches.
      text = text
        .replace(/```[\s\S]*?```/g, '')     // fenced code blocks
        .replace(/^\|.*\|[ \t]*$/gm, '')    // markdown table rows
        .replace(/^[ \t]*[-=*_]{3,}[ \t]*$/gm, ''); // horizontal rules

      if (!text.trim()) return;

      // Split on sentence-ending punctuation first, then re-split long clauses on commas.
      // Deliberately exclude ".", "!", "?" to avoid cutting decimal numbers like "99.9%".
      // Second pass: clauses longer than 20 chars get split further on ，、； for faster synthesis.
      const rawSentences = text.match(/[^。！？\n]+[。！？\n]?/g) || [text];
      const sentences = [];
      for (const raw of rawSentences) {
        if (raw.length > 20) {
          const sub = raw.match(/[^，、；]+[，、；]?/g) || [raw];
          for (const s of sub) { if (s.trim()) sentences.push(s); }
        } else {
          if (raw.trim()) sentences.push(raw);
        }
      }
      for (const s of sentences) {
        if (!s.trim()) continue;
        if (profileId) {
          this._ttsWs.send(JSON.stringify({ text: s.trim(), profile_id: profileId }));
        } else {
          this._ttsWs.send(s.trim());
        }
      }
    },

    /* ─────────────────── STT 语音输入（方案 B：服务端 faster-whisper）────────────────── */

    async loadSttConfig() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sttConfig());
        if (res.ok) {
          const d = await res.json();
          this.sttConfig = { enabled: !!d.enabled, language: d.language || 'zh' };
        }
      } catch (e) {
        this.sttConfig = { enabled: true, language: 'zh' };
      }
    },

    async onSttServerEnabledToggle(ev) {
      const enabled = !!(ev && ev.target && ev.target.checked);
      const prev = !!this.sttConfig.enabled;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsSttEnabled(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
        });
        const j = await res.json().catch(() => ({}));
        if (!res.ok || j.ok === false) {
          if (ev && ev.target) ev.target.checked = prev;
          const msg = (j.detail || j.error || res.statusText || 'Save failed');
          if (this.showToast) this.showToast(String(msg), 'error');
          return;
        }
        await this.loadSttConfig();
        if (ev && ev.target) ev.target.checked = !!this.sttConfig.enabled;
        if (this.showToast) this.showToast(this.t('toastSttEnabledSaved'), 'success');
      } catch (e) {
        if (ev && ev.target) ev.target.checked = prev;
        if (this.showToast) this.showToast(String(e.message || e), 'error');
      }
    },

    async toggleVoiceInput() {
      if (!this.toolbarConfig.voiceInput || !this.toolbarConfig.voiceInput.enabled) return;
      if (!this.sttConfig.enabled) {
        this.showToast?.(this.t('toastSTTNotEnabled'), 'error') || (this.statusText = this.t('toastSTTNotEnabled'));
        return;
      }
      if (this.voiceRecording) {
        this._stopVoiceRecording();
        return;
      }
      await this._startVoiceRecording();
    },

    async _startVoiceRecording() {
      this._voiceChunks = [];
      this._voiceVadSilenceStart = null;
      this._voiceHadSpeech = false;
      this._voiceRecordStartTime = Date.now();
      this._sttBaseText = this.inputText;
      const silenceTimeoutMs = this.voiceSilenceTimeoutMs || 1500;
      const maxDurationMs = this.voiceMaxDurationMs || 30000;
      const minDurationMs = this.voiceMinDurationMs || 500;
      const vadEnabled = this.voiceVadEnabled;

      try {
        this._voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
        this._voiceMime = mime;
        this._mediaRecorder = new MediaRecorder(this._voiceStream);
        this._mediaRecorder.ondataavailable = (e) => {
          if (e.data && e.data.size > 0) this._voiceChunks.push(e.data);
        };
        this._mediaRecorder.onstop = () => {
          this._cleanupVoiceStream();
          const blob = new Blob(this._voiceChunks, { type: mime });
          this._voiceChunks = [];
          // 整段录音从未检测到声音：跳过 STT 请求，直接重启监听
          if (!this._voiceHadSpeech && this.voiceConversationMode) {
            this.statusText = this.t('statusReady');
            this._scheduleVoiceConversationNext(0);
            return;
          }
          if (blob.size > 0) this._sendAudioToStt(blob);
        };
        this._mediaRecorder.start(500);
        this.voiceRecording = true;
        this.statusText = this.t('statusListening');

        this._voiceMaxTimer = setTimeout(() => {
          if (this.voiceRecording) this._stopVoiceRecording();
        }, maxDurationMs);

        if (vadEnabled && window.AudioContext) {
          const ctx = new (window.AudioContext || window.webkitAudioContext)();
          const src = ctx.createMediaStreamSource(this._voiceStream);
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 256;
          analyser.smoothingTimeConstant = 0.8;
          src.connect(analyser);
          const data = new Uint8Array(analyser.frequencyBinCount);
          this._voiceVadIntervalId = setInterval(() => {
            if (!this.voiceRecording) return;
            analyser.getByteFrequencyData(data);
            const avg = data.reduce((a, b) => a + b, 0) / data.length;
            const elapsed = Date.now() - this._voiceRecordStartTime;
            if (avg < 15) {
              if (this._voiceVadSilenceStart == null) this._voiceVadSilenceStart = Date.now();
              if (Date.now() - this._voiceVadSilenceStart >= silenceTimeoutMs && elapsed >= minDurationMs)
                this._stopVoiceRecording();
            } else {
              this._voiceHadSpeech = true;
              this._voiceVadSilenceStart = null;
            }
          }, 200);
          this._voiceVadCtx = ctx;
        }
      } catch (e) {
        this.showToast?.('无法访问麦克风: ' + e.message, 'error') || (this.statusText = '麦克风不可用');
      }
    },

    _stopVoiceRecording() {
      if (!this.voiceRecording) return;
      this.voiceRecording = false;
      if (this._voiceConversationTimerId) {
        clearTimeout(this._voiceConversationTimerId);
        this._voiceConversationTimerId = null;
      }
      if (this._voiceMaxTimer) {
        clearTimeout(this._voiceMaxTimer);
        this._voiceMaxTimer = null;
      }
      if (this._voiceVadIntervalId) {
        clearInterval(this._voiceVadIntervalId);
        this._voiceVadIntervalId = null;
      }
      if (this._voiceVadCtx) {
        this._voiceVadCtx.close();
        this._voiceVadCtx = null;
      }
      if (this._mediaRecorder && this._mediaRecorder.state !== 'inactive') {
        this._mediaRecorder.stop();
      } else {
        this._cleanupVoiceStream();
      }
      this._mediaRecorder = null;
      this.statusText = this.t('statusReady');
    },

    _cleanupVoiceStream() {
      if (this._voiceStream) {
        this._voiceStream.getTracks().forEach(t => t.stop());
        this._voiceStream = null;
      }
    },

    _scheduleVoiceConversationNext(overrideDelayMs) {
      if (this._voiceConversationTimerId) clearTimeout(this._voiceConversationTimerId);
      const delayMs = overrideDelayMs !== undefined ? overrideDelayMs : (this.voiceConversationResumeDelayMs ?? 3500);
      this._voiceConversationTimerId = setTimeout(() => {
        this._voiceConversationTimerId = null;
        if (!this.voiceConversationMode || this.voiceRecording) return;
        if (!this.toolbarConfig.voiceInput?.enabled || !this.sttConfig.enabled) return;
        if (!this.currentSessionId) return;
        this._startVoiceRecording();
      }, delayMs);
    },

    /* ─────── 对话模式：Barge-in 监听 ─────── */

    /** 开启 barge-in 监听器：TTS 播放期间检测到麦克风有声音就立即打断。 */
    async _startBargeInMonitor() {
      if (this._bargeInMonitorId) return; // 已在运行
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        this._bargeInStream = stream;
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.7;
        src.connect(analyser);
        this._bargeInCtx = ctx;
        const data = new Uint8Array(analyser.frequencyBinCount);
        // 连续超阈值帧数，避免单帧噪音误触发
        let aboveCount = 0;
        this._bargeInMonitorId = setInterval(() => {
          if (!this.voiceConversationMode) { this._stopBargeInMonitor(); return; }
          if (!this.isPlayingAudio) { aboveCount = 0; return; } // 只在 TTS 播放期间检测
          if (this.voiceRecording) { aboveCount = 0; return; }  // 已在录音就跳过
          analyser.getByteFrequencyData(data);
          const avg = data.reduce((a, b) => a + b, 0) / data.length;
          if (avg > 18) {
            aboveCount++;
            if (aboveCount >= 2) { // 连续 2 帧（约 300ms）才触发
              aboveCount = 0;
              this._bargeIn();
            }
          } else {
            aboveCount = 0;
          }
        }, 150);
        uiLog('conv', 'barge-in monitor started');
      } catch (e) {
        uiLog('conv', 'barge-in monitor failed (mic permission?)', e.message);
      }
    },

    _stopBargeInMonitor() {
      if (this._bargeInMonitorId) {
        clearInterval(this._bargeInMonitorId);
        this._bargeInMonitorId = null;
      }
      if (this._bargeInCtx) {
        try { this._bargeInCtx.close(); } catch (_) {}
        this._bargeInCtx = null;
      }
      if (this._bargeInStream) {
        this._bargeInStream.getTracks().forEach(t => t.stop());
        this._bargeInStream = null;
      }
    },

    /** 打断当前 TTS 播放并立即开始录音。 */
    _bargeIn() {
      uiLog('conv', 'barge-in: interrupting TTS');
      this.stopTTSPlayback();
      if (!this.voiceRecording) {
        this._startVoiceRecording();
      }
    },

    /** 开启/关闭完整对话模式（带 barge-in）。 */
    async toggleConversationMode() {
      this.voiceConversationMode = !this.voiceConversationMode;
      localStorage.setItem('voiceConversationMode', String(this.voiceConversationMode));
      if (this.voiceConversationMode) {
        if (!this.sttConfig || !this.sttConfig.enabled) {
          this.showToast?.(this.t('toastSTTNotEnabled'), 'error') || (this.statusText = this.t('toastSTTNotEnabled'));
          this.voiceConversationMode = false;
          localStorage.setItem('voiceConversationMode', 'false');
          return;
        }
        // TTS 建议开启（barge-in 依赖 TTS 播放状态）
        if (!this.ttsEnabled) {
          this.statusText = this.locale === 'en' ? 'Tip: enable TTS for best conversation experience' : '提示：建议同时开启 TTS 以获得最佳对话体验';
        }
        await this._startBargeInMonitor();
        if (!this.voiceRecording && this.currentSessionId) {
          await this._startVoiceRecording();
        }
      } else {
        // 关闭对话模式
        this._stopBargeInMonitor();
        if (this.voiceRecording) {
          this._stopVoiceRecording();
        }
        if (this._voiceConversationTimerId) {
          clearTimeout(this._voiceConversationTimerId);
          this._voiceConversationTimerId = null;
        }
        this.statusText = this.t('statusReady');
      }
    },

    /** 将任意浏览器音频 Blob 解码并重新编码为 16-bit PCM WAV，避免服务端依赖 ffmpeg/torchcodec。
     *  复用单例 AudioContext，避免 Chrome 6-context 上限导致多次录音后 decodeAudioData 失败。 */
    async _blobToWav(blob) {
      if (!this._wavCtx || this._wavCtx.state === 'closed') {
        this._wavCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      const ctx = this._wavCtx;
      const arrayBuffer = await blob.arrayBuffer();
      const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
      const numCh = 1;
      const sr = audioBuffer.sampleRate;
      const pcm = audioBuffer.getChannelData(0);
      const dataLen = pcm.length * 2;
      const buf = new ArrayBuffer(44 + dataLen);
      const v = new DataView(buf);
      const s = (o, str) => { for (let i = 0; i < str.length; i++) v.setUint8(o + i, str.charCodeAt(i)); };
      s(0, 'RIFF'); v.setUint32(4, 36 + dataLen, true);
      s(8, 'WAVE'); s(12, 'fmt ');
      v.setUint32(16, 16, true); v.setUint16(20, 1, true);
      v.setUint16(22, numCh, true); v.setUint32(24, sr, true);
      v.setUint32(28, sr * numCh * 2, true); v.setUint16(32, numCh * 2, true);
      v.setUint16(34, 16, true);
      s(36, 'data'); v.setUint32(40, dataLen, true);
      let off = 44;
      for (let i = 0; i < pcm.length; i++) {
        const x = Math.max(-1, Math.min(1, pcm[i]));
        v.setInt16(off, x < 0 ? x * 0x8000 : x * 0x7fff, true);
        off += 2;
      }
      return new Blob([buf], { type: 'audio/wav' });
    },

    async _sendAudioToStt(blob) {
      this.statusText = this.t('statusRecognizing');
      const lang = this.voiceInputLang || this.sttConfig.language || 'zh';
      const params = new URLSearchParams({ lang });

      // SenseVoice 需要 WAV；用 Web Audio API 在前端转换
      let audioBlob;
      try {
        audioBlob = await this._blobToWav(blob);
      } catch (e) {
        uiLog('stt', 'wav convert failed', e.message);
        this.showToast?.('语音识别失败: 音频转换错误，请重试', 'error');
        if (this.voiceConversationMode) this._scheduleVoiceConversationNext(0);
        return;
      }

      const formData = new FormData();
      formData.append('audio', audioBlob, 'audio.wav');
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.stt() + '?' + params.toString(), {
          method: 'POST',
          body: formData,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          const err = data.error || res.statusText;
          this.showToast?.('语音识别失败: ' + err, 'error') || (this.statusText = err);
          if (this.voiceConversationMode) this._scheduleVoiceConversationNext(0);
          return;
        }
        const text = (data.text || '').trim();
        const baseText = this._sttBaseText ?? '';
        this._sttBaseText = '';
        if (!text) {
          // 无识别结果（静默/噪声）：立即重新监听，不等 resumeDelay
          this.inputText = baseText;
          this.statusText = this.t('statusReady');
          if (this.voiceConversationMode) this._scheduleVoiceConversationNext(0);
          return;
        }
        const mode = this.voiceConversationMode ? 'send' : (this.voiceInputResultMode || 'fill');
        if (mode === 'send') {
          this.inputText = text;
          await this.sendMessage();
          // 对话模式 + TTS 开启：不用固定延迟，等 _playNext() 队列清空后自动恢复录音
          // 对话模式 + TTS 关闭：用固定延迟（给 AI 一些时间回答）
          if (this.voiceConversationMode && !this.ttsEnabled) this._scheduleVoiceConversationNext();
        } else {
          // fill 模式：用 baseText + 最终识别结果（而非 partial+拼接）
          this.inputText = (baseText + (baseText && text ? ' ' : '') + text).trim();
        }
        this.statusText = this.t('statusReady');
      } catch (e) {
        this.showToast?.('语音识别请求失败: ' + e.message, 'error') || (this.statusText = '识别失败');
        if (this.voiceConversationMode) this._scheduleVoiceConversationNext(0);
      }
    },

    async _playNext() {
      if (this._audioQueue.length === 0) {
        this.isPlayingAudio = false;
        // Natural playback end — commit accumulated chunks to cache
        if (this._ttsCachingKey && this._ttsCacheAccumulator.length > 0) {
          const key = this._ttsCachingKey;
          this._ttsCache[key] = this._ttsCacheAccumulator.slice();
          this._ttsCacheOrder = this._ttsCacheOrder.filter(k => k !== key);
          this._ttsCacheOrder.push(key);
          // LRU eviction — keep max 10 entries
          while (this._ttsCacheOrder.length > 10) {
            delete this._ttsCache[this._ttsCacheOrder.shift()];
          }
        }
        this._ttsCachingKey = null;
        this._ttsCacheAccumulator = [];
        // 对话模式：TTS 播完后立即开始下一轮录音（无需等待固定延迟）
        if (this.voiceConversationMode && !this.voiceRecording && !this.isStreaming &&
            this.sttConfig && this.sttConfig.enabled && this.currentSessionId) {
          this._startVoiceRecording();
        }
        return;
      }
      this.isPlayingAudio = true;
      const data = this._audioQueue.shift();

      try {
        if (!this._audioCtx) {
          this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        // Re-resume in case context was auto-suspended (e.g. page went to background)
        if (this._audioCtx.state === 'suspended') {
          await this._audioCtx.resume();
        }
        const buffer = await this._audioCtx.decodeAudioData(data.slice(0));
        const source = this._audioCtx.createBufferSource();
        source.buffer = buffer;
        source.connect(this._audioCtx.destination);
        this._currentAudioSource = source;
        source.onended = () => {
          this._currentAudioSource = null;
          this._playNext();
        };
        source.start();
      } catch (e) {
        // Skip this chunk, play next
        this._playNext();
      }
    },

    /* ─────────────────── Electron IPC ─────────────────── */

    _setCaptureErrorStatus(message) {
      this.statusText = message;
      if (this._captureErrorResetTimer) clearTimeout(this._captureErrorResetTimer);
      this._captureErrorResetTimer = setTimeout(() => {
        this._captureErrorResetTimer = null;
        if (this.statusText === message) this.statusText = this.t('statusReady');
      }, 10000);
    },

    async takeScreenshot() {
      if (!IS_ELECTRON || !window.electronAPI) return;
      this.statusText = this.t('statusScreenshotting');
      try {
        const dataUrl = await window.electronAPI.captureScreen();
        if (!dataUrl) {
          this._setCaptureErrorStatus('截图失败：屏幕捕获不可用。可尝试更新显卡驱动、关闭多余显示器，或使用「上传图片」代替。');
          return;
        }
        // Store screenshot for VLM; description appended to next message automatically
        this.pendingScreenshot = dataUrl;
        this.pendingImageType = 'screenshot';
        uiLog('VLM', 'screenshot captured', {size: dataUrl.length});
        this.statusText = this.locale === 'en' ? 'Screenshot captured, will analyze on send' : '截图已捕获，发送消息时将自动分析画面';
      } catch (e) {
        this._setCaptureErrorStatus('截图失败：屏幕捕获不可用。可尝试更新显卡驱动、关闭多余显示器，或使用「上传图片」代替。');
      }
    },

    clearPendingImage() {
      this.pendingScreenshot = null;
      this.pendingImageType = 'screenshot';
    },

    onUploadImage(event) {
      const file = event.target.files && event.target.files[0];
      if (!file || !file.type.startsWith('image/')) return;
      const reader = new FileReader();
      reader.onload = (e) => {
        const dataUrl = e.target && e.target.result;
        if (dataUrl) {
          this.pendingScreenshot = dataUrl;
          this.pendingImageType = 'upload';
          this.statusText = this.locale === 'en' ? 'Image attached, will analyze on send' : '已附加图片，发送消息时将一并分析';
          uiLog('VLM', 'image uploaded', { type: file.type });
        }
      };
      reader.readAsDataURL(file);
      event.target.value = '';
    },

    /* ─────────────────── Helpers ─────────────────── */

    _now() {
      return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    },
    /** 气泡下时间戳：数字为 Unix 秒则格式化为本地时间，已是字符串则原样显示 */
    formatMsgTime(ts) {
      if (ts == null) return '';
      if (typeof ts === 'string') return ts;
      const ms = Number(ts);
      if (Number.isNaN(ms)) return '';
      const date = ms > 1e12 ? new Date(ms) : new Date(ms * 1000);
      return date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    },

    async copyMessage(content) {
      try {
        await navigator.clipboard.writeText(content);
      } catch (_) {
        // Fallback for environments without clipboard API
        const ta = document.createElement('textarea');
        ta.value = content;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
    },

    renderMarkdown(text) {
      if (!text) return '';
      try {
        return marked.parse(text);
      } catch (_) {
        return text.replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }
    },

    scrollToBottom() {
      const el = this.$refs.messagesArea;
      if (el) el.scrollTop = el.scrollHeight;
    },

    loadMoreHistory() {
      const el = this.$refs.messagesArea;
      const prevHeight = el ? el.scrollHeight : 0;
      this.msgDisplayCount = Math.min(this.msgDisplayCount + 60, this.messages.length);
      this.$nextTick(() => {
        if (el) el.scrollTop = el.scrollHeight - prevHeight;
      });
    },

    handleMessagesScroll(e) {
      const el = e.target;
      if (el.scrollTop < 80 && this.msgDisplayCount < this.messages.length) {
        this.loadMoreHistory();
      }
    },

    handleKeydown(e) {
      // Enter sends; Shift+Enter inserts newline
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.sendMessage();
      }
    },

    /* ─────────────────── Status Panel (P4) ─────────────────── */

    async fetchStatus() {
      const sid = this.currentGroupId ? this.selectedStatusProfileId : this.currentSessionId;
      if (!sid) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessionStatus(sid));
        if (!res.ok) return;
        const data = await res.json();
        this.currentStatus = data;
        const es = data.emotion_state;
        if (es) {
          this.editEnergy = Math.round(es.energy_level ?? 80);
          this.editEmotion = '';  // 默认"不修改"，用户主动选择才覆盖
        }
        this.editAffinityDelta = 0;
      } catch (_) {}
    },

    toggleStatusPanel() {
      this.showStatusPanel = !this.showStatusPanel;
      if (this.showStatusPanel) this.fetchStatus();
    },

    async fetchUserActivity() {
      const sid = this.currentGroupId ? this.selectedCadenceProfileId : this.currentSessionId;
      if (!sid) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessionUserActivity(sid));
        if (!res.ok) return;
        this.userActivity = await res.json();
      } catch (_) {
        this.userActivity = {};
      }
    },

    toggleCadencePanel() {
      this.showCadencePanel = !this.showCadencePanel;
      if (this.showCadencePanel) this.fetchUserActivity();
    },

    async saveStatusAdjust() {
      const sid = this.currentGroupId ? this.selectedStatusProfileId : this.currentSessionId;
      if (!sid) return;
      try {
        const body = { energy_level: this.editEnergy };
        if (this.editEmotion) body.primary_emotion = this.editEmotion;
        if (this.editAffinityDelta !== 0 && this.currentStatus.affinity_state) {
          body.affinity = (this.currentStatus.affinity_state.affinity || 0) + this.editAffinityDelta;
        }
        const res = await fetch(getBaseUrl() + API_PATHS.sessionStatus(sid), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
          this.statusText = '✓ 状态已更新';
          this.editAffinityDelta = 0;
          await this.fetchStatus();
        } else {
          this.statusText = `更新失败: ${data.error || ''}`;
        }
      } catch (e) {
        this.statusText = `更新失败: ${e.message}`;
      }
    },

    /* ─────────────────── ASE / Reflection panel ─────────────────── */

    async fetchAseStatus() {
      if (!this.currentSessionId) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessionAseStatus(this.currentSessionId));
        if (!res.ok) return;
        this.aseStatus = await res.json();
      } catch (_) {}
    },

    toggleAsePanel() {
      this.showAsePanel = !this.showAsePanel;
      // Always clear and restart timer at the appropriate interval:
      // 5s while panel is open, 30s in background when closed.
      if (this._asePollTimer) { clearInterval(this._asePollTimer); this._asePollTimer = null; }
      if (this.showAsePanel) {
        this.fetchAseStatus();
        this._asePollTimer = setInterval(() => this.fetchAseStatus(), 5000);
      } else {
        this._asePollTimer = setInterval(() => this.fetchAseStatus(), 30000);
      }
    },

    /* ─────────────────── P5-C: Toolbar config ─────────────────── */

    _loadToolbarConfig() {
      const saved = localStorage.getItem('toolbarConfig');
      if (saved) {
        try {
          const parsed = JSON.parse(saved);
          // Merge saved enabled flags onto defaults (preserves new keys)
          for (const key of Object.keys(this.toolbarConfig)) {
            if (parsed[key] !== undefined) {
              this.toolbarConfig[key].enabled = parsed[key].enabled ?? this.toolbarConfig[key].enabled;
            }
          }
        } catch (_) {}
      }
    },

    _saveToolbarConfig() {
      localStorage.setItem('toolbarConfig', JSON.stringify(this.toolbarConfig));
    },

    toggleToolbarBtn(key) {
      this.toolbarConfig[key].enabled = !this.toolbarConfig[key].enabled;
      this._saveToolbarConfig();
    },

    /* ─────────────────── P5-C: Todos panel ─────────────────── */

    async refreshTodos() {
      if (!this.currentSessionId) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessionTodos(this.currentSessionId));
        if (res.ok) this.todos = (await res.json()).todos || [];
      } catch (_) {}
    },

    toggleTodosPanel() {
      this.showTodosPanel = !this.showTodosPanel;
      if (this.showTodosPanel) {
        this.showTimerPanel = false;
        this.showCommandsPanel = false;
        this.showToolbarConfig = false;
        this.refreshTodos();
      }
    },

    async addTodo() {
      const text = (this.newTodoText || '').trim();
      if (!text || !this.currentSessionId) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessionTodos(this.currentSessionId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: text }),
        });
        if (!res.ok) { uiWarn('todos', 'addTodo failed', res.status); return; }
        this.newTodoText = '';
        await this.refreshTodos();
      } catch (e) { uiWarn('todos', 'addTodo error', e.message); }
    },

    async toggleTodo(id) {
      if (!this.currentSessionId) return;
      const t = this.todos.find(t => t.id === id);
      if (!t) return;
      try {
        await fetch(getBaseUrl() + API_PATHS.sessionTodoById(this.currentSessionId, id), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ done: !t.done }),
        });
        await this.refreshTodos();
      } catch (_) {}
    },

    async deleteTodo(id) {
      if (!this.currentSessionId) return;
      try {
        await fetch(getBaseUrl() + API_PATHS.sessionTodoById(this.currentSessionId, id), { method: 'DELETE' });
        await this.refreshTodos();
      } catch (_) {}
    },

    /* ─────────────────── P5-C: Timer panel ─────────────────── */

    toggleTimerPanel() {
      this.showTimerPanel = !this.showTimerPanel;
      if (this.showTimerPanel) {
        this.showTodosPanel = false;
        this.showCommandsPanel = false;
        this.showToolbarConfig = false;
        this.refreshTimers();
        // 每秒用 end_timestamp 与本地时间计算剩余，避免仅靠轮询导致数字静止
        if (!this._timerRefreshId) {
          this._timerRefreshId = setInterval(() => {
            if (this.showTimerPanel) this._tickTimerDisplay();
          }, 1000);
        }
      } else {
        clearInterval(this._timerRefreshId);
        this._timerRefreshId = null;
      }
    },

    /** 根据服务端返回的 end_timestamp 更新剩余秒数（不请求网络） */
    _tickTimerDisplay() {
      const list = this.timerList;
      if (!list || !list.length) return;
      const now = Date.now() / 1000;
      let needSync = false;
      for (const t of list) {
        const end = t.end_timestamp != null ? Number(t.end_timestamp) : (now + (t.remaining || 0));
        if (t.end_timestamp == null) t.end_timestamp = end;
        const prev = t.remaining;
        const rem = Math.max(0, Math.floor(end - now));
        t.remaining = rem;
        if (prev > 0 && rem === 0) needSync = true;
      }
      if (needSync) this.refreshTimers();
    },

    async refreshTimers() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.timers());
        if (!res.ok) return;
        const raw = (await res.json()).timers || [];
        const now = Date.now() / 1000;
        this.timerList = raw.map((t) => {
          const end = t.end_timestamp != null ? Number(t.end_timestamp) : (now + (t.remaining || 0));
          const rem = Math.max(0, Math.floor(end - now));
          return { ...t, end_timestamp: end, remaining: rem };
        });
      } catch (_) {}
    },

    async createTimer() {
      const secs = parseInt(this.newTimerSeconds);
      const label = (this.newTimerLabel || '').trim() || '计时器';
      if (!secs || secs <= 0) return;
      const sessionId = this.currentGroupId ? 'group:' + this.currentGroupId : (this.currentSessionId || '');
      try {
        await fetch(getBaseUrl() + API_PATHS.timers(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ seconds: secs, label, session_id: sessionId }),
        });
        this.newTimerLabel = '';
        this.newTimerSeconds = 60;
        await this.refreshTimers();
      } catch (_) {}
    },

    async cancelTimer(id) {
      try {
        await fetch(getBaseUrl() + API_PATHS.timerById(id), { method: 'DELETE' });
        await this.refreshTimers();
      } catch (_) {}
    },

    fmtRemaining(secs) {
      if (secs <= 0) return '已到期';
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      const s = secs % 60;
      if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
      return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    },

    /* ─────────────────── P5-C: Commands panel ─────────────────── */

    toggleCommandsPanel() {
      this.showCommandsPanel = !this.showCommandsPanel;
      if (this.showCommandsPanel) {
        this.showTodosPanel = false;
        this.showTimerPanel = false;
        this.showToolbarConfig = false;
      }
    },

    insertCommand(cmd) {
      this.inputText = cmd;
      this.showCommandsPanel = false;
      this.$nextTick(() => this.$refs.chatTextarea?.focus());
    },

    /* ─────────────────── P5-B: Bubble operations ─────────────────── */

    // Map display index → absolute index in this.messages[]
    _absoluteIdx(displayIdx) {
      const offset = Math.max(0, this.messages.length - this.msgDisplayCount);
      return offset + displayIdx;
    },

    // Map absolute messages[] index → store (chat_records.json) index
    // system_notice entries are frontend-only and must be excluded from the count
    _storeIdx(absIdx) {
      const hasNotice = this.messages.length > 0 && this.messages[0].role === 'system_notice';
      return (this._historyOffset || 0) + absIdx - (hasNotice ? 1 : 0);
    },

    async deleteMessage(displayIdx) {
      const absIdx = this._absoluteIdx(displayIdx);
      const msg = this.messages[absIdx];
      if (!msg) return;
      const storeIdx = this._storeIdx(absIdx);
      if (this.currentGroupId) {
        try {
          await fetch(getBaseUrl() + API_PATHS.groupMessageByStoreIdx(this.currentGroupId, storeIdx), { method: 'DELETE' });
        } catch (_) {}
      } else if (this.currentSessionId) {
        try {
          await fetch(getBaseUrl() + API_PATHS.sessionMessagesDelete(this.currentSessionId, storeIdx), { method: 'DELETE' });
        } catch (_) {}
      }
      this.messages.splice(absIdx, 1);
    },

    editMessage(displayIdx) {
      const absIdx = this._absoluteIdx(displayIdx);
      const msg = this.messages[absIdx];
      if (!msg) return;
      this.inputText = msg.content;
      const storeIdx = this._storeIdx(absIdx);
      this.messages.splice(absIdx);
      if (this.currentGroupId) {
        fetch(getBaseUrl() + API_PATHS.groupMessagesFrom(this.currentGroupId, storeIdx), { method: 'DELETE' }).catch(() => {});
      } else if (this.currentSessionId) {
        fetch(getBaseUrl() + API_PATHS.sessionMessagesFrom(this.currentSessionId, storeIdx), { method: 'DELETE' }).catch(() => {});
      }
      this.$nextTick(() => this.$refs.chatTextarea?.focus());
    },

    async rewriteMessage(displayIdx) {
      const absIdx = this._absoluteIdx(displayIdx);
      let userAbsIdx = -1;
      let userMsg = null;
      for (let i = absIdx - 1; i >= 0; i--) {
        if (this.messages[i].role === 'user') { userAbsIdx = i; userMsg = this.messages[i]; break; }
      }
      if (!userMsg) return;

      const userStoreIdx = this._storeIdx(userAbsIdx);
      this.messages.splice(userAbsIdx);
      if (this.currentGroupId) {
        try {
          await fetch(getBaseUrl() + API_PATHS.groupMessagesFrom(this.currentGroupId, userStoreIdx), { method: 'DELETE' });
        } catch (_) {}
      } else if (this.currentSessionId) {
        try {
          await fetch(getBaseUrl() + API_PATHS.sessionMessagesFrom(this.currentSessionId, userStoreIdx), { method: 'DELETE' });
        } catch (_) {}
      }
      this.inputText = userMsg.content;
      await this.sendMessage();
    },

    toggleCollapse(displayIdx) {
      const absIdx = this._absoluteIdx(displayIdx);
      const msg = this.messages[absIdx];
      if (msg) msg.collapsed = !msg.collapsed;
    },

    replayTTS(displayIdx) {
      if (!this.ttsEnabled) {
        const prev = this.statusText;
        this.statusText = '请先开启语音（🔊）';
        setTimeout(() => { this.statusText = prev; }, 2000);
        return;
      }
      const absIdx = this._absoluteIdx(displayIdx);
      const msg = this.messages[absIdx];
      if (!msg) return;
      const cacheKey = msg.content.trim();
      const cached = this._ttsCache[cacheKey];
      if (cached && cached.length > 0) {
        // Cache hit — replay from stored chunks without calling TTS model
        this.stopTTSPlayback();
        this._ttsCachingKey = null; // don't re-cache a replay
        cached.forEach(chunk => this._audioQueue.push(chunk.slice(0)));
        if (!this.isPlayingAudio) this._playNext();
      } else {
        // 无缓存重播：先断连再重连并发送，避免后端仍在发「上一批」残留句子的音频导致听起来从「下一句」开始（Qwen / GPT-SoVITS 同逻辑）
        const profileId = this.currentGroupId && msg.sender ? this._getProfileIdForSender(msg.sender) : undefined;
        this.stopTTSPlayback();
        if (this._ttsWs && this._ttsWs.readyState === WebSocket.OPEN) {
          this._ttsWs.close();
          this._ttsWs = null;
        }
        this._ttsPendingReplayText = { text: msg.content.trim(), profileId };
        this.connectTTS();
      }
    },
  },

  watch: {
    currentSessionId(newId) {
      // Start background ASE poll (30s) as soon as a session becomes active.
      if (newId && !this._asePollTimer) {
        this._asePollTimer = setInterval(() => this.fetchAseStatus(), 30000);
      }
    },
    ttsEnabled(val) {
      localStorage.setItem('ttsEnabled', val);
      if (val) {
        this.connectTTS();
      } else {
        this.disconnectTTS();
      }
    },
  },

  computed: {
    /** 群组列表：置顶在前（按 pinnedGroupIds 顺序），其余按 created_at 降序 */
    sortedGroups() {
      const list = this.groups || [];
      const pinned = this.pinnedGroupIds || [];
      const pinnedSet = new Set(pinned);
      const a = pinned.filter(id => list.some(g => g.group_id === id)).map(id => list.find(g => g.group_id === id));
      const b = list.filter(g => !pinnedSet.has(g.group_id)).sort((x, y) => (y.created_at || 0) - (x.created_at || 0));
      return [...a, ...b];
    },
    isGroupChat() {
      return !!this.currentGroupId;
    },
    mainTitle() {
      return this.isGroupChat ? (this.currentGroup && this.currentGroup.display_name) || this.currentGroupId : this.currentSessionName;
    },
    mainInitial() {
      return (this.mainTitle || '?').charAt(0).toUpperCase();
    },
    /** 群聊下 sender 显示名 → 头像 URL（从 participants.profile_id 对应 session 的 avatar） */
    groupSenderToAvatar() {
      if (!this.currentGroup || !Array.isArray(this.currentGroup.participants)) return {};
      const m = {};
      for (const p of this.currentGroup.participants) {
        const name = (p.name || '').trim() || this.sessions.find(s => s.id === p.profile_id)?.display_name || p.profile_id;
        const avatar = this.sessions.find(s => s.id === p.profile_id)?.avatar || '';
        if (name) m[name] = avatar;
      }
      return m;
    },
    currentSessionInitial() {
      return (this.currentSessionName || '?').charAt(0).toUpperCase();
    },
    /** 当前查看状态的人格 ID（单聊=当前 session，群聊=选择器选中） */
    statusProfileId() {
      return this.currentGroupId ? this.selectedStatusProfileId : this.currentSessionId;
    },
    /** 当前查看状态的人格显示名（用于状态面板标题） */
    statusProfileDisplayName() {
      if (!this.currentGroupId) return this.currentSessionName || '';
      if (!this.selectedStatusProfileId || !this.currentGroup || !this.currentGroup.participants) return '';
      const p = this.currentGroup.participants.find(x => x.profile_id === this.selectedStatusProfileId);
      return (p && ((p.name || '').trim() || this.sessions.find(s => s.id === p.profile_id)?.display_name)) || this.selectedStatusProfileId;
    },
    /** 频率面板当前查看的人格 ID */
    cadenceProfileId() {
      return this.currentGroupId ? this.selectedCadenceProfileId : this.currentSessionId;
    },
    /** 频率面板当前人格显示名 */
    cadenceProfileDisplayName() {
      if (!this.currentGroupId) return this.currentSessionName || '';
      if (!this.selectedCadenceProfileId || !this.currentGroup || !this.currentGroup.participants) return '';
      const p = this.currentGroup.participants.find(x => x.profile_id === this.selectedCadenceProfileId);
      return (p && ((p.name || '').trim() || this.sessions.find(s => s.id === p.profile_id)?.display_name)) || this.selectedCadenceProfileId;
    },
    /** 上次聊天时间文案（与 time_context 一致），放 computed 避免模板里当方法引用显示成 function） */
    lastUserActivityLabel() {
      const ts = this.userActivity && this.userActivity.last_user_ts;
      if (ts == null) return '尚未有过对话记录';
      const s = (Date.now() / 1000) - ts;
      const min = Math.floor(s / 60);
      if (min < 2) return '刚刚';
      if (min < 60) return min + ' 分钟前';
      const h = Math.floor(min / 60);
      if (h < 24) return h + ' 小时前';
      return Math.floor(h / 24) + ' 天前';
    },
    hasMessages() {
      return this.messages.length > 0 || this.isStreaming;
    },
    displayMessages() {
      const offset = Math.max(0, this.messages.length - this.msgDisplayCount);
      return this.messages.slice(offset);
    },
    hasMoreHistory() {
      return this.messages.length > this.msgDisplayCount;
    },
    /** 群组设置弹窗内：按当前筛选条件过滤后的搜索结果（仅弹窗内使用） */
    filteredGroupSettingsRecordResults() {
      const isSearching = (this.groupSettingsRecordSearchQuery || '').trim().length > 0;
      const list = isSearching ? (this.groupSettingsRecordSearchResults || []) : (this.groupSettingsRecordList || []);
      const senderFilter = (this.groupSettingsRecordFilter || '').trim();
      if (!senderFilter) return list;
      if (senderFilter === '用户') return list.filter(m => (m.role || '') === 'user');
      return list.filter(m => (m.sender || '').trim() === senderFilter);
    },
    /** 群聊下按 sender 筛选后的消息列表（用于「从此处重新开始」等需遍历时） */
    filteredGroupMessages() {
      if (!this.isGroupChat || !this.groupChatSenderFilter) return this.displayMessages;
      return this.displayMessages.filter(m => (m.sender || (m.role === 'user' ? '用户' : '')) === this.groupChatSenderFilter);
    },
    emotionLayers() {
      const es = this.currentStatus.emotion_state;
      if (!es) return [];
      if (Array.isArray(es.emotion_layers) && es.emotion_layers.length) {
        return es.emotion_layers.slice(0, 3);
      }
      // Fallback from legacy fields
      const layers = [];
      if (es.primary_emotion) layers.push({ emotion: es.primary_emotion, intensity: es.primary_weight ?? 1.0 });
      if (es.secondary_emotion) layers.push({ emotion: es.secondary_emotion, intensity: es.secondary_weight ?? 0.3 });
      if (es.tertiary_emotion) layers.push({ emotion: es.tertiary_emotion, intensity: es.tertiary_weight ?? 0.2 });
      return layers;
    },
    statusSummary() {
      const es = this.currentStatus.emotion_state;
      if (!es) return '状态';
      const topEmotion = this.emotionLayers[0] ? this.emotionLayers[0].emotion : '-';
      return `${topEmotion} E:${Math.round(es.energy_level ?? 80)}`;
    },
    energyPct() {
      const es = this.currentStatus.emotion_state;
      return es ? Math.min(100, Math.max(0, Math.round(es.energy_level ?? 80))) : 80;
    },
    currentEmotionLabel() {
      if (!this.emotionLayers.length) return '-';
      // Show top 2 emotions in toolbar label
      if (this.emotionLayers.length >= 2) {
        return `${this.emotionLayers[0].emotion}/${this.emotionLayers[1].emotion}`;
      }
      return this.emotionLayers[0].emotion;
    },

    aseOverallLabel() {
      const map = {
        disabled:          '已禁用',
        daily_limit:       '今日已满',
        consecutive_limit: '等待回应',
        waiting_silence:   '等待沉默',
        waiting_urgency:   '紧迫度不足',
        waiting_cooldown:  '冷却中',
        waiting_check:     '待检测',
        ready:             '发言中',
        unknown:           '—',
      };
      return map[this.aseStatus.overall_status] || '—';
    },
    aseOverallStyle() {
      const s = this.aseStatus.overall_status;
      if (s === 'ready') return 'background:var(--green,#4caf50);color:#fff;';
      if (s === 'waiting_check') return 'background:#8bc34a;color:#fff;';
      if (s === 'disabled' || s === 'daily_limit' || s === 'consecutive_limit')
        return 'background:var(--accent);color:#fff;';
      return 'background:var(--accent-dim);color:var(--text-secondary);';
    },
  },
};

const vueApp = Vue.createApp(App);
vueApp.mount('#app');
