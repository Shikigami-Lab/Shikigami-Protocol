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

};
