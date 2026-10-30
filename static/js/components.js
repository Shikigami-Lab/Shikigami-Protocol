/* components.js —— 全局 Vue 组件定义（无构建步骤，字符串模板）。
 *
 * app.js 在 Vue.createApp() 之后、mount() 之前遍历 window.SK_COMPONENTS 注册。
 * 默认插槽内容在父作用域求值，所以槽内照样能用 t() / v-model / 父组件数据 ——
 * 这正是「把现有 markup 原样塞进组件」安全的原因。
 */
window.SK_COMPONENTS = {

  // Presentation only: parent owns run mapping, permissions and all side effects.
  'agent-task-card': {
    props: {
      run: { type: Object, required: true },
      t: { type: Function, required: true },
      label: { type: String, default: '' },
      stateLabel: { type: String, default: '' },
      timeLabel: { type: String, default: '' },
      eventLabel: { type: Function, required: true },
      eventText: { type: Function, required: true },
      artifactPath: { type: Function, required: true },
      terminal: { type: Boolean, default: false },
      canContinue: { type: Boolean, default: false },
      canDetach: { type: Boolean, default: false },
      hasTimers: { type: Boolean, default: false },
    },
    emits: ['cancel', 'continue', 'approve', 'artifact', 'timers', 'detach'],
    data() { return { expanded: false }; },
    computed: {
      pendingApprovalKey() {
        return !this.terminal && this.run.approval
          ? (this.run.approval.approval_id || this.run.approval.sequence || 'pending') : null;
      },
      toolWarning() {
        return this.run.state === 'completed' && (this.run.events || [])
          .some(event => event.type === 'tool_result' && event.is_error === true);
      },
    },
    watch: {
      // Only a new approval reopens the card; ordinary polling preserves toggles.
      pendingApprovalKey: { immediate: true, handler(key) { if (key !== null) this.expanded = true; } },
    },
    template: `
      <article class="agent-task-card" :aria-label="t('agentTaskDetails')">
        <button type="button" class="agent-task-summary" :aria-expanded="expanded"
                @click="expanded = !expanded">
          <span class="agent-task-chevron" aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
          <span>{{ t('agentTaskDetails') }}</span><span aria-hidden="true">·</span>
          <span class="agent-state" :class="['agent-state-' + run.state, { 'agent-state-tool-warning': toolWarning }]"
                :title="label || stateLabel">{{ label || stateLabel }}</span>
          <span v-if="run.error" class="agent-error agent-task-attention">{{ t('agentNeedsAttention') }}</span>
          <time v-if="timeLabel" class="agent-task-time">{{ timeLabel }}</time>
        </button>
        <div v-show="expanded" class="agent-task-details">
          <div class="agent-run-header">
            <span class="agent-state" :class="['agent-state-' + run.state, { 'agent-state-tool-warning': toolWarning }]">{{ stateLabel || label }}</span>
            <time v-if="timeLabel">{{ timeLabel }}</time>
            <div class="agent-actions">
              <button v-if="hasTimers" type="button" class="s-btn s-btn-small s-btn-secondary" @click="$emit('timers')">{{ t('agentOpenTimers') }}</button>
              <button v-if="terminal" type="button" class="s-btn s-btn-small s-btn-secondary" :disabled="!canContinue" @click="$emit('continue')">{{ t('agentContinue') }}</button>
              <button v-if="!terminal" type="button" class="s-btn s-btn-small s-btn-secondary" :disabled="run.cancelBusy || run.cancelling" @click="$emit('cancel')">{{ t(run.cancelling ? 'agentState_cancelling' : 'agentCancel') }}</button>
              <button v-if="canDetach" type="button" class="s-btn s-btn-small s-btn-secondary" @click="$emit('detach')">{{ t('agentBackground') }}</button>
            </div>
          </div>
          <p v-if="run.parent_run_id" class="form-hint">{{ t('agentParentTask') }}</p>
          <div v-if="run.artifacts?.length" class="agent-artifacts">
            <span>{{ t('agentArtifacts') }}:</span>
            <button v-for="(artifact, index) in run.artifacts" :key="index" type="button"
                    class="agent-artifact-link" :disabled="!artifactPath(artifact)" @click="$emit('artifact', artifact)">{{ artifactPath(artifact) || t('agentWorkspace') }}</button>
          </div>
          <div v-if="run.approval && !terminal" class="agent-approval">
            <strong>{{ t('agentExtraApproval') }}: {{ run.approval.action }}</strong>
            <p>{{ run.approval.reason }}</p>
            <pre>{{ run.approval.arguments }}</pre>
            <div class="agent-actions">
              <button type="button" class="s-btn s-btn-small s-btn-primary" :disabled="run.approvalBusy || run.cancelling" @click="$emit('approve', 'allowed-once')">{{ t('agentAllowOnce') }}</button>
              <button type="button" class="s-btn s-btn-small s-btn-secondary" :disabled="run.approvalBusy || run.cancelling" @click="$emit('approve', 'rejected')">{{ t('agentReject') }}</button>
            </div>
          </div>
          <p v-if="run.workspace_warning" class="form-hint agent-warning" role="status">{{ run.workspace_warning }}</p>
          <p v-if="run.error" class="agent-error" role="alert">{{ run.error }}</p>
          <div class="agent-task-events">
            <div v-for="event in run.events" :key="event.sequence" class="agent-event">
              <div :class="{ 'agent-error': event.is_error || event.type === 'failed' }">{{ eventLabel(event, run) }}</div>
              <pre v-if="eventText(event)">{{ eventText(event) }}</pre>
            </div>
          </div>
        </div>
      </article>`,
  },

  /* 可折叠区块：取代 section-collapsible / -header / -body 三层 + 一个外部
     show*Section 状态标志。
       自管模式：  <collapsible-section :title="t('secX')">…</collapsible-section>
       受控模式：  <collapsible-section :title="…" v-model:open="flag">…  （需要外部
                   程序化展开/收起时用，例如深链接按钮、切人格时重置） */
  'collapsible-section': {
    props: {
      title:       { type: String, default: '' },
      bodyClass:   { default: '' },
      open:        { default: null },              // 传了即受控；null=自管
      defaultOpen: { type: Boolean, default: false },
    },
    emits: ['update:open'],
    data() {
      return { innerOpen: this.defaultOpen };
    },
    computed: {
      isOpen: {
        get() { return this.open !== null ? this.open : this.innerOpen; },
        set(v) {
          if (this.open !== null) this.$emit('update:open', v);
          else this.innerOpen = v;
        },
      },
    },
    template: `
      <div class="section-collapsible">
        <div class="section-collapsible-header" @click="isOpen = !isOpen">
          {{ title }}
          <span>{{ isOpen ? '▲' : '▼' }}</span>
        </div>
        <div v-show="isOpen" class="section-collapsible-body" :class="bodyClass">
          <slot></slot>
        </div>
      </div>`,
  },

  /* 开关行：param-row + 一段 label + toggle-switch 开关。
       <toggle-row :label="t('lblX')" v-model="form.x" />
       <toggle-row :label="…" :title="t('titleX')" v-model="…" :disabled="…" />
     param-row 上的 style / 额外 class / v-for / v-if 会经 attribute fallthrough
     自动落到根 div，无需额外 prop。需要 change 副作用时父用 @update:model-value。
     label 需要富内容（图标等）时用默认插槽取代 label prop。 */
  'toggle-row': {
    props: {
      label:      { type: String, default: '' },
      title:      { type: String, default: '' },
      modelValue: { default: false },
      disabled:   { type: Boolean, default: false },
    },
    emits: ['update:modelValue'],
    template: `
      <div class="param-row">
        <span class="param-label" :title="title || null"><slot>{{ label }}</slot></span>
        <label class="toggle-switch">
          <input type="checkbox" :aria-label="label" :checked="modelValue" :disabled="disabled"
                 @change="$emit('update:modelValue', $event.target.checked)" />
          <span class="toggle-slider"></span>
        </label>
      </div>`,
  },

  /* 三态开关：人格级覆盖的「继承全局 / 强制开 / 强制关」分段按钮。
       <tri-toggle v-model="profileEnginesForm.xxx_enabled"></tri-toggle>
     v-model 取值 null（继承）/ true / false。标签经 inject 的 gt() 国际化。 */
  'tri-toggle': {
    inject: ['gt'],
    props: { modelValue: { default: null } },
    emits: ['update:modelValue'],
    template: `
      <div class="engine-tri-toggle">
        <button type="button" :class="['tri-btn',{active: modelValue===null}]"
                @click="$emit('update:modelValue', null)">{{ gt('optInherit') }}</button>
        <button type="button" :class="['tri-btn',{active: modelValue===true}]"
                @click="$emit('update:modelValue', true)">{{ gt('optOn') }}</button>
        <button type="button" :class="['tri-btn',{active: modelValue===false}]"
                @click="$emit('update:modelValue', false)">{{ gt('optOff') }}</button>
      </div>`,
  },

};
