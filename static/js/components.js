/* components.js —— 全局 Vue 组件定义（无构建步骤，字符串模板）。
 *
 * app.js 在 Vue.createApp() 之后、mount() 之前遍历 window.SK_COMPONENTS 注册。
 * 默认插槽内容在父作用域求值，所以槽内照样能用 t() / v-model / 父组件数据 ——
 * 这正是「把现有 markup 原样塞进组件」安全的原因。
 */
window.SK_COMPONENTS = {

  /* 可折叠区块：取代 section-collapsible / -header / -body 三层 + 一个外部
     show*Section 状态标志。组件自管开合状态。
       <collapsible-section :title="t('secX')" :body-class="{disabled:...}">
         ...body...
       </collapsible-section> */
  'collapsible-section': {
    props: {
      title:       { type: String, default: '' },
      bodyClass:   { default: '' },
      defaultOpen: { type: Boolean, default: false },
    },
    data() {
      return { open: this.defaultOpen };
    },
    template: `
      <div class="section-collapsible">
        <div class="section-collapsible-header" @click="open = !open">
          {{ title }}
          <span>{{ open ? '▲' : '▼' }}</span>
        </div>
        <div v-show="open" class="section-collapsible-body" :class="bodyClass">
          <slot></slot>
        </div>
      </div>`,
  },

};
