# Changelog

All notable changes to Shikigami Protocol will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed

### Planned

### Known Issues

---

## [0.11.0] — 2026-10-30

### Added

- [UI] **Paste images into chat.** Pasting an image into the chat box attaches it to the pending message. Add text, remove the attachment, or send it through the existing image-analysis flow when ready.
- [LLM] **Model picker for saved presets.** Fetch the provider's model list in Settings, filter it by typing, and select with the mouse or keyboard. Manual model entry remains available; save connection changes before refreshing the list.
- [Prompt] **History time context.** Single and group chat prompts include time context aligned with the messages actually sent to the model, without adding time prefixes to message text.
- [UI] **Slash-command autocomplete** — typing `/` in the chat box now pops an inline suggestion dropdown (keyword filter, ↑↓ navigation, Tab/Enter to complete the command name, Esc to dismiss). Once an argument is typed, Enter sends exactly what you wrote and the dropdown only hints the argument format. The ⌘ commands panel gained a search box, a "recently used" section, click-to-run for argument-less commands (`/topic`, `/fact list`, …), and a footer listing the natural-language phrases that trigger tools directly. Both views render from a single command registry.
- [Commands] **`/topic` — ask for a fresh topic on demand.** Picks material from the topic-discovery sources (preferring external ones: web trends / random topics, falling back to the user's life / conversation recall / the AI itself) and injects it into the very same reply — zero extra LLM calls. `/topic trend|回忆|生活|自己|随机` forces a source. Also triggers on bare natural-language requests like "换个话题" / "聊点新鲜的" / "anything new?"; anything beyond the bare request — a negation ("别换个话题"), a statement ("我没有什么新鲜事"), a search request, or a topic of your own ("换个话题，聊聊X") — is left to normal chat. Neither the command nor the trigger picks material while topic discovery is turned off.
- [Memory] **Remembers how you address the character.** Nicknames you use for the character are detected from your own messages and accumulate in the user portrait: they don't fade when unused, up to 8 are kept (the least used drop out first), and they can be removed by hand. Detection runs as one extra helper-model call after each portrait refresh and needs the portrait enabled. The forms show as chips in Settings → User Portrait, where detection can also be turned off.
- [Prompt] **SillyTavern macros** — `{{char}}` and `{{user}}` (plus `{{persona}}`, `{{time}}`, `{{date}}`, `{{newline}}`, `{{trim}}`) are expanded when prompts are built: persona, style rules, lorebook, custom segments, evolved text, chat history, and the reflection / emotion / memory helper prompts. Imported character cards previously passed the literal macros to the model, and they leaked into replies and memory. Chat messages only get `{{char}}` and `{{user}}` expanded, and legacy uppercase `<USER>` / `<BOT>` / `<CHAR>` tags only in card (system) text, so templates or code such as `std::vector<char>` are left alone.
- [Memory] **Layered user portrait.** Besides the long-term portrait, the AI keeps a short "recent context" note (expires after 72 hours by default) and a list of contextual details that are only brought into a chat when their keywords come up. Both are shown in Settings → User Portrait, where the recent context can be cleared and details deleted one by one.
- [Memory] **Mutual address in the chat prompt.** The prompt states how the user wants to be called and, when no name is set, how the character usually addresses the user according to its style rules (for example Mochi's "投喂者"). Only explicit, affirmative rules count: pronouns, negated rules ("don't call them …") and the character's own nickname are ignored, and an address form is never presented as the user's name.
- [Persona] The persona prompt includes a standing rule to respect the user's agency: the character doesn't treat actions, feelings or choices the user hasn't expressed as established facts.
- [LLM] **Inline reasoning is stripped.** Models that put their reasoning inside `<think>…</think>` in the reply itself (MiniMax M2/M3, Qwen3 on Ollama, …) no longer leak it into the chat, history, memory extraction or emotion classification. Only a block at the start of a reply counts; a `<think>` mentioned later in the text is kept.

### Fixed

- [Chat] Single-message deletion now uses stable message IDs in single and group chats, including ASE proactive messages and timer-expiry reminders. Deleting from chat or the history panel updates the matching active view and broadcasts the deletion to connected views of the same conversation. A failed deletion is reported instead of silently removing the local bubble.
- [LLM] Full endpoint URLs ending in `/chat/completions`, including OpenRouter endpoints, are normalized to the API base URL for chat requests and model listing. Image analysis (VLM) reuses the same normalization.
- [Timer] Fixed a failure that prevented timer-expiry reminders from appearing in chat.
- [Windows] A peer reset during asyncio transport shutdown (WinError 10054) no longer interrupts socket release or floods the server log with cleanup tracebacks. Other transport and application errors remain visible.
- [Memory] The shipped `daily_reinforcement_enabled` default is now `false`, matching the inactive reinforcement path. This changes the default setting; it does not enable daily reinforcement. Daily weight decay is unaffected.
- [Memory] Profile edits saved while a user-portrait refresh or a persona evolution was waiting on the model are no longer overwritten when that call finishes.
- [Memory] Day summaries now receive the character's persona and how to refer to the user, not just the character's name, including profiles with a custom day-summary prompt (all bundled examples have one). Without it, models sometimes wrote "as an AI, I can't…" disclaimers instead of an in-character diary, and those summaries were injected back into every chat. The built-in diary prompt also asks for 3–5 sentences and never breaks character.
- [Wizard] The persona wizard and prompt autofill report an empty model reply (blocked by a content filter, timed out) or an LLM error as such, instead of a confusing JSON parse error. Replies with prose around the JSON, or with code fences inside JSON strings, now parse.
- [Electron] Links in chat messages open in the system browser instead of replacing the app window, which had no way back.
- [Electron] Startup no longer ends with no window. If the first page load fails (the local service isn't listening yet, or a network hiccup), the app keeps retrying, and waits up to 5 minutes while the service process is still starting. If the service process exits, an error dialog appears right away and points to the logs; if it never answers, the dialog appears when the wait runs out, and the app quits because no page ever loaded. Previously the splash screen just closed and no window appeared.
- [Electron] The in-app docs viewer only opens pages served by the local service. Links inside it open in the system browser, and other sites can no longer open a window inside the app.
- [UI] Keys pressed while composing text with an input method (Enter, arrow keys, Tab) are left to the IME instead of sending the message or moving the command selection.
- [Trends] The "recent web trends" chat segment (`trend_awareness`) was never registered with the prompt pipeline, so normal conversations never saw any fetched trends — it now actually injects (4h cooldown, can be turned off per profile in segment settings).
- [Trends] The fetcher now sends a browser User-Agent; several sites reject bot-style UAs outright. Note that Reddit's unauthenticated `.json` API is now fully blocked regardless of UA — use an `rss` source with the `/.rss` endpoint instead.
- [Reflection] Characters kept saying good morning around 05:00–06:00 no matter when the user actually gets up. The reflection engine's "user is probably asleep → keep urgency low" hint used a hard-coded 23:00–06:00 window, so once it lifted, the silence-based urgency rules pushed the first reflection over the ASE threshold. The sleep window is now learned from the user's own last 30 days of messages across all profiles and groups: among runs of quiet hours (hours with messages on ≤10% of the days they messaged at all) that include at least one hour between 00:00 and 06:00, the longest is used if it lasts 4–14 hours; otherwise, and with fewer than 7 days of data, it falls back to 23:00–06:00. The "slow reply → be more proactive" cadence note is no longer emitted inside that window.
- [ASE] When the model decides there is nothing worth saying and answers with the silence marker, nothing is sent. Previously a literal "（静默）" / "(silence)" message was stored, shown as a chat bubble and could be read aloud by TTS.
- [ASE] A failed LLM request during proactive speech is no longer posted and stored as the character's message ("[LLM Error: …]"); it is logged and skipped, as chat and group chat already did.

### Changed

- [Topics] Expanded the built-in icebreaker and choice-question pools. Random-topic selection remembers recently used text to reduce repeats, applies the configured recent-use window, and keeps cached built-in candidates within the selected pool.
- [Persona Evolution] Evolution is now conservative: at most 0–2 changes per round, each backed by evidence that holds across multiple conversations, and a round with too little evidence is skipped instead of forcing a change. The original persona is the baseline: every 3rd evolution (configurable in the evolution settings, 0 = off) rebuilds on it and keeps only changes with lasting evidence, a result that drops one of the original section headings is rejected, and user details (profile facts, temporary states, sensitive information) stay out of the persona. A persona over its length cap is still compressed in rounds without new changes (one extra model call when the first reply skips).
- [Memory] The user portrait records only what the user actually said or did (no psychological analysis, guessed motives or judgmental labels) and leaves out sensitive details such as real names, addresses, ID numbers, credentials, medical or financial details unless the user has volunteered them repeatedly.
- [UI] The prompt autofill panel shows which model will run it: the analysis model when it is enabled, otherwise the active chat model.
- [Performance] Chat history and group history now page, count and delete in SQL instead of loading the whole table, and history pages keep a stable order for messages sharing a timestamp.
- [Memory] Portrait refresh returns structured JSON (the long-term portrait plus recent context and contextual details) with length targets of 800–2500 characters (zh) / 400–1200 words (en), and rolling back a portrait restores all three layers.
- [i18n] The "person you know" section of the chat prompt uses English headings when the UI language is English.
- [UI] The memory change history in the facts view is collapsed by default and scrolls when expanded, so it no longer pushes the fact list off screen.
- [ASE] After a longer silence, proactive messages no longer assume the user is back: the prompt states how long ago they last spoke and that it isn't known whether they're around, so a plan like "say it once they're back" isn't acted on as if that were already true.
- [ASE/Reflection] Trend topic candidates now carry a snippet excerpt, so the reflection model can judge whether a bare headline is actually worth raising.
- [ASE/Reflection] Topic-selection balancing: the prompt now explicitly counts fresh outside topics as legitimate picks; `ai_self` (the AI's own thought) is cooled down for a round after being picked twice in a row; and when recent initiations all drew on memories or the AI's own state, the candidate list nudges toward an external topic.

### Security

- [API] The local API no longer accepts cross-origin requests from arbitrary web pages. Only origins on localhost or a private network address are allowed — the app window itself, and a phone or tablet opening the UI over the local network. Previously any website open in the browser could call the API while the app was running.
- [Models] Changing a preset's `base_url` now requires entering the API key again. Previously the stored key was kept and sent to the new endpoint on the next call, so anyone able to reach the API could redirect the key to a server they control.

### Build / Release

- GitHub Releases no longer ship pre-built desktop installers (`.exe` / `.dmg` / `.AppImage`). Tags still create a draft release with generated notes; use GitHub’s source archives or clone the repository.

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
- [CI] GitHub Actions quota management (paths-ignore, workflow_dispatch) and bundling details are reflected in `.github/workflows/` and the release docs.
- [Docs] Comprehensively updated `README.md` and `README_zh.md` with visual placeholders, badges, and optimized layout.

### Fixed
- [Fix] Resolved missing `package.json` and broken static resource paths after electron-builder packaging.
- [Fix] Fixed redundant status lines displaying in the SenseVoice configuration UI.
- [Fix] Synchronized default port mismatch between Electron and Python backend.
- [Fix] Corrected documentation path references in Linux/macOS `init.sh` scripts.
- [Docs] Cleaned up broken links (e.g., `CONTRIBUTING`) and removed references to internal working files.

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

