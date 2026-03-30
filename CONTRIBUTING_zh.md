# Contributing to Shikigami Protocol

<a href="CONTRIBUTING.md">English</a>

感谢你愿意为本项目花时间。请先读完本节再提交 PR。

## 许可证

本仓库以 [**GNU Affero General Public License v3.0**（AGPL-3.0）](LICENSE) 发布。合并进主线的代码须在 **AGPL-3.0**（或维护者明确说明的兼容条款）下可被分发。

## Developer Certificate of Origin（DCO）

本项目要求贡献者遵守 **[DCO 1.1](DCO.md)**（开发者来源证书）。

- **每个提交**的说明中须包含一行 **`Signed-off-by`**，格式为：

  ```text
  Signed-off-by: Random J Developer <random@developer.example.org>
  ```

  其中姓名与邮箱须与你在 Git / GitHub 上公开身份一致。

- 最简单的方式：使用 **`git commit -s`**（`-s` 会自动加上 `Signed-off-by`）。

- 若 PR 含多个 commit，请**确保每个 commit** 都带 `Signed-off-by`；或在维护者要求下将分支整理为带 sign-off 的提交。

- **含义简述**：你声明该贡献由你本人有权按本仓库许可提交，或符合 [DCO](DCO.md) 中的 (b)(c) 条；**不涉及**向项目方转让著作权。完整条文见 [DCO.md](DCO.md)。

## 代码与 PR

- 尽量保持 PR 主题单一、可审查；大改动请先开 Issue 讨论。
- 遵循现有代码风格；Python 可参考 `ruff` / 项目惯例，前端参考 `static/` 既有写法。
- 提交前在本地确认应用能启动、相关路径无语法错误。
- **语言**：PR 标题、描述及 commit message 请使用英文；代码注释中英文均可。

### 分支命名与 commit 规范

使用简短的语义前缀：

| 前缀 | 适用场景 |
|---|---|
| `fix/` | 缺陷修复 |
| `feat/` | 新功能 |
| `docs/` | 仅文档改动 |
| `refactor/` | 代码重构，不改变行为 |
| `chore/` | 构建、依赖、工具链 |

commit message 同理：`fix: correct affinity decay on session reload`。

### 关于 AI 辅助编程

不禁止也不鼓励使用 AI 工具。使用后代码的责任仍由贡献者承担——请自行 review、测试，并在 code review 中能正常讨论。未经审阅的批量输出可能直接关闭。

## 安全漏洞

请勿在公开 Issue 中披露可利用细节；报告方式见 **[SECURITY.md](SECURITY.md)**。

## 关于从 MIT 迁移到 AGPL-3.0

若仓库历史上曾在 **MIT** 下发布，整体改为 **AGPL-3.0** 时：维护者对自己享有著作权的代码可选择新许可；**若存在他人仅在 MIT 下合并进来的贡献**，理想情况下应取得相应贡献者同意，或对相关部分另行处理。**请咨询专业法律顾问。**

---

*以上不构成法律意见；有疑问请咨询专业法律顾问。*
