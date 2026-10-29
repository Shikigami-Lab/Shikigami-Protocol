/* components.js —— 全局 Vue 组件定义（无构建步骤，字符串模板）。
 *
 * app.js 在 Vue.createApp() 之后、mount() 之前遍历 window.SK_COMPONENTS 注册。
 * 默认插槽内容在父作用域求值，所以槽内照样能用 t() / v-model / 父组件数据 ——
 * 这正是「把现有 markup 原样塞进组件」安全的原因。
 */
window.SK_COMPONENTS = {

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
          <input type="checkbox" :checked="modelValue" :disabled="disabled"
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
