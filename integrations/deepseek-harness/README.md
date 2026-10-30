# DeepSeek Harness application integration

The existing chat, Settings and timer UI connect to the pinned full Harness SDK for tool-assisted chat, native files/code, controlled web retrieval, search, original-history tools and task continuation. Task details appear inline with their associated messages. The workspace button in the chat header opens a right-hand drawer with **Files** and **Tasks** tabs. **AI tools and all optional capabilities are disabled by default.**

## Modules

- `src/agent/context.py`: adapts the existing `PromptPipeline` messages into trusted profile/source/revision context, preserving normalized text roles and time indices.
- `src/agent/runtime.py`: binds each run to its initiating profile, starts its Docker worker, and handles model/MCP/web IPC, streamed UI events, workspace snapshots and cancellation.
- `src/agent/settings.py`: validates and atomically saves settings using the existing configuration. Defaults: disabled, 6 model requests per run, 120-second timeout; accepted ranges are 1–32 requests and 10–600 seconds.
- `src/agent/capabilities.py`: combines application opt-ins with per-character restrictions and selects the native and business-tool allowlists.
- `src/agent/model_gateway.py`: uses the configured model preset through native Google or OpenAI-compatible transport. Endpoint selection and API keys stay on the host.
- `src/agent/mcp_server.py` and `business_tools.py`: use official Python `mcp==1.26.0` to expose the existing timer service, `web_search` through the existing searcher, and profile-bound `history_search`, `history_context` and `task_context`. Original messages are read from `ConversationStore`, not reconstructed from facts or summaries.
- `src/agent/workspace.py` and `src/api/workspace.py`: persist character files and bounded snapshots, with host controls for inventory, preview/download, upload and individual-file version restore.
- `src/agent/web_gateway.py` and integration `fetch.py` / `fetch.mjs`: bridge validated URL requests to the pinned upstream `ctx.web.fetch` in a disposable network-only helper.
- `src/agent/link_store.py`: stores minimal run/UI projections, message/continuation links, profile invalidations, action receipts and the reflection outbox in SQLite; it is not a second raw chat or execution log.
- `worker.py`, `agent.mjs`, `history.mjs` and `controls.mjs`: launch the full `sdk` profile and adapt native history/control hooks and the native MCP client. SDK and runtime-bin are pinned to `0.2.1rc2`; the upstream model–tool loop is not reimplemented.

## Run

This source-based setup requires the host application's Python environment and a running Docker engine capable of Linux containers. It does not provide a new installer. From the repository root, with that Python environment active:

```powershell
pip install -r requirements.txt
docker build -t shikigami-harness:workspace-0.2.1rc2 integrations/deepseek-harness
```

The root requirements include `mcp==1.26.0`; the image installs the pinned Harness dependencies from this integration's requirements. The workspace image adds Python, Pillow and Matplotlib for bounded code/image tasks. Both the SDK and runtime-bin are pinned to `0.2.1rc2`. Building the image and installing dependencies require network access.

Start the application normally. Configure the existing **Models** tab, then open **Settings → Tools** to enable AI tools and inspect runtime status. Tools displays the current model preset read-only; it follows the existing model selection and has no separate key form. Advanced settings control request and timeout limits. **Profile → Tools** controls that profile's timer permission and capability restrictions.

Enable only the capabilities needed for a task in **Settings → Tools**. These settings live under the existing `agent` configuration; all default to `false`:

| Setting | Tools made available |
| --- | --- |
| `workspace_enabled` | Native `read`, `write`, `edit`, `glob`, `grep` in the character workspace |
| `code_enabled` | Native `bash`, `job_output`, `job_kill`; requires `workspace_enabled` |
| `web_enabled` | Native `web_fetch` through the controlled helper, plus free DDGS or optionally Serper-backed `web_search` |
| `history_enabled` | `history_search` by keyword/local date range and `history_context` around an original message ID |

The main `enabled` switch must also be on. Per-character restrictions under `tool_configs.agent` can disable any globally enabled capability; they cannot enable a globally disabled one. `task_context` is available when any of these capabilities is enabled. Existing timer permissions remain separate.

Free search uses DDGS's registered `duckduckgo` backend, with a bounded fallback to its registered `yahoo` backend. Attempts and execution time are limited, and cancellation stops further search attempts. No paid search API key is needed unless you choose Serper; the configured chat model has its own requirements. An optional proxy is selected from `DDGS_PROXY`, then `HTTPS_PROXY`, then `HTTP_PROXY` (lowercase HTTPS/HTTP variants are also supported). Keep proxy credentials in the host environment, outside source files.

Search failures, including provider challenges, remain errors if fallback cannot return usable results. DDGS's `No results found` response can also indicate a blocked or failed provider request; it is not treated as proof that no matching pages exist. The tool reports an unsuccessful search instead of presenting that ambiguity as a successful empty result.

Ask to create a timer: the reply stays in the chat bubble, expandable task details appear inline with the associated messages, and the timer appears in the existing timer panel. The chat Stop button requests native run cancellation; cancelling a run does not undo a timer already created. Cancel that timer through its panel, a direct command, or a permitted tool request.

For workspace tasks, ask the character to find dated original messages and write a cited note, or to write and run a Python script that creates an image. Use the workspace button in the chat header to open the right-hand drawer. **Files** retains inventory, text/image preview, download, upload and saved-version restore. **Tasks** provides task records and their controls alongside the inline message details. Task activity links to source messages and actual artifacts. Upload/restore controls wait until the character's active task has stopped; snapshots are bounded file versions, not a full-system backup.

The existing Stop control targets the current character's active tasks, including detached work; it displays cancellation in progress until terminal status arrives. Partial replies retain their run ID and completion status in history. **Continue** selects a prior terminal task for the next chat request: a fresh task reads the prior records and existing files before proceeding. It never restarts a stopped OS process. Ordinary user chat can continue while a task runs; such chat is conversation-only while that character has an active task.

### Source updates and rollback

1. Stop the application and its workers. Back up the source/dependency versions together with configuration, profiles, chat/memory databases, agent records, workspaces and snapshot databases. Preserve SQLite WAL/SHM sidecars where present; copying only a live main database is insufficient.
2. Apply the source update, including removal of `src/commands/nl_triggers.py`. Preserve `.env`, `config/app.yaml`, profiles and runtime data. History changes add SQLite columns; retain the matching pre-update backup for rollback.
3. Install requirements and rebuild the image as above. Start with AI tools disabled, check normal chat/history and runtime status, then enable capabilities individually. Check files, citations, Stop and Continue before everyday use.
4. To roll back, stop the application and workers, then restore matching source, dependencies/image and data/configuration together. Preserve newer work separately first. Disabling tools or restoring source alone does not undo completed effects.

## Behavior and isolation

- With tools on, ordinary natural-language chat uses Harness, except for conversation-only replies while that character already has an active task. Conversation-only replies and tools-off chat use plain LLM chat without natural-language regex tool execution. Buttons and slash commands continue to call existing services directly. The replaced `nl_triggers` path and unused timer NL helpers have been removed.
- Each run has a Docker container with `--network none`, a read-only root filesystem, resource limits and a separate native Harness home. Host bind mounts are limited to the read-only integration bundle, that run's writable Harness home and, when enabled, the character's dedicated files directory at `/workspace`; temporary container filesystems are also writable. No whole-project, private-runtime, profile-directory or Docker-socket mount is used. Code runs in this container with no direct network access.
- Model and MCP traffic crosses the host IPC bridge. Secrets remain on the host, and an unavailable container does not trigger silent host execution. Native file/code tools and business tools are restricted to the run's authorized capabilities; this does not expose all upstream SDK tools.
- Web retrieval uses a separate ephemeral, read-only-root helper with network access, invoking upstream `ctx.web.fetch`. It has no workspace, chat history, model configuration or credentials; only the integration bundle and temporary runtime files are available. The bridge accepts a public HTTP(S) URL without credentials, bounds requests/results and time, and uses the native provider's destination/redirect protections. It is not a general traffic proxy or host webpage fetch fallback.
- If **all** system DNS answers for a hostname fall in the fake-IP range `198.18.0.0/15`, the bridge automatically sends only that hostname to Google's `https://dns.google/resolve` DNS endpoint, without credentials, cookies or URL path/query. Validated public IPs are pinned in that helper's `/etc/hosts`. Mixed or non-public answers fail closed; OS DNS/proxy settings are unchanged. This DNS lookup does not move webpage retrieval out of the native provider.
- Chat text remains in `ConversationStore`; additive `agent_run_id` and `generation_status` columns preserve task provenance and incomplete replies. Harness owns its native session records. Agent state lives under ignored `data/agent/`; character files persist under `data/agent/workspaces/files/<profile-hash>/`, with the host-only snapshot database outside the model mount. Profile invalidations persist across restart, preventing stale runs or deleted source messages from becoming continuation context.
- Snapshots are taken before and after workspace tasks. The process file-size limit is 10 MiB (`fsize=10485760`); file reads/uploads/restores have the same limit. Inventory is bounded to 500 files, 2,000 entries and 16 path levels. Upload/restore and each snapshot have a 50 MiB budget; retention is at most 10 snapshots per character, 128 globally and 256 MiB of stored file-version content. These are bounded storage operations, not a total-disk quota on arbitrary code writes. Oversized files, incomplete inventories and snapshot omissions are explicitly surfaced in the UI/manifest; omitted content is not recoverable from that snapshot.
- Continuation validates character ownership, history revision and original message linkage, then starts a new run using bounded prior-task records. Startup marks interrupted runs as failed/unfinished; it does not automatically replay completed or uncertain effects. Cancellation stops further steps and cleans up workers/helpers, but preserves already-written files and completed effects. A cleanup failure remains visible and blocks conflicting workspace access.
- Tool outcomes and terminal task observations are committed atomically to SQLite with their associated receipts/run state. The next **existing reflection batch** consumes pending observations; acknowledgment follows successful atomic `runtime_state` saving. This does not introduce autonomous actions or the proposed richer character-state system.

Historical text without recorded assistant chunks uses `stream: []` in native seed events, without model replay or invented chunks. Missing historical attribution uses the local convention `shikigami-history/unknown-provider` / `unknown-model`; these are not upstream sentinels or evidence of the original model.

## Verification and limits

Run automated checks from the repository root:

```powershell
python -m unittest discover -s tests
node --test tests/agent-ui.test.js tests/agent-workspace-ui.test.js
node --test integrations/deepseek-harness/tests/controls.test.mjs integrations/deepseek-harness/tests/history.test.mjs
```

Keep validation evidence in these categories:

- **Automated regressions:** context/provenance, profile permissions, history invalidation, workspace boundaries, snapshots, search fallback/cancellation and safe error reporting.
- **Offline native runtime:** synthetic model fixtures exercise native history/control behavior without real API keys. File/code tasks, Stop and restart/Continue also need application-level coverage.
- **Browser workflows:** check inline task/message association, the Files/Tasks drawer, file preview/upload/restore, Stop/Continue, character switching, themes and narrow layouts.
- **Provider integration:** verify search and fetch results separately from model transport. A successful HTTP response alone does not prove a successful tool result; provider-only checks do not establish the full model-facing workflow.

For the synthetic, offline protocol fixture:

```powershell
./integrations/deepseek-harness/run-smoke.ps1 -Build
```

Omit `-Build` to reuse its image. Building requires network access; the fixture itself runs with a fake model, no real API keys and no container network.

Timers remain in memory and do not survive application restart. Receipts do not make external effects exactly-once across a crash; stopping a run does not roll back completed effects. This integration does not provide browser/desktop automation, a subagent UI, idle autonomous activity, a new character-state system, group-chat tools or a new installer. Office authoring is not a guaranteed capability.
