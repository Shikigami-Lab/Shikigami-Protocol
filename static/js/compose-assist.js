/* Composer-only AI writing actions. Generated text is never sent automatically. */
window.ComposeAssistMixin = {
  data() {
    return {
      composeAssistOpen: false,
      composeAssistMode: '',
      composeAssistInstruction: '',
      composeAssistCandidate: '',
      composeAssistError: '',
      composeAssistBusy: false,
      composeAssistBaseDraft: '',
      composeAssistTargetMessageId: null,
      composeAssistTargetProfileId: '',
      composeAssistSessionId: '',
      composeAssistGroupId: '',
      _composeAssistController: null,
    };
  },
  watch: {
    currentSessionId() { this.closeComposeAssist(false); },
    currentGroupId() { this.closeComposeAssist(false); },
  },
  unmounted() { this.closeComposeAssist(false); },
  methods: {
    _composeAssistTarget() {
      const message = [...(this.messages || [])].reverse().find(item =>
        item.role === 'assistant' && item.id != null && !item._streaming && (item.content || '').trim());
      if (!message) return null;
      if (!this.currentGroupId) return { messageId: message.id, profileId: this.currentSessionId || '' };
      const participants = this.currentGroup?.participants || [];
      const sender = (message.sender || '').trim();
      const matches = participants.filter(participant => {
        const session = this.sessions.find(item => item.id === participant.profile_id);
        return (participant.name || session?.display_name || participant.profile_id || '').trim() === sender;
      });
      if (matches.length !== 1) return null;
      return { messageId: message.id, profileId: matches[0].profile_id };
    },
    _composeAssistIsCurrent() {
      if (this.inputText !== this.composeAssistBaseDraft ||
          (this.currentSessionId || '') !== this.composeAssistSessionId ||
          (this.currentGroupId || '') !== this.composeAssistGroupId) return false;
      if (this.composeAssistTargetMessageId == null) return true;
      const target = this._composeAssistTarget();
      return !!target && target.messageId === this.composeAssistTargetMessageId &&
        target.profileId === this.composeAssistTargetProfileId;
    },
    openComposeAssist(mode) {
      if (this.isStreaming) {
        this.showToast?.(this.t('composeAssistWaitChat'), 'error');
        return;
      }
      const draft = this.inputText || '';
      const target = this._composeAssistTarget();
      if (mode === 'continue' && !draft.trim()) {
        this.showToast?.(this.t('composeAssistNeedDraft'), 'error');
        return;
      }
      if ((mode === 'reply' || this.currentGroupId) && !target) {
        this.showToast?.(this.t('composeAssistNeedTarget'), 'error');
        return;
      }
      this.showCommandsPanel = false;
      this.composeAssistMode = mode;
      this.composeAssistInstruction = '';
      this.composeAssistCandidate = '';
      this.composeAssistError = '';
      this.composeAssistBaseDraft = draft;
      this.composeAssistTargetMessageId = target?.messageId ?? null;
      this.composeAssistTargetProfileId = target?.profileId || '';
      this.composeAssistSessionId = this.currentSessionId || '';
      this.composeAssistGroupId = this.currentGroupId || '';
      this.composeAssistOpen = true;
      this.$nextTick(() => this.$refs.composeAssistInstruction?.focus());
    },
    async generateComposeAssist() {
      if (this.composeAssistBusy || !this._composeAssistIsCurrent()) {
        this.composeAssistError = this.t('composeAssistContextChanged');
        return;
      }
      this.composeAssistBusy = true;
      this.composeAssistCandidate = '';
      this.composeAssistError = '';
      const controller = new AbortController();
      this._composeAssistController = controller;
      try {
        const response = await fetch(getBaseUrl() + API_PATHS.chatComposeAssist(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: controller.signal,
          body: JSON.stringify({
            mode: this.composeAssistMode,
            session_id: this.composeAssistSessionId || this.composeAssistTargetProfileId,
            group_id: this.composeAssistGroupId || null,
            target_message_id: this.composeAssistTargetMessageId,
            target_profile_id: this.composeAssistTargetProfileId || null,
            draft: this.composeAssistBaseDraft,
            instruction: this.composeAssistInstruction.trim(),
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || this.t('composeAssistFailed'));
        if (!this._composeAssistIsCurrent()) {
          this.composeAssistError = this.t('composeAssistContextChanged');
          return;
        }
        const generated = (data.text || '').trim();
        if (!generated) throw new Error(this.t('composeAssistFailed'));
        this.composeAssistCandidate = this.composeAssistMode === 'continue'
          ? this.composeAssistBaseDraft.replace(/\s+$/, '') + '\n\n' + generated
          : generated;
      } catch (error) {
        if (error.name !== 'AbortError') this.composeAssistError = error.message || this.t('composeAssistFailed');
      } finally {
        if (this._composeAssistController === controller) {
          this._composeAssistController = null;
          this.composeAssistBusy = false;
        }
      }
    },
    applyComposeAssist() {
      if (!this.composeAssistCandidate || !this._composeAssistIsCurrent()) {
        this.composeAssistError = this.t('composeAssistContextChanged');
        return;
      }
      this.inputText = this.composeAssistCandidate;
      this.closeComposeAssist(false);
      this.$nextTick(() => this.$refs.chatTextarea?.focus());
    },
    closeComposeAssist(restoreFocus = true) {
      if (this._composeAssistController) this._composeAssistController.abort();
      this._composeAssistController = null;
      this.composeAssistOpen = false;
      this.composeAssistBusy = false;
      if (restoreFocus) this.$nextTick(() => this.$refs.chatTextarea?.focus());
    },
  },
};
