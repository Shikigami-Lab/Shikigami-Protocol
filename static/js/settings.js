// ─────────────────────────────────────────────────────────────────────────────
// Settings Mixin — mixed into the main Vue App via  mixins: [SettingsMixin]
// Loaded BEFORE app.js. getBaseUrl() is defined in app.js (window.getBaseUrl) and used at call time for HTTPS/ws.
// method bodies at call time (both are global non-module scripts).
// ─────────────────────────────────────────────────────────────────────────────

const EMOTION_KEYS_FALLBACK = [
  'calm', 'joyful', 'excited', 'confident', 'gentle', 'grateful',
  'nostalgic', 'thoughtful', 'dreamy', 'concerned', 'playful_teasing',
  'sad', 'disappointed', 'angry', 'sarcastic', 'vulnerable', 'tired',
];

const PRESET_TEMPLATES = {
  openai: {
    name: 'openai_new', type: 'openai_compat',
    base_url: '', model: 'gpt-4o-mini',
    temperature: 0.9, temperature_enabled: true,
    top_p: 0.95, top_p_enabled: true,
    presence_penalty: 0.5, presence_penalty_enabled: true,
    frequency_penalty: 0.4, frequency_penalty_enabled: true,
  },
  gemini: {
    name: 'gemini_new', type: 'openai_compat',
    base_url: 'https://generativelanguage.googleapis.com/v1beta/openai/',
    model: 'gemini-2.0-flash',
    temperature: 0.9, temperature_enabled: true,
    top_p: 0.95, top_p_enabled: true,
    presence_penalty: 0.5, presence_penalty_enabled: false,
    frequency_penalty: 0.4, frequency_penalty_enabled: false,
  },
  ollama: {
    name: 'ollama_new', type: 'openai_compat',
    base_url: 'http://localhost:11434/v1', model: 'qwen2.5:7b',
    temperature: 0.9, temperature_enabled: true,
    top_p: 0.95, top_p_enabled: true,
    presence_penalty: 0.5, presence_penalty_enabled: false,
    frequency_penalty: 0.4, frequency_penalty_enabled: false,
  },
  lmstudio: {
    name: 'lmstudio_new', type: 'openai_compat',
    base_url: 'http://localhost:1234/v1', model: '',
    temperature: 0.9, temperature_enabled: true,
    top_p: 0.95, top_p_enabled: true,
    presence_penalty: 0.5, presence_penalty_enabled: false,
    frequency_penalty: 0.4, frequency_penalty_enabled: false,
  },
  custom: {
    name: 'custom_new', type: 'openai_compat',
    base_url: '', model: '',
    temperature: 0.9, temperature_enabled: true,
    top_p: 0.95, top_p_enabled: true,
    presence_penalty: 0.5, presence_penalty_enabled: true,
    frequency_penalty: 0.4, frequency_penalty_enabled: true,
  },
};

const SettingsMixin = {
  data() {
    return {
      // ── Toast stack (fixed-position, visible over settings overlay too) ──
      toasts: [],   // [{ id, msg, type }]

      // ── Engine health warnings (polled from /api/engine_warnings) ──
      engineWarning: '',

      // ── Canvas state ──
      settingsOpen: false,
      activeTab: 'profiles',
      settingsTabs: [
        { id: 'onboarding', labelZh: '新手引导', labelEn: 'First steps', shortEn: 'Guide', icon: '◇' },
        { id: 'profiles',   labelZh: '人格',  labelEn: 'Profiles',   shortEn: 'Profile', icon: '◆' },
        { id: 'llm',        labelZh: '模型',  labelEn: 'Models',     shortEn: 'Models',  icon: '◈' },
        { id: 'tts',        labelZh: '语音',  labelEn: 'Voice',      shortEn: 'Voice',   icon: '♪' },
        { id: 'behavior',   labelZh: '行为',  labelEn: 'Behavior',   shortEn: 'Behavior', icon: '◎' },
        { id: 'memory',     labelZh: '记忆',  labelEn: 'Memory',     shortEn: 'Memory',  icon: '◫' },
        { id: 'tools',      labelZh: '工具',  labelEn: 'Tools',      shortEn: 'Tools',   icon: '⚙' },
        { id: 'system',     labelZh: '系统',  labelEn: 'System',     shortEn: 'System',  icon: '≡' },
      ],
      // 标准情绪键（由 GET /settings/emotion_keys 填充；私库含扩展键）
      emotionStandardKeys: [],

      // 记忆面板 — 现已整合进 canvas（memory tab），保留变量以兼容现有逻辑
      memoryPanelOpen: false,
      memoryPanelTab: 'content',  // 'content' | 'settings'
      memoryPanelLoading: false,  // 记忆内容/人格切换时加载中
      // Persona canvas state
      personaEmotionState: null,   // { primary_emotion, energy_level, user_sentiment }
      personaAffinityState: null,  // { affinity, status, total_interactions }

      // ── LLM Tab ──
      llmPresets: {},       // { presetName: {base_url, model, has_key, ...} }
      llmActivePreset: '',
      selectedPresetName: '',
      llmShowKey: false,
      llmShowNewMenu: false,
      presetForm: {
        name: '', type: 'custom',
        api_key: '',
        base_url: '', model: '',
        temperature: 0.9, temperature_enabled: true,
        top_p: 0.95, top_p_enabled: true,
        presence_penalty: 0.5, presence_penalty_enabled: true,
        frequency_penalty: 0.4, frequency_penalty_enabled: true,
        max_tokens: null, max_tokens_enabled: false,
        extra_body_raw: '',
      },
      presetSaving: false,
      llmTestResult: null,   // { presetName, ok, latency_ms, error }
      llmTestLoading: false,

      // ── 入门 / 环境自检 ──
      setupGuide: null,
      setupGuideLoading: false,
      onboardingGptsovitsDir: '',
      setupVerifyResult: null,
      setupVerifyLoading: false,
      setupDownloadPollId: null,

      // ── TTS Tab ──
      ttsVoices: [],
      kokoroVoiceGroups: {},
      kokoroVoicesLoaded: false,
      sttModelPath: '',
      ttsForm: {
        engine: 'edge_tts',
        // Edge TTS
        voice: 'zh-CN-XiaoxiaoNeural', rate_pct: 0, language: 'zh-CN',
        // GPT-SoVITS
        gptsovits_host: '127.0.0.1', gptsovits_port: 9880, gptsovits_dir: '',
        gptsovits_text_lang: 'zh', gptsovits_prompt_lang: 'zh',
        gptsovits_speed: 1.0, gptsovits_temperature: 1.0,
        gptsovits_top_p: 1.0, gptsovits_top_k: 15, gptsovits_repetition_penalty: 1.35,
        gptsovits_ref_audio_path: '', gptsovits_prompt_text: '',
        // KokoroTTS
        kokoro_voice: '', kokoro_lang: 'zh', kokoro_speed: 1.0, kokoro_auto_detect_lang: true,
        // Qwen3-TTS
        qwen3_mode: 'custom_voice',
        qwen3_model_id: '', qwen3_device: 'cuda:0', qwen3_dtype: 'bfloat16',
        qwen3_language: 'Chinese', qwen3_speaker: 'Vivian',
        qwen3_instruct: '', qwen3_voice_description: '',
        qwen3_ref_audio_path: '', qwen3_ref_text: '',
        qwen3_temperature: 0.9, qwen3_top_p: 1.0, qwen3_top_k: 50, qwen3_repetition_penalty: 1.05,
        qwen3_attn_implementation: 'eager', qwen3_use_torch_compile: false,
        qwen3_use_sentence_chunking: false, qwen3_sentence_max_chars: 0,
      },
      ttsLoading: false,
      ttsSubTab: 'tts',   // 'tts' | 'stt'
      ttsAdvancedOpen: { gptsovits: false, qwen3: false },
      ttsTestState: null, // null | 'loading' | 'ok' | 'error'
      ttsSaveState: null, // null | 'saving' | 'saved'
      _ttsFormLoaded: false,
      _ttsSaveTimer: null,

      // ── Profiles Tab ──
      profiles: [],
      selectedProfileId: '',
      profileForm: {
        profile_id: '', display_name: '', base_prompt: '',
        style_constraint: '', avatar: '',
        lorebook_ref: '',  // lorebooks/<id>.json，空=不绑定文件（可用内嵌世界书）
        user_persona: { name: '', introduction: '' },
      },
      // ── 用户画像（AI 维护）— 扁平结构（仿 personaEvolutionForm 模式）─────────────
      userPortraitForm: {
        content: '',
        updated_at: 0,
        refresh_count: 0,
        changelog_count: 0,
        enabled: true,
        auto_refresh_in_daily_job: true,
        min_turns_for_burst_refresh: 100,
        min_hours_between_refresh: 6,
        use_day_summary_as_input: true,
        use_facts_as_input: true,
        max_input_conv_turns: 60,
      },
      _userPortraitFormLoaded: false,
      _userPortraitEditSaveTimer: null,
      _userPortraitConfigSaveTimer: null,
      userPortraitRefreshing: false,
      userPortraitResetting: false,
      userPortraitShowAdvanced: false,
      userPortraitShowChangelog: false,
      userPortraitChangelog: [],
      profileSaving: false,
      profileAllSaving: false,
      _profileFormLoaded: false,
      _profileSaveTimer: null,
      importingTavern: false,
      selectedProfileGroupId: '',  // 分组占位，后续可扩展
      profileSubTab: 'basic',
      /** 设置页左侧人格列表拖拽排序（与主界面侧栏 session 列表逻辑一致） */
      profileDragging: false,
      profileDragOverId: null,
      profileDragActiveId: null,

      // ── Tools sub-tab ──
      toolsList: [],            // [{tool_id, label, type, description, config, ...}]
      toolsLoading: false,
      toolsSaving: {},          // { tool_id: bool }
      trendStatus: { unused_count: 0, total_count: 0, last_fetched_at: null },
      trendFetching: false,
      trendNewSource: { type: 'rss', label: '', url: '', query: '' },
      weatherStatus: { available: false },
      trendPresetSourcesZh: [
        { label: 'B站热搜', type: 'api', url: 'https://s.search.bilibili.com/main/hotword' },
        { label: '少数派',  type: 'rss', url: 'https://sspai.com/feed' },
      ],
      trendPresetSourcesEn: [
        { label: 'Hacker News',      type: 'rss', url: 'https://hnrss.org/frontpage' },
        { label: 'Reddit Popular',   type: 'rss', url: 'https://www.reddit.com/r/popular/.rss' },
        { label: 'Reddit WorldNews', type: 'rss', url: 'https://www.reddit.com/r/worldnews/.rss' },
      ],
      profilePromptForm: {
        emotion_prompts: {},         // emotionKey → multiline string (array joined with \n)
        emotion_zh_descriptions: {}, // emotionKey → one-line description for TTS
        energy_prompts: { '0': '', '10': '', '30': '', '60': '', '80': '' },
        affinity_prompts: { '-100': '', '0': '', '200': '', '400': '', '600': '', '800': '', '1000': '', '1200': '' },
      },
      emotionPromptsExpanded: false,
      energyPromptsExpanded: false,
      affinityPromptsExpanded: false,
      lorebooksList: [],
      lorebooksListLoading: false,
      lorebookEditId: '',
      lorebookEditForm: {
        display_name: '',
        scan_turns: 10,
        default_entry_mode: 'keyword', // 条目设为「继承」时使用：keyword | constant
        entries: [], // { _key, label, keysText, content, enabled, mode: inherit|keyword|constant }
      },
      lorebookLibrarySaving: false,
      lorebookLibraryDeleting: false,
      lorebookImporting: false,
      showLorebookCreateModal: false,
      lorebookCreateNameDraft: '',
      lorebookCreateSaving: false,
      showLorebookDeleteConfirm: false,
      reflPromptExpanded: false,
      memPromptExpanded: false,
      profileWizardOpen: false,
      profileWizardDesc: '',
      profileWizardGenerating: false,
      profileWizardResult: null,
      profileWizardError: '',
      profileWizardPresetUsed: '',
      profileCreateModalOpen: false,
      profileCreateNameDraft: '',
      profileCreateSaving: false,
      promptAutofillBusy: false,
      promptAutofillAllowOverwrite: false,
      promptAutofillErrorRaw: '',
      promptAutofillShowDebug: false,
      specialDates: [],
      newSpecialDate: { date: '', label: '' },
      _specialDatesLoaded: false,
      _specialDatesSaveTimer: null,

      // ── Inline action confirmation (replaces window.confirm to avoid Electron focus-loss) ──
      confirmingAction: null,  // { key, label, data }

      // ── Segment config (Prompt 增强) ──
      segmentsExpanded: false,
      segmentList: [],              // [{segment_id, label, description, is_core, priority, enabled, trigger_mode, trigger_param}]
      reflectionSegmentList: [],    // reflection-targeted segments (inject_into="reflection")
      customSegmentList: [],        // [{segment_id, label, content, priority, enabled, trigger_mode, trigger_param}]
      segmentSaving: false,
      newCustomSeg: null,       // null = hidden; object = editing new custom seg

      // ── System Tab ──
      currentTheme: '',
      systemForm: { log_level: 'info', font_size: 14, bubble_width: 78 },
      systemEnvForm: { proxy_enabled: false, proxy_url: '' },
      serverAddress: '',
      systemSaving: false,
      systemEnvSaving: false,
      uiPrefsShowLauncher: true,
      uiPrefsSaving: false,
      autoSaveState: null,   // null | 'saving' | 'saved'
      _autoSaveStateTimer: null,
      _systemFormLoaded: false,
      _systemSaveTimer: null,
      _systemEnvSaveTimer: null,

      // ── Auto-updater ──────────────────────────────────────────────────────
      updateStatus: 'idle',   // idle | checking | available | not-available | downloading | ready | error
      updateVersion: '',
      updatePercent: 0,

      // ── 行为 tab 子 tab ──
      behaviorSubTab: 'protagonist',   // 'protagonist' | 'reflection' | 'emotion'
      // ── Engines Config (P4) ──
      enginesForm: { emotion_enabled: true, emotion_freq: 5, emotion_decay_enabled: true, emotion_decay_full_secs: 14400, emotion_decay_skip_if_active: 300, emotion_neutral: 'calm', affinity_enabled: true, affinity_freq: 5, affinity_delta_clamp: 15, energy_enabled: true, energy_interval: 300 },
      enginesSaving: false,
      _enginesFormLoaded: false,
      _enginesSaveTimer: null,
      _analysisSaveTimer: null,
      // ── 主角（用户）全局设定 ──
      userPersonaForm: { name: '', introduction: '' },
      userPersonaSaving: false,
      _userPersonaFormLoaded: false,
      _userPersonaSaveTimer: null,
      profileEnginesForm: { emotion_enabled: null, emotion_freq: '', affinity_enabled: null, affinity_freq: '', affinity_delta_clamp: '', energy_enabled: null, energy_interval: '', reflection_enabled: null, ase_enabled: null, topic_discovery_enabled: null },
      /** 人格引擎页「继承」时对应的全局默认值（来自 GET /profiles/:id/engine_config 的 global_defaults） */
      profileEngineGlobalDefaults: null,
      profileEnginesSaving: false,
      _profileEnginesFormLoaded: false,
      _profileEnginesSaveTimer: null,

      // ── Analysis Model (P4) ──
      showAnalysisModel: false,
      showExtractionModel: false,
      showEmbeddingSection: false,
      showReflectionModelSection: false,
      showVlmModelSection: false,
      showTopicDiscoverySection: false,
      topicDiscoveryAdvancedOpen: false,
      analysisModelEnabled: false,
      analysisModelPreset: '',
      analysisModelSaving: false,
      // Per-role override params for analysis model
      analysisOverride: { temperature: null, top_p: null, presence_penalty: null, frequency_penalty: null, max_tokens: null,
        temperature_enabled: false, top_p_enabled: false, presence_penalty_enabled: false, frequency_penalty_enabled: false, max_tokens_enabled: false },

      // ── Memory Tab (P6) ──
      memoryActiveSubTab: 'facts',
      // ── Chat History Sub-tab ──
      chatHistory: [],
      chatHistoryTotal: 0,
      chatHistoryOffset: 0,
      chatHistoryLimit: 50,
      chatHistorySearch: '',
      chatHistoryLoading: false,
      chatHistoryContextSince: 0,
      chatHistoryContextStart: 0,
      memoryStatus: null,
      memoryGlobalExpanded: false,
      memoryProfileExpanded: false,
      memoryGlobalForm: {
        enabled: false,
        vector_enabled: false,
        extraction_frequency: 5,
        extraction_weight_threshold: 0.5,
        max_facts_in_prompt: 8,
        day_summary_enabled: false,
        day_summary_keep_days: 14,
        day_summary_max_messages: 100,
        day_summary_max_conv_chars: 12000,
        recent_fact_quota: 3,
        semantic_fact_quota: 2,
        recent_days: 3,
        semantic_distance: 0.45,
        vector_dedup_distance_threshold: 0.1,
        daily_run_at_hour: 0,
        daily_run_at_minute: 5,
        daily_forgetting_enabled: false,
        daily_decay_factor: 0.998,
        daily_decay_min_weight: 0.1,
        daily_reinforcement_enabled: false,
        daily_consolidation_enabled: false,
        daily_consolidation_after_days: 30,
        daily_consolidation_weight_below: 0.3,
        daily_consolidation_batch_max: 15,
      },
      memoryGlobalSaving: false,

      // ── Embedding Tab ──
      embeddingForm: {
        provider: 'gemini',
        model: 'gemini-embedding-001',
        base_url: '',
        api_key: '',
        has_key: false,
        local_model: 'all-MiniLM-L6-v2',
        device: 'auto',
        offline: false,
      },
      embeddingSaving: false,
      embeddingShowKey: false,
      _embeddingFormLoaded: false,
      _embeddingSaveTimer: null,
      embedTestResult: null,
      embedTestLoading: false,
      memoryProfileForm: {
        enabled: null,
        vector_enabled: null,
        day_summary_enabled: null,
        extraction_frequency: '',
        extraction_weight_threshold: '',
        max_facts_in_prompt: '',
        extraction_llm_preset: '',
        extraction_prompt: '',
        day_summary_prompt: '',
        recent_fact_quota: '',
        semantic_fact_quota: '',
        recent_days: '',
        semantic_distance: '',
        vector_dedup_distance_threshold: '',
        group_chat_self_recap_max_groups: '',
        group_chat_merge_into_history: true,
        daily_forgetting_enabled: null,
        daily_decay_factor: '',
        daily_decay_min_weight: '',
        daily_reinforcement_enabled: null,
        daily_consolidation_enabled: null,
        daily_consolidation_after_days: '',
        daily_consolidation_weight_below: '',
        daily_consolidation_batch_max: '',
      },
      memoryProfileSaving: false,
      memGlobalSaveState: null,
      memProfileSaveState: null,
      _memGlobalFormLoaded: false,
      _memGlobalSaveTimer: null,
      _memProfileFormLoaded: false,
      _memProfileSaveTimer: null,
      memGlobalAdvOpen: { ltm: false, vector: false, daySummary: false, decay: false, consolidation: false },
      memProfileAdvOpen: { extract: false, vector: false, forgetting: false },
      memoryFormProfileId: '',      // 记忆设置表单所属人格，load 完成后设置，save 时用此 id 避免盖错人
      memoryContentProfileId: '',   // 记忆内容（事实/向量/摘要/历史）所属人格，load 完成后设置
      memoryFacts: [],
      memoryFactForm: { content: '', category: 'other', weight: 1.0 },
      memoryEditingFact: null,
      memoryVectors: [],
      memoryVectorText: '',
      memorySummaries: [],
      diaryStartDate: (() => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); })(),
      diaryDays: 1,
      diaryForce: false,
      diaryLoading: false,
      diaryPreview: [],       // [{ date, message_count, has_summary }]
      diaryPreviewLoading: false,
      diaryResultPerDate: [], // 生成后的 per_date
      diaryMessage: '',
      diaryMessageOk: false,
      syncFactsResult: '',
      syncFactsLoading: false,
      forgettingRunLoading: false,
      forgettingPreview: null,   // 每日遗忘预览结果 { skip, reason, day_summary, decay, ... }
      forgettingPreviewLoading: false,
      factSortBy: 'time',    // 'time' | 'category' | 'weight'
      factSearch: '',
      factFilterEmotional: 'all',  // 'all' | 'with_note' 有情感注记
      vectorSortBy: 'time',  // 'time' (only option for now)
      factsDisplayLimit: 30,
      vectorsDisplayLimit: 30,
      summariesDisplayLimit: 30,
      vectorSearch: '',

      // ── Reflection / ASE / VLM Tab ──
      reflectionAdvancedOpen: false,
      aseAdvancedOpen: false,
      reflectionForm: {
        reflection_enabled:         true,
        reflection_interval:        180,
        reflection_interval_min:    20,
        reflection_interval_max:    1800,
        reflection_retry:           60,
        reflection_recent_turns:              10,
        reflection_recent_dialogue_max_turns: 8,
        reflection_ttl:                       600,
        reflection_idle_throttle_after: 900,
        reflection_idle_interval:       600,
        reflection_model_enabled:       true,
        reflection_primary_preset:  '',
        reflection_fallback_preset: '',
        // Per-role override params
        reflection_temperature: null, reflection_temperature_enabled: false,
        reflection_top_p: null, reflection_top_p_enabled: false,
        reflection_presence_penalty: null, reflection_presence_penalty_enabled: false,
        reflection_frequency_penalty: null, reflection_frequency_penalty_enabled: false,
        reflection_max_tokens: null, reflection_max_tokens_enabled: false,
        ase_enabled:                true,
        ase_mode:                   'medium',
        ase_vlm_mode:               'random',
        ase_modes: {
          low:    { check_interval: 600, min_silent_seconds: 900,  urgency_threshold: 0.55, min_interval: 1800, max_per_24h: 5,  max_consecutive_without_response: 3,  extra_behavior: '' },
          medium: { check_interval: 300, min_silent_seconds: 300,  urgency_threshold: 0.35, min_interval: 600,  max_per_24h: 15, max_consecutive_without_response: 5,  extra_behavior: '' },
          high:   { check_interval: 120, min_silent_seconds: 180,  urgency_threshold: 0.25, min_interval: 240,  max_per_24h: 30, max_consecutive_without_response: 10, extra_behavior: '' },
          game:   { check_interval: 60,  min_silent_seconds: 90,   urgency_threshold: 0.40, min_interval: 120,  max_per_24h: 40, max_consecutive_without_response: 0,  extra_behavior: '' },
          focus:  { check_interval: 600, min_silent_seconds: 1200, urgency_threshold: 0.70, min_interval: 1800, max_per_24h: 3,  max_consecutive_without_response: 2,  extra_behavior: '' },
        },
        vlm_enabled:                false,
        vlm_model_preset:           '',
        vlm_for_chat:               true,
        vlm_for_ase:                true,
        vlm_ase_wait_seconds:       3,
        topic_discovery: {
          enabled:            true,
          candidate_cap:      8,
          recent_used_window: 10,
          chosen_topic_ttl:   1800,
          sources: {
            trend:               { enabled: true },
            conversation_recall: { enabled: true },
            user_life:           { enabled: true },
            ai_self:             { enabled: true },
            random_api: {
              enabled: false, mode: 'builtin', builtin_pool: 'icebreaker',
              http_url: '', http_json_path: '',
            },
          },
        },
      },
      reflectionSaving: false,
      vlmSaving: false,
      _reflectionFormLoaded: false,
      _reflectionSaveTimer: null,
      reflectionProfileForm: {
        custom_prompt: '',
        source: '',
        chat_inject_topic_anchor: true,
        long_absence_hours: 48,
        segments: [],  // 统一列表（与人格 Prompt 同构）：内置 + 自定义，含 trigger_mode/trigger_param
      },
      reflectionProfileSaving: false,
      _reflectionProfileFormLoaded: false,
      _reflectionProfileSaveTimer: null,
      reflectionFormProfileId: '',
      refaseExpanded: false,
      // 人格演化
      personaEvolutionForm: {
        base_prompt_original: '',
        style_constraint_original: '',
        core_anchor: '',
        base_prompt_evolved: '',
        style_constraint_evolved: '',
        evolution_count: 0,
        evolved_at: null,
        changelog: [],
        enabled: false,
        min_interval_turns: 200,
      },
      _personaEvolutionFormLoaded: false,
      personaEvolutionOpen: false,
      personaEvolutionExtracting: false,
      personaEvolutionTriggering: false,
      newCustomRefaseSeg: null,
      activeReflectionSegTab: 'reflection',  // 'reflection' | 'ase'
    };
  },

  computed: {
    selectedProfileIndex() {
      if (!this.selectedProfileId || !this.profiles.length) return -1;
      return this.profiles.findIndex(p => p.profile_id === this.selectedProfileId);
    },
    /** 人格 JSON 内嵌 lorebook 条目数（未绑定 lorebooks/ 文件时仍可能生效） */
    legacyInlineLorebookCount() {
      const p = this.profiles.find((x) => x.profile_id === this.selectedProfileId);
      if (!p || (p.lorebook_ref || '').trim()) return 0;
      const lb = p.lorebook;
      return Array.isArray(lb) ? lb.length : 0;
    },
    lorebookDeleteConfirmBody() {
      const id = this.lorebookEditId || '';
      return this.t('confirmDeleteLorebook').replace(/\{id\}/g, id);
    },
    memoryBudgetBreakdown() {
      void this.locale; // 切换语言时重算（t() 依赖 locale）
      const _n = (p, g, def) => { const v = parseInt(p); return !isNaN(v) ? v : (parseInt(g) || def); };
      const max     = _n(this.memoryProfileForm.max_facts_in_prompt, this.memoryGlobalForm.max_facts_in_prompt, 8);
      const recent  = _n(this.memoryProfileForm.recent_fact_quota,   this.memoryGlobalForm.recent_fact_quota,   3);
      const semantic= _n(this.memoryProfileForm.semantic_fact_quota, this.memoryGlobalForm.semantic_fact_quota, 2);
      const slot3Cap= Math.min(Math.max(0, max - recent), semantic);
      return this.t('memoryBudgetBreakdownFmt')
        .replace('{recent}', String(recent))
        .replace('{semantic}', String(semantic))
        .replace('{slot3Cap}', String(slot3Cap))
        .replace('{max}', String(max));
    },

    sortedMemoryFacts() {
      let facts = this.memoryFacts;
      if (this.factFilterEmotional === 'with_note') {
        facts = facts.filter(f => (f.emotional_note || '').trim().length > 0);
      }
      const kw = this.factSearch.trim().toLowerCase();
      if (kw) {
        facts = facts.filter(f => (f.content || '').toLowerCase().includes(kw)
            || (f.category || '').toLowerCase().includes(kw)
            || (f.emotional_note || '').toLowerCase().includes(kw));
      }
      const pinned    = facts.filter(f => f.pinned);
      const nonPinned = facts.filter(f => !f.pinned);
      const sortKey   = this.factSortBy;
      const sorted = [...nonPinned].sort((a, b) => {
        if (sortKey === 'category') {
          const c = (a.category || 'other').localeCompare(b.category || 'other', 'zh');
          return c !== 0 ? c : (b.updated_at || 0) - (a.updated_at || 0);
        }
        if (sortKey === 'weight') {
          const w = (b.weight || 1) - (a.weight || 1);
          return w !== 0 ? w : (b.updated_at || 0) - (a.updated_at || 0);
        }
        // default: time desc
        return (b.updated_at || 0) - (a.updated_at || 0);
      });
      // pinned also sorted by time desc among themselves
      const sortedPinned = [...pinned].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
      return [...sortedPinned, ...sorted];
    },

    sortedMemoryVectors() {
      const kw = this.vectorSearch.trim().toLowerCase();
      const vecs = kw
        ? this.memoryVectors.filter(v => (v.document || '').toLowerCase().includes(kw))
        : this.memoryVectors;
      return [...vecs].sort((a, b) => {
        const ta = a.metadata?.created_at || 0;
        const tb = b.metadata?.created_at || 0;
        return tb - ta;  // newest first
      });
    },

    displayedMemoryFacts() {
      return this.sortedMemoryFacts.slice(0, this.factsDisplayLimit);
    },
    displayedMemoryVectors() {
      return this.sortedMemoryVectors.slice(0, this.vectorsDisplayLimit);
    },
    displayedMemorySummaries() {
      return this.memorySummaries.slice(0, this.summariesDisplayLimit);
    },

    // 人格记忆「当前生效」开关（用于总开关关闭时禁用子项）
    memoryProfileEffectiveEnabled() {
      if (!this.selectedProfileId) return this.memoryGlobalForm.enabled;
      const v = this.memoryProfileForm.enabled;
      return v === null || v === undefined ? this.memoryGlobalForm.enabled : !!v;
    },
    memoryProfileEffectiveVectorEnabled() {
      if (!this.selectedProfileId) return this.memoryGlobalForm.vector_enabled;
      const v = this.memoryProfileForm.vector_enabled;
      return v === null || v === undefined ? this.memoryGlobalForm.vector_enabled : !!v;
    },
    memoryProfileEffectiveDaySummaryEnabled() {
      if (!this.selectedProfileId) return this.memoryGlobalForm.day_summary_enabled;
      const v = this.memoryProfileForm.day_summary_enabled;
      return v === null || v === undefined ? this.memoryGlobalForm.day_summary_enabled : !!v;
    },
    memoryProfileEffectiveForgettingEnabled() {
      if (!this.selectedProfileId) return this.memoryGlobalForm.daily_forgetting_enabled;
      const v = this.memoryProfileForm.daily_forgetting_enabled;
      return v === null || v === undefined ? this.memoryGlobalForm.daily_forgetting_enabled : !!v;
    },

    /** 入门页教程：与 /api/setup/status 的 bundles 一致 */
    onboardingMemBundles() {
      const g = this.setupGuide;
      if (!g || !Array.isArray(g.bundles)) return [];
      return g.bundles.filter((x) => x.category === 'memory' || x.category === 'embedding');
    },
    onboardingTtsBundles() {
      const g = this.setupGuide;
      if (!g || !Array.isArray(g.bundles)) return [];
      return g.bundles.filter((x) => x.category === 'tts');
    },
    onboardingSttBundle() {
      const g = this.setupGuide;
      if (!g || !Array.isArray(g.bundles)) return null;
      return g.bundles.find((x) => x.id === 'sherpa_sense_voice') || null;
    },

    // ── Persona canvas header computeds ──
    /** 条宽 0–100%（亲密度可为负，负值时条为 0，避免 CSS width 为负） */
    personaAffinityPct() {
      const a = this.personaAffinityState;
      if (!a) return 0;
      const v = Number(a.affinity);
      if (Number.isNaN(v)) return 0;
      return Math.max(0, Math.min(100, Math.round(v / 12)));
    },
    /** 右侧数值：负亲密度显示原始分数，非负显示与条一致的百分比 */
    personaAffinityValueText() {
      const a = this.personaAffinityState;
      if (!a) return '—';
      const v = Number(a.affinity);
      if (Number.isNaN(v)) return '—';
      if (v < 0) return String(v);
      return `${Math.min(100, Math.round(v / 12))}%`;
    },
    personaAffinityIsNegative() {
      const a = this.personaAffinityState;
      if (!a) return false;
      const v = Number(a.affinity);
      return !Number.isNaN(v) && v < 0;
    },
    personaEnergyPct() {
      const e = this.personaEmotionState;
      return e ? Math.min(100, Math.round(e.energy_level || 0)) : 0;
    },
    personaStatusLabel() {
      const a = this.personaAffinityState;
      return a ? (a.status || '') : '';
    },
    energyLevelsI18n() {
      return {
        '0':  this.t('energyLvlExhausted'),
        '10': this.t('energyLvlLazy'),
        '30': this.t('energyLvlStable'),
        '60': this.t('energyLvlFull'),
        '80': this.t('energyLvlPeak'),
      };
    },
    affinityLevelsI18n() {
      return {
        '-100': this.t('affinityLvlHate'),
        '0':    this.t('affinityLvlStranger'),
        '200':  this.t('affinityLvlAcquainted'),
        '400':  this.t('affinityLvlFamiliar'),
        '600':  this.t('affinityLvlCloseFriend'),
        '800':  this.t('affinityLvlVeryClose'),
        '1000': this.t('affinityLvlSoulmate'),
        '1200': this.t('affinityLvlEternal'),
      };
    },
    emotionKeyList() {
      const standard = (this.emotionStandardKeys && this.emotionStandardKeys.length)
        ? this.emotionStandardKeys
        : EMOTION_KEYS_FALLBACK;
      const extra = Object.keys(this.profilePromptForm.emotion_prompts)
        .filter(k => !standard.includes(k));
      return [...standard, ...extra];
    },
    currentTabLabel() {
      const tab = this.settingsTabs.find(t => t.id === this.activeTab);
      if (!tab) return '';
      return this.locale === 'en' ? tab.labelEn : tab.labelZh;
    },
  },

  watch: {
    ttsForm: {
      deep: true,
      handler() {
        if (!this._ttsFormLoaded) return;
        clearTimeout(this._ttsSaveTimer);
        this._ttsSaveTimer = setTimeout(() => this.saveTTSConfig(true), 800);
      },
    },
    memoryGlobalForm: {
      deep: true,
      handler() {
        if (!this._memGlobalFormLoaded) return;
        clearTimeout(this._memGlobalSaveTimer);
        this._memGlobalSaveTimer = setTimeout(() => this.saveMemoryGlobalConfig(true), 800);
      },
    },
    memoryProfileForm: {
      deep: true,
      handler() {
        if (!this._memProfileFormLoaded) return;
        clearTimeout(this._memProfileSaveTimer);
        this._memProfileSaveTimer = setTimeout(() => this.saveMemoryProfileConfig(true), 800);
      },
    },
    systemForm: {
      deep: true,
      handler() {
        if (!this._systemFormLoaded) return;
        clearTimeout(this._systemSaveTimer);
        this._systemSaveTimer = setTimeout(() => this.saveSystemConfig(true), 800);
      },
    },
    systemEnvForm: {
      deep: true,
      handler() {
        if (!this._systemFormLoaded) return;
        clearTimeout(this._systemEnvSaveTimer);
        this._systemEnvSaveTimer = setTimeout(() => this.saveSystemEnv(true), 800);
      },
    },
    enginesForm: {
      deep: true,
      handler() {
        if (!this._enginesFormLoaded) return;
        clearTimeout(this._enginesSaveTimer);
        this._enginesSaveTimer = setTimeout(() => this.saveEnginesConfig(true), 800);
      },
    },
    analysisModelEnabled() {
      if (!this._enginesFormLoaded) return;
      clearTimeout(this._analysisSaveTimer);
      this._analysisSaveTimer = setTimeout(() => this.saveAnalysisModel(true), 800);
    },
    analysisModelPreset() {
      if (!this._enginesFormLoaded) return;
      clearTimeout(this._analysisSaveTimer);
      this._analysisSaveTimer = setTimeout(() => this.saveAnalysisModel(true), 800);
    },
    analysisOverride: {
      deep: true,
      handler() {
        if (!this._enginesFormLoaded) return;
        clearTimeout(this._analysisSaveTimer);
        this._analysisSaveTimer = setTimeout(() => this.saveAnalysisModel(true), 800);
      },
    },
    reflectionForm: {
      deep: true,
      handler() {
        if (!this._reflectionFormLoaded) return;
        clearTimeout(this._reflectionSaveTimer);
        this._reflectionSaveTimer = setTimeout(() => this.saveReflectionConfig(true), 800);
      },
    },
    userPortraitForm: {
      deep: true,
      handler() {
        if (!this._userPortraitFormLoaded || !this.selectedProfileId) return;
        clearTimeout(this._userPortraitEditSaveTimer);
        this._userPortraitEditSaveTimer = setTimeout(() => {
          this.saveUserPortraitContent(true);
          this.saveUserPortraitConfig(true);
        }, 800);
      },
    },
    userPersonaForm: {
      deep: true,
      handler() {
        if (!this._userPersonaFormLoaded) return;
        clearTimeout(this._userPersonaSaveTimer);
        this._userPersonaSaveTimer = setTimeout(() => this.saveUserPersona(true), 800);
      },
    },
    profileForm: {
      deep: true,
      handler() {
        if (!this._profileFormLoaded || !this.profileForm.profile_id) return;
        clearTimeout(this._profileSaveTimer);
        this._profileSaveTimer = setTimeout(() => this.saveProfile({ skipToast: true }), 800);
      },
    },
    profileEnginesForm: {
      deep: true,
      handler() {
        if (!this._profileEnginesFormLoaded || !this.profileForm.profile_id) return;
        clearTimeout(this._profileEnginesSaveTimer);
        this._profileEnginesSaveTimer = setTimeout(() => this.saveProfileEngineConfig({ skipToast: true }), 800);
      },
    },
    specialDates: {
      deep: true,
      handler() {
        if (!this._specialDatesLoaded || !this.profileForm.profile_id) return;
        clearTimeout(this._specialDatesSaveTimer);
        this._specialDatesSaveTimer = setTimeout(() => this.saveSpecialDates({ skipToast: true }), 800);
      },
    },
    profilePromptForm: {
      deep: true,
      handler() {
        if (!this._profileFormLoaded || !this.profileForm.profile_id) return;
        clearTimeout(this._profileSaveTimer);
        this._profileSaveTimer = setTimeout(() => this.saveProfile({ skipToast: true }), 800);
      },
    },
    reflectionProfileForm: {
      deep: true,
      handler() {
        if (!this._reflectionProfileFormLoaded || !this.reflectionFormProfileId) return;
        clearTimeout(this._reflectionProfileSaveTimer);
        this._reflectionProfileSaveTimer = setTimeout(() => this.saveReflectionProfileConfig({ skipToast: true }), 800);
      },
    },
    personaEvolutionForm: {
      deep: true,
      handler() {
        if (!this._personaEvolutionFormLoaded || !this.selectedProfileId) return;
        clearTimeout(this._personaEvolutionSaveTimer);
        this._personaEvolutionSaveTimer = setTimeout(() => this.savePersonaEvolutionData(), 800);
      },
    },
    segmentList: {
      deep: true,
      handler() {
        if (!this._profileFormLoaded || !this.profileForm.profile_id) return;
        clearTimeout(this._segmentSaveTimer);
        this._segmentSaveTimer = setTimeout(() => this.saveSegmentConfig({ skipToast: true }), 800);
      },
    },
    customSegmentList: {
      deep: true,
      handler() {
        if (!this._profileFormLoaded || !this.profileForm.profile_id) return;
        clearTimeout(this._segmentSaveTimer);
        this._segmentSaveTimer = setTimeout(() => this.saveSegmentConfig({ skipToast: true }), 800);
      },
    },
    embeddingForm: {
      deep: true,
      handler() {
        if (!this._embeddingFormLoaded) return;
        clearTimeout(this._embeddingSaveTimer);
        this._embeddingSaveTimer = setTimeout(() => this.saveEmbeddingConfig(true), 800);
      },
    },
    activeTab(newTab, oldTab) {
      if (oldTab === 'onboarding' && this.setupDownloadPollId) {
        clearInterval(this.setupDownloadPollId);
        this.setupDownloadPollId = null;
      }
      if (newTab === 'onboarding' && this.settingsOpen) {
        this.loadSetupGuide();
      }
      if (newTab === 'behavior' && this.settingsOpen) {
        this.loadReflectionConfig();
        this.loadEnginesConfig();
        this.loadUserPersona();
        if (this.selectedProfileId) this.loadReflectionProfileConfig(this.selectedProfileId);
        if (!this.llmPresets || !Object.keys(this.llmPresets).length) {
          this.loadLLMPresets();
        }
      }
      if (newTab === 'tools' && this.settingsOpen) {
        this.loadToolsConfig();
      }
      if (newTab === 'memory' && this.settingsOpen) {
        this.memoryPanelLoading = true;
        (async () => {
          try {
            if (!this.profiles.length) await this.loadProfiles();
            if (this.selectedProfileId) await this.loadMemoryData(this.selectedProfileId);
          } finally {
            this.memoryPanelLoading = false;
          }
        })();
      }
      if (newTab === 'profiles' && this.settingsOpen) {
        this._loadPersonaState();
      }
    },
    memoryPanelTab(tab) {
      const isMemoryOpen = this.memoryPanelOpen || (this.settingsOpen && this.activeTab === 'memory');
      if (tab === 'settings' && isMemoryOpen) {
        this.loadMemoryGlobalConfig();
        if (!this.profiles.length) this.loadProfiles();
        if (this.selectedProfileId) this.loadMemoryProfileConfig(this.selectedProfileId);
      }
    },
    selectedProfileId(id) {
      if (id && this.activeTab === 'profiles' && this.settingsOpen) {
        this._loadPersonaState();
      }
      if (id && this.activeTab === 'profiles' && this.profileSubTab === 'behavior') {
        this.loadReflectionProfileConfig(id);
      }
      if (id && this.activeTab === 'profiles' && (this.profileSubTab === 'basic' || this.profileSubTab === 'tools')) {
        this.loadToolsConfig(id);
      }
    },
    profileSubTab(newTab) {
      if (newTab === 'behavior' && this.activeTab === 'profiles' && this.settingsOpen) {
        if (!this.llmPresets || !Object.keys(this.llmPresets).length) this.loadLLMPresets();
        if (this.selectedProfileId) this.loadReflectionProfileConfig(this.selectedProfileId);
      }
      if (newTab === 'tools' && this.activeTab === 'profiles' && this.settingsOpen && this.selectedProfileId) {
        this.loadToolsConfig(this.selectedProfileId);
      }
    },
  },

  methods: {
    /**
     * 用户文档链接（随 locale 选 *.zh.md / *.en.md）。
     * 参数可为 STEM（如 GETTING_STARTED）或旧名 GETTING_STARTED.md（自动去后缀与语言段）。
     */
    userDocUrl(stemOrFile) {
      if (typeof window === 'undefined' || !stemOrFile) return '#';
      const o = window.location && window.location.origin;
      let s = String(stemOrFile).trim();
      s = s.replace(/\.md$/i, '');
      s = s.replace(/\.(zh|en)$/i, '');
      const loc = this.locale === 'en' ? 'en' : 'zh';
      const filename = `${s}.${loc}.md`;
      return (o || '') + '/docs-viewer.html?doc=' + encodeURIComponent(filename);
    },
    /** 未渲染的 Markdown（供复制；与 server.py 白名单一致） */
    userDocRawUrl(stemOrFile) {
      if (typeof window === 'undefined' || !stemOrFile) return '#';
      const o = window.location && window.location.origin;
      let s = String(stemOrFile).trim();
      s = s.replace(/\.md$/i, '');
      s = s.replace(/\.(zh|en)$/i, '');
      const loc = this.locale === 'en' ? 'en' : 'zh';
      const filename = `${s}.${loc}.md`;
      return (o || '') + '/user-docs/' + encodeURIComponent(filename);
    },

    /* ─────────────── Toast ─────────────── */

    showToast(msg, type) {
      // 1. 状态栏（聊天界面底部）
      const prefix = type === 'error' ? '❌ ' : type === 'success' ? '✓ ' : '';
      this.statusText = prefix + msg;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.statusText = this.t('statusReady'); }, 3000);
      // 2. 固定悬浮 toast（设置面板打开时也可见）
      const id = Date.now() + Math.random();
      this.toasts.push({ id, msg, type });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 3500);
    },

    /** Render pip install/uninstall API errors: strings pass through; `{ key, detail?, pkg?, dir?, prefix? }` uses i18n. */
    formatPipError(err) {
      if (err == null || err === '') return '';
      if (typeof err === 'string') return err;
      if (typeof err === 'object' && typeof err.key === 'string') {
        let msg = this.t(err.key);
        if (err.pkg != null) msg = msg.replace(/\{pkg\}/g, String(err.pkg));
        if (err.dir != null) msg = msg.replace(/\{dir\}/g, String(err.dir));
        if (err.prefix != null && err.detail != null)
          msg = msg + '\n' + String(err.prefix) + ': ' + String(err.detail);
        else if (err.detail) msg = msg + '\n' + String(err.detail);
        return msg;
      }
      return String(err);
    },

    formatPipErrorsList(errors) {
      if (!errors || !errors.length) return '';
      return errors.map((e) => this.formatPipError(e)).filter(Boolean).join('\n\n');
    },

    /* ─────────────── Engine Health ─────────────── */

    async loadEngineWarnings() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.apiEngineWarnings());
        if (!res.ok) return;
        const data = await res.json();
        this.engineWarning = data.warnings && data.warnings.length > 0
          ? data.warnings[0]
          : '';
      } catch (_) {}
    },

    /* ─────────────── Drawer ─────────────── */

    openSettings() {
      this.settingsOpen = true;
      this.confirmingAction = null;
      {
        let t = localStorage.getItem('theme') || '';
        if (t === 'velvet_legacy') { t = 'velvet'; localStorage.setItem('theme', t); }
        this.currentTheme = t;
      }
      this.loadLLMPresets();
      this.loadTTSConfig();
      this.loadProfiles().then(() => {
        if (this.activeTab === 'profiles') this._loadPersonaState();
      });
      this.loadSystemConfig();
      this.loadEnginesConfig();  // 辅助分析模型等仍从全局 engines 接口取
      this.loadEmbeddingConfig();
      this.loadMemoryGlobalConfig();
      this.loadReflectionConfig();
    },

    closeSettings() {
      if (this.setupDownloadPollId) {
        clearInterval(this.setupDownloadPollId);
        this.setupDownloadPollId = null;
      }
      this.settingsOpen = false;
      this.confirmingAction = null;
      const active = document.activeElement;
      if (active && typeof active.blur === 'function') active.blur();
    },

    onboardingBundleTitle(b) {
      if (!b || !b.id) return '';
      void this.locale;
      const loc = this.locale === 'en' ? 'en' : 'zh';
      const k = `modelBundle_${b.id}_title`;
      const L = window.LOCALES && window.LOCALES[loc];
      if (L && L[k]) return L[k];
      return this.locale === 'en'
        ? (b.title_en || b.title_zh || b.id)
        : (b.title_zh || b.title_en || b.id);
    },
    onboardingBundleDesc(b) {
      if (!b || !b.id) return '';
      void this.locale;
      const loc = this.locale === 'en' ? 'en' : 'zh';
      const k = `modelBundle_${b.id}_desc`;
      const L = window.LOCALES && window.LOCALES[loc];
      if (L && L[k]) return L[k];
      return this.locale === 'en'
        ? (b.description_en || b.description_zh || '')
        : (b.description_zh || b.description_en || '');
    },

    async loadSetupGuide() {
      this.setupGuideLoading = true;
      try {
        const r = await fetch(getBaseUrl() + API_PATHS.setupStatus());
        if (r.ok) {
          const data = await r.json();
          this.setupGuide = data;
          if (data.stt_model_path !== undefined) this.sttModelPath = data.stt_model_path;
          // Sync gptsovits_dir into ttsForm so the inline save button works
          if (data.tts && data.tts.gpt_sovits_dir != null) {
            this.ttsForm.gptsovits_dir = data.tts.gpt_sovits_dir;
            this.onboardingGptsovitsDir = data.tts.gpt_sovits_dir;
          }
        }
      } catch (_) {}
      finally {
        this.setupGuideLoading = false;
      }
    },

    async saveOnboardingGptsovitsDir() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsTtsGptSovitsDir(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dir: this.onboardingGptsovitsDir || '' }),
        });
        const j = await res.json().catch(() => ({}));
        if (res.ok && j.ok) {
          this.showToast(this.t('onboardingGptsovitsSaved'), 'success');
          await this.loadSetupGuide();
        } else {
          this.showToast((j.detail || j.error || res.statusText || 'Save failed'), 'error');
        }
      } catch (e) {
        this.showToast(String(e.message || e), 'error');
      }
    },

    async pickOnboardingSttModelDir() {
      if (!window.electronAPI || typeof window.electronAPI.setupWizardPickSttModel !== 'function') {
        this.showToast(this.t('setupWizardPickSttModelNeedElectron'), 'error');
        return;
      }
      let picked;
      try {
        picked = await window.electronAPI.setupWizardPickSttModel();
      } catch (e) {
        this.showToast(String(e.message || e), 'error');
        return;
      }
      if (!picked || !picked.path) return;
      try {
        const res = await fetch(getBaseUrl() + '/api/setup/apply-stt-model', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model_path: picked.path }),
        });
        const j = await res.json().catch(() => ({}));
        if (res.ok && j.ok !== false) {
          this.showToast(this.t('setupWizardPickSttModelSaved'), 'success');
          await this.loadSetupGuide();
        } else {
          this.showToast((j.error || j.detail || res.statusText || 'Save failed'), 'error');
        }
      } catch (e) {
        this.showToast(String(e.message || e), 'error');
      }
    },

    async launchOnboardingGptsovits() {
      if (this.setupGuide?.tts?.gpt_sovits_port_open) {
        return;
      }
      try {
        const res = await fetch(getBaseUrl() + '/api/setup/launch-gptsovits', { method: 'POST' });
        const j = await res.json().catch(() => ({}));
        if (res.ok && j.ok !== false) {
          this.showToast(this.t('onboardingTtsGptSoVitsLaunch') + ' …', 'success');
          await this.loadSetupGuide();
        } else {
          this.showToast((j.error || j.detail || 'Launch failed'), 'error');
        }
      } catch (e) {
        this.showToast(String(e.message || e), 'error');
      }
    },

    async runSetupVerify() {
      this.setupVerifyLoading = true;
      this.setupVerifyResult = null;
      try {
        const r = await fetch(getBaseUrl() + API_PATHS.setupVerify(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        this.setupVerifyResult = await r.json().catch(() => ({}));
      } catch (e) {
        this.setupVerifyResult = { llm_error: String(e), embedding_error: String(e) };
      } finally {
        this.setupVerifyLoading = false;
      }
    },

    openUrl(url) {
      if (window.electronAPI && window.electronAPI.openExternal) {
        window.electronAPI.openExternal(url);
      } else {
        window.open(url, '_blank', 'noopener');
      }
    },

    openMemoryPanel() {
      // 现已整合进 canvas，直接跳转到 memory tab
      this.openSettings();
      this.$nextTick(() => { this.activeTab = 'memory'; });
    },

    closeMemoryPanel() {
      // 兼容旧引用：关闭整个 canvas
      this.closeSettings();
    },

    /* ─────────────── Persona state (for canvas header) ─────────────── */

    async _loadPersonaState() {
      const id = this.selectedProfileId;
      if (!id) return;
      try {
        const res = await fetch(getBaseUrl() + `/sessions/${id}/status`);
        if (!res.ok) return;
        const data = await res.json();
        this.personaEmotionState = data.emotion_state || null;
        this.personaAffinityState = data.affinity_state || null;
      } catch (_) {}
    },

    /* ─────────────── Radar chart (pure SVG, no deps) ─────────────── */

    /* ─────────────── Theme ─────────────── */

    applyTheme(name) {
      if (name === 'velvet_legacy') name = 'velvet'; // legacy theme id
      this.currentTheme = name;
      document.documentElement.setAttribute('data-theme', name);
      localStorage.setItem('theme', name);
      fetch(getBaseUrl() + '/api/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme: name }),
      }).catch(() => {});
    },

    /* ─────────────── Bubble Size ─────────────── */

    applyBubbleSize() {
      const root = document.documentElement;
      root.style.setProperty('--font-size-bubble', this.systemForm.font_size + 'px');
      root.style.setProperty('--bubble-max-width', this.systemForm.bubble_width + '%');
      localStorage.setItem('bubble_font_size', this.systemForm.font_size);
      localStorage.setItem('bubble_max_width', this.systemForm.bubble_width);
    },

    /* ─────────────── LLM Presets ─────────────── */

    async loadLLMPresets() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsLlmPresets());
        const data = await res.json();
        this.llmPresets = data.presets || {};
        this.llmActivePreset = data.active || '';
        // Auto-select: active preset or first in list
        const autoSelect = this.llmActivePreset || Object.keys(this.llmPresets)[0] || '';
        if (autoSelect && (!this.selectedPresetName || !this.llmPresets[this.selectedPresetName])) {
          this.selectPreset(autoSelect);
        }
      } catch (e) {
        console.error('[settings] loadLLMPresets:', e);
      }
    },

    selectPreset(name) {
      this.selectedPresetName = name;
      const p = this.llmPresets[name];
      if (!p) return;
      const unsupported = p.unsupported_params || [];
      this.presetForm = {
        name,
        type: p.type || 'openai_compat',
        api_key: '',          // never pre-fill; show placeholder if has_key
        base_url: p.base_url || '',
        model: p.model || '',
        temperature: p.temperature != null ? p.temperature : 0.9,
        temperature_enabled: !unsupported.includes('temperature'),
        top_p: p.top_p != null ? p.top_p : 0.95,
        top_p_enabled: !unsupported.includes('top_p'),
        presence_penalty: p.presence_penalty != null ? p.presence_penalty : 0.5,
        presence_penalty_enabled: !unsupported.includes('presence_penalty'),
        frequency_penalty: p.frequency_penalty != null ? p.frequency_penalty : 0.4,
        frequency_penalty_enabled: !unsupported.includes('frequency_penalty'),
        max_tokens: p.max_tokens != null ? p.max_tokens : 8192,
        max_tokens_enabled: p.max_tokens != null,
        extra_body_raw: p.extra_body ? JSON.stringify(p.extra_body, null, 2) : '',
      };
      this.llmShowKey = false;
    },

    _collectUnsupported(form) {
      return ['temperature', 'top_p', 'presence_penalty', 'frequency_penalty']
        .filter(p => !form[`${p}_enabled`]);
    },

    async saveLLMPreset() {
      if (this.presetSaving || !this.selectedPresetName) return;
      this.presetSaving = true;
      try {
        const body = {
          name: this.presetForm.name || this.selectedPresetName,
          type: this.presetForm.type,
          base_url: this.presetForm.base_url,
          model: this.presetForm.model,
          temperature: parseFloat(this.presetForm.temperature),
          top_p: parseFloat(this.presetForm.top_p),
          presence_penalty: parseFloat(this.presetForm.presence_penalty),
          frequency_penalty: parseFloat(this.presetForm.frequency_penalty),
          max_tokens: this.presetForm.max_tokens_enabled ? (parseInt(this.presetForm.max_tokens, 10) || null) : null,
          unsupported_params: this._collectUnsupported(this.presetForm),
        };
        // extra_body: parse JSON if non-empty, skip if blank or invalid
        const rawEB = (this.presetForm.extra_body_raw || '').trim();
        if (rawEB) {
          try {
            body.extra_body = JSON.parse(rawEB);
          } catch {
            this.showToast(this.t('toastExtraBodyInvalid'), 'error');
            this.presetSaving = false;
            return;
          }
        }
        // Only send api_key if the user typed something
        if (this.presetForm.api_key.trim()) {
          body.api_key = this.presetForm.api_key.trim();
        }
        const res = await fetch(
          getBaseUrl() + API_PATHS.settingsLlmPreset(this.selectedPresetName),
          { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
        );
        const data = await res.json();
        if (data.ok) {
          this.showToast(this.t('toastLLMSaved'), 'success');
          await this.loadLLMPresets();
          this.loadSetupGuide();
        } else {
          this.showToast(`保存失败: ${data.error || 'unknown'}`, 'error');
        }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.presetSaving = false;
      }
    },

    async createLLMPreset(templateKey) {
      this.llmShowNewMenu = false;
      const tmpl = PRESET_TEMPLATES[templateKey] || PRESET_TEMPLATES.custom;
      // Find a unique name
      let name = tmpl.name;
      let i = 1;
      while (this.llmPresets[name]) { name = `${tmpl.name}_${i++}`; }
      try {
        const body = {
          name,
          type: tmpl.type,
          base_url: tmpl.base_url,
          model: tmpl.model,
          temperature: tmpl.temperature,
          top_p: tmpl.top_p,
          presence_penalty: tmpl.presence_penalty,
          frequency_penalty: tmpl.frequency_penalty,
          unsupported_params: ['presence_penalty', 'frequency_penalty']
            .filter(p => !tmpl[`${p}_enabled`]),
        };
        const res = await fetch(getBaseUrl() + API_PATHS.settingsLlmPresets(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
          await this.loadLLMPresets();
          this.selectPreset(name);
        } else {
          this.showToast(`创建失败: ${data.error || 'unknown'}`, 'error');
        }
      } catch (e) {
        this.showToast(`创建失败: ${e.message}`, 'error');
      }
    },

    async deleteLLMPreset() {
      if (!this.selectedPresetName) return;
      if (this.selectedPresetName === this.llmActivePreset) {
        this.showToast(this.t('toastCannotDeleteActive'), 'error');
        return;
      }
      this.confirmingAction = {
        key: 'deleteLLMPreset',
        label: `删除预设 "${this.selectedPresetName}"`,
        data: { presetName: this.selectedPresetName },
      };
    },

    async _doDeleteLLMPreset(presetName) {
      try {
        await fetch(
          getBaseUrl() + API_PATHS.settingsLlmPreset(presetName),
          { method: 'DELETE' }
        );
        const names = Object.keys(this.llmPresets).filter(n => n !== presetName);
        this.selectedPresetName = '';
        await this.loadLLMPresets();
        if (names.length) this.selectPreset(names[0]);
      } catch (e) {
        this.showToast(`删除失败: ${e.message}`, 'error');
      }
    },

    async setActiveLLMPreset() {
      if (!this.selectedPresetName) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsLlmActive(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: this.selectedPresetName }),
        });
        const data = await res.json();
        if (data.ok) {
          this.llmActivePreset = this.selectedPresetName;
          this.showToast(`✓ 已激活 ${this.selectedPresetName}`, 'success');
          await this.loadLLMPresets();
        }
      } catch (e) {
        this.showToast(`激活失败: ${e.message}`, 'error');
      }
    },

    async testLLMPreset() {
      const name = this.selectedPresetName;
      if (!name || this.llmTestLoading) return;
      this.llmTestLoading = true;
      this.llmTestResult = null;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsLlmPresetTest(name), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
        });
        const data = await res.json();
        this.llmTestResult = { presetName: name, ...data };
      } catch (e) {
        this.llmTestResult = { presetName: name, ok: false, error: e.message };
      } finally {
        this.llmTestLoading = false;
      }
    },

    /* ─────────────── TTS ─────────────── */

    async loadKokoroVoices() {
      if (this.kokoroVoicesLoaded) return;
      try {
        const r = await fetch(getBaseUrl() + API_PATHS.settingsKokoroVoices());
        if (r.ok) {
          const d = await r.json();
          this.kokoroVoiceGroups = d.groups || {};
          this.kokoroVoicesLoaded = true;
        }
      } catch (_) {}
    },

    async loadTTSConfig() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsTtsConfig());
        if (!res.ok) {
          console.warn('[settings] loadTTSConfig: server returned', res.status, res.statusText);
          return;
        }
        const text = await res.text();
        if (!text || !text.trim()) return;
        let data;
        try {
          data = JSON.parse(text);
        } catch (_) {
          console.warn('[settings] loadTTSConfig: response is not valid JSON');
          return;
        }
        this.ttsForm.engine = data.engine || 'edge_tts';
        const et = data.edge_tts || {};
        if (et.voice) this.ttsForm.voice = et.voice;
        if (et.rate) {
          // Parse "+5%" or "-10%" → number
          const pct = parseInt(et.rate, 10);
          if (!isNaN(pct)) this.ttsForm.rate_pct = pct;
        }
        const gs = data.gpt_sovits || {};
        if (gs.host)               this.ttsForm.gptsovits_host = gs.host;
        if (gs.port)               this.ttsForm.gptsovits_port = gs.port;
        if (gs.dir != null)        this.ttsForm.gptsovits_dir = gs.dir || '';
        if (gs.text_lang)          this.ttsForm.gptsovits_text_lang = gs.text_lang;
        if (gs.prompt_lang)        this.ttsForm.gptsovits_prompt_lang = gs.prompt_lang;
        if (gs.speed_factor != null) this.ttsForm.gptsovits_speed = gs.speed_factor;
        if (gs.temperature != null)  this.ttsForm.gptsovits_temperature = gs.temperature;
        if (gs.top_p != null)        this.ttsForm.gptsovits_top_p = gs.top_p;
        if (gs.top_k != null)        this.ttsForm.gptsovits_top_k = gs.top_k;
        if (gs.repetition_penalty != null) this.ttsForm.gptsovits_repetition_penalty = gs.repetition_penalty;
        if (gs.ref_audio_path != null) this.ttsForm.gptsovits_ref_audio_path = gs.ref_audio_path;
        if (gs.prompt_text != null)    this.ttsForm.gptsovits_prompt_text = gs.prompt_text;
        const kok = data.kokoro || {};
        if (kok.voice != null)            this.ttsForm.kokoro_voice = kok.voice;
        if (kok.lang != null)             this.ttsForm.kokoro_lang = kok.lang;
        if (kok.speed != null)            this.ttsForm.kokoro_speed = kok.speed;
        if (kok.auto_detect_lang != null) this.ttsForm.kokoro_auto_detect_lang = kok.auto_detect_lang;
        const q3 = data.qwen3_tts || {};
        if (q3.mode != null)             this.ttsForm.qwen3_mode = q3.mode;
        if (q3.model_id != null)        this.ttsForm.qwen3_model_id = q3.model_id;
        if (q3.device != null)          this.ttsForm.qwen3_device = q3.device;
        if (q3.dtype != null)           this.ttsForm.qwen3_dtype = q3.dtype;
        if (q3.language != null)        this.ttsForm.qwen3_language = q3.language;
        if (q3.speaker != null)         this.ttsForm.qwen3_speaker = q3.speaker;
        if (q3.instruct != null)        this.ttsForm.qwen3_instruct = q3.instruct;
        if (q3.voice_description != null) this.ttsForm.qwen3_voice_description = q3.voice_description;
        if (q3.ref_audio_path != null)  this.ttsForm.qwen3_ref_audio_path = q3.ref_audio_path;
        if (q3.ref_text != null)        this.ttsForm.qwen3_ref_text = q3.ref_text;
        if (q3.temperature != null)     this.ttsForm.qwen3_temperature = q3.temperature;
        if (q3.top_p != null)           this.ttsForm.qwen3_top_p = q3.top_p;
        if (q3.top_k != null)           this.ttsForm.qwen3_top_k = q3.top_k;
        if (q3.repetition_penalty != null) this.ttsForm.qwen3_repetition_penalty = q3.repetition_penalty;
        if (q3.attn_implementation != null) this.ttsForm.qwen3_attn_implementation = q3.attn_implementation;
        if (q3.use_torch_compile != null) this.ttsForm.qwen3_use_torch_compile = q3.use_torch_compile;
        if (q3.use_sentence_chunking != null) this.ttsForm.qwen3_use_sentence_chunking = q3.use_sentence_chunking;
        if (q3.sentence_max_chars != null) this.ttsForm.qwen3_sentence_max_chars = q3.sentence_max_chars;
        // Load voice list for edge_tts
        if (this.ttsForm.engine === 'edge_tts') this.loadTTSVoices();
        this.$nextTick(() => { this._ttsFormLoaded = true; });
      } catch (e) {
        console.error('[settings] loadTTSConfig:', e);
      }
    },

    /** 切换 Qwen3 模式时，若当前 model_id 与模式不匹配则自动改为对应默认（本地目录，离线可用；保存后 app.yaml 会更新） */
    onQwen3ModeChange() {
      if (this.ttsForm.engine !== 'qwen3_tts') return;
      const m = this.ttsForm.qwen3_mode;
      const id = (this.ttsForm.qwen3_model_id || '').trim();
      const localBase = 'models/Qwen-Qwen3-TTS-12Hz-1.7B-Base';
      const localCustom = 'models/Qwen-Qwen3-TTS-12Hz-1.7B-CustomVoice';
      const localDesign = 'models/Qwen-Qwen3-TTS-12Hz-1.7B-VoiceDesign';
      // voice_clone 需 Base；当前为 CustomVoice 或 VoiceDesign 时都要切回 Base
      if (m === 'voice_clone' && (!id || /CustomVoice/i.test(id) || /VoiceDesign/i.test(id))) {
        this.ttsForm.qwen3_model_id = localBase;
      } else if (m === 'custom_voice' && (!id || /Base/i.test(id) || /VoiceDesign/i.test(id))) {
        // custom_voice 需 CustomVoice；当前为 Base 或 VoiceDesign 时都要切回 CustomVoice
        this.ttsForm.qwen3_model_id = localCustom;
      } else if (m === 'voice_design' && (!id || /CustomVoice/i.test(id) || (/Base/i.test(id) && !/VoiceDesign/i.test(id)))) {
        this.ttsForm.qwen3_model_id = localDesign;
      }
    },

    async loadTTSVoices() {
      this.ttsLoading = true;
      this.ttsVoices = [];
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsTtsVoices(this.ttsForm.language));
        const data = await res.json();
        this.ttsVoices = data.voices || [];
      } catch (_) {
        this.ttsVoices = [];
      } finally {
        this.ttsLoading = false;
      }
    },

    /** 与 POST /settings/tts/save、/settings/tts/test 共用，避免试听仍用未保存的旧 default_tts */
    buildTtsSavePayload() {
      return {
        engine:                      this.ttsForm.engine,
        voice:                       this.ttsForm.voice,
        rate_pct:                    parseInt(this.ttsForm.rate_pct, 10) || 0,
        language:                    this.ttsForm.language,
        gptsovits_host:              this.ttsForm.gptsovits_host,
        gptsovits_port:              parseInt(this.ttsForm.gptsovits_port, 10) || 9880,
        gptsovits_dir:               this.ttsForm.gptsovits_dir || '',
        gptsovits_text_lang:         this.ttsForm.gptsovits_text_lang,
        gptsovits_prompt_lang:       this.ttsForm.gptsovits_prompt_lang,
        gptsovits_speed:             parseFloat(this.ttsForm.gptsovits_speed) || 1.0,
        gptsovits_temperature:       parseFloat(this.ttsForm.gptsovits_temperature) || 1.0,
        gptsovits_top_p:             parseFloat(this.ttsForm.gptsovits_top_p) || 1.0,
        gptsovits_top_k:             parseInt(this.ttsForm.gptsovits_top_k, 10) || 15,
        gptsovits_repetition_penalty: parseFloat(this.ttsForm.gptsovits_repetition_penalty) || 1.35,
        gptsovits_ref_audio_path: this.ttsForm.gptsovits_ref_audio_path || '',
        gptsovits_prompt_text: this.ttsForm.gptsovits_prompt_text || '',
        qwen3_mode:                  this.ttsForm.qwen3_mode || 'custom_voice',
        qwen3_model_id:              this.ttsForm.qwen3_model_id || '',
        qwen3_device:                this.ttsForm.qwen3_device || 'cuda:0',
        qwen3_dtype:                 this.ttsForm.qwen3_dtype || 'bfloat16',
        qwen3_language:              this.ttsForm.qwen3_language || 'Chinese',
        qwen3_speaker:               this.ttsForm.qwen3_speaker || 'Vivian',
        qwen3_instruct:              this.ttsForm.qwen3_instruct || '',
        qwen3_voice_description:     this.ttsForm.qwen3_voice_description || '',
        qwen3_ref_audio_path:        this.ttsForm.qwen3_ref_audio_path || '',
        qwen3_ref_text:              this.ttsForm.qwen3_ref_text || '',
        qwen3_temperature:           parseFloat(this.ttsForm.qwen3_temperature) || 0.9,
        qwen3_top_p:                 parseFloat(this.ttsForm.qwen3_top_p) || 1.0,
        qwen3_top_k:                 parseInt(this.ttsForm.qwen3_top_k, 10) || 50,
        qwen3_repetition_penalty:    parseFloat(this.ttsForm.qwen3_repetition_penalty) || 1.05,
        qwen3_attn_implementation:   this.ttsForm.qwen3_attn_implementation || 'eager',
        qwen3_use_torch_compile:    !!this.ttsForm.qwen3_use_torch_compile,
        qwen3_use_sentence_chunking: !!this.ttsForm.qwen3_use_sentence_chunking,
        qwen3_sentence_max_chars:    parseInt(this.ttsForm.qwen3_sentence_max_chars, 10) || 0,
        kokoro_voice:               this.ttsForm.kokoro_voice || '',
        kokoro_lang:                this.ttsForm.kokoro_lang || 'zh',
        kokoro_speed:               parseFloat(this.ttsForm.kokoro_speed) || 1.0,
        kokoro_auto_detect_lang:    !!this.ttsForm.kokoro_auto_detect_lang,
      };
    },

    async saveTTSConfig(silent = false) {
      this.ttsSaveState = 'saving';
      try {
        const body = this.buildTtsSavePayload();
        const res = await fetch(getBaseUrl() + API_PATHS.settingsTtsSave(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
          this.ttsSaveState = 'saved';
          setTimeout(() => { this.ttsSaveState = null; }, 2000);
          if (!silent) this.showToast(this.t('toastTtsSaved'), 'success');
          // 重连 TTS WebSocket，让新参数（语速等）立即生效
          if (this.ttsEnabled) {
            this.disconnectTTS();
            setTimeout(() => this.connectTTS(), 150);
          }
        } else {
          this.ttsSaveState = null;
          if (!silent) this.showToast(`TTS 保存失败: ${data.error || ''}`, 'error');
        }
      } catch (e) {
        this.ttsSaveState = null;
        if (!silent) this.showToast(`TTS 保存失败: ${e.message}`, 'error');
      }
    },

    async testTTSVoice() {
      this.ttsTestState = 'loading';
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsTtsTest(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.buildTtsSavePayload()),
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'synthesis failed');
        const audio = new Audio(`data:${data.mime};base64,${data.audio}`);
        await audio.play();
        this.ttsTestState = 'ok';
      } catch (e) {
        this.ttsTestState = 'error';
        console.error('[testTTS]', e);
        const msg = (e && e.message) ? String(e.message) : String(e);
        if (this.showToast) this.showToast(msg, 'error');
      }
      setTimeout(() => { this.ttsTestState = null; }, 3000);
    },

    /* ─────────────── Profiles ─────────────── */

    async loadProfiles() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profiles());
        const data = await res.json();
        this.profiles = data.profiles || [];
        if (this.profiles.length && !this.selectedProfileId) {
          this.selectProfile(this.profiles[0].profile_id);
        }
      } catch (_) {}
    },

    async saveProfileOrder() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profilesOrder(), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile_ids: this.profiles.map(p => p.profile_id) }),
        });
        const data = await res.json();
        if (data.ok) {
          this.showToast(this.t('toastOrderSaved'), 'success');
          if (typeof this.loadSessions === 'function') await this.loadSessions();
        }
      } catch (e) {
        this.showToast(`保存顺序失败: ${e.message}`, 'error');
      }
    },
    async saveProfileLoadIntoChat(profileId, loadIntoChat) {
      const p = this.profiles.find(x => x.profile_id === profileId);
      if (!p) return;
      const prev = p.load_into_chat !== false;
      p.load_into_chat = loadIntoChat;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profileLoadIntoChat(profileId), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ load_into_chat: loadIntoChat }),
        });
        const data = await res.json();
        if (data.ok) {
          if (typeof this.loadSessions === 'function') await this.loadSessions();
          this.showToast(loadIntoChat ? '✓ 已加入侧栏，可与其对话' : '✓ 已从侧栏移除，仅作储存（群聊用到时会按需加载）', 'success');
        } else { p.load_into_chat = prev; this.showToast(`保存失败: ${data.detail || data.error || ''}`, 'error'); }
      } catch (e) {
        p.load_into_chat = prev;
        this.showToast(`保存失败: ${e.message}`, 'error');
      }
    },
    /** 与主界面侧栏 session 列表相同的 HTML5 拖拽（桌面） */
    profileDragStart(ev, profileId) {
      if (!this.supportsHtml5Drag) return;
      this.profileDragging = true;
      this.profileDragActiveId = profileId;
      ev.dataTransfer.setData('text/plain', profileId);
      ev.dataTransfer.effectAllowed = 'move';
    },
    profileDragEnd() {
      this.profileDragging = false;
      this.profileDragOverId = null;
      this.profileDragActiveId = null;
      this._profileDragEndAt = Date.now();
    },
    profileItemClick(profileId) {
      if (this.profileDragging) return;
      if (this._profileDragEndAt && Date.now() - this._profileDragEndAt < 300) return;
      this.selectProfile(profileId);
    },
    profileDragOver(ev, profileId) {
      ev.dataTransfer.dropEffect = 'move';
      this.profileDragOverId = profileId;
    },
    async profileDrop(ev, dropTargetId) {
      this.profileDragOverId = null;
      const draggedId = ev.dataTransfer.getData('text/plain');
      if (!draggedId || draggedId === dropTargetId) return;
      const fromIdx = this.profiles.findIndex(p => p.profile_id === draggedId);
      const toIdx = this.profiles.findIndex(p => p.profile_id === dropTargetId);
      if (fromIdx === -1 || toIdx === -1) return;
      const copy = this.profiles.slice();
      const [item] = copy.splice(fromIdx, 1);
      copy.splice(toIdx, 0, item);
      this.profiles = copy;
      await this.saveProfileOrder();
    },
    /** 触屏：与 app.js 侧栏 sessionPointerDown 相同（即时拖动到目标行再松手排序） */
    profilePointerDown(ev, profileId) {
      if (this.supportsHtml5Drag) return;

      ev.preventDefault(); // 触屏：避免滚动；桌面不能 prevent，否则手柄无法启动原生 drag
      this.profileDragging = true;
      this.profileDragActiveId = profileId;
      this.profileDragOverId = null;

      const startY = ev.clientY;
      let hasMoved = false;

      const onMove = (e) => {
        if (!hasMoved && Math.abs(e.clientY - startY) > 6) hasMoved = true;
        if (!hasMoved) return;
        const el = document.elementFromPoint(e.clientX, e.clientY);
        const item = el && el.closest('[data-profile-id]');
        const overId = item ? item.getAttribute('data-profile-id') : null;
        this.profileDragOverId = (overId && overId !== profileId) ? overId : null;
      };

      const onUp = async () => {
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
        document.removeEventListener('pointercancel', onUp);

        const targetId = this.profileDragOverId;
        this.profileDragging = false;
        this.profileDragOverId = null;
        this.profileDragActiveId = null;

        if (hasMoved && targetId && targetId !== profileId) {
          const fromIdx = this.profiles.findIndex(p => p.profile_id === profileId);
          const toIdx = this.profiles.findIndex(p => p.profile_id === targetId);
          if (fromIdx !== -1 && toIdx !== -1) {
            const copy = this.profiles.slice();
            const [moved] = copy.splice(fromIdx, 1);
            copy.splice(toIdx, 0, moved);
            this.profiles = copy;
            await this.saveProfileOrder();
          }
        }
      };

      document.addEventListener('pointermove', onMove);
      document.addEventListener('pointerup', onUp);
      document.addEventListener('pointercancel', onUp);
    },
    selectProfile(profileId) {
      this._profileFormLoaded = false;
      this.selectedProfileId = profileId;
      const p = this.profiles.find(x => x.profile_id === profileId);
      if (!p) return;
      this.profileForm = {
        profile_id: p.profile_id,
        display_name: p.display_name || '',
        base_prompt: p.base_prompt || '',
        style_constraint: p.style_constraint || '',
        avatar: p.avatar || '',
        lorebook_ref: p.lorebook_ref || '',
        user_persona: {
          name:         (p.user_persona || {}).name         || '',
          introduction: (p.user_persona || {}).introduction || '',
        },
        gpt_sovits_ref_audio_path: p.gpt_sovits_ref_audio_path || '',
        gpt_sovits_ref_text: p.gpt_sovits_ref_text || '',
        qwen3_tts_ref_audio_path: p.qwen3_tts_ref_audio_path || '',
        qwen3_tts_ref_text: p.qwen3_tts_ref_text || '',
        qwen3_tts_instruct: p.qwen3_tts_instruct || '',
        qwen3_tts_voice_description: p.qwen3_tts_voice_description || '',
        qwen3_tts_speaker: p.qwen3_tts_speaker || '',
        kokoro_voice: p.kokoro_voice || '',
        kokoro_lang: p.kokoro_lang || '',
        anti_assistant_mode: !!p.anti_assistant_mode,
        // Voice generation state (不持久化，仅本次会话)
        voiceGenDescription: '',
        voiceGenText: '',
        voiceGenLanguage: 'Auto',
        voiceGenSpeed: 'normal',
        voiceGenPitch: 'normal',
        voiceGenEmotionStrength: 'normal',
        voiceGenPreviewUrl: null,
        voiceGenFilename: '',
        voiceGenSaveTarget: 'both',
        voiceGenGenerating: false,
        voiceGenSaving: false,
      };
      // Reset segment panel and load config for the newly selected profile
      this.segmentsExpanded = false;
      this.newCustomSeg = null;
      this.loadSegmentConfig(profileId);
      this.loadSpecialDates(profileId);
      this.loadProfileEngineConfig(profileId);
      // Load reflection + memory prompts so they're available in the Prompts sub-tab
      this.loadReflectionProfileConfig(profileId);
      this.loadPersonaEvolution(profileId);
      this.loadUserPortrait(profileId);
      this.loadMemoryProfileConfig(profileId);
      // Populate prompt override form
      const ec = p.emotion_config || {};
      const standardEmotionKeys = (this.emotionStandardKeys && this.emotionStandardKeys.length)
        ? this.emotionStandardKeys
        : EMOTION_KEYS_FALLBACK;
      // emotion_prompts: array values → joined multiline string; pre-fill all standard keys
      const epRaw = ec.emotion_prompts || {};
      const epFlat = {};
      for (const k of standardEmotionKeys) {
        const v = epRaw[k];
        epFlat[k] = Array.isArray(v) ? v.join('\n') : (typeof v === 'string' ? v : '');
      }
      for (const [k, v] of Object.entries(epRaw)) {
        if (!(k in epFlat))
          epFlat[k] = Array.isArray(v) ? v.join('\n') : (typeof v === 'string' ? v : '');
      }
      this.profilePromptForm.emotion_prompts = epFlat;
      // emotion_zh_descriptions
      const zhRaw = ec.emotion_zh_descriptions || {};
      const zhFlat = {};
      for (const k of standardEmotionKeys) zhFlat[k] = zhRaw[k] || '';
      for (const [k, v] of Object.entries(zhRaw)) { if (!(k in zhFlat)) zhFlat[k] = v || ''; }
      this.profilePromptForm.emotion_zh_descriptions = zhFlat;
      // energy_prompts
      const energy = ec.energy_prompts || {};
      for (const k of ['0', '10', '30', '60', '80']) {
        this.profilePromptForm.energy_prompts[k] = energy[k] || '';
      }
      // affinity_prompts — read from emotion_config first, fall back to legacy top-level
      const aff = ec.affinity_prompts || p.affinity_prompts || {};
      for (const k of ['-100', '0', '200', '400', '600', '800', '1000', '1200']) {
        this.profilePromptForm.affinity_prompts[k] = aff[k] || '';
      }
      this.$nextTick(() => { this._profileFormLoaded = true; });
    },

    async loadLorebooksList() {
      this.lorebooksListLoading = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.lorebooks());
        const data = await res.json().catch(() => ({}));
        this.lorebooksList = (data.ok && data.lorebooks) ? data.lorebooks : [];
      } catch (_) {
        this.lorebooksList = [];
      } finally {
        this.lorebooksListLoading = false;
      }
    },

    openProfileLorebookTab() {
      this.profileSubTab = 'lorebook';
      this.loadLorebooksList();
    },

    _newLorebookEditEntryKey(suffix) {
      return `lb-edit-${suffix || 'x'}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    },

    _parseLorebookKeysText(text) {
      if (!text || typeof text !== 'string') return [];
      return text.split(/[\n,，、]+/).map((s) => s.trim()).filter(Boolean);
    },

    _buildLorebookEntriesForApi(rows) {
      const out = [];
      for (const e of rows || []) {
        const content = (e.content || '').trim();
        if (!content) continue;
        let em = e.mode;
        if (em !== 'inherit' && em !== 'keyword' && em !== 'constant') {
          em = (e.constant || e.mode === 'constant') ? 'constant' : 'keyword';
        }
        const keys = this._parseLorebookKeysText(e.keysText);
        const dm = this.lorebookEditForm.default_entry_mode || 'keyword';
        const eff = em === 'inherit' ? dm : em;
        if (eff === 'keyword' && keys.length === 0) continue;
        const row = {
          keys,
          content,
          enabled: e.enabled !== false,
          mode: em,
        };
        const label = (e.label || '').trim();
        if (label) row.label = label;
        out.push(row);
      }
      return out;
    },

    _mapApiEntriesToEditRows(entries, idPrefix) {
      const arr = Array.isArray(entries) ? entries : [];
      return arr.map((e, idx) => {
        let mode = 'keyword';
        if (e.mode === 'inherit') mode = 'inherit';
        else if (e.mode === 'keyword') mode = 'keyword';
        else if (e.mode === 'constant') mode = 'constant';
        else mode = (e.constant || e.mode === 'constant') ? 'constant' : 'keyword';
        return {
          _key: `e-${idPrefix}-${idx}`,
          keysText: Array.isArray(e.keys) ? e.keys.join(', ') : (typeof e.keys === 'string' ? e.keys : ''),
          content: e.content || '',
          enabled: e.enabled !== false,
          mode,
          label: e.label || '',
        };
      });
    },

    /** 条目实际触发方式（继承书本默认时） */
    effectiveLorebookRowMode(ent) {
      if (ent.mode === 'keyword') return 'keyword';
      if (ent.mode === 'constant') return 'constant';
      const dm = this.lorebookEditForm.default_entry_mode || 'keyword';
      return dm === 'constant' ? 'constant' : 'keyword';
    },

    async selectLorebookForEdit(lorebookId) {
      if (!lorebookId) {
        this.lorebookEditId = '';
        this.lorebookEditForm = { display_name: '', scan_turns: 10, default_entry_mode: 'keyword', entries: [] };
        return;
      }
      // 再次点击当前已选中的世界书 → 收起编辑区，避免重复加载整本
      if (lorebookId === this.lorebookEditId) {
        this.lorebookEditId = '';
        this.lorebookEditForm = { display_name: '', scan_turns: 10, default_entry_mode: 'keyword', entries: [] };
        return;
      }
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.lorebookById(lorebookId));
        const data = await res.json();
        if (!res.ok || !data.ok) {
          this.showToast(this.t('toastLorebookLoadFail'), 'error');
          return;
        }
        this.lorebookEditId = data.lorebook_id || lorebookId;
        const st = data.lorebook_settings || {};
        let scanN = parseInt(st.scan_turns, 10);
        if (Number.isNaN(scanN)) scanN = 10;
        const dem = st.default_entry_mode === 'constant' ? 'constant' : 'keyword';
        this.lorebookEditForm = {
          display_name: data.display_name || this.lorebookEditId,
          scan_turns: Math.min(50, Math.max(1, scanN)),
          default_entry_mode: dem,
          entries: this._mapApiEntriesToEditRows(data.entries, this.lorebookEditId),
        };
      } catch (e) {
        this.showToast(`${this.t('toastLorebookLoadFail')}: ${e.message}`, 'error');
      }
    },

    addLorebookEditEntry() {
      this.lorebookEditForm.entries.push({
        _key: this._newLorebookEditEntryKey(this.lorebookEditId),
        label: '',
        keysText: '',
        content: '',
        enabled: true,
        mode: 'inherit',
      });
    },

    removeLorebookEditEntry(idx) {
      if (idx < 0 || idx >= this.lorebookEditForm.entries.length) return;
      this.lorebookEditForm.entries.splice(idx, 1);
    },

    openLorebookCreateModal() {
      this.lorebookCreateNameDraft = this.t('lblNewLorebookDefaultName');
      this.showLorebookCreateModal = true;
      this.$nextTick(() => {
        const el = document.getElementById('lorebook-create-name-input');
        if (el) {
          el.focus();
          el.select();
        }
      });
    },

    cancelLorebookCreateModal() {
      if (this.lorebookCreateSaving) return;
      this.showLorebookCreateModal = false;
    },

    async confirmCreateLorebookFile() {
      const name = (this.lorebookCreateNameDraft || '').trim();
      if (!name) {
        this.showToast(this.t('toastLorebookCreateNeedName'), 'error');
        return;
      }
      if (this.lorebookCreateSaving) return;
      this.lorebookCreateSaving = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.lorebooks(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: name }),
        });
        const data = await res.json().catch(() => ({}));
        if (data.ok) {
          this.showLorebookCreateModal = false;
          this.lorebookCreateNameDraft = '';
          await this.loadLorebooksList();
          await this.selectLorebookForEdit(data.lorebook_id);
          this.showToast(this.t('toastLorebookCreated'), 'success');
        } else {
          const err = data.detail || data.error || `HTTP ${res.status}`;
          this.showToast(`${this.t('toastLorebookCreateFail')}: ${typeof err === 'string' ? err : JSON.stringify(err)}`, 'error');
        }
      } catch (e) {
        this.showToast(`${this.t('toastLorebookCreateFail')}: ${e.message}`, 'error');
      } finally {
        this.lorebookCreateSaving = false;
      }
    },

    async saveLorebookLibraryFile() {
      if (!this.lorebookEditId || this.lorebookLibrarySaving) return;
      this.lorebookLibrarySaving = true;
      try {
        const entries = this._buildLorebookEntriesForApi(this.lorebookEditForm.entries);
        const res = await fetch(getBaseUrl() + API_PATHS.lorebookById(this.lorebookEditId), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            display_name: (this.lorebookEditForm.display_name || this.lorebookEditId).trim(),
            entries,
            lorebook_settings: {
              scan_turns: Math.min(50, Math.max(1, parseInt(this.lorebookEditForm.scan_turns, 10) || 10)),
              default_entry_mode: (this.lorebookEditForm.default_entry_mode === 'constant' ? 'constant' : 'keyword'),
            },
          }),
        });
        const data = await res.json();
        if (data.ok) {
          this.showToast(this.t('toastLorebookFileSaved'), 'success');
          await this.loadLorebooksList();
        } else {
          this.showToast(data.detail || this.t('toastLorebookSaveFail'), 'error');
        }
      } catch (e) {
        this.showToast(`${this.t('toastLorebookSaveFail')}: ${e.message}`, 'error');
      } finally {
        this.lorebookLibrarySaving = false;
      }
    },

    openLorebookDeleteDialog() {
      if (!this.lorebookEditId || this.lorebookLibraryDeleting) return;
      this.showLorebookDeleteConfirm = true;
    },

    cancelLorebookDeleteDialog() {
      this.showLorebookDeleteConfirm = false;
    },

    async confirmDeleteLorebookLibraryFile() {
      if (!this.lorebookEditId || this.lorebookLibraryDeleting) return;
      const id = this.lorebookEditId;
      this.lorebookLibraryDeleting = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.lorebookById(id), { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.ok) {
          this.showLorebookDeleteConfirm = false;
          if (this.profileForm.lorebook_ref === id) this.profileForm.lorebook_ref = '';
          this.lorebookEditId = '';
          this.lorebookEditForm = { display_name: '', scan_turns: 10, default_entry_mode: 'keyword', entries: [] };
          await this.loadLorebooksList();
          this.showToast(this.t('toastLorebookDeleted'), 'success');
        } else {
          this.showToast(this.t('toastLorebookDeleteFail'), 'error');
        }
      } catch (e) {
        this.showToast(`${this.t('toastLorebookDeleteFail')}: ${e.message}`, 'error');
      } finally {
        this.lorebookLibraryDeleting = false;
      }
    },

    importLorebookFromTavern() {
      if (this.lorebookImporting) return;
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,.png';
      input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        this.lorebookImporting = true;
        const formData = new FormData();
        formData.append('file', file);
        try {
          const res = await fetch(getBaseUrl() + API_PATHS.lorebooksImportTavern(), {
            method: 'POST',
            body: formData,
          });
          const data = await res.json().catch(() => ({}));
          if (res.ok && data.ok) {
            this.showToast(data.message || this.t('toastLorebookImportOk'), 'success');
            await this.loadLorebooksList();
            await this.selectLorebookForEdit(data.lorebook_id);
          } else {
            const detail = data.detail || data.error || `HTTP ${res.status}`;
            this.showToast(`${this.t('toastLorebookImportFail')}: ${detail}`, 'error');
          }
        } catch (err) {
          this.showToast(`${this.t('toastLorebookImportFail')}: ${err.message}`, 'error');
        } finally {
          this.lorebookImporting = false;
        }
      };
      input.click();
    },

    async saveProfile(opts = {}) {
      const profileId = this.profileForm?.profile_id;
      if (this.profileSaving || !profileId) return;
      this.profileSaving = true;
      const skipToast = opts.skipToast === true;
      if (skipToast) this.setAutoSaveState('saving');
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.profile(profileId),
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              display_name: this.profileForm.display_name,
              base_prompt: this.profileForm.base_prompt,
              style_constraint: this.profileForm.style_constraint,
              avatar: this.profileForm.avatar,
              gpt_sovits_ref_audio_path: this.profileForm.gpt_sovits_ref_audio_path,
              gpt_sovits_ref_text: this.profileForm.gpt_sovits_ref_text,
              qwen3_tts_ref_audio_path: this.profileForm.qwen3_tts_ref_audio_path,
              qwen3_tts_ref_text: this.profileForm.qwen3_tts_ref_text,
              qwen3_tts_instruct: this.profileForm.qwen3_tts_instruct,
              qwen3_tts_voice_description: this.profileForm.qwen3_tts_voice_description,
              qwen3_tts_speaker: this.profileForm.qwen3_tts_speaker,
              kokoro_voice: this.profileForm.kokoro_voice,
              kokoro_lang: this.profileForm.kokoro_lang,
              emotion_prompts: this._buildEmotionPromptsObj(),
              emotion_zh_descriptions: this._buildEmotionZhDescriptions(),
              energy_prompts: this._buildPromptDict(this.profilePromptForm.energy_prompts,
                ['0', '10', '30', '60', '80']),
              affinity_prompts: this._buildPromptDict(this.profilePromptForm.affinity_prompts,
                ['-100', '0', '200', '400', '600', '800', '1000', '1200']),
              lorebook_ref: this.profileForm.lorebook_ref != null ? this.profileForm.lorebook_ref : '',
              user_persona: {
                name:         (this.profileForm.user_persona.name         || '').trim(),
                introduction: (this.profileForm.user_persona.introduction || '').trim(),
              },
              anti_assistant_mode: !!this.profileForm.anti_assistant_mode,
            }),
          }
        );
        const data = await res.json();
        if (data.ok) {
          if (skipToast) this.setAutoSaveState('saved'); else this.showToast(this.t('toastProfileSaved'), 'success');
          await this.loadProfiles();
          await this.loadSessions(); // refresh sidebar display names
          if (this.memoryFormProfileId === profileId) {
            const memCfg = {};
            if (this.memoryProfileForm.extraction_prompt !== undefined)
              memCfg.extraction_prompt = this.memoryProfileForm.extraction_prompt;
            if (this.memoryProfileForm.day_summary_prompt !== undefined)
              memCfg.day_summary_prompt = this.memoryProfileForm.day_summary_prompt;
            if (Object.keys(memCfg).length) {
              fetch(getBaseUrl() + API_PATHS.profileMemoryConfig(profileId), {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(memCfg),
              }).catch(() => {});
            }
          }
        } else {
          if (skipToast) this.setAutoSaveState(null);
          if (!skipToast) this.showToast(`保存失败: ${data.error || ''}`, 'error');
        }
      } catch (e) {
        if (skipToast) this.setAutoSaveState(null);
        if (!skipToast) this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.profileSaving = false;
      }
    },

    async loadProfileEngineConfig(profileId) {
      if (!profileId) return;
      this._profileEnginesFormLoaded = false;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profileEngineConfig(profileId));
        if (!res.ok) {
          this.profileEngineGlobalDefaults = null;
          return;
        }
        const data = await res.json();
        const _v = (v) => (v != null && v !== '') ? v : '';
        this.profileEnginesForm = {
          emotion_enabled:       data.emotion_enabled    ?? null,
          emotion_freq:          _v(data.emotion_freq),
          affinity_enabled:      data.affinity_enabled   ?? null,
          affinity_freq:         _v(data.affinity_freq),
          affinity_delta_clamp:  _v(data.affinity_delta_clamp),
          energy_enabled:        data.energy_enabled     ?? null,
          energy_interval:       _v(data.energy_interval),
          reflection_enabled:    data.reflection_enabled ?? null,
          ase_enabled:           data.ase_enabled        ?? null,
          topic_discovery_enabled: data.topic_discovery_enabled ?? null,
        };
        this.profileEngineGlobalDefaults = data.global_defaults && typeof data.global_defaults === 'object'
          ? { ...data.global_defaults }
          : null;
      } catch (e) {
        console.error('[settings] loadProfileEngineConfig:', e);
        this.profileEngineGlobalDefaults = null;
      }
      this.$nextTick(() => { this._profileEnginesFormLoaded = true; });
    },

    /** 人格「引擎配置」子页：展示全局默认（继承时实际生效值） */
    engineGlobalDefaultLine(part) {
      const g = this.profileEngineGlobalDefaults;
      if (!g || typeof g !== 'object') return '—';
      const t = (k) => this.t(k);
      if (part === 'emotion') {
        const on = g.emotion_enabled !== false;
        const freq = g.emotion_freq ?? 5;
        if (!on) return t('optOff');
        return `${t('optOn')} · ${t('lblFreqEveryN')} ${freq} ${t('lblFreqMsgs')}`;
      }
      if (part === 'affinity') {
        const on = g.affinity_enabled !== false;
        const freq = g.affinity_freq ?? 5;
        const dc = g.affinity_delta_clamp ?? 15;
        if (!on) return t('optOff');
        return `${t('optOn')} · ${t('lblFreqEveryN')} ${freq} ${t('lblFreqMsgs')} · Δ≤${dc}`;
      }
      if (part === 'energy') {
        const sec = g.energy_interval ?? 300;
        return `${sec} ${t('lblEnergyIntervalUnit')}`;
      }
      if (part === 'reflection') {
        return g.reflection_enabled ? t('optOn') : t('optOff');
      }
      if (part === 'ase') {
        return g.ase_enabled ? t('optOn') : t('optOff');
      }
      if (part === 'topic_discovery') {
        return g.topic_discovery_enabled ? t('optOn') : t('optOff');
      }
      return '—';
    },

    async generateProfileVoice() {
      const profileId = this.profileForm?.profile_id;
      if (!profileId || this.profileForm.voiceGenGenerating) return;
      this.profileForm.voiceGenGenerating = true;
      this.profileForm.voiceGenPreviewUrl = null;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profileGenerateVoice(profileId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            voice_description:  this.profileForm.voiceGenDescription,
            text:               this.profileForm.voiceGenText,
            language:           this.profileForm.voiceGenLanguage,
            speed:              this.profileForm.voiceGenSpeed,
            pitch:              this.profileForm.voiceGenPitch,
            emotion_strength:   this.profileForm.voiceGenEmotionStrength,
          }),
        });
        if (!res.ok) {
          const d = await res.json().catch(() => ({}));
          this.showToast(d.detail || this.t('toastVoiceGenFailed'), 'error');
          return;
        }
        const arrayBuffer = await res.arrayBuffer();
        const blob = new Blob([arrayBuffer], { type: 'audio/wav' });
        // 释放旧的 Blob URL
        if (this.profileForm.voiceGenPreviewUrl) {
          URL.revokeObjectURL(this.profileForm.voiceGenPreviewUrl);
        }
        this.profileForm.voiceGenPreviewUrl = URL.createObjectURL(blob);
        this.showToast(this.t('toastVoiceGenDone'), 'success');
      } catch (e) {
        this.showToast(`${this.t('toastVoiceGenFailed')}: ${e.message}`, 'error');
      } finally {
        this.profileForm.voiceGenGenerating = false;
      }
    },

    async saveGeneratedVoice() {
      const profileId = this.profileForm?.profile_id;
      if (!profileId || !this.profileForm.voiceGenPreviewUrl || this.profileForm.voiceGenSaving) return;
      this.profileForm.voiceGenSaving = true;
      try {
        const target = this.profileForm.voiceGenSaveTarget || 'both';
        const targets = target === 'both' ? ['gpt_sovits', 'qwen3_tts'] : [target];
        const res = await fetch(getBaseUrl() + API_PATHS.profileSaveGeneratedVoice(profileId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            targets,
            filename: this.profileForm.voiceGenFilename.trim(),
            // Persist the same reference text used for generation into profile ref_text
            ref_text: (this.profileForm.voiceGenText || '').trim(),
          }),
        });
        if (res.ok) {
          const d = await res.json();
          // 同步 profileForm 里对应 ref path
          if (targets.includes('gpt_sovits'))  this.profileForm.gpt_sovits_ref_audio_path = d.saved_path;
          if (targets.includes('qwen3_tts'))   this.profileForm.qwen3_tts_ref_audio_path  = d.saved_path;
          // Also sync the reference text into profile-level fields so that
          // users don't need to copy it manually after saving the generated voice.
          const refText = (this.profileForm.voiceGenText || '').trim();
          if (refText) {
            if (targets.includes('gpt_sovits')) this.profileForm.gpt_sovits_ref_text = refText;
            if (targets.includes('qwen3_tts'))  this.profileForm.qwen3_tts_ref_text  = refText;
          }
          this.showToast(this.t('toastVoiceGenSaved'), 'success');
        } else {
          const d = await res.json().catch(() => ({}));
          this.showToast(d.detail || this.t('toastSaveFailed'), 'error');
        }
      } catch (e) {
        this.showToast(`${this.t('toastSaveFailed')}: ${e.message}`, 'error');
      } finally {
        this.profileForm.voiceGenSaving = false;
      }
    },

    async saveProfileEngineConfig(opts = {}) {
      const profileId = this.profileForm?.profile_id;
      if (this.profileEnginesSaving || !profileId) return;
      this.profileEnginesSaving = true;
      const skipToast = opts.skipToast === true;
      if (skipToast) this.setAutoSaveState('saving');
      try {
        const body = {};
        // enabled toggles: true/false/null(inherit global)
        body.emotion_enabled     = this.profileEnginesForm.emotion_enabled;
        body.affinity_enabled    = this.profileEnginesForm.affinity_enabled;
        body.energy_enabled      = this.profileEnginesForm.energy_enabled;
        body.reflection_enabled  = this.profileEnginesForm.reflection_enabled;
        body.ase_enabled         = this.profileEnginesForm.ase_enabled;
        body.topic_discovery_enabled = this.profileEnginesForm.topic_discovery_enabled;
        // frequency / interval overrides
        if (this.profileEnginesForm.emotion_freq !== '') {
          const v = parseInt(this.profileEnginesForm.emotion_freq, 10);
          if (!isNaN(v)) body.emotion_freq = v;
        }
        if (this.profileEnginesForm.affinity_freq !== '') {
          const v = parseInt(this.profileEnginesForm.affinity_freq, 10);
          if (!isNaN(v)) body.affinity_freq = v;
        }
        if (this.profileEnginesForm.affinity_delta_clamp !== '') {
          const v = parseFloat(this.profileEnginesForm.affinity_delta_clamp);
          if (!isNaN(v)) body.affinity_delta_clamp = v;
        }
        if (this.profileEnginesForm.energy_interval !== '') {
          const v = parseInt(this.profileEnginesForm.energy_interval, 10);
          if (!isNaN(v)) body.energy_interval = v;
        }
        const res = await fetch(
          getBaseUrl() + API_PATHS.profileEngineConfig(profileId),
          { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
        );
        const data = res.ok ? await res.json().catch(() => null) : null;
        if (data && data.ok) { if (skipToast) this.setAutoSaveState('saved'); else this.showToast(this.t('toastEngineConfigSaved'), 'success'); }
        else { if (skipToast) this.setAutoSaveState(null); else this.showToast('保存失败', 'error'); }
      } catch (e) {
        if (skipToast) this.setAutoSaveState(null); else this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.profileEnginesSaving = false;
      }
    },

    async loadSpecialDates(profileId) {
      this._specialDatesLoaded = false;
      try {
        const r = await fetch(getBaseUrl() + API_PATHS.profileSpecialDates(profileId));
        const d = await r.json();
        this.specialDates = d.dates || [];
      } catch (_) { this.specialDates = []; }
      this.$nextTick(() => { this._specialDatesLoaded = true; });
    },
    addSpecialDate() {
      if (!this.newSpecialDate.date || !this.newSpecialDate.label) return;
      this.specialDates.push({ ...this.newSpecialDate });
      this.newSpecialDate = { date: '', label: '' };
    },
    removeSpecialDate(idx) { this.specialDates.splice(idx, 1); },
    async saveSpecialDates(opts = {}) {
      const profileId = this.profileForm?.profile_id;
      if (!profileId) return;
      const skipToast = opts.skipToast === true;
      try {
        const r = await fetch(
          getBaseUrl() + API_PATHS.profileSpecialDates(profileId),
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dates: this.specialDates }),
          }
        );
        if (r.ok) { if (!skipToast) this.showToast(this.t('toastSpecialDatesSaved'), 'success'); }
        else { if (!skipToast) this.showToast('✗ 保存失败', 'error'); }
      } catch (e) { if (!skipToast) this.showToast(`✗ ${e.message}`, 'error'); }
    },

    async onAvatarFileChange(event, profileId) {
      const file = event.target.files[0];
      if (!file || !profileId) return;
      const formData = new FormData();
      formData.append('file', file);
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profileAvatar(profileId), {
          method: 'POST',
          body: formData,
        });
        const data = await res.json();
        if (data.ok) {
          this.profileForm.avatar = data.avatar;
          // Reload so sidebar also picks up the new avatar
          await this.loadProfiles();
          await this.loadSessions();
          this.showToast(this.t('toastAvatarUpdated'), 'success');
        } else {
          this.showToast(data.error || '上传失败', 'error');
        }
      } catch (e) {
        this.showToast(`上传失败: ${e.message}`, 'error');
      }
      // Reset file input so the same file can be reselected
      event.target.value = '';
    },

    /** Electron 下 window.prompt 常不可用，改用设置内弹窗 */
    openProfileCreateModal() {
      this.profileCreateModalOpen = true;
      this.profileCreateNameDraft = '';
      this.$nextTick(() => {
        const el = document.getElementById('profile-create-name-input');
        if (el) el.focus();
      });
    },
    cancelProfileCreateModal() {
      if (this.profileCreateSaving) return;
      this.profileCreateModalOpen = false;
      this.profileCreateNameDraft = '';
    },
    async confirmProfileCreate() {
      const displayName = (this.profileCreateNameDraft || '').trim();
      if (!displayName) {
        this.showToast(this.t('toastProfileCreateNeedName'), 'error');
        return;
      }
      if (this.profileCreateSaving) return;
      this.profileCreateSaving = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profileCreate(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: displayName }),
        });
        const data = await res.json().catch(() => ({}));
        if (data.ok) {
          this.profileCreateModalOpen = false;
          this.profileCreateNameDraft = '';
          await this.loadProfiles();
          this.selectProfile(data.profile_id);
          await this.loadSessions();
          this.showToast(this.t('toastProfileCreated'), 'success');
        } else {
          const err = data.detail || data.error || `HTTP ${res.status}`;
          this.showToast(`${this.t('toastProfileCreateFail')}: ${typeof err === 'string' ? err : JSON.stringify(err)}`, 'error');
        }
      } catch (e) {
        this.showToast(`${this.t('toastProfileCreateFail')}: ${e.message}`, 'error');
      } finally {
        this.profileCreateSaving = false;
      }
    },

    importTavernCard() {
      if (this.importingTavern) return;
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,.png';
      input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        this.importingTavern = true;
        this.showToast('正在导入角色卡…', '');
        const formData = new FormData();
        formData.append('file', file);
        try {
          const res = await fetch(getBaseUrl() + '/profiles/import/tavern', {
            method: 'POST',
            body: formData,
          });
          let data;
          try {
            data = await res.json();
          } catch (_) {
            throw new Error(`服务器返回了非 JSON 响应 (HTTP ${res.status})`);
          }
          if (res.ok) {
            this.showToast(data.message || '导入成功', 'success');
            await this.loadProfiles();
            this.selectProfile(data.profile_id);
            // 有世界书条目时自动切到 Prompt 子标签并展开世界书区块
            if ((data.lorebook_count || 0) > 0) {
              this.profileSubTab = 'lorebook';
              this.loadLorebooksList();
              if (data.lorebook_ref) this.selectLorebookForEdit(data.lorebook_ref);
            }
            await this.loadSessions();
          } else {
            const detail = data.detail || data.error || `HTTP ${res.status}`;
            this.showToast(`导入失败: ${detail}`, 'error');
          }
        } catch (err) {
          this.showToast(`导入失败: ${err.message}`, 'error');
        } finally {
          this.importingTavern = false;
        }
      };
      input.click();
    },

    /** 当前人格的世界书 segment 是否已在 Prompt 增强中启用 */
    isLorebookSegmentEnabled() {
      const seg = this.segmentList.find(s => s.segment_id === 'lorebook');
      return !seg || seg.enabled;
    },

    deleteProfile(profileId) {
      if (!profileId) return;
      const name = (this.profiles.find(p => p.profile_id === profileId) || {}).display_name || profileId;
      this.confirmingAction = {
        key: 'deleteProfile',
        label: this.locale === 'en' ? `Delete persona "${name}"` : `删除人格「${name}」`,
        data: { profileId },
      };
      // 确认条在滚动区顶部；从下方点删除时滚到顶部才能看到
      this.scrollSettingsBodyToTop();
    },

    async _doDeleteProfile(profileId) {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profile(profileId), {
          method: 'DELETE',
        });
        const data = await res.json();
        if (data.ok) {
          await this.loadProfiles();
          if (this.selectedProfileId === profileId) {
            this.selectedProfileId = '';
            this.profileForm = { profile_id: '', display_name: '', base_prompt: '', style_constraint: '', avatar: '', lorebook_ref: '', user_persona: { name: '', description: '', personality: '', role_in_story: '' } };
          }
          if (data.cleanup_failed) {
            this.showToast(
              this.locale === 'en'
                ? 'Persona deleted, but some data files (ChromaDB) are still locked. They will be cleaned up automatically — restart the app if they persist.'
                : '人格已删除，但部分数据文件（ChromaDB）仍被占用，将在后台自动清理。如持续存在请重启应用。',
              'warning'
            );
          }
        } else {
          this.showToast(data.error || '删除失败', 'error');
        }
      } catch (e) {
        this.showToast(`删除失败: ${e.message}`, 'error');
      }
    },

    /* ─────────────── Memory management (replaces clearProfileHistory) ─────────────── */

    initMemoryAction(key, profileId) {
      const labels = {
        clearHistory:  '清空全部历史记录',
        clearShortMem: '重置短期上下文',
        clearFacts:    '清空全部长期事实库',
        clearVectors:  '清空全部向量库',
        clearSummaries:'清空全部对话摘要',
        backup:        '备份全部记忆',
      };
      this.confirmingAction = { key, label: labels[key] || key, data: { profileId } };
    },

    cancelAction() {
      this.confirmingAction = null;
    },

    async executeAction() {
      if (!this.confirmingAction) return;
      const { key, data } = this.confirmingAction;
      this.confirmingAction = null;
      if (key === 'clearHistory')        await this._doClearHistory(data.profileId);
      else if (key === 'clearShortMem')  await this._doClearShortMem(data.profileId);
      else if (key === 'clearFacts')     await this._doClearFacts(data.profileId);
      else if (key === 'clearVectors')   await this._doClearVectors(data.profileId);
      else if (key === 'clearSummaries') await this._doClearSummaries(data.profileId);
      else if (key === 'backup')         await this._doBackupMemory(data.profileId);
      else if (key === 'deleteProfile')  await this._doDeleteProfile(data.profileId);
      else if (key === 'deleteLLMPreset') await this._doDeleteLLMPreset(data.presetName);
    },

    async _doClearHistory(profileId) {
      try {
        await fetch(getBaseUrl() + API_PATHS.sessionHistory(profileId), { method: 'DELETE' });
        if (profileId === this.currentSessionId) this.messages = [];
        this.showToast(this.t('toastHistoryCleared'), 'success');
      } catch (e) { this.showToast(`清空失败: ${e.message}`, 'error'); }
    },

    async _doClearShortMem(profileId) {
      try {
        await fetch(getBaseUrl() + API_PATHS.sessionHistoryRecent(profileId), { method: 'DELETE' });
        this.showToast(this.t('toastShortMemCleared'), 'success');
        // Refresh context_since_ts display in the history sub-tab
        if (this.memoryActiveSubTab === 'history') {
          await this.loadChatHistory(profileId, false);
        }
      } catch (e) { this.showToast(`重置失败: ${e.message}`, 'error'); }
    },

    async loadChatHistory(profileId, reset = true) {
      if (!profileId) return;
      if (reset) {
        this.chatHistory = [];
        this.chatHistoryOffset = 0;
        this.chatHistoryTotal = 0;
      }
      this.chatHistoryLoading = true;
      try {
        const url = getBaseUrl() + API_PATHS.sessionChatHistory(profileId, this.chatHistoryLimit, this.chatHistoryOffset, this.chatHistorySearch || '');
        const res = await fetch(url);
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        if (reset) {
          this.chatHistory = data.messages;
        } else {
          this.chatHistory.push(...data.messages);
        }
        this.chatHistoryTotal = data.total;
        this.chatHistoryContextSince = data.context_since_ts || 0;
        this.chatHistoryContextStart = data.context_start_ts || 0;
        this.memoryContentProfileId = profileId;
      } catch (e) {
        this.showToast(`加载历史失败: ${e.message}`, 'error');
      } finally {
        this.chatHistoryLoading = false;
      }
    },

    async chatHistoryLoadMore() {
      this.chatHistoryOffset += this.chatHistoryLimit;
      await this.loadChatHistory(this.memoryContentProfileId || this.selectedProfileId, false);
    },

    async deleteChatMessage(msgId) {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (!profileId) return;
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.sessionMessageById(profileId, msgId),
          { method: 'DELETE' }
        );
        if (!res.ok) throw new Error(await res.text());
        // 从本地列表移除，避免重新拉取
        const idx = this.chatHistory.findIndex(m => m.id === msgId);
        if (idx !== -1) {
          this.chatHistory.splice(idx, 1);
          this.chatHistoryTotal = Math.max(0, this.chatHistoryTotal - 1);
        }
      } catch (e) {
        this.showToast(`删除失败: ${e.message}`, 'error');
      }
    },

    async _doBackupMemory(profileId) {
      if (!profileId) { this.showToast('请先在下拉框中选择一个人格', 'error'); return; }
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.sessionBackup(profileId), { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          const warn = data.warnings && data.warnings.length ? `（${data.warnings.length} 个文件跳过）` : '';
          this.showToast(`✓ 已备份 ${data.files.length} 个文件至 ${data.path}${warn}`, 'success');
        } else {
          this.showToast(`备份失败: ${data.detail || data.error || '未知错误'}`, 'error');
        }
      } catch (e) { this.showToast(`备份失败: ${e.message}`, 'error'); }
    },

    async _doClearFacts(profileId) {
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memoryFactsAll(profileId),
          { method: 'DELETE' }
        );
        const data = await res.json();
        if (res.ok) {
          this.showToast(`✓ 已清空事实库（${data.deleted} 条）`, 'success');
          await this.loadMemoryFacts(profileId);
          await this.loadMemoryStatus(profileId);
        } else {
          this.showToast(`清空失败: ${data.detail || ''}`, 'error');
        }
      } catch (e) { this.showToast(`清空失败: ${e.message}`, 'error'); }
    },

    async _doClearVectors(profileId) {
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memoryVectorsAll(profileId),
          { method: 'DELETE' }
        );
        const data = await res.json();
        if (res.ok) {
          this.showToast(`✓ 已清空向量库（${data.deleted} 条）`, 'success');
          await this.loadMemoryVectors(profileId);
          await this.loadMemoryStatus(profileId);
        } else {
          this.showToast(`清空失败: ${data.detail || ''}`, 'error');
        }
      } catch (e) { this.showToast(`清空失败: ${e.message}`, 'error'); }
    },

    async _doClearSummaries(profileId) {
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memorySummaries(profileId),
          { method: 'DELETE' }
        );
        const data = await res.json();
        if (data.ok) {
          this.memorySummaries = [];
          await this.loadMemoryStatus(profileId);
          this.showToast(`✓ 已清空摘要（${data.deleted ?? 0} 条）`, 'success');
        } else {
          this.showToast(`清空失败: ${data.detail || data.error || ''}`, 'error');
        }
      } catch (e) { this.showToast(`清空失败: ${e.message}`, 'error'); }
    },

    /* ─────────────── Segment Config ─────────────── */

    async loadSegmentConfig(profileId) {
      if (!profileId) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profileSegments(profileId));
        if (!res.ok) return;
        const data = await res.json();
        this.segmentList = (data.segments || []).filter(s =>
            s.inject_into === 'chat'
        ).map(s => ({ ...s }));
        this.reflectionSegmentList = (data.segments || []).filter(s =>
            s.inject_into === 'reflection' && !s.is_core
        ).map(s => ({ ...s }));
        this.customSegmentList = (data.custom_segments || []).filter(c =>
            c.inject_into === 'chat'
        ).map(c => ({ ...c }));
      } catch (e) {
        console.error('[settings] loadSegmentConfig:', e);
      }
    },

    async saveSegmentConfig(opts = {}) {
      const profileId = this.profileForm?.profile_id;
      if (this.segmentSaving || !profileId) return;
      this.segmentSaving = true;
      const skipToast = opts.skipToast === true;
      try {
        const body = {
          segments: [...this.segmentList].map(s => ({
            segment_id:   s.segment_id,
            enabled:      s.enabled,
            priority:     parseInt(s.priority, 10),
            trigger_mode: s.trigger_mode,
            trigger_param: parseFloat(s.trigger_param),
            trigger_keywords: s.trigger_keywords || null,
            content:      s.content || '',
          })),
          custom_segments: this.customSegmentList.map(c => ({
            segment_id:   c.segment_id,
            label:        c.label,
            content:      c.content,
            priority:     parseInt(c.priority, 10),
            enabled:      c.enabled,
            trigger_mode: c.trigger_mode,
            trigger_param: parseFloat(c.trigger_param),
            trigger_keywords: c.trigger_keywords || null,
            inject_into:  c.inject_into || 'chat',
          })),
        };
        const res = await fetch(
          getBaseUrl() + API_PATHS.profileSegments(profileId),
          { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
        );
        const data = await res.json();
        if (data.ok) {
          if (!skipToast) this.showToast(this.t('toastSegmentSaved'), 'success');
        } else {
          if (!skipToast) this.showToast(`段落保存失败: ${data.error || ''}`, 'error');
        }
      } catch (e) {
        if (!skipToast) this.showToast(`段落保存失败: ${e.message}`, 'error');
      } finally {
        this.segmentSaving = false;
      }
    },

    /** 人格页：一次性保存该人格的基础信息、引擎配置、段落配置、重要日期 */
    async saveCurrentProfileAll() {
      const profileId = this.profileForm?.profile_id || this.selectedProfileId;
      if (!profileId || this.profileAllSaving) return;
      this.profileAllSaving = true;
      const opts = { skipToast: true };
      try {
        await this.saveProfile(opts);
        await this.saveProfileEngineConfig(opts);
        await this.saveSegmentConfig(opts);
        await this.saveSpecialDates(opts);
        // 自省 + ASE 段落（时段问候、沉默后首条等）写在 segments_config.json，须走 reflection_config PUT；此前若只点「保存全部」会丢改动的触发参数
        if (
          this.reflectionFormProfileId === profileId &&
          Array.isArray(this.reflectionProfileForm.segments) &&
          this.reflectionProfileForm.segments.length > 0
        ) {
          await this.saveReflectionProfileConfig({ skipToast: true });
        }
        this.showToast(this.t('toastProfileAllSaved'), 'success');
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.profileAllSaving = false;
      }
    },

    textareaRows(text, min = 3, max = 25) {
      if (!text) return min;
      return Math.min(max, Math.max(min, (text.match(/\n/g) || []).length + 1));
    },

    _buildPromptDict(source, keys) {
      const out = {};
      for (const k of keys) {
        const v = (source[k] || '').trim();
        if (v) out[k] = v;
      }
      return out;
    },

    _buildEmotionPromptsObj() {
      const out = {};
      for (const [k, v] of Object.entries(this.profilePromptForm.emotion_prompts)) {
        const text = (v || '').trim();
        if (text) out[k] = text.split('\n').filter(l => l !== '');
      }
      return out; // {} = clear override
    },

    _buildEmotionZhDescriptions() {
      const out = {};
      for (const [k, v] of Object.entries(this.profilePromptForm.emotion_zh_descriptions)) {
        const text = (v || '').trim();
        if (text) out[k] = text;
      }
      return out;
    },

    /* ─────────────── Profile Wizard ─────────────── */

    openProfileWizard() {
      this.profileWizardOpen = true;
      this.profileWizardDesc = '';
      this.profileWizardResult = null;
      this.profileWizardError = '';
      this.profileWizardPresetUsed = '';
      this.profileWizardGenerating = false;
      this.profileWizardDisableEmotion    = false;
      this.profileWizardDisableAffinity   = false;
      this.profileWizardDisableReflection = false;
      this.profileWizardDisableMemory     = false;
    },

    applyPromptAutofillPatch(patch) {
      if (!patch || typeof patch !== 'object') return;
      if (patch.style_constraint != null && String(patch.style_constraint).trim()) {
        this.profileForm.style_constraint = String(patch.style_constraint).trim();
      }
      if (patch.emotion_prompts && typeof patch.emotion_prompts === 'object') {
        for (const [k, v] of Object.entries(patch.emotion_prompts)) {
          const lines = Array.isArray(v)
            ? v.map((x) => String(x).trim()).filter(Boolean)
            : String(v || '').split('\n').map((l) => l.trim()).filter(Boolean);
          if (lines.length) this.profilePromptForm.emotion_prompts[k] = lines.join('\n');
        }
      }
      if (patch.emotion_zh_descriptions && typeof patch.emotion_zh_descriptions === 'object') {
        for (const [k, v] of Object.entries(patch.emotion_zh_descriptions)) {
          const s = String(v || '').trim();
          if (s) this.profilePromptForm.emotion_zh_descriptions[k] = s;
        }
      }
      if (patch.energy_prompts && typeof patch.energy_prompts === 'object') {
        for (const [k, v] of Object.entries(patch.energy_prompts)) {
          const s = String(v || '').trim();
          if (s) this.profilePromptForm.energy_prompts[k] = s;
        }
      }
      if (patch.affinity_prompts && typeof patch.affinity_prompts === 'object') {
        for (const [k, v] of Object.entries(patch.affinity_prompts)) {
          const s = String(v || '').trim();
          if (s) this.profilePromptForm.affinity_prompts[k] = s;
        }
      }
      if (patch.reflection_custom_prompt != null && String(patch.reflection_custom_prompt).trim()) {
        this.reflectionProfileForm.custom_prompt = String(patch.reflection_custom_prompt).trim();
      }
      if (patch.memory_extraction_prompt != null && String(patch.memory_extraction_prompt).trim()) {
        this.memoryProfileForm.extraction_prompt = String(patch.memory_extraction_prompt).trim();
      }
      if (patch.memory_day_summary_prompt != null && String(patch.memory_day_summary_prompt).trim()) {
        this.memoryProfileForm.day_summary_prompt = String(patch.memory_day_summary_prompt).trim();
      }
      if (patch.core_anchor != null && String(patch.core_anchor).trim()) {
        this.personaEvolutionForm.core_anchor = String(patch.core_anchor).trim();
      }
    },

    async runPromptAutofill() {
      const profileId = this.profileForm?.profile_id;
      if (!profileId || this.promptAutofillBusy) return;
      const base = (this.profileForm.base_prompt || '').trim();
      if (base.length < 15) {
        this.showToast(this.t('toastPromptAutofillNeedBase'), 'error');
        return;
      }
      this.promptAutofillBusy = true;
      this.promptAutofillErrorRaw = '';
      this.promptAutofillShowDebug = false;
      try {
        const snapshot = {
          base_prompt: this.profileForm.base_prompt || '',
          style_constraint: this.profileForm.style_constraint || '',
          emotion_prompts: { ...this.profilePromptForm.emotion_prompts },
          emotion_zh_descriptions: { ...this.profilePromptForm.emotion_zh_descriptions },
          energy_prompts: { ...this.profilePromptForm.energy_prompts },
          affinity_prompts: { ...this.profilePromptForm.affinity_prompts },
          reflection_custom_prompt: this.reflectionProfileForm?.custom_prompt || '',
          memory_extraction_prompt: this.memoryProfileForm?.extraction_prompt || '',
          memory_day_summary_prompt: this.memoryProfileForm?.day_summary_prompt || '',
        };
        const res = await fetch(getBaseUrl() + API_PATHS.profilePromptAutofill(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            fill_empty_only: !this.promptAutofillAllowOverwrite,
            scopes: [],
            snapshot,
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          const detail = data.detail || data.error || `HTTP ${res.status}`;
          this.promptAutofillErrorRaw = typeof detail === 'string' ? detail : JSON.stringify(detail);
          this.showToast(this.t('toastPromptAutofillFail'), 'error');
          return;
        }
        if (data.ok && data.patch && Object.keys(data.patch).length) {
          this.applyPromptAutofillPatch(data.patch);
          const keys = (data.filled_keys && data.filled_keys.length) ? data.filled_keys.join(', ') : '';
          this.showToast(keys ? `${this.t('toastPromptAutofillOk')} (${keys})` : this.t('toastPromptAutofillOk'), 'success');
        } else if (data.ok) {
          this.showToast(this.t('toastPromptAutofillNone'), 'success');
        } else {
          this.promptAutofillErrorRaw = data.raw || data.error || '';
          this.showToast(data.error || this.t('toastPromptAutofillFail'), 'error');
        }
      } catch (e) {
        this.promptAutofillErrorRaw = String(e.message || e);
        this.showToast(`${this.t('toastPromptAutofillFail')}: ${e.message}`, 'error');
      } finally {
        this.promptAutofillBusy = false;
      }
    },

    async runProfileWizard() {
      if (!this.profileWizardDesc.trim() || this.profileWizardGenerating) return;
      this.profileWizardGenerating = true;
      this.profileWizardError = '';
      this.profileWizardResult = null;
      try {
        const res = await fetch(getBaseUrl() + '/profiles/wizard', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            description:        this.profileWizardDesc,
            disable_emotion:    !!this.profileWizardDisableEmotion,
            disable_affinity:   !!this.profileWizardDisableAffinity,
            disable_reflection: !!this.profileWizardDisableReflection,
            disable_memory:     !!this.profileWizardDisableMemory,
          }),
        });
        const data = await res.json();
        if (data.ok && data.profile) {
          const p = data.profile;
          this.profileWizardResult = {
            display_name:              p.display_name || '',
            base_prompt:               p.base_prompt || '',
            style_constraint:          p.style_constraint || '',
            emotion_prompts:           p.emotion_prompts || {},
            emotion_zh_descriptions:   p.emotion_zh_descriptions || {},
            energy_prompts:            p.energy_prompts || {},
            affinity_prompts:          p.affinity_prompts || {},
            reflection_custom_prompt:  p.reflection_custom_prompt || '',
            memory_extraction_prompt:  p.memory_extraction_prompt || '',
            memory_day_summary_prompt: p.memory_day_summary_prompt || '',
          };
          this.profileWizardPresetUsed = data.preset_used || '';
        } else {
          this.profileWizardError = data.error || data.detail || '生成失败，请重试';
        }
      } catch (e) {
        this.profileWizardError = `请求失败：${e.message}`;
      } finally {
        this.profileWizardGenerating = false;
      }
    },

    /** Apply wizard result to form fields and save via direct API calls (bypasses async form-load races). */
    async _saveWizardToProfile(profileId) {
      const r = this.profileWizardResult;
      if (!r) return;

      // display_name: use wizard result name, or fall back to current form, or profile id
      const displayName = r.display_name || this.profileForm.display_name || profileId;

      // Build emotion_prompts: wizard returns {key: [lines]} or {key: "text"}; convert to array
      const emotionPromptsObj = {};
      for (const [k, v] of Object.entries(r.emotion_prompts || {})) {
        emotionPromptsObj[k] = Array.isArray(v) ? v : (v || '').split('\n').filter(Boolean);
      }

      // Only send non-empty dicts so we don't accidentally wipe existing data
      const profileBody = {
        display_name: displayName,
        base_prompt: r.base_prompt || '',
        style_constraint: r.style_constraint || '',
      };
      if (!this.profileWizardDisableEmotion) {
        if (Object.keys(emotionPromptsObj).length)
          profileBody.emotion_prompts = emotionPromptsObj;
        if (r.emotion_zh_descriptions && Object.keys(r.emotion_zh_descriptions).length)
          profileBody.emotion_zh_descriptions = r.emotion_zh_descriptions;
        if (r.energy_prompts && Object.keys(r.energy_prompts).length)
          profileBody.energy_prompts = r.energy_prompts;
      }
      if (!this.profileWizardDisableAffinity) {
        if (r.affinity_prompts && Object.keys(r.affinity_prompts).length)
          profileBody.affinity_prompts = r.affinity_prompts;
      }

      // 1. Save main profile (base_prompt, style_constraint, emotion/energy/affinity)
      const profileRes = await fetch(getBaseUrl() + API_PATHS.profile(profileId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(profileBody),
      });
      if (!profileRes.ok) {
        const err = await profileRes.json().catch(() => ({}));
        throw new Error(err.detail || `profile save failed: ${profileRes.status}`);
      }
      // 2. Save reflection custom_prompt
      if (!this.profileWizardDisableReflection && r.reflection_custom_prompt) {
        await fetch(getBaseUrl() + API_PATHS.profileReflectionConfig(profileId), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ custom_prompt: r.reflection_custom_prompt }),
        });
      }
      // 2b. Save core_anchor if wizard generated it
      if (r.core_anchor && String(r.core_anchor).trim()) {
        await fetch(getBaseUrl() + API_PATHS.profilePersonaEvolution(profileId), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ core_anchor: String(r.core_anchor).trim() }),
        });
      }
      // 3. Save memory extraction/day-summary prompt
      if (!this.profileWizardDisableMemory && (r.memory_extraction_prompt || r.memory_day_summary_prompt)) {
        await fetch(getBaseUrl() + API_PATHS.profileMemoryConfig(profileId), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            extraction_prompt: r.memory_extraction_prompt || '',
            day_summary_prompt: r.memory_day_summary_prompt || '',
          }),
        });
      }
    },

    async applyWizardToNew() {
      if (!this.profileWizardResult) return;
      const name = this.profileWizardResult.display_name || '新人格';
      const profileId = name.toLowerCase()
        .replace(/\s+/g, '_').replace(/[^a-z0-9_\u4e00-\u9fa5]/g, '')
        || `wizard_${Date.now()}`;
      try {
        const res = await fetch(getBaseUrl() + '/profiles/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: name, profile_id: profileId }),
        });
        const data = await res.json();
        if (!data.ok) { this.showToast(`创建失败：${data.error || ''}`, 'error'); return; }
        const pid = data.profile_id || profileId;
        await this._saveWizardToProfile(pid);
        await this.loadProfiles();
        this.selectProfile(pid);
        this.profileWizardOpen = false;
        this.profileSubTab = 'prompts';
        this.showToast('✓ 新人格已创建并保存', 'success');
      } catch (e) {
        this.showToast(`创建失败：${e.message}`, 'error');
      }
    },

    async applyWizardToCurrent() {
      if (!this.profileWizardResult || !this.selectedProfileId) return;
      try {
        if (this.profileWizardResult.display_name)
          this.profileForm.display_name = this.profileWizardResult.display_name;
        await this._saveWizardToProfile(this.selectedProfileId);
        await this.loadProfiles();
        this.selectProfile(this.selectedProfileId);
        this.profileWizardOpen = false;
        this.profileSubTab = 'prompts';
        this.showToast('✓ 已覆盖当前人格并保存', 'success');
      } catch (e) {
        this.showToast(`保存失败：${e.message}`, 'error');
      }
    },

    beginAddCustomSeg() {
      const uid = `custom_${Date.now()}`;
      this.newCustomSeg = {
        segment_id: uid, label: '', content: '',
        priority: 50, enabled: true,
        trigger_mode: 'always', trigger_param: 1.0,
        trigger_keywords: null,
        inject_into: 'chat',
      };
    },

    confirmAddCustomSeg() {
      if (!this.newCustomSeg) return;
      if (!this.newCustomSeg.label.trim()) {
        this.showToast('请填写段落名称', 'error');
        return;
      }
      if (!this.newCustomSeg.content.trim()) {
        this.showToast('请填写注入内容', 'error');
        return;
      }
      this.customSegmentList.push({ ...this.newCustomSeg, inject_into: this.newCustomSeg.inject_into || 'chat' });
      this.newCustomSeg = null;
    },

    cancelAddCustomSeg() {
      this.newCustomSeg = null;
    },

    deleteCustomSeg(idx) {
      this.customSegmentList.splice(idx, 1);
    },

    segTriggerLabel(mode) {
      return { always: '始终', probability: '概率', cooldown: '冷却' }[mode] || mode;
    },

    async loadEmotionStandardKeys() {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsEmotionKeys());
        if (!res.ok) return;
        const data = await res.json();
        if (Array.isArray(data.keys) && data.keys.length)
          this.emotionStandardKeys = data.keys;
      } catch (_) {}
    },

    /* ─────────────── System ─────────────── */

    async loadSystemConfig() {
      this._systemFormLoaded = false;
      // Restore bubble size from localStorage (frontend-only, no backend involved)
      const savedFontSize = localStorage.getItem('bubble_font_size');
      const savedBubbleWidth = localStorage.getItem('bubble_max_width');
      if (savedFontSize)    this.systemForm.font_size    = parseInt(savedFontSize, 10);
      if (savedBubbleWidth) this.systemForm.bubble_width = parseInt(savedBubbleWidth, 10);
      this.applyBubbleSize();

      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsSystem());
        const data = await res.json();
        this.systemForm.log_level = data.log_level || 'info';
        this.serverAddress = data.server_address || '';
      } catch (_) {}
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsSystemEnv());
        if (res.ok) {
          const data = await res.json();
          this.systemEnvForm.proxy_enabled = !!data.proxy_enabled;
          this.systemEnvForm.proxy_url = data.proxy_url || '';
        }
      } catch (_) {}
      try {
        const res = await fetch(getBaseUrl() + '/api/preferences');
        if (res.ok) {
          const prefs = await res.json();
          this.uiPrefsShowLauncher = prefs.show_startup_launcher !== false;
        }
      } catch (_) {}
      this.$nextTick(() => { this._systemFormLoaded = true; });
    },

    async saveUiPrefsLauncher() {
      if (this.uiPrefsSaving) return;
      this.uiPrefsSaving = true;
      try {
        await fetch(getBaseUrl() + '/api/preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ show_startup_launcher: !!this.uiPrefsShowLauncher }),
        });
      } catch (_) {}
      finally {
        this.uiPrefsSaving = false;
      }
    },

    async saveSystemConfig(silent = false) {
      if (this.systemSaving) return;
      this.systemSaving = true;
      if (silent) this.setAutoSaveState('saving');
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsSystem(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ log_level: this.systemForm.log_level }),
        });
        const data = await res.json();
        if (data.ok) { if (silent) this.setAutoSaveState('saved'); else this.showToast(this.t('toastSystemSaved'), 'success'); }
        else { if (silent) this.setAutoSaveState(null); this.showToast(`保存失败: ${data.error || ''}`, 'error'); }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.systemSaving = false;
      }
    },

    async saveSystemEnv(silent = false) {
      if (this.systemEnvSaving) return;
      this.systemEnvSaving = true;
      if (silent) this.setAutoSaveState('saving');
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsSystemEnv(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.systemEnvForm),
        });
        const data = await res.json();
        if (data.ok) { if (silent) this.setAutoSaveState('saved'); else this.showToast(this.t('toastEnvSaved'), 'success'); }
        else { if (silent) this.setAutoSaveState(null); this.showToast(`保存失败: ${data.error || ''}`, 'error'); }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.systemEnvSaving = false;
      }
    },

    /* ─────────────── Auto-updater ─────────────── */
    checkForUpdates() {
      if (window.electronAPI && window.electronAPI.checkForUpdates) {
        this.updateStatus = 'checking';
        window.electronAPI.checkForUpdates();
      }
    },
    downloadUpdate() {
      if (window.electronAPI && window.electronAPI.downloadUpdate)
        window.electronAPI.downloadUpdate();
    },
    installUpdate() {
      if (window.electronAPI && window.electronAPI.installUpdate)
        window.electronAPI.installUpdate();
    },

    /* ─────────────── Engines & Analysis Model (P4) ─────────────── */

    async loadEnginesConfig() {
      this._enginesFormLoaded = false;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsEngines());
        if (!res.ok) return;
        const data = await res.json();
        this.enginesForm.emotion_freq                 = data.emotion_freq                 ?? 5;
        this.enginesForm.emotion_decay_enabled        = data.emotion_decay_enabled        ?? true;
        this.enginesForm.emotion_decay_full_secs      = data.emotion_decay_full_secs      ?? 14400;
        this.enginesForm.emotion_decay_skip_if_active = data.emotion_decay_skip_if_active ?? 300;
        this.enginesForm.emotion_neutral              = data.emotion_neutral              ?? 'calm';
        this.enginesForm.affinity_freq        = data.affinity_freq        ?? 5;
        this.enginesForm.affinity_delta_clamp = data.affinity_delta_clamp ?? 15;
        this.enginesForm.energy_interval      = data.energy_interval      ?? 300;
        this.analysisModelEnabled = data.analysis_enabled ?? false;
        this.analysisModelPreset  = data.analysis_preset  ?? '';
        // Load override params
        const ao = this.analysisOverride;
        ao.temperature         = data.analysis_temperature        ?? 0.5;
        ao.temperature_enabled = data.analysis_temperature        != null;
        ao.top_p               = data.analysis_top_p              ?? 0.95;
        ao.top_p_enabled       = data.analysis_top_p              != null;
        ao.presence_penalty    = data.analysis_presence_penalty   ?? 0.0;
        ao.presence_penalty_enabled = data.analysis_presence_penalty != null;
        ao.frequency_penalty   = data.analysis_frequency_penalty  ?? 0.0;
        ao.frequency_penalty_enabled = data.analysis_frequency_penalty != null;
        ao.max_tokens          = data.analysis_max_tokens         ?? 512;
        ao.max_tokens_enabled  = data.analysis_max_tokens         != null;
      } catch (e) {
        console.error('[settings] loadEnginesConfig:', e);
      }
      this.$nextTick(() => { this._enginesFormLoaded = true; });
    },

    async saveEnginesConfig(silent = false) {
      if (this.enginesSaving) return;
      this.enginesSaving = true;
      if (silent) this.setAutoSaveState('saving');
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsEngines(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            emotion_freq:                 parseInt(this.enginesForm.emotion_freq, 10) || 5,
            emotion_decay_enabled:        !!this.enginesForm.emotion_decay_enabled,
            emotion_decay_full_secs:      Math.max(0, parseInt(this.enginesForm.emotion_decay_full_secs, 10) || 0),
            emotion_decay_skip_if_active: Math.max(0, parseInt(this.enginesForm.emotion_decay_skip_if_active, 10) || 0),
            emotion_neutral:              (this.enginesForm.emotion_neutral || 'calm').toString(),
            affinity_freq:        parseInt(this.enginesForm.affinity_freq, 10) || 5,
            affinity_delta_clamp: parseFloat(this.enginesForm.affinity_delta_clamp) || 15,
            energy_interval:      parseInt(this.enginesForm.energy_interval, 10) || 300,
          }),
        });
        const data = await res.json();
        if (data.ok) { if (silent) this.setAutoSaveState('saved'); else this.showToast(this.t('toastEnginesSaved'), 'success'); }
        else this.showToast(`保存失败: ${data.error || ''}`, 'error');
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.enginesSaving = false;
      }
    },

    async loadUserPersona() {
      this._userPersonaFormLoaded = false;
      try {
        const res = await fetch(getBaseUrl() + '/settings/user_persona');
        const data = await res.json();
        this.userPersonaForm.name         = data.name         || '';
        this.userPersonaForm.introduction = data.introduction || '';
      } catch (e) {
        console.error('[settings] loadUserPersona:', e);
      }
      this.$nextTick(() => { this._userPersonaFormLoaded = true; });
    },

    async saveUserPersona(silent = false) {
      if (this.userPersonaSaving) return;
      this.userPersonaSaving = true;
      if (silent) this.setAutoSaveState('saving');
      try {
        const res = await fetch(getBaseUrl() + '/settings/user_persona', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name:         this.userPersonaForm.name.trim(),
            introduction: this.userPersonaForm.introduction.trim(),
          }),
        });
        const data = await res.json();
        if (data.ok) { if (silent) this.setAutoSaveState('saved'); else this.showToast(this.t('toastSaved') || '已保存', 'success'); }
        else { if (silent) this.setAutoSaveState(null); this.showToast(`保存失败: ${data.error || ''}`, 'error'); }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.userPersonaSaving = false;
      }
    },

    // ── User Portrait (AI-maintained) ────────────────────────────────────
    async loadUserPortrait(profileId) {
      if (!profileId) return;
      this._userPortraitFormLoaded = false;
      try {
        const res = await fetch(getBaseUrl() + `/profiles/${encodeURIComponent(profileId)}/user_portrait`);
        if (!res.ok) return;
        const data = await res.json();
        const cfg = data.config || {};
        // 整体替换 — 仿 personaEvolutionForm 的模式
        this.userPortraitForm = {
          content:                     data.content || '',
          updated_at:                  data.updated_at || 0,
          refresh_count:               data.refresh_count || 0,
          changelog_count:             data.changelog_count || 0,
          enabled:                     cfg.enabled !== false,
          auto_refresh_in_daily_job:   cfg.auto_refresh_in_daily_job !== false,
          min_turns_for_burst_refresh: cfg.min_turns_for_burst_refresh ?? 100,
          min_hours_between_refresh:   cfg.min_hours_between_refresh ?? 6,
          use_day_summary_as_input:    cfg.use_day_summary_as_input !== false,
          use_facts_as_input:          cfg.use_facts_as_input !== false,
          max_input_conv_turns:        cfg.max_input_conv_turns ?? 60,
        };
        this.$nextTick(() => { this._userPortraitFormLoaded = true; });
      } catch (e) {
        console.error('[settings] loadUserPortrait:', e);
      }
    },

    async saveUserPortraitContent(silent = true) {
      const pid = this.selectedProfileId;
      if (!pid) return;
      try {
        if (silent) this.setAutoSaveState('saving');
        const res = await fetch(getBaseUrl() + `/profiles/${encodeURIComponent(pid)}/user_portrait/edit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: this.userPortraitForm.content || '' }),
        });
        const data = await res.json();
        if (data.ok) { if (silent) this.setAutoSaveState('saved'); }
        else { if (silent) this.setAutoSaveState(null); this.showToast('画像保存失败', 'error'); }
      } catch (e) {
        this.showToast(`画像保存失败: ${e.message}`, 'error');
      }
    },

    async saveUserPortraitConfig(silent = true) {
      const pid = this.selectedProfileId;
      if (!pid) return;
      try {
        if (silent) this.setAutoSaveState('saving');
        const f = this.userPortraitForm;
        const res = await fetch(getBaseUrl() + `/profiles/${encodeURIComponent(pid)}/user_portrait/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            enabled:                     !!f.enabled,
            auto_refresh_in_daily_job:   !!f.auto_refresh_in_daily_job,
            min_turns_for_burst_refresh: parseInt(f.min_turns_for_burst_refresh, 10) || 0,
            min_hours_between_refresh:   parseInt(f.min_hours_between_refresh, 10) || 0,
            use_day_summary_as_input:    !!f.use_day_summary_as_input,
            use_facts_as_input:          !!f.use_facts_as_input,
            max_input_conv_turns:        parseInt(f.max_input_conv_turns, 10) || 60,
          }),
        });
        const data = await res.json();
        if (data.ok) { if (silent) this.setAutoSaveState('saved'); }
        else { if (silent) this.setAutoSaveState(null); }
      } catch (e) {
        console.error('[settings] saveUserPortraitConfig:', e);
      }
    },

    async refreshUserPortrait() {
      const pid = this.selectedProfileId;
      if (!pid || this.userPortraitRefreshing) return;
      this.userPortraitRefreshing = true;
      try {
        const res = await fetch(getBaseUrl() + `/profiles/${encodeURIComponent(pid)}/user_portrait/refresh`, {
          method: 'POST',
        });
        const data = await res.json();
        if (data.ok) {
          this.showToast(this.locale === 'en' ? 'Portrait refreshed' : '画像已刷新', 'success');
          await this.loadUserPortrait(pid);
        } else {
          this.showToast(data.reason || (this.locale === 'en' ? 'Refresh skipped' : '刷新被跳过'), 'info');
        }
      } catch (e) {
        this.showToast(`刷新失败: ${e.message}`, 'error');
      } finally {
        this.userPortraitRefreshing = false;
      }
    },

    async resetUserPortrait() {
      const pid = this.selectedProfileId;
      if (!pid || this.userPortraitResetting) return;
      const ok = window.confirm(this.locale === 'en'
        ? 'Clear current portrait content? (the seed introduction is kept; refresh_count is preserved)'
        : '确认清空当前画像？（self-introduction 种子保留，refresh_count 也保留）');
      if (!ok) return;
      this.userPortraitResetting = true;
      try {
        const res = await fetch(getBaseUrl() + `/profiles/${encodeURIComponent(pid)}/user_portrait/reset`, {
          method: 'POST',
        });
        const data = await res.json();
        if (data.ok) {
          await this.loadUserPortrait(pid);
          this.showToast(this.locale === 'en' ? 'Portrait cleared' : '画像已清空', 'success');
        }
      } catch (e) {
        this.showToast(`重置失败: ${e.message}`, 'error');
      } finally {
        this.userPortraitResetting = false;
      }
    },

    async toggleUserPortraitChangelog() {
      this.userPortraitShowChangelog = !this.userPortraitShowChangelog;
      if (this.userPortraitShowChangelog) {
        await this.loadUserPortraitChangelog(this.selectedProfileId);
      }
    },

    async loadUserPortraitChangelog(profileId) {
      if (!profileId) return;
      try {
        const res = await fetch(getBaseUrl() + `/profiles/${encodeURIComponent(profileId)}/user_portrait/changelog`);
        const data = await res.json();
        this.userPortraitChangelog = data.entries || [];
      } catch (e) {
        console.error('[settings] loadUserPortraitChangelog:', e);
      }
    },

    async rollbackUserPortrait(version) {
      const pid = this.selectedProfileId;
      if (!pid) return;
      const ok = window.confirm(this.locale === 'en'
        ? `Rollback portrait to version ${version}?`
        : `回滚画像到版本 ${version}？`);
      if (!ok) return;
      try {
        const res = await fetch(getBaseUrl() + `/profiles/${encodeURIComponent(pid)}/user_portrait/rollback`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version }),
        });
        const data = await res.json();
        if (data.ok) {
          await this.loadUserPortrait(pid);
          await this.loadUserPortraitChangelog(pid);
          this.showToast(this.locale === 'en' ? 'Rolled back' : '已回滚', 'success');
        }
      } catch (e) {
        this.showToast(`回滚失败: ${e.message}`, 'error');
      }
    },

    formatPortraitTime(ts) {
      if (!ts) return this.locale === 'en' ? 'never' : '从未';
      try {
        const d = new Date(ts * 1000);
        return d.toLocaleString(this.locale === 'en' ? 'en-US' : 'zh-CN');
      } catch (e) {
        return String(ts);
      }
    },

    async saveAnalysisModel(silent = false) {
      if (this.analysisModelSaving) return;
      this.analysisModelSaving = true;
      if (silent) this.setAutoSaveState('saving');
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsSecondaryModels(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            analysis_enabled:            this.analysisModelEnabled,
            analysis_preset:             this.analysisModelPreset,
            analysis_temperature:        this.analysisOverride.temperature_enabled        ? parseFloat(this.analysisOverride.temperature)       : null,
            analysis_top_p:              this.analysisOverride.top_p_enabled              ? parseFloat(this.analysisOverride.top_p)              : null,
            analysis_presence_penalty:   this.analysisOverride.presence_penalty_enabled   ? parseFloat(this.analysisOverride.presence_penalty)   : null,
            analysis_frequency_penalty:  this.analysisOverride.frequency_penalty_enabled  ? parseFloat(this.analysisOverride.frequency_penalty)  : null,
            analysis_max_tokens:         this.analysisOverride.max_tokens_enabled         ? (parseInt(this.analysisOverride.max_tokens, 10) || null) : null,
          }),
        });
        const data = await res.json();
        if (data.ok) { if (silent) this.setAutoSaveState('saved'); else this.showToast(this.t('toastAnalysisModelSaved'), 'success'); }
        else { if (silent) this.setAutoSaveState(null); this.showToast(`保存失败: ${data.error || ''}`, 'error'); }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.analysisModelSaving = false;
      }
    },

    /* ─────────────── Memory (P6) ─────────────── */

    async loadMemoryGlobalConfig() {
      this._memGlobalFormLoaded = false;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsMemory());
        if (!res.ok) return;
        const data = await res.json();
        this.memoryGlobalForm = {
          enabled:               data.enabled               ?? false,
          vector_enabled:        data.vector_enabled        ?? false,
          extraction_frequency:  data.extraction_frequency  ?? 5,
          extraction_weight_threshold: data.extraction_weight_threshold ?? 0.5,
          max_facts_in_prompt:   data.max_facts_in_prompt   ?? 8,
          max_history_turns:     data.max_history_turns     ?? 20,
          day_summary_enabled:     data.day_summary_enabled     ?? false,
          day_summary_keep_days:   data.day_summary_keep_days   ?? 14,
          day_summary_max_messages:   data.day_summary_max_messages   ?? 100,
          day_summary_max_conv_chars: data.day_summary_max_conv_chars ?? 12000,
          recent_fact_quota: data.recent_fact_quota ?? 3,
          semantic_fact_quota: data.semantic_fact_quota ?? 2,
          recent_days:       data.recent_days       ?? 3,
          semantic_distance: data.semantic_distance ?? 0.45,
          vector_dedup_distance_threshold: data.vector_dedup_distance_threshold ?? 0.1,
          daily_run_at_hour: data.daily_run_at_hour ?? 0,
          daily_run_at_minute: data.daily_run_at_minute ?? 5,
          daily_forgetting_enabled: data.daily_forgetting_enabled ?? false,
          daily_decay_factor: data.daily_decay_factor ?? 0.998,
          daily_decay_min_weight: data.daily_decay_min_weight ?? 0.1,
          daily_reinforcement_enabled: data.daily_reinforcement_enabled ?? false,
          daily_consolidation_enabled: data.daily_consolidation_enabled ?? false,
          daily_consolidation_after_days: data.daily_consolidation_after_days ?? 30,
          daily_consolidation_weight_below: data.daily_consolidation_weight_below ?? 0.3,
          daily_consolidation_batch_max: data.daily_consolidation_batch_max ?? 15,
        };
        this.$nextTick(() => { this._memGlobalFormLoaded = true; });
      } catch (e) {
        console.error('[settings] loadMemoryGlobalConfig:', e);
      }
    },

    async saveMemoryGlobalConfig(silent = false) {
      if (this.memoryGlobalSaving) return;
      this.memoryGlobalSaving = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsMemory(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            enabled:               this.memoryGlobalForm.enabled,
            vector_enabled:        this.memoryGlobalForm.vector_enabled,
            extraction_frequency:  parseInt(this.memoryGlobalForm.extraction_frequency, 10) || 5,
            extraction_weight_threshold: Math.max(0, Math.min(1, parseFloat(this.memoryGlobalForm.extraction_weight_threshold) || 0.5)),
            max_facts_in_prompt:   parseInt(this.memoryGlobalForm.max_facts_in_prompt, 10) || 8,
            max_history_turns:    Math.max(1, Math.min(200, parseInt(this.memoryGlobalForm.max_history_turns, 10) || 20)),
            day_summary_enabled:     this.memoryGlobalForm.day_summary_enabled,
            day_summary_keep_days:   parseInt(this.memoryGlobalForm.day_summary_keep_days, 10) || 14,
            day_summary_max_messages:   Math.max(10, Math.min(500, parseInt(this.memoryGlobalForm.day_summary_max_messages, 10) || 100)),
            day_summary_max_conv_chars: Math.max(2000, Math.min(100000, parseInt(this.memoryGlobalForm.day_summary_max_conv_chars, 10) || 12000)),
            recent_fact_quota: parseInt(this.memoryGlobalForm.recent_fact_quota, 10) ?? 3,
            semantic_fact_quota: parseInt(this.memoryGlobalForm.semantic_fact_quota, 10) ?? 2,
            recent_days:       parseInt(this.memoryGlobalForm.recent_days, 10) ?? 3,
            semantic_distance: parseFloat(this.memoryGlobalForm.semantic_distance) ?? 0.45,
            vector_dedup_distance_threshold: Math.max(0.01, Math.min(0.5, parseFloat(this.memoryGlobalForm.vector_dedup_distance_threshold) || 0.1)),
            daily_run_at_hour: Math.max(0, Math.min(23, parseInt(this.memoryGlobalForm.daily_run_at_hour, 10) || 0)),
            daily_run_at_minute: Math.max(0, Math.min(59, parseInt(this.memoryGlobalForm.daily_run_at_minute, 10) || 5)),
            daily_forgetting_enabled: !!this.memoryGlobalForm.daily_forgetting_enabled,
            daily_decay_factor: Math.max(0.9, Math.min(1, parseFloat(this.memoryGlobalForm.daily_decay_factor) || 0.998)),
            daily_decay_min_weight: Math.max(0, Math.min(1, parseFloat(this.memoryGlobalForm.daily_decay_min_weight) || 0.1)),
            daily_reinforcement_enabled: !!this.memoryGlobalForm.daily_reinforcement_enabled,
            daily_consolidation_enabled: !!this.memoryGlobalForm.daily_consolidation_enabled,
            daily_consolidation_after_days: Math.max(7, Math.min(365, parseInt(this.memoryGlobalForm.daily_consolidation_after_days, 10) || 30)),
            daily_consolidation_weight_below: Math.max(0, Math.min(1, parseFloat(this.memoryGlobalForm.daily_consolidation_weight_below) || 0.3)),
            daily_consolidation_batch_max: Math.max(5, Math.min(30, parseInt(this.memoryGlobalForm.daily_consolidation_batch_max, 10) || 15)),
          }),
        });
        const data = await res.json();
        if (data.ok) {
          if (!silent) this.showToast(this.t('toastMemoryGlobalSaved'), 'success');
          this.memGlobalSaveState = 'saved';
          setTimeout(() => { this.memGlobalSaveState = null; }, 2000);
        } else {
          this.showToast(`保存失败: ${data.error || ''}`, 'error');
        }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.memoryGlobalSaving = false;
      }
    },

    async loadEmbeddingConfig() {
      this._embeddingFormLoaded = false;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsEmbedding());
        if (!res.ok) return;
        const d = await res.json();
        this.embeddingForm.provider    = d.provider    ?? 'gemini';
        this.embeddingForm.model       = d.model       ?? 'gemini-embedding-001';
        this.embeddingForm.base_url    = d.base_url    ?? '';
        this.embeddingForm.has_key     = !!d.has_key;
        this.embeddingForm.api_key     = '';  // never pre-fill
        this.embeddingForm.local_model = d.local_model ?? 'all-MiniLM-L6-v2';
        this.embeddingForm.device      = d.device      ?? 'auto';
        this.embeddingForm.offline     = d.offline     ?? false;
      } catch (e) {
        console.error('[settings] loadEmbeddingConfig:', e);
      }
      this.$nextTick(() => { this._embeddingFormLoaded = true; });
    },

    async saveEmbeddingConfig(silent = false) {
      if (this.embeddingSaving) return;
      this.embeddingSaving = true;
      if (silent) this.setAutoSaveState('saving');
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsEmbedding(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            provider:    this.embeddingForm.provider,
            model:       this.embeddingForm.model || '',
            base_url:    this.embeddingForm.base_url || '',
            api_key:     this.embeddingForm.api_key || '',
            local_model: this.embeddingForm.local_model || 'all-MiniLM-L6-v2',
            device:      this.embeddingForm.device || 'auto',
            offline:     !!this.embeddingForm.offline,
          }),
        });
        const d = await res.json();
        if (d.ok) {
          this.embeddingForm.api_key = '';
          await this.loadEmbeddingConfig();
          if (silent) this.setAutoSaveState('saved'); else this.showToast(this.t('toastEmbeddingSaved'), 'success');
        } else {
          this.showToast(d.error || this.t('toastSaveFailed'), 'error');
        }
      } catch (e) {
        this.showToast(this.t('toastSaveFailed') + ': ' + e.message, 'error');
      } finally {
        this.embeddingSaving = false;
      }
    },

    async testEmbedding() {
      if (this.embedTestLoading) return;
      this.embedTestLoading = true;
      this.embedTestResult = null;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsEmbeddingTest(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            provider:    this.embeddingForm.provider,
            model:       this.embeddingForm.model || '',
            base_url:    this.embeddingForm.base_url || '',
            api_key:     this.embeddingForm.api_key || '',
            local_model: this.embeddingForm.local_model || 'all-MiniLM-L6-v2',
            offline:     !!this.embeddingForm.offline,
            device:      this.embeddingForm.device || 'auto',
          }),
        });
        const data = await res.json();
        this.embedTestResult = data;
      } catch (e) {
        this.embedTestResult = { ok: false, error: e.message };
      } finally {
        this.embedTestLoading = false;
      }
    },

    async loadMemoryProfileConfig(profileId) {
      if (!profileId) return;
      this._memProfileFormLoaded = false;
      this.memoryFormProfileId = '';
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profileMemoryConfig(profileId));
        if (!res.ok) return;
        const data = await res.json();
        const cfg = data.memory_config || {};
        const _pn = (v) => v != null ? v : '';
        this.memoryProfileForm = {
          enabled:              cfg.enabled              ?? null,
          vector_enabled:       cfg.vector_enabled       ?? null,
          day_summary_enabled:  cfg.day_summary_enabled  ?? null,
          extraction_frequency:   _pn(cfg.extraction_frequency),
          extraction_weight_threshold: _pn(cfg.extraction_weight_threshold),
          max_facts_in_prompt:    _pn(cfg.max_facts_in_prompt),
          max_history_turns:      _pn(cfg.max_history_turns),
          extraction_llm_preset:  cfg.extraction_llm_preset ?? '',
          extraction_prompt:      cfg.extraction_prompt     ?? '',
          day_summary_prompt:     cfg.day_summary_prompt    ?? '',
          recent_fact_quota: _pn(cfg.recent_fact_quota),
          semantic_fact_quota: _pn(cfg.semantic_fact_quota),
          recent_days:       _pn(cfg.recent_days),
          semantic_distance: _pn(cfg.semantic_distance),
          vector_dedup_distance_threshold: _pn(cfg.vector_dedup_distance_threshold),
          group_chat_self_recap_max_groups: _pn(cfg.group_chat_self_recap_max_groups),
          group_chat_merge_into_history: cfg.group_chat_merge_into_history !== false,
          daily_forgetting_enabled: cfg.daily_forgetting_enabled ?? null,
          daily_decay_factor: _pn(cfg.daily_decay_factor),
          daily_decay_min_weight: _pn(cfg.daily_decay_min_weight),
          daily_reinforcement_enabled: cfg.daily_reinforcement_enabled ?? null,
          daily_consolidation_enabled: cfg.daily_consolidation_enabled ?? null,
          daily_consolidation_after_days: _pn(cfg.daily_consolidation_after_days),
          daily_consolidation_weight_below: _pn(cfg.daily_consolidation_weight_below),
          daily_consolidation_batch_max: _pn(cfg.daily_consolidation_batch_max),
        };
        this.memoryFormProfileId = profileId;
        this.$nextTick(() => { this._memProfileFormLoaded = true; });
      } catch (e) {
        console.error('[settings] loadMemoryProfileConfig:', e);
      }
    },

    async saveMemoryProfileConfig(silent = false) {
      if (this.memoryProfileSaving || !this.memoryFormProfileId) return;
      this.memoryProfileSaving = true;
      try {
        const cfg = {};
        if (this.memoryProfileForm.enabled !== null)             cfg.enabled             = this.memoryProfileForm.enabled;
        if (this.memoryProfileForm.vector_enabled !== null)      cfg.vector_enabled      = this.memoryProfileForm.vector_enabled;
        if (this.memoryProfileForm.day_summary_enabled !== null) cfg.day_summary_enabled = this.memoryProfileForm.day_summary_enabled;
        if (this.memoryProfileForm.extraction_frequency !== '')       cfg.extraction_frequency  = parseInt(this.memoryProfileForm.extraction_frequency, 10);
        if (this.memoryProfileForm.extraction_weight_threshold !== '') cfg.extraction_weight_threshold = Math.max(0, Math.min(1, parseFloat(this.memoryProfileForm.extraction_weight_threshold) || 0.5));
        if (this.memoryProfileForm.max_facts_in_prompt !== '')        cfg.max_facts_in_prompt   = parseInt(this.memoryProfileForm.max_facts_in_prompt, 10);
        if (this.memoryProfileForm.max_history_turns !== '' && this.memoryProfileForm.max_history_turns != null) {
          const v = parseInt(this.memoryProfileForm.max_history_turns, 10);
          if (!isNaN(v)) cfg.max_history_turns = Math.max(1, Math.min(200, v));
        }
        if (this.memoryProfileForm.extraction_llm_preset !== undefined) cfg.extraction_llm_preset = this.memoryProfileForm.extraction_llm_preset;
        if (this.memoryProfileForm.extraction_prompt.trim())          cfg.extraction_prompt     = this.memoryProfileForm.extraction_prompt.trim();
        if (this.memoryProfileForm.day_summary_prompt !== undefined)  cfg.day_summary_prompt    = this.memoryProfileForm.day_summary_prompt;
        // 槽位配额（留空则不覆盖全局配置）
        const _pi = (v) => v !== '' ? parseInt(v, 10) : undefined;
        const _pf = (v) => v !== '' ? parseFloat(v) : undefined;
        if (_pi(this.memoryProfileForm.recent_fact_quota) !== undefined) cfg.recent_fact_quota = _pi(this.memoryProfileForm.recent_fact_quota);
        if (_pi(this.memoryProfileForm.semantic_fact_quota) !== undefined) cfg.semantic_fact_quota = _pi(this.memoryProfileForm.semantic_fact_quota);
        if (_pi(this.memoryProfileForm.recent_days) !== undefined)       cfg.recent_days       = _pi(this.memoryProfileForm.recent_days);
        if (_pf(this.memoryProfileForm.semantic_distance) !== undefined) cfg.semantic_distance = _pf(this.memoryProfileForm.semantic_distance);
        if (_pf(this.memoryProfileForm.vector_dedup_distance_threshold) !== undefined) cfg.vector_dedup_distance_threshold = Math.max(0.01, Math.min(0.5, _pf(this.memoryProfileForm.vector_dedup_distance_threshold)));
        if (this.memoryProfileForm.daily_forgetting_enabled !== null) cfg.daily_forgetting_enabled = this.memoryProfileForm.daily_forgetting_enabled;
        if (_pf(this.memoryProfileForm.daily_decay_factor) !== undefined) cfg.daily_decay_factor = _pf(this.memoryProfileForm.daily_decay_factor);
        if (_pf(this.memoryProfileForm.daily_decay_min_weight) !== undefined) cfg.daily_decay_min_weight = _pf(this.memoryProfileForm.daily_decay_min_weight);
        if (this.memoryProfileForm.daily_reinforcement_enabled !== null) cfg.daily_reinforcement_enabled = this.memoryProfileForm.daily_reinforcement_enabled;
        if (this.memoryProfileForm.daily_consolidation_enabled !== null) cfg.daily_consolidation_enabled = this.memoryProfileForm.daily_consolidation_enabled;
        if (_pi(this.memoryProfileForm.daily_consolidation_after_days) !== undefined) cfg.daily_consolidation_after_days = _pi(this.memoryProfileForm.daily_consolidation_after_days);
        if (_pf(this.memoryProfileForm.daily_consolidation_weight_below) !== undefined) cfg.daily_consolidation_weight_below = _pf(this.memoryProfileForm.daily_consolidation_weight_below);
        if (_pi(this.memoryProfileForm.daily_consolidation_batch_max) !== undefined) cfg.daily_consolidation_batch_max = _pi(this.memoryProfileForm.daily_consolidation_batch_max);
        if (_pi(this.memoryProfileForm.group_chat_self_recap_max_groups) !== undefined) cfg.group_chat_self_recap_max_groups = Math.max(1, Math.min(20, _pi(this.memoryProfileForm.group_chat_self_recap_max_groups) || 5));
        if (this.memoryProfileForm.group_chat_merge_into_history === false) cfg.group_chat_merge_into_history = false;
        else if (this.memoryProfileForm.group_chat_merge_into_history === true) cfg.group_chat_merge_into_history = true;
        const res = await fetch(
          getBaseUrl() + API_PATHS.profileMemoryConfig(this.memoryFormProfileId),
          { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) }
        );
        const data = res.ok ? (await res.json().catch(() => null)) : null;
        if (data && data.ok) {
          if (!silent) this.showToast(this.t('toastMemoryProfileSaved'), 'success');
          this.memProfileSaveState = 'saved';
          setTimeout(() => { this.memProfileSaveState = null; }, 2000);
        } else {
          this.showToast(`保存失败: ${(data && data.error) || !res.ok ? `HTTP ${res.status}` : 'unknown'}`, 'error');
        }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.memoryProfileSaving = false;
      }
    },

    async loadMemoryData(profileId) {
      if (!profileId) return;
      this.memoryContentProfileId = '';
      this.memoryPanelLoading = true;
      try {
        await Promise.all([
          this.loadMemoryStatus(profileId),
          this.loadMemoryFacts(profileId),
          this.loadMemoryVectors(profileId),
          this.loadMemorySummaries(profileId),
          this.loadMemoryProfileConfig(profileId),
          this.loadChatHistory(profileId),
        ]);
        this.memoryContentProfileId = profileId;
      } finally {
        this.memoryPanelLoading = false;
      }
    },

    async loadMemoryStatus(profileId) {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.memoryStatus(profileId));
        if (res.ok) this.memoryStatus = await res.json();
      } catch (_) {}
    },

    async loadMemoryFacts(profileId) {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.memoryFacts(profileId));
        const data = await res.json();
        this.memoryFacts = data.facts || [];
        this.factsDisplayLimit = 30;
      } catch (_) { this.memoryFacts = []; }
    },

    async loadMemoryVectors(profileId) {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.memoryVectors(profileId));
        const data = await res.json();
        this.memoryVectors = data.vectors || data.items || [];
        this.vectorsDisplayLimit = 30;
      } catch (_) { this.memoryVectors = []; }
    },

    async loadMemorySummaries(profileId, days = 30) {
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.memorySummariesDays(profileId, days));
        const data = await res.json();
        this.memorySummaries = data.summaries || [];
        this.summariesDisplayLimit = 30;
      } catch (_) { this.memorySummaries = []; }
    },

    factsLoadMore() {
      this.factsDisplayLimit += 30;
    },
    vectorsLoadMore() {
      this.vectorsDisplayLimit += 30;
    },
    summariesLoadMore() {
      this.summariesDisplayLimit += 30;
    },

    async loadDiaryPreview(profileId) {
      if (!profileId) return;
      this.diaryPreviewLoading = true;
      this.diaryPreview = [];
      try {
        const days = Math.max(1, Math.min(365, parseInt(this.diaryDays, 10) || 1));
        const res = await fetch(
          getBaseUrl() + API_PATHS.memorySummariesPreview(profileId, this.diaryStartDate, days)
        );
        const data = await res.json();
        if (res.ok && Array.isArray(data.per_date)) this.diaryPreview = data.per_date;
      } catch (_) {}
      this.diaryPreviewLoading = false;
    },

    diaryPreviewStatusLabel(row) {
      if (row.message_count === 0) return this.t('diaryStatusNoChats');
      if (row.has_summary) return this.t('diaryStatusHasSummary');
      return this.t('diaryStatusCanGenerate');
    },

    /** 遗忘预览等：「n 条」/「n items」 */
    memItemCountText(n) {
      if (n == null || n === '') return '';
      return this.t('memCountItemsFmt').replace('{n}', String(n));
    },

    /** 遗忘任务「合并为摘要」一行完整文案 */
    formatConsolidationForgettingLine(c) {
      if (!c) return '';
      return this.t('memForgettingConsolidationLineFmt')
        .replace('{batch}', String(c.batch_size))
        .replace('{candidates}', String(c.candidates_count))
        .replace('{total}', String(c.total_facts));
    },

    async generateDiary(profileId) {
      if (!profileId || this.diaryLoading) return;
      this.diaryMessage = '';
      this.diaryResultPerDate = [];
      this.diaryLoading = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.memorySummariesGenerate(profileId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            start_date: this.diaryStartDate,
            days: Math.max(1, Math.min(365, parseInt(this.diaryDays, 10) || 1)),
            force: !!this.diaryForce,
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          this.diaryMessage = (typeof data.detail === 'string' ? data.detail : data.error) || '请求失败';
          this.diaryMessageOk = false;
          return;
        }
        const gen = data.generated || [];
        const noMsg = data.no_messages || [];
        const skip = data.skipped || [];
        const perDate = data.per_date || [];
        this.diaryResultPerDate = perDate;

        if (gen.length > 0) {
          let msg = `已生成 ${gen.length} 天：${gen.join('、')}`;
          if (noMsg.length > 0) msg += `；其中 ${noMsg.length} 天无对话未生成：${noMsg.join('、')}`;
          if (perDate.some(p => p.truncated)) msg += '（部分日期对话过多已截断至前100条）';
          this.diaryMessage = msg;
          this.diaryMessageOk = true;
          await this.loadMemorySummaries(profileId, 90);
        } else if (noMsg.length > 0 && gen.length === 0 && skip.length === 0) {
          this.diaryMessage = '所选日期范围内无对话记录';
          this.diaryMessageOk = false;
        } else {
          const parts = [];
          if (skip.length > 0) parts.push(`${skip.length} 天已跳过（已有摘要或生成失败）`);
          if (noMsg.length > 0) parts.push(`${noMsg.length} 天无对话`);
          this.diaryMessage = parts.length ? parts.join('；') : '未生成新摘要';
          this.diaryMessageOk = false;
        }
      } catch (e) {
        this.diaryMessage = (e && e.message) ? e.message : '网络或服务器错误';
        this.diaryMessageOk = false;
      } finally {
        this.diaryLoading = false;
      }
    },

    diaryResultStatusLabel(row) {
      if (row.status === 'generated') return row.truncated ? '已生成（已截断）' : '已生成';
      if (row.status === 'no_messages') return '无对话';
      return row.note || '已跳过';
    },

    async addMemoryFact() {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (!this.memoryFactForm.content.trim() || !profileId) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.memoryFacts(profileId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            content:  this.memoryFactForm.content.trim(),
            category: this.memoryFactForm.category || 'other',
            weight:   Math.max(0.1, Math.min(3, parseFloat(this.memoryFactForm.weight) || 1.0)),
            tags: [],
          }),
        });
        const data = await res.json();
        if (data.id) {
          this.memoryFacts.push(data);
          this.memoryFactForm = { content: '', category: 'other', weight: 1.0 };
          this.showToast('✓ 事实已添加', 'success');
          await this.loadMemoryStatus(profileId);
        } else {
          this.showToast(`添加失败: ${data.detail || data.error || ''}`, 'error');
        }
      } catch (e) { this.showToast(`添加失败: ${e.message}`, 'error'); }
    },

    startEditMemoryFact(fact) {
      this.memoryEditingFact = { ...fact };
    },

    async saveMemoryFact() {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (!this.memoryEditingFact || !profileId) return;
      const { id, content, category, weight } = this.memoryEditingFact;
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memoryFactById(profileId, id),
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content, category, weight: parseFloat(weight) || 1.0 }),
          }
        );
        const data = await res.json();
        if (data.ok) {
          this.memoryEditingFact = null;
          await this.loadMemoryFacts(profileId);
          this.showToast('✓ 事实已更新', 'success');
        } else {
          this.showToast(`更新失败: ${data.error || ''}`, 'error');
        }
      } catch (e) { this.showToast(`更新失败: ${e.message}`, 'error'); }
    },

    async deleteMemoryFact(factId) {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (!profileId) return;
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memoryFactById(profileId, factId),
          { method: 'DELETE' }
        );
        const data = await res.json();
        if (data.ok) {
          this.memoryFacts = this.memoryFacts.filter(f => f.id !== factId);
          this.showToast('✓ 事实已删除', 'success');
          this.loadMemoryStatus(profileId);
        } else {
          this.showToast(`删除失败: ${data.error || ''}`, 'error');
        }
      } catch (e) { this.showToast(`删除失败: ${e.message}`, 'error'); }
    },

    async toggleMemoryFactPin(fact) {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (!profileId) return;
      const newPinned = !fact.pinned;
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memoryFactById(profileId, fact.id),
          { method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned: newPinned }) }
        );
        const data = await res.json();
        if (data.ok) {
          fact.pinned = newPinned;
          this.showToast(newPinned ? '📌 已设为永久显示' : '✓ 已取消永久显示', 'success');
        } else {
          this.showToast(`操作失败: ${data.error || ''}`, 'error');
        }
      } catch (e) { this.showToast(`操作失败: ${e.message}`, 'error'); }
    },

    async addMemoryVector() {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (!this.memoryVectorText.trim() || !profileId) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.memoryVectors(profileId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: this.memoryVectorText.trim() }),
        });
        const data = await res.json();
        if (data.id) {
          this.memoryVectorText = '';
          await this.loadMemoryVectors(profileId);
          this.loadMemoryStatus(profileId);
          this.showToast('✓ 向量已添加', 'success');
        } else {
          this.showToast(`添加失败: ${data.error || ''}`, 'error');
        }
      } catch (e) { this.showToast(`添加失败: ${e.message}`, 'error'); }
    },

    async deleteMemoryVector(memId) {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (!profileId) return;
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memoryVectorById(profileId, memId),
          { method: 'DELETE' }
        );
        const data = await res.json();
        if (data.ok) {
          this.memoryVectors = this.memoryVectors.filter(v => v.id !== memId);
          this.showToast('✓ 向量已删除', 'success');
          this.loadMemoryStatus(profileId);
        } else {
          this.showToast(`删除失败: ${data.error || ''}`, 'error');
        }
      } catch (e) { this.showToast(`删除失败: ${e.message}`, 'error'); }
    },

    async syncFactsToVectors() {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (this.syncFactsLoading || !profileId) return;
      this.syncFactsLoading = true;
      this.syncFactsResult = '同步中…';
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memoryVectorsSyncFacts(profileId),
          { method: 'POST' }
        );
        const data = await res.json();
        if (res.ok) {
          this.syncFactsResult = `✓ 新增 ${data.synced} 条（共 ${data.total} 条事实，向量库 ${data.vector_count} 条）`;
          await this.loadMemoryVectors(profileId);
          await this.loadMemoryStatus(profileId);
        } else {
          this.syncFactsResult = `同步失败: ${data.detail || ''}`;
        }
      } catch (e) {
        this.syncFactsResult = `同步失败: ${e.message}`;
      } finally {
        this.syncFactsLoading = false;
      }
    },

    async loadForgettingPreview(profileId) {
      if (!profileId || this.forgettingPreviewLoading) return;
      this.forgettingPreviewLoading = true;
      this.forgettingPreview = null;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.memoryForgettingPreview(profileId));
        const data = await res.json();
        if (res.ok) this.forgettingPreview = data;
        else this.showToast('预览失败: ' + (data.detail || ''), 'error');
      } catch (e) {
        this.showToast('预览失败: ' + e.message, 'error');
      } finally {
        this.forgettingPreviewLoading = false;
      }
    },

    async runForgettingNow() {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (this.forgettingRunLoading || !profileId) return;
      this.forgettingRunLoading = true;
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memoryForgettingRun(profileId),
          { method: 'POST' }
        );
        const data = await res.json();
        if (res.ok && data.ok) {
          this.showToast('✓ 已执行一次每日记忆/遗忘任务', 'success');
          this.forgettingPreview = null;
          await this.loadMemoryFacts(profileId);
          await this.loadMemoryStatus(profileId);
        } else {
          this.showToast(`执行失败: ${data.detail || data.error || ''}`, 'error');
        }
      } catch (e) {
        this.showToast(`执行失败: ${e.message}`, 'error');
      } finally {
        this.forgettingRunLoading = false;
      }
    },

    async deleteMemorySummary(dateStr) {
      const profileId = this.memoryContentProfileId || this.selectedProfileId;
      if (!profileId) return;
      try {
        const res = await fetch(
          getBaseUrl() + API_PATHS.memorySummaryByDate(profileId, dateStr),
          { method: 'DELETE' }
        );
        const data = await res.json();
        if (data.ok) {
          this.memorySummaries = this.memorySummaries.filter(s => s.date !== dateStr);
          this.showToast('✓ 摘要已删除', 'success');
        } else {
          this.showToast(`删除失败: ${data.error || ''}`, 'error');
        }
      } catch (e) { this.showToast(`删除失败: ${e.message}`, 'error'); }
    },

    /* ─────────────── Reflection / ASE / VLM ─────────────── */

    async loadReflectionConfig() {
      this._reflectionFormLoaded = false;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsReflection());
        const data = await res.json();
        // Merge scalar fields directly
        const { ase_modes, ...rest } = data;
        Object.assign(this.reflectionForm, rest);
        // Init _enabled flags from API response (null = not overriding → disabled)
        for (const k of ['temperature', 'top_p', 'presence_penalty', 'frequency_penalty', 'max_tokens']) {
          const key = `reflection_${k}`;
          this.reflectionForm[`${key}_enabled`] = data[key] != null;
          if (data[key] != null) this.reflectionForm[key] = data[key];
        }
        // Deep-merge ase_modes so nested objects are updated without replacing refs
        if (ase_modes) {
          for (const mode of Object.keys(ase_modes)) {
            if (this.reflectionForm.ase_modes[mode]) {
              Object.assign(this.reflectionForm.ase_modes[mode], ase_modes[mode]);
            } else {
              this.reflectionForm.ase_modes[mode] = ase_modes[mode];
            }
          }
        }
      } catch (e) {
        console.warn('[reflection] load config failed:', e);
      }
      this.$nextTick(() => { this._reflectionFormLoaded = true; });
    },

    async saveReflectionConfig(silent = false) {
      if (this.reflectionSaving) return;
      if (silent) this.setAutoSaveState('saving');
      this.reflectionSaving = true;
      try {
        const f = this.reflectionForm;
        const body = {
          ...f,
          // Convert _enabled flags to null (means "use preset default")
          reflection_temperature:       f.reflection_temperature_enabled       ? parseFloat(f.reflection_temperature)       : null,
          reflection_top_p:             f.reflection_top_p_enabled             ? parseFloat(f.reflection_top_p)             : null,
          reflection_presence_penalty:  f.reflection_presence_penalty_enabled  ? parseFloat(f.reflection_presence_penalty)  : null,
          reflection_frequency_penalty: f.reflection_frequency_penalty_enabled ? parseFloat(f.reflection_frequency_penalty) : null,
          reflection_max_tokens:        f.reflection_max_tokens_enabled        ? (parseInt(f.reflection_max_tokens, 10) || null) : null,
        };
        const res = await fetch(getBaseUrl() + API_PATHS.settingsReflection(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
          if (silent) this.setAutoSaveState('saved'); else this.showToast('自省与主动设置已保存', 'success');
        } else {
          if (silent) this.setAutoSaveState(null);
          this.showToast(`保存失败: ${data.detail || ''}`, 'error');
        }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.reflectionSaving = false;
      }
    },

    onReflectionProfileChange(profileId) {
      if (profileId && this.profiles.some(p => p.profile_id === profileId)) this.selectProfile(profileId);
      this.loadReflectionProfileConfig(profileId);
      this.loadPersonaEvolution(profileId);
      this.loadUserPortrait(profileId);
    },
    onMemoryContentProfileChange(profileId) {
      if (profileId && this.profiles.some(p => p.profile_id === profileId)) this.selectProfile(profileId);
      this.loadMemoryData(profileId);
    },
    onMemorySettingsProfileChange(profileId) {
      if (profileId && this.profiles.some(p => p.profile_id === profileId)) this.selectProfile(profileId);
      this.loadMemoryProfileConfig(profileId);
    },
    /** 全局自动保存状态指示（saving → saved → null） */
    setAutoSaveState(state) {
      this.autoSaveState = state;
      if (state === 'saved') {
        clearTimeout(this._autoSaveStateTimer);
        this._autoSaveStateTimer = setTimeout(() => { this.autoSaveState = null; }, 2000);
      }
    },

    /** 设置抽屉内容区滚回顶部（确认条、人格头等在上方时） */
    scrollSettingsBodyToTop() {
      this.$nextTick(() => {
        const el = this.$refs.settingsBodyScroll;
        if (el && typeof el.scrollTop === 'number') el.scrollTop = 0;
      });
    },

    /** 自省/ASE 段落：归一化 trigger_param（勿用 parseFloat(x)||1，会吞掉 0 与 time_window 的 510） */
    coerceRefaseTriggerParam(seg) {
      const mode = (seg && seg.trigger_mode) || 'always';
      const v = seg && seg.trigger_param;
      if (typeof v === 'number' && Number.isFinite(v)) return v;
      if (v !== null && v !== undefined && v !== '') {
        const p = parseFloat(String(v));
        if (Number.isFinite(p)) return p;
      }
      if (mode === 'time_window') return 510;
      if (mode === 'first_after_silence') return 60;
      if (mode === 'cooldown') return 60;
      if (mode === 'probability') return 1;
      if (mode === 'every_n_turns') return 5;
      return 1;
    },

    refaseTwStartHour(holder) {
      const raw = Math.round(this.coerceRefaseTriggerParam(holder));
      return Math.floor(raw / 100);
    },

    refaseTwEndHour(holder) {
      const raw = Math.round(this.coerceRefaseTriggerParam(holder));
      let e = raw % 100;
      if (e < 0) e = 0;
      if (e > 23) e = 23;
      return e;
    },

    refaseTwSetStartHour(holder, evt) {
      const h = Math.min(23, Math.max(0, parseInt(String(evt.target.value), 10) || 0));
      const end = this.refaseTwEndHour(holder);
      holder.trigger_param = h * 100 + end;
    },

    refaseTwSetEndHour(holder, evt) {
      const end = Math.min(23, Math.max(0, parseInt(String(evt.target.value), 10) || 0));
      const start = this.refaseTwStartHour(holder);
      holder.trigger_param = start * 100 + end;
    },

    async loadReflectionProfileConfig(profileId) {
      if (!profileId) {
        this.reflectionFormProfileId = '';
        return;
      }
      this._reflectionProfileFormLoaded = false;
      // 同步标记当前人格，避免先清空再拉取导致「空状态」闪屏与 .settings-body 滚动被重置到顶
      this.reflectionFormProfileId = profileId;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profileReflectionConfig(profileId));
        if (!res.ok) return;
        const data = await res.json();
        // 竞态：切换人格或关闭设置后丢弃过期响应
        if (profileId !== this.selectedProfileId || profileId !== this.reflectionFormProfileId) return;
        const segments = (data.segments || []).map(s => ({
          ...s,
          trigger_param: this.coerceRefaseTriggerParam(s),
        }));
        this.reflectionProfileForm = {
          custom_prompt: data.custom_prompt || '',
          source: data.source || 'reflection_config',
          chat_inject_topic_anchor: data.chat_inject_topic_anchor !== false,
          long_absence_hours: typeof data.long_absence_hours === 'number' ? data.long_absence_hours : 48,
          segments,
        };
      } catch (e) {
        console.error('[settings] loadReflectionProfileConfig:', e);
      }
      this.$nextTick(() => { this._reflectionProfileFormLoaded = true; });
    },

    async saveReflectionProfileConfig(opts = {}) {
      const skipToast = opts.skipToast === true;
      if (this.reflectionProfileSaving || !this.reflectionFormProfileId) return;
      this.reflectionProfileSaving = true;
      if (skipToast) this.setAutoSaveState('saving');
      try {
        const segments = (this.reflectionProfileForm.segments || []).map(s => ({
          segment_id: s.segment_id,
          label: s.label || s.segment_id,
          content: (s.content || '').trim(),
          priority: parseInt(s.priority, 10) || 50,
          enabled: s.enabled !== false,
          trigger_mode: s.trigger_mode || 'always',
          trigger_param: this.coerceRefaseTriggerParam(s),
          trigger_keywords: s.trigger_keywords || null,
          inject_into: s.inject_into || 'reflection_ase',
        }));
        const res = await fetch(
          getBaseUrl() + API_PATHS.profileReflectionConfig(this.reflectionFormProfileId),
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              custom_prompt: this.reflectionProfileForm.custom_prompt || '',
              chat_inject_topic_anchor: this.reflectionProfileForm.chat_inject_topic_anchor,
              long_absence_hours: this.reflectionProfileForm.long_absence_hours,
              segments,
            }),
          }
        );
        const data = res.ok ? await res.json().catch(() => null) : null;
        if (data && data.ok) {
          if (skipToast) this.setAutoSaveState('saved'); else this.showToast('✓ 该人格自省 Prompt 已保存', 'success');
        } else {
          if (skipToast) this.setAutoSaveState(null);
          this.showToast('保存失败', 'error');
        }
      } catch (e) {
        if (skipToast) this.setAutoSaveState(null);
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.reflectionProfileSaving = false;
      }
    },

    async loadPersonaEvolution(profileId) {
      if (!profileId) return;
      this._personaEvolutionFormLoaded = false;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profilePersonaEvolution(profileId));
        if (!res.ok) return;
        const data = await res.json();
        const ev = data.persona_evolved || {};
        this.personaEvolutionForm = {
          base_prompt_original: data.base_prompt_original || '',
          style_constraint_original: data.style_constraint_original || '',
          core_anchor: ev.core_anchor || '',
          base_prompt_evolved: ev.base_prompt || '',
          style_constraint_evolved: ev.style_constraint || '',
          evolution_count: ev.evolution_count || 0,
          evolved_at: ev.evolved_at || null,
          changelog: data.changelog || [],
          enabled: ev.enabled !== false,
          min_interval_turns: ev.min_interval_turns ?? 200,
          max_evolution_facts: ev.max_evolution_facts ?? 50,
          max_evolution_conv_turns: ev.max_evolution_conv_turns ?? 40,
          turns_until_next: data.turns_until_next ?? null,
          turn_counter: data.turn_counter ?? 0,
        };
        this.$nextTick(() => { this._personaEvolutionFormLoaded = true; });
      } catch (e) {
        console.error('[settings] loadPersonaEvolution:', e);
      }
    },

    async savePersonaEvolutionData() {
      const profileId = this.selectedProfileId;
      if (!profileId) return;
      this.setAutoSaveState('saving');
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profilePersonaEvolution(profileId), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            core_anchor: this.personaEvolutionForm.core_anchor || '',
            base_prompt: this.personaEvolutionForm.base_prompt_evolved || '',
            style_constraint: this.personaEvolutionForm.style_constraint_evolved || '',
            enabled: this.personaEvolutionForm.enabled,
            min_interval_turns: this.personaEvolutionForm.min_interval_turns,
            max_evolution_facts: this.personaEvolutionForm.max_evolution_facts,
            max_evolution_conv_turns: this.personaEvolutionForm.max_evolution_conv_turns,
          }),
        });
        if (res.ok) {
          this.setAutoSaveState('saved');
        }
      } catch (e) {
        console.error('[settings] savePersonaEvolutionData:', e);
      }
    },

    async extractAnchorFromProfile() {
      const profileId = this.selectedProfileId;
      if (!profileId || this.personaEvolutionExtracting) return;
      this.personaEvolutionExtracting = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profilePersonaEvolutionAnchor(profileId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const data = res.ok ? await res.json().catch(() => null) : null;
        if (data && data.ok) {
          this.personaEvolutionForm.core_anchor = data.core_anchor || '';
          this.showToast('✓ 核心锚点已提炼', 'success');
        } else {
          this.showToast('提炼失败', 'error');
        }
      } catch (e) {
        this.showToast(`提炼失败: ${e.message}`, 'error');
      } finally {
        this.personaEvolutionExtracting = false;
      }
    },

    async rollbackPersonaEvolution(version) {
      const profileId = this.selectedProfileId;
      if (!profileId) return;
      if (!confirm(`确定回滚到第 ${version} 次演化之前的版本吗？`)) return;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profilePersonaEvolutionRollback(profileId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version }),
        });
        const data = res.ok ? await res.json().catch(() => null) : null;
        if (data && data.ok) {
          this.showToast('✓ 已回滚', 'success');
          await this.loadPersonaEvolution(profileId);
        } else {
          this.showToast('回滚失败', 'error');
        }
      } catch (e) {
        this.showToast(`回滚失败: ${e.message}`, 'error');
      }
    },

    async triggerPersonaEvolution() {
      const profileId = this.selectedProfileId;
      if (!profileId || this.personaEvolutionTriggering) return;
      this.personaEvolutionTriggering = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.profilePersonaEvolutionTrigger(profileId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const data = res.ok ? await res.json().catch(() => null) : null;
        if (data && data.ok) {
          this.showToast('✓ ' + (this.locale === 'en' ? 'Evolution complete' : '演化完成'), 'success');
          await this.loadPersonaEvolution(profileId);
        } else {
          const reason = (data && data.reason) || (this.locale === 'en' ? 'Evolution skipped' : '演化被跳过');
          this.showToast(reason, 'error');
        }
      } catch (e) {
        this.showToast((this.locale === 'en' ? 'Evolution failed: ' : '演化失败：') + e.message, 'error');
      } finally {
        this.personaEvolutionTriggering = false;
      }
    },

    fmtEvolutionDate(ts) {
      if (!ts) return '';
      return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    },

    beginAddCustomRefaseSeg(tab) {
      const activeTab = tab || this.activeReflectionSegTab || 'reflection';
      const defaultInject = activeTab === 'ase' ? 'ase' : 'reflection_ase';
      this.newCustomRefaseSeg = {
        label: '',
        content: '',
        inject_into: defaultInject,
        priority: 50,
        enabled: true,
        trigger_mode: 'always',
        trigger_param: 1,
        trigger_keywords: null,
        _tab: activeTab,
      };
    },
    cancelAddCustomRefaseSeg() {
      this.newCustomRefaseSeg = null;
    },
    confirmAddCustomRefaseSeg() {
      if (!this.newCustomRefaseSeg) return;
      const segId = 'refase_' + Date.now();
      const draft = { ...this.newCustomRefaseSeg };
      this.reflectionProfileForm.segments.push({
        segment_id: segId,
        label: (draft.label || segId).trim(),
        content: (draft.content || '').trim(),
        inject_into: draft.inject_into || 'reflection_ase',
        priority: parseInt(draft.priority, 10) || 50,
        enabled: true,
        trigger_mode: draft.trigger_mode || 'always',
        trigger_param: this.coerceRefaseTriggerParam(draft),
        trigger_keywords: draft.trigger_keywords || null,
        is_builtin: false,
        description: '',
        default_content: '',
      });
      this.newCustomRefaseSeg = null;
    },
    deleteRefaseSeg(idx) {
      if (idx >= 0 && idx < (this.reflectionProfileForm.segments || []).length && !this.reflectionProfileForm.segments[idx].is_builtin) {
        this.reflectionProfileForm.segments.splice(idx, 1);
      }
    },

    async saveVlmConfig() {
      if (this.vlmSaving) return;
      this.vlmSaving = true;
      try {
        const res = await fetch(getBaseUrl() + API_PATHS.settingsReflection(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.reflectionForm),
        });
        const data = await res.json();
        if (data.ok) {
          this.showToast('VLM 设置已保存', 'success');
        } else {
          this.showToast(`保存失败: ${data.detail || ''}`, 'error');
        }
      } catch (e) {
        this.showToast(`保存失败: ${e.message}`, 'error');
      } finally {
        this.vlmSaving = false;
      }
    },

    /* 侧栏人格列表：触屏长按手柄拖动排序（与 app.js 中 HTML5 drag 互补；方法放在 Mixin 确保模板能解析到） */
    sessionPointerDown(ev, sessionId) {
      if (this.supportsHtml5Drag) return;
      if (!ev || (ev.button != null && ev.button !== 0)) return;
      try {
        if (ev.target && typeof ev.target.setPointerCapture === 'function') {
          ev.target.setPointerCapture(ev.pointerId);
        }
      } catch (_) {}

      const pointerId = ev.pointerId;
      this._sessionPointer = {
        pointerId,
        startX: ev.clientX,
        startY: ev.clientY,
        moved: false,
        armed: true,
        armTimer: null,
      };

      this._sessionPointer.armTimer = window.setTimeout(() => {
        const st = this._sessionPointer;
        if (!st || !st.armed) return;
        this.sessionDragging = true;
        this.sessionDragActiveId = sessionId;
        this.sessionDragOverId = null;
      }, 180);

      this._sessionPointerMoveHandler = (e) => this.sessionPointerMove(e);
      this._sessionPointerUpHandler = (e) => this.sessionPointerUp(e);
      window.addEventListener('pointermove', this._sessionPointerMoveHandler, { passive: false });
      window.addEventListener('pointerup', this._sessionPointerUpHandler, { passive: true });
      window.addEventListener('pointercancel', this._sessionPointerUpHandler, { passive: true });
    },

    sessionPointerMove(ev) {
      const st = this._sessionPointer;
      if (!st || st.pointerId !== ev.pointerId) return;
      const dx = ev.clientX - st.startX;
      const dy = ev.clientY - st.startY;
      if (Math.abs(dx) + Math.abs(dy) > 6) st.moved = true;

      if (!this.sessionDragging) {
        if (st.moved) {
          st.armed = false;
          if (st.armTimer) window.clearTimeout(st.armTimer);
        }
        return;
      }

      ev.preventDefault();

      const activeId = this.sessionDragActiveId;
      if (!activeId) return;

      const el = document.elementFromPoint(ev.clientX, ev.clientY);
      const item = el && el.closest ? el.closest('.session-item[data-session-id]') : null;
      if (!item) {
        this.sessionDragOverId = null;
        return;
      }
      const overId = item.getAttribute('data-session-id');
      if (!overId || overId === activeId) {
        this.sessionDragOverId = null;
        return;
      }
      this.sessionDragOverId = overId;

      const fromIdx = this.sessions.findIndex((s) => s.id === activeId);
      const toIdx = this.sessions.findIndex((s) => s.id === overId);
      if (fromIdx === -1 || toIdx === -1 || fromIdx === toIdx) return;

      const copy = this.sessions.slice();
      const [movedItem] = copy.splice(fromIdx, 1);
      copy.splice(toIdx, 0, movedItem);
      this.sessions = copy;
    },

    async sessionPointerUp(ev) {
      const st = this._sessionPointer;
      if (!st || (ev && st.pointerId !== ev.pointerId)) return;

      if (st.armTimer) window.clearTimeout(st.armTimer);
      this._sessionPointer = null;

      if (this._sessionPointerMoveHandler) {
        window.removeEventListener('pointermove', this._sessionPointerMoveHandler);
        this._sessionPointerMoveHandler = null;
      }
      if (this._sessionPointerUpHandler) {
        window.removeEventListener('pointerup', this._sessionPointerUpHandler);
        window.removeEventListener('pointercancel', this._sessionPointerUpHandler);
        this._sessionPointerUpHandler = null;
      }

      if (this.sessionDragging) {
        this.sessionDragging = false;
        this.sessionDragOverId = null;
        this.sessionDragActiveId = null;
        await this.saveSessionOrder();
      }
    },

    /* ─────────────── Tools tab ─────────────── */

    async loadToolsConfig(profileId) {
      const pid = profileId || this.selectedProfileId || '';
      this.toolsLoading = true;
      try {
        const url = pid ? `/api/tools?profile_id=${encodeURIComponent(pid)}` : '/api/tools';
        const res = await fetch(getBaseUrl() + url);
        const data = await res.json();
        this.toolsList = data.tools || [];
        if (pid) {
          await this.loadTrendStatus(pid);
          await this.loadWeatherStatus(pid);
        }
      } catch (e) {
        this.showToast(this.t('errLoadTools'), 'error');
      } finally {
        this.toolsLoading = false;
      }
    },

    async saveToolConfig(toolId, config, profileId) {
      const pid = profileId || this.selectedProfileId;
      if (!pid) return;
      this.toolsSaving = { ...this.toolsSaving, [toolId]: true };
      try {
        const res = await fetch(getBaseUrl() + `/api/profiles/${pid}/tool_configs`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tool_id: toolId, config }),
        });
        const data = await res.json();
        if (data.ok) {
          this.showToast(this.t('savedOk'), 'success');
          await this.loadToolsConfig(pid);
        }
      } catch (e) {
        this.showToast(this.t('errSave'), 'error');
      } finally {
        this.toolsSaving = { ...this.toolsSaving, [toolId]: false };
      }
    },

    getToolConfig(toolId) {
      const t = (this.toolsList || []).find(t => t.tool_id === toolId);
      return t ? t.config : {};
    },

    async loadTrendStatus(profileId) {
      const pid = profileId || this.selectedProfileId;
      if (!pid) return;
      try {
        const res = await fetch(getBaseUrl() + `/api/profiles/${pid}/trend/status`);
        this.trendStatus = await res.json();
      } catch (e) { /* silent */ }
    },

    async manualTrendFetch(profileId) {
      const pid = profileId || this.selectedProfileId;
      if (!pid || this.trendFetching) return;
      this.trendFetching = true;
      try {
        const res = await fetch(getBaseUrl() + `/api/profiles/${pid}/trend/fetch`, { method: 'POST' });
        const data = await res.json();
        this.showToast(this.t('trendFetchDone').replace('{n}', data.added || 0), 'success');
        await this.loadTrendStatus(pid);
      } catch (e) {
        this.showToast(this.t('errTrendFetch'), 'error');
      } finally {
        this.trendFetching = false;
      }
    },

    async clearTrendCache(profileId) {
      const pid = profileId || this.selectedProfileId;
      if (!pid) return;
      try {
        await fetch(getBaseUrl() + `/api/profiles/${pid}/trend/cache`, { method: 'DELETE' });
        this.showToast(this.t('trendClearCacheDone'), 'success');
        await this.loadTrendStatus(pid);
      } catch (e) {
        this.showToast('Error', 'error');
      }
    },

    addPresetTrendSource(trendCfg, preset, profileId) {
      const sources = [...(trendCfg.sources || [])];
      if (sources.some(ss => ss.url === preset.url)) return;
      sources.push({ type: preset.type, label: preset.label, url: preset.url });
      this.saveToolConfig('trend', { ...trendCfg, sources }, profileId);
    },

    addTrendSource(trendCfg, profileId) {
      const s = this.trendNewSource;
      if (!s.label) { this.showToast(this.t('errTrendSourceLabel'), 'error'); return; }
      if ((s.type === 'rss' || s.type === 'api') && !s.url) { this.showToast(this.t('errTrendSourceUrl'), 'error'); return; }
      if (s.type === 'websearch' && !s.query) { this.showToast(this.t('errTrendSourceQuery'), 'error'); return; }
      const sources = [...(trendCfg.sources || [])];
      const entry = { type: s.type, label: s.label };
      if (s.type === 'rss' || s.type === 'api') entry.url = s.url;
      if (s.type === 'websearch') entry.query = s.query;
      sources.push(entry);
      this.trendNewSource = { type: 'rss', label: '', url: '', query: '' };
      this.saveToolConfig('trend', { ...trendCfg, sources }, profileId);
    },

    removeTrendSource(trendCfg, idx, profileId) {
      const sources = [...(trendCfg.sources || [])];
      sources.splice(idx, 1);
      this.saveToolConfig('trend', { ...trendCfg, sources }, profileId);
    },

    async loadWeatherStatus(profileId) {
      const pid = profileId || this.selectedProfileId;
      if (!pid) return;
      try {
        const res = await fetch(getBaseUrl() + `/api/profiles/${pid}/weather/status`);
        this.weatherStatus = await res.json();
      } catch (e) { /* silent */ }
    },
  },
};
