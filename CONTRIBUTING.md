# Contributing to Shikigami Protocol

Thank you for your interest. Please read this document before opening a pull request.

<a href="CONTRIBUTING_zh.md">简体中文</a>

## License

This repository is distributed under the [**GNU Affero General Public License v3.0** (AGPL-3.0)](LICENSE). Code merged into the default branch must be distributable under **AGPL-3.0** (or another license explicitly approved by the maintainers).

## Developer Certificate of Origin (DCO)

Contributions must follow **[DCO 1.1](DCO.md)**.

- Every **commit** message must include a **`Signed-off-by`** line:

  ```text
  Signed-off-by: Random J Developer <random@developer.example.org>
  ```

  Use the name and email that match your public Git / GitHub identity.

- The easiest way: **`git commit -s`** (`-s` adds `Signed-off-by` automatically).

- If your PR contains multiple commits, **each commit** should include `Signed-off-by`, or squash/rebase as requested by maintainers.

- **What it means:** You certify you have the right to submit the contribution under this project’s license, per [DCO](DCO.md) clauses (a)–(d). This is **not** a copyright assignment to the project owners. Full text: [DCO.md](DCO.md).

## Code and pull requests

- Keep PRs focused and reviewable; open an issue first for large design changes.
- Follow existing style (Python: project conventions / `ruff`; frontend: patterns in `static/`).
- Before submitting, run the app locally and ensure there are no obvious syntax/runtime errors in touched paths.
- **Language**: use English for PR titles, descriptions, and commit messages. Code comments may be in English or Chinese.

### Branch naming and commit messages

Use a short semantic prefix:

| Prefix | When to use |
|---|---|
| `fix/` | Bug fix |
| `feat/` | New feature |
| `docs/` | Documentation only |
| `refactor/` | Code restructure, no behavior change |
| `chore/` | Build, deps, tooling |

Commit messages follow the same pattern: `fix: correct affinity decay on session reload`.

### On AI-assisted coding

AI tools are neither encouraged nor discouraged. If you use them, you are still responsible for the code — review it, test it, and be ready to discuss it in review. PRs that appear to be unreviewed bulk output may be closed without merging.

## Security issues

Do **not** post exploit details in public issues. Report according to the security / maintainer contact described in the README or Security policy.

## Relicensing from MIT to AGPL-3.0

If the project was previously published under **MIT**, moving the whole tree to **AGPL-3.0** may require care: maintainers may relicense their own copyright; **third-party contributions accepted only under MIT** may need separate consent or removal of those parts. **Consult a qualified attorney.**

---

*This is not legal advice.*
