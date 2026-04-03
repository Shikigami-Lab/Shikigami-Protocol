/* global Vue */
(function () {
  const api = window.electronAPI;

  function tw(key, locale) {
    const loc = (locale === 'en' || locale === 'zh') ? locale
      : (localStorage.getItem('locale') === 'en' ? 'en' : 'zh');
    const L = window.LOCALES && window.LOCALES[loc];
    return (L && L[key]) || key;
  }

  function formatPipError(err, locale) {
    if (err == null || err === '') return '';
    if (typeof err === 'string') {
      const s = err.trim();
      if (s.startsWith('{') && s.includes('"key"')) {
        try {
          const o = JSON.parse(s);
          if (o && typeof o === 'object' && typeof o.key === 'string') return formatPipError(o, locale);
        } catch (_) { /* ignore */ }
      }
      return err;
    }
    if (typeof err === 'object' && typeof err.key === 'string') {
      let msg = tw(err.key, locale);
      if (err.pkg != null) msg = msg.replace(/\{pkg\}/g, String(err.pkg));
      if (err.dir != null) msg = msg.replace(/\{dir\}/g, String(err.dir));
      if (err.prefix != null && err.detail != null) msg = `${msg}\n${String(err.prefix)}: ${String(err.detail)}`;
      else if (err.detail) msg = `${msg}\n${String(err.detail)}`;
      return msg;
    }
    return String(err);
  }

  function formatPipErrorsList(errors, locale) {
    if (!errors || !errors.length) return '';
    return errors.map((e) => formatPipError(e, locale)).filter(Boolean).join('\n\n');
  }

  const app = Vue.createApp({
    data() {
      return {
        g: null,
        statusError: '',
        /** 首屏 true：避免首帧在 g=null 时既不显示「加载中」也不显示下方区块 */
        statusLoading: true,
        statusRefreshing: false,
        busy: false,
        lastLog: '',
        dl: null,
        pipErrorText: '',
        downloadErrorText: '',
        miscErrorText: '',
        /** @type {{ type: 'torch-cuda', index_url: string } | null} */
        pendingAfterPipUninstall: null,
        _lastPipTarget: null,
        _unsub: null,
        gptsovitsDir: '',
        gptsovitsSaveMsg: '',
        /** @type {'zh'|'en'} 与 localStorage locale 一致，供语言按钮高亮 */
        wizardUiLocale: (() => {
          try {
            return localStorage.getItem('locale') === 'en' ? 'en' : 'zh';
          } catch (_) {
            return 'zh';
          }
        })(),
      };
    },
    computed: {
      memBundles() {
        if (!this.g || !this.g.bundles) return [];
        return this.g.bundles.filter((x) => x.category === 'memory' || x.category === 'embedding');
      },
      ttsBundles() {
        if (!this.g || !this.g.bundles) return [];
        return this.g.bundles.filter((x) => x.category === 'tts');
      },
      kokoroBundles() {
        if (!this.g || !this.g.bundles) return [];
        return this.g.bundles.filter((x) => x.category === 'kokoro');
      },
      sttBundle() {
        if (!this.g || !this.g.bundles) return null;
        return this.g.bundles.find((x) => x.id === 'sherpa_sense_voice') || null;
      },
      hasAnyError() {
        return !!(this.pipErrorText || this.downloadErrorText || this.miscErrorText);
      },
    },
    mounted() {
      if (!api || !api.setupWizardGetStatus) {
        this.statusLoading = false;
        this.statusError = 'Electron API missing';
        this._removeBootSplash();
        return;
      }
      this._unsub = api.onSetupWizardEvent((ev) => this.onEvent(ev));
      try {
        this.wizardUiLocale = localStorage.getItem('locale') === 'en' ? 'en' : 'zh';
      } catch (_) { /* ignore */ }
      this.refresh(true)
        .catch(() => {})
        .finally(() => this._removeBootSplash());
    },
    unmounted() {
      if (this._unsub) this._unsub();
    },
    methods: {
      _removeBootSplash() {
        const el = document.getElementById('wiz-pre-vue');
        if (el && el.parentNode) el.parentNode.removeChild(el);
      },
      tw(key) {
        return tw(key, this.wizardUiLocale);
      },
      formatPipError(err) {
        return formatPipError(err, this.wizardUiLocale);
      },
      formatPipErrorsList(errors) {
        return formatPipErrorsList(errors, this.wizardUiLocale);
      },
      bundleTitle(b) {
        if (!b || !b.id) return '';
        const en = this.wizardUiLocale === 'en';
        const k = `modelBundle_${b.id}_title`;
        const L = window.LOCALES && window.LOCALES[en ? 'en' : 'zh'];
        if (L && L[k]) return L[k];
        return en ? (b.title_en || b.title_zh || b.id) : (b.title_zh || b.title_en || b.id);
      },
      bundleDesc(b) {
        if (!b || !b.id) return '';
        const en = this.wizardUiLocale === 'en';
        const k = `modelBundle_${b.id}_desc`;
        const L = window.LOCALES && window.LOCALES[en ? 'en' : 'zh'];
        if (L && L[k]) return L[k];
        return en ? (b.description_en || b.description_zh || '') : (b.description_zh || b.description_en || '');
      },
      clearOpErrors() {
        this.pipErrorText = '';
        this.downloadErrorText = '';
        this.miscErrorText = '';
      },
      onEvent(ev) {
        if (!ev) return;
        if (ev.type === 'download') {
          this.dl = {
            phase: ev.phase,
            progress_pct: ev.progress_pct,
            message: ev.message,
            error: ev.error,
            bundle_id: ev.bundle_id,
          };
          this.lastLog = ev.message || '';
          if (ev.phase === 'error' && ev.error) {
            this.downloadErrorText = String(ev.error);
          }
          if (ev.phase === 'running' || ev.phase === 'success') this.downloadErrorText = '';
        }
        if (ev.type === 'pip') {
          this.lastLog = ev.message || '';
          if (ev.phase === 'error') {
            this.pipErrorText = this.formatPipError(ev.error) || (ev.message ? String(ev.message) : this.tw('setupWizardPipFailedGeneric'));
          } else if (ev.phase === 'success') {
            this.pipErrorText = '';
          }
        }
        if (ev.type === 'child_done') {
          if (
            ev.exitCode === 0
            && ev.op === 'pip-uninstall'
            && this.pendingAfterPipUninstall
            && this.pendingAfterPipUninstall.type === 'torch-cuda'
          ) {
            const url = this.pendingAfterPipUninstall.index_url || '';
            this.pendingAfterPipUninstall = null;
            this.pipErrorText = '';
            this.startOp({
              op: 'pip-install',
              packages: ['torch', 'torchvision', 'torchaudio'],
              target: 'torch_cuda',
              index_url: url,
            });
            return;
          }
          // After torch_cuda installs successfully, repair qwen-tts only if it was
          // already installed (torch upgrade can break its native extension linkage).
          if (ev.exitCode === 0 && ev.op === 'pip-install') {
            const wasQwenInstalled = this.g && this.g.qwen_tts_installed;
            const lastTarget = this._lastPipTarget || '';
            if (lastTarget === 'torch_cuda' && wasQwenInstalled) {
              this._lastPipTarget = null;
              this.startOp({
                op: 'pip-install',
                packages: ['qwen-tts>=0.0.1', 'soundfile>=0.12.0'],
                target: 'qwen_tts_repair',
                index_url: '',
              });
              return;
            }
          }
          this.busy = false;
          if (ev.exitCode !== 0) {
            if (ev.op === 'pip-uninstall') this.pendingAfterPipUninstall = null;
            if (ev.op === 'pip-install') {
              const msg = this.pipErrorText || this.formatPipError(this.lastLog) || this.tw('setupWizardPipFailedGeneric');
              this.pipErrorText = msg;
            }
            if (ev.op === 'pip-uninstall' && !this.pipErrorText) {
              this.pipErrorText = this.tw('setupWizardUninstallFailedGeneric');
            }
            if (ev.op === 'download') {
              const msg = this.downloadErrorText || (this.dl && this.dl.error) || this.tw('setupWizardDownloadFailedGeneric');
              this.downloadErrorText = msg;
            }
            if (ev.op === 'apply-stt') {
              this.miscErrorText = this.tw('setupWizardApplySttFailed');
              window.alert(this.miscErrorText);
            }
            if (ev.op === 'launch-gptsovits') {
              this.miscErrorText = this.tw('setupWizardGptsovitsLaunchFailed');
              window.alert(this.miscErrorText);
            }
          } else {
            if (ev.op === 'pip-install') this.pipErrorText = '';
            if (ev.op === 'pip-uninstall') this.pipErrorText = '';
            if (ev.op === 'download') this.downloadErrorText = '';
            if (ev.op === 'apply-stt' || ev.op === 'launch-gptsovits') this.miscErrorText = '';
          }
          this.refresh();
        }
        if (ev.type === 'setup_result' && ev.result) {
          if (ev.result.ok) this.pipErrorText = '';
          else {
            const msg = this.formatPipErrorsList(ev.result.errors) || this.tw('setupWizardUninstallFailedGeneric');
            this.pipErrorText = msg;
          }
        }
      },
      async refresh(isInitial) {
        // First load: show full loading screen. Subsequent refreshes: silent background update
        // (no flicker — keep g in place while fetching).
        if (isInitial) {
          this.statusLoading = true;
        } else {
          this.statusRefreshing = true;
        }
        this.statusError = '';
        try {
          const r = await api.setupWizardGetStatus();
          if (r.ok) {
            this.g = r.data;
            if (this.g && this.g.download) this.dl = this.g.download;
            if (this.g && this.g.tts && this.g.tts.gpt_sovits_dir != null) {
              this.gptsovitsDir = this.g.tts.gpt_sovits_dir;
            }
          } else {
            this.statusError = r.error || 'status failed';
          }
        } catch (e) {
          this.statusError = String(e.message || e);
        } finally {
          this.statusLoading = false;
          this.statusRefreshing = false;
        }
      },
      startOp(payload) {
        this.clearOpErrors();
        this.busy = true;
        this.lastLog = '';
        if (payload.op === 'pip-install') this._lastPipTarget = payload.target || null;
        api.setupWizardStartOp(payload);
      },
      pipInstall(packages, target, index_url) {
        this.startOp({ op: 'pip-install', packages, target, index_url: index_url || '' });
      },
      /** @param {string[]} packages @param {string[]} [dirs] @param {boolean} [skipConfirm] */
      pipUninstall(packages, dirs, skipConfirm) {
        const pkgs = packages || [];
        if (!pkgs.length) return;
        if (!skipConfirm && !window.confirm(this.tw('confirmUninstallPackage'))) return;
        // 勿在「先卸 torch 再装 CUDA」链式流程里清空：skipConfirm=true 时由 pipTorchCuda 预设 pending。
        if (!skipConfirm) this.pendingAfterPipUninstall = null;
        this.startOp({ op: 'pip-uninstall', packages: pkgs, dirs: dirs || [] });
      },
      pipQwen() {
        this.pipInstall(['qwen-tts>=0.0.1', 'soundfile>=0.12.0'], 'qwen_tts', '');
      },
      async saveGptsovitsDir() {
        this.gptsovitsSaveMsg = '';
        this.miscErrorText = '';
        if (!api || !api.setupWizardSaveGptsovitsDir) {
          this.miscErrorText = 'Electron API missing';
          return;
        }
        this.busy = true;
        try {
          const res = await api.setupWizardSaveGptsovitsDir(this.gptsovitsDir || '');
          if (res && res.ok) {
            this.gptsovitsSaveMsg = this.tw('onboardingGptsovitsSaved');
            await this.refresh();
          } else {
            this.miscErrorText = (res && res.error) || this.tw('setupWizardPipFailedGeneric');
          }
        } catch (e) {
          this.miscErrorText = String((e && e.message) || e);
        } finally {
          this.busy = false;
        }
      },
      pipTorchCuda() {
        const url = (this.g && this.g.cuda_info && this.g.cuda_info.recommended_url)
          || 'https://download.pytorch.org/whl/cu124';
        if (this.g && this.g.torch_installed) {
          if (!window.confirm(this.tw('confirmUninstallTorchForCuda'))) return;
          this.pendingAfterPipUninstall = { type: 'torch-cuda', index_url: url };
          this.pipUninstall(['torch', 'torchvision', 'torchaudio'], [], true);
          return;
        }
        this.pipInstall(['torch', 'torchvision', 'torchaudio'], 'torch_cuda', url);
      },
      async doDownload(bundleId, source) {
        if (source === 'modelscope' && this.g && !this.g.modelscope_installed) {
          this.miscErrorText = '';
          this.pipErrorText = '';
          this.downloadErrorText = this.tw('setupWizardNeedModelscopeFirst');
          return;
        }
        this.startOp({ op: 'download', bundle_id: bundleId, source });
      },
      async pickStt() {
        const r = await api.setupWizardPickSttModel();
        if (!r || !r.path) return;
        this.startOp({ op: 'apply-stt', model_path: r.path });
      },
      async proceed() {
        if (this.busy && !window.confirm(this.tw('setupWizardAbortBusyConfirm'))) return;
        await api.setupWizardProceed();
      },
      /** @param {'zh'|'en'} lang */
      setWizardLocale(lang) {
        if (lang !== 'zh' && lang !== 'en') return;
        if (this.wizardUiLocale === lang) return;
        try {
          localStorage.setItem('locale', lang);
          document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
        } catch (_) { /* ignore */ }
        // Updating wizardUiLocale triggers Vue reactivity — tw() re-evaluates automatically,
        // no page reload needed.
        this.wizardUiLocale = lang;
      },
    },
  });

  app.mount('#wiz');
})();
