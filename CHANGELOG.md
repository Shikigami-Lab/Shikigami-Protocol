# Changelog

All notable changes to Shikigami Protocol will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Build / Release

- GitHub Releases no longer ship pre-built desktop installers (`.exe` / `.dmg` / `.AppImage`). Tags still trigger a release with generated notes; use GitHub’s source archives or clone the repository.

### Planned

### Known Issues


---

## [0.10.18] — 2026-06-11

### Fixed

- [ASE] Proactive speech could get permanently stuck after editing a profile or re-checking "load into chat": those flows replace the in-memory Session object, but the ASE loop kept reading the stale one — its urgency stayed 0 forever (every check skipped as `low_urgency`) while the status panel, reading the live object, showed all gates green. The loop now always operates on the live current session.
- [ASE] Once the silence window opened, the adaptive sleep degenerated into a 1-second busy loop (thousands of checks per hour, flooding `ase.log`). It now sleeps the normal `check_interval` once the window is open and only wakes early to catch the window opening.
- [UI] Unchecking "load into chat" on the profile you are currently viewing switched the header to another character but kept showing the old character's chat bubbles (and stayed subscribed to its event stream). The settings page now runs the full session re-sync.
- [Memory] Daily weight decay was a silent no-op: with the default `daily_decay_factor: 0.998`, the per-step change (~0.002) was rounded away by 2-decimal weight precision, so facts never actually decayed (and facts below weight 0.5 could never be reinforced). Weights now keep 4-decimal precision internally; the UI still displays 2 decimals.
- [Memory] The daily memory job (day summary, forgetting, portrait refresh) only ran if the app happened to be running at its scheduled time (default 00:05). It now catches up on startup: ~90s after launch, any profile that hasn't run today's job gets it run once (idempotent per day via `last_daily_run.json`, which was previously written but never read).

---

## [0.10.17] — 2026-05-18

### Added
- [ASE] **Proactive topic discovery** — when the AI speaks up after a silence, reflection picks a concrete topic from five pluggable sources (conversation recall / the user's life / the AI itself / web trends / an optional random-topic API), and the AI performs it in character. New `topic_discovery` config block and a "Proactive Topics" section in Settings → Reflection/ASE.
- [Memory] **User Portrait** — the AI maintains an evolving portrait of the user, refreshed automatically (daily job + conversation-volume burst), with a 20-entry history and rollback.
- [Memory] **Memory change log + rollback** — the daily memory/forgetting job now records a 20-entry change history (weight changes, added summary facts, vectors removed). Each run can be inspected and rolled back from Settings → Memory.
- [Emotion] **Dynamic emotion decay** — emotion now automatically fades back toward calm after a configurable period of user silence.
- [Reflection] Proactive-speech context injection — recent-proactive-log dedup, high-weight memory recall, and user-engagement stats — making proactive speech more grounded and less repetitive.

### Changed
- [Reflection] Reflection now selects a concrete proactive topic (`topic_pick` / `topic_angle`) instead of a free-text anchor; ASE pulls the fresh source material at speak time.
- [UI] Introduced a design-token system (spacing / radius / font-size / shadow / transition) with theme-adaptive derived colors, and began extracting reusable Vue components (collapsible sections, toggle rows, tri-state toggles).

### Fixed
- [Reflection/ASE] A personality with reflection or ASE individually disabled no longer leaks stale reflection state into chat prompts or the status panel.
- [Emotion] Emotion auto-decay now actually triggers — it was previously reset every cycle by the energy-recovery timestamp.
- [Persona Evolution] Automatic persona evolution is no longer silently disabled when memory extraction is off, and no longer misses its trigger when frequencies don't divide evenly.
- [Memory] Fixed an ASE memory-manager construction bug that silently broke memory segments during proactive speech.
- [Config] Unified the `max_history_turns` default across code and config; settings no longer silently regress the value on save.
- [UI] Inherited per-personality config fields now show the real global value as their placeholder instead of a hardcoded number.
- [UI] Fixed grayed-out card backgrounds and theme-breaking hardcoded warning colors; ASE status labels are now translated.
- [Chat] Fixed a duplicate stream-completion signal sent on empty / error responses.
- [Memory] Fixed a timezone inconsistency in day-summary date bucketing.

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

