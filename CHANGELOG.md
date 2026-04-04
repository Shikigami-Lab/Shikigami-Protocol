# Changelog

All notable changes to Shikigami Protocol will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Build / Release

- GitHub Releases no longer ship pre-built desktop installers (`.exe` / `.dmg` / `.AppImage`). Tags still trigger a release with generated notes; use GitHub’s source archives or clone the repository.

### In Progress

- [ASE/Reflection] Better ASE — more natural and surprising proactive speech (see `docs/ARCHITECTURE_REFERENCE.en.md` / `.zh.md` for reflection & ASE overview):
  - **speak_reason field**: Reflection now outputs a 6th JSON field (`speak_reason`) identifying the motivation type (`memory_recall` / `trend_share` / `emotional_overflow` / `silence_concern` / `none`). Urgency is now driven by motivation strength, not just silence duration.
  - **Proactive topic dedup** (`reflection_proactive_log`, priority 65): Injects the last 10 proactive speech entries into reflection so the AI avoids repeating the same topics. Consecutive `silence_concern` entries also trigger urgency suppression.
  - **Memory injection for surprise** (`reflection_memory_facts`, priority 70): Fetches high-weight long-term facts and injects them as optional conversation seeds, enabling the "how did she remember that?" effect.
  - **User engagement awareness** (`reflection_user_engagement`, priority 52): Unconditionally injects message frequency stats, time-of-day context, and 3-day participation trend so urgency decisions are informed by actual user activity patterns.
  - **Recent dialogue max_turns config** (`reflection.recent_dialogue_max_turns`, default 8): Caps the recent dialogue injected into reflection by turn count rather than characters. Configurable globally and surfaced in Settings → Reflection.
  - **speak_reason → ASE speech context**: `ReflectionStateSegment` injects a motivation guidance hint when speak_reason is non-none, informing how the AI should approach its proactive message.
  - **UI**: New "自省功能模块 / Reflection modules" section in persona editor Prompt Enhancement; new max_turns input in global Reflection settings.

### Planned

### Known Issues


---

## [0.9.17] — 2026-03-24

### Added
- [Build] Integrated PyInstaller for backend bundling, enabling fully portable execution without requiring local Python installation.
- [CI/CD] Automated GitHub Release workflow, supporting draft creation and automated artifact uploads.
- [UI] Completely redesigned Onboarding UI with a clearer flow and user-friendly setup guide.
- [UI] Implemented debounced auto-save for specific settings (TTS/Memory) with visual status indicators.
- [Personas] Added top-tier preset personas: Mochi (Tsundere Nekomata) and revamped Luna (Gentle & Quiet), complete with high-quality exclusive avatars, fully utilizing the emotion and affinity engines.
- [Sync] GitHub Actions quota management (paths-ignore, workflow_dispatch) and bundling details are reflected in `.github/workflows/` and release docs; internal sync playbooks stay out of the public tree.
- [Docs] Comprehensively updated `README.md` and `README_zh.md` with visual placeholders, badges, and optimized layout.

### Fixed
- [Fix] Resolved missing `package.json` and broken static resource paths after electron-builder packaging.
- [Fix] Fixed redundant status lines displaying in the SenseVoice configuration UI.
- [Fix] Synchronized default port mismatch between Electron and Python backend.
- [Fix] Corrected documentation path references in Linux/macOS `init.sh` scripts.
- [Docs] Cleaned up broken links (e.g., `CONTRIBUTING`) and removed references to internal private files (e.g., `CLAUDE.md`).

### Optimized
- [CI/CD] Optimized GitHub Actions consumption by adding `workflow_dispatch` manual triggers and `paths-ignore` filtering.
- [Git] Optimized `.gitignore` to strictly exclude `.cursor/` and other local development artifacts.
- [Assets] Standardized app icon paths and author information across all platforms.

---

## [0.9.7] — 2026-03-24 (Skip logs for 0.9.8-0.9.16)

### Added
- Auto-update support

---

## [0.9.6] — 2026-03

### Fixed

- `server.exe` crash on launch
- Port mismatch between Electron and Python backend
- Uninstall cleanup issues

---

## [0.9.4] — 2026-02

### Notes

- Early public release. Core systems (emotion engine, memory pipeline, ASE, reflection) functional.

---

## [0.9.3] — 2026-02

### Notes

- Initial CD pipeline and automated release workflow.

---

## [0.9.2] — 2026-01

### Added

- Onboarding UI overhaul
- ModelScope & Qwen3-TTS installers in Settings

---

## [0.9.1] — 2026-01

### Notes

- First tagged release.

---

## [0.9.0] — 2025-12

### Notes

- Initial public beta.

