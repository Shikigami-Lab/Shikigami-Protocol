# DeepSeek Harness application integration

The existing chat, Settings and timer UI connect to the pinned full Harness SDK for tool-assisted chat, native files/code, controlled web retrieval, search, original-history tools, plans and task continuation. Task details appear inline with their associated messages. The workspace button in the chat header opens a right-hand drawer with **Files**, **Plans** and **Tasks** tabs. Opt-in reflection tasks use the same Harness loop. **AI tools, autonomous tasks and all optional capabilities are disabled by default.**

## Modules

- `src/agent/context.py`: adapts the existing `PromptPipeline` messages into trusted profile/source/revision context, preserving normalized text roles and time indices.
- `src/agent/runtime.py`: binds each run to its initiating profile, starts its Docker worker, and handles model/MCP/web IPC, streamed UI events, workspace snapshots and cancellation.
- `src/agent/settings.py`: validates and atomically saves settings using the existing configuration. Defaults: disabled, 6 model requests per run, 120-second timeout; accepted ranges are 1–32 requests and 10–600 seconds.
- `src/agent/capabilities.py`: resolves application defaults and per-character tri-state overrides, and selects the native and business-tool allowlists.
- `src/agent/model_gateway.py`: uses the configured model preset through native Google or OpenAI-compatible transport. Endpoint selection and API keys stay on the host.
- `src/agent/mcp_server.py` and `business_tools.py`: use official Python `mcp==1.26.0` to expose the existing timer service, `web_search` through the existing searcher, and profile-bound `history_search`, `history_context` and `task_context`. Original messages are read from `ConversationStore`, not reconstructed from facts or summaries.
- `src/agent/workspace.py` and `src/api/workspace.py`: persist character files and bounded snapshots, with host controls for inventory, preview/download, upload and individual-file version restore.
- `src/agent/workspace_catalog.py` and `todo_tools.py`: keep host-owned file descriptions and separate user todos / character plans, with links to workspace files and task runs.
- `src/agent/autonomy.py`: admits opt-in reflection-origin tasks for the selected single-chat character, using the existing runtime and shared limits.
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

Start the application normally. In **Settings → Models → Tool execution model**, select a saved preset or keep the default that follows the active chat preset. This model runs tasks delegated from chat and reflection. The chat model handles the initial reply or delegation and writes any final chat reply from the task result. Reflection uses its own preset to decide whether to start a task. **Settings → Tools** shows the effective model and controls request and timeout limits. **Profile → Tools** controls that profile's timer permission and overrides for tools, autonomy and capabilities.

Enable only the capabilities needed for a task, through application defaults in **Settings → Tools** or character overrides in **Profile → Tools**. These defaults live under the existing `agent` configuration; all default to `false`:

| Setting | Tools made available |
| --- | --- |
| `workspace_enabled` | Native `read`, `write`, `edit`, `glob`, `grep` in the character workspace |
| `code_enabled` | Native `bash`, `job_output`, `job_kill`; requires `workspace_enabled` |
| `web_enabled` | Native `web_fetch` through the controlled helper, plus free DDGS or optionally Serper-backed `web_search` |
| `history_enabled` | `history_search` by keyword/local date range, with newest/oldest ordering and paged original messages; `history_context` around an original message ID; `task_search` by prior task topic or recorded artifact name |
| `memory_enabled` | `memory_search` looks up recallable facts and experiences in the bound character's enabled memory. Uses the existing keyword and optional vector search; returns stored notes and available single-chat source IDs, read-only. |
| `todo_enabled` | `todo_list`, `todo_add`, `todo_update`, `todo_delete` for user todos and character plans; also requires the profile's existing todo permission |

Application `enabled`, `autonomy_enabled` and capability booleans are defaults. Under a character's `tool_configs.agent`, `true` or `false` explicitly overrides the matching default; missing or `null` inherits it. A character can therefore opt in while the global default is off. Effective `enabled` must be on for tool use; autonomous tasks additionally require effective `autonomy_enabled`. Code still requires effective workspace access. `task_context` is available when any capability is enabled, and `workspace_describe` accompanies workspace access. Existing timer and todo permissions remain separate gates.

Character overrides do not change shared hard bounds: the per-run model request limit and timeout still apply. Autonomous admission also enforces the configured minimum interval (default 900 seconds, range 60–86400) per character and rolling hourly request cap shared across characters (default 12, range 1–128). Each admitted task reserves its request allowance, including tool continuation rounds; unused reservations remain counted for the rolling hour.

Free search attempts the registered DDGS backends `duckduckgo`, `yahoo`, and `yandex` in order. It tries each at most once with a six-second wait per source, and cancellation stops further attempts. No paid search API key is needed unless you choose Serper; the configured chat model has its own requirements. An optional proxy is selected from `DDGS_PROXY`, then `HTTPS_PROXY`, then `HTTP_PROXY` (lowercase HTTPS/HTTP variants are also supported). Keep proxy credentials in the host environment, outside source files.

Search failures, including provider challenges, remain errors if fallback cannot return usable results. DDGS's `No results found` response can also indicate a blocked or failed provider request; it is not treated as proof that no matching pages exist. The tool reports an unsuccessful search instead of presenting that ambiguity as a successful empty result.

Ask to create a timer: the reply stays in the chat bubble, expandable task details appear inline with the associated messages, and the timer appears in the existing timer panel. Ask for a reminder at a specific date and time to schedule a one-time dated reminder through the same permission and panel. The chat Stop button requests native run cancellation; cancelling a run does not undo a timer already created. Cancel a countdown through its panel, a direct command, or a permitted tool request. Cancel or dismiss a dated reminder through the panel or a permitted tool request. **Reminders → Daily report** configures one recurring report per character. You can also ask the character to configure it through its timer permission. At the configured server-local time, a loaded character uses the active chat model and current local task data to send a report to its own chat. You can pause it and inspect its latest result in the drawer. Offline occurrences outside the scheduler's short grace period are marked missed.

For workspace tasks, ask the character to find dated original messages and write a cited note, or to write and run a Python script that creates an image. Use the workspace button in the chat header to open the right-hand drawer. **Files** retains inventory, text/image preview, download, upload and saved-version restore, with optional descriptions, categories and pinning. **Plans** separates user todos from the character's own plans, with controls and links to existing workspace files. **Tasks** provides task records and their controls alongside the inline message details. Task activity links to source messages and actual artifacts. Search in **Tasks** by topic or filename to find older work by the current character. The lookup searches up to 1,000 current-revision task records, including available original requests and recorded artifact names; workspace preview opens the current file at that path. Upload/restore controls wait until the character's active task has stopped; snapshots are bounded file versions, not a full-system backup.

Todos use separate `user` and `ai` owners; legacy items without an owner remain user todos. `source_path`, `evidence_path` and `last_run_id` link plans to notes, results and runs. Tool-supplied file links must refer to existing files in that character's workspace and require workspace access. Notebook prose does not automatically create a todo, and a pending plan is not a new user instruction.

The existing Stop control targets the current character's active tasks, including detached work; it displays cancellation in progress until terminal status arrives. Partial replies retain their run ID and completion status in history. **Continue** selects a prior terminal task for the next chat request: a fresh task reads the prior records and existing files before proceeding. It never restarts a stopped OS process. Ordinary user chat can continue while a task runs; such chat is conversation-only while that character has an active task.

With effective tools and autonomy enabled, the existing reflection cycle may propose a concrete action or choose no action. Only the selected single-chat character can start new autonomous work; admitted tasks keep their captured character and continue after switching views, subject to permissions and limits. They can use enabled research, history, workspace and character-plan tools, but cannot mutate user todos or use timers. Results appear in task records/workspace files and return to that character's own reflection; autonomous tasks do not post background chat messages.

**Behavior → Proactive Topics → Web research** lets the proactive chat model choose whether to look up a public topic after its ordinary speaking checks. This requires effective tools, autonomy and web permission for the selected character. The Harness task receives the public query and can use only web search and fetch. Successful results go back to the same proactive turn, and the chat model writes a reply tied to the observed links. If it chooses to speak without research, it uses the ordinary chat call.

### Source updates and rollback

1. Stop the application and its workers. Back up the source/dependency versions together with configuration, profiles, chat/memory databases, agent records, workspaces and snapshot databases. Preserve SQLite WAL/SHM sidecars where present; copying only a live main database is insufficient.
2. Apply the source update, including removal of `src/commands/nl_triggers.py`. Preserve `.env`, `config/app.yaml`, profiles and runtime data. History changes add SQLite columns; retain the matching pre-update backup for rollback.
3. Install requirements and rebuild the image as above. Start with AI tools disabled, check normal chat/history and runtime status, then enable capabilities individually. Check files, citations, Stop and Continue before everyday use.
4. To roll back, stop the application and workers, then restore matching source, dependencies/image and data/configuration together. Preserve newer work separately first. Disabling tools or restoring source alone does not undo completed effects.

## Behavior and isolation

- With tools on, a single chat-model request can answer directly, ask a question, or delegate a task. Delegation starts Harness with the tool model. The chat model writes the final bubble after execution. A continued task goes straight to execution. Conversation-only replies during an active task and tools-off chat use the chat model without delegation. Buttons and slash commands continue to call existing services directly. The replaced `nl_triggers` path and unused timer NL helpers have been removed.
- Each run has a Docker container with `--network none`, a read-only root filesystem, resource limits and a separate native Harness home. Host bind mounts are limited to the read-only integration bundle, that run's writable Harness home and, when enabled, the character's dedicated files directory at `/workspace`; temporary container filesystems are also writable. No whole-project, private-runtime, profile-directory or Docker-socket mount is used. Code runs in this container with no direct network access.
- Model and MCP traffic crosses the host IPC bridge. Secrets remain on the host, and an unavailable container does not trigger silent host execution. Native file/code tools and business tools are restricted to the run's authorized capabilities; this does not expose all upstream SDK tools.
- Web retrieval uses a separate ephemeral, read-only-root helper with network access, invoking upstream `ctx.web.fetch`. It has no workspace, chat history, model configuration or credentials; only the integration bundle and temporary runtime files are available. The bridge accepts a public HTTP(S) URL without credentials, bounds requests/results and time, and uses the native provider's destination/redirect protections. It is not a general traffic proxy or host webpage fetch fallback.
- If **all** system DNS answers for a hostname fall in the fake-IP range `198.18.0.0/15`, the bridge automatically sends only that hostname to Google's `https://dns.google/resolve` DNS endpoint, without credentials, cookies or URL path/query. Validated public IPs are pinned in that helper's `/etc/hosts`. Mixed or non-public answers fail closed; OS DNS/proxy settings are unchanged. This DNS lookup does not move webpage retrieval out of the native provider.
- Chat text remains in `ConversationStore`; additive `agent_run_id` and `generation_status` columns preserve task provenance and incomplete replies. Harness owns its native session records. Agent state lives under ignored `data/agent/`; current character files persist under `profiles/<id>/workspace/`. Host-owned `todos.json` and `workspace_index.json` sit alongside that directory in `profiles/<id>/`, outside the model mount. Legacy files under `data/agent/workspaces/files/<profile-hash>/` are copied forward with verification and retained; conflicting nonempty destinations fail closed. The existing version database stays at `data/agent/workspaces/versions.sqlite3`, outside the model mount. Profile invalidations persist across restart, preventing stale runs or deleted source messages from becoming continuation context.
- With effective workspace access, a partial directory of at most 12 files and 6,000 characters enters the existing system context, including optional descriptions, categories, pinning and modification times, not file bodies. `workspace_describe` updates metadata for existing own-workspace files. Directory entries and descriptions are data, not instructions or authority; use file tools to inspect relevant contents.
- Snapshots are taken before and after workspace tasks. The process file-size limit is 10 MiB (`fsize=10485760`); file reads/uploads/restores have the same limit. Inventory is bounded to 500 files, 2,000 entries and 16 path levels. Upload/restore and each snapshot have a 50 MiB budget; retention is at most 10 snapshots per character, 128 globally and 256 MiB of stored file-version content. These are bounded storage operations, not a total-disk quota on arbitrary code writes. Oversized files, incomplete inventories and snapshot omissions are explicitly surfaced in the UI/manifest; omitted content is not recoverable from that snapshot.
- Continuation validates character ownership, history revision and original message linkage, then starts a new run using bounded prior-task records. Startup marks interrupted runs as failed/unfinished; it does not automatically replay completed or uncertain effects. Cancellation stops further steps and cleans up workers/helpers, but preserves already-written files and completed effects. A cleanup failure remains visible and blocks conflicting workspace access.
- Tool outcomes and terminal task observations are committed atomically to SQLite with their associated receipts/run state. The next **existing reflection batch** for the owning character consumes pending observations; acknowledgment follows successful atomic `runtime_state` saving. Tool results, quoted history, group dialogue and web content grant no new user authority. A reflection batch digesting tool results cannot start another autonomous action. This uses the existing reflection loop, not the proposed richer character-state system.

Historical text without recorded assistant chunks uses `stream: []` in native seed events, without model replay or invented chunks. Missing historical attribution uses the local convention `shikigami-history/unknown-provider` / `unknown-model`; these are not upstream sentinels or evidence of the original model.

## Verification and limits

Run automated checks from the repository root:

```powershell
python -m unittest discover -s tests
node --test tests/agent-ui.test.js tests/agent-workspace-ui.test.js tests/todo-ui.test.js
node --test integrations/deepseek-harness/tests/*.test.mjs
```

Keep validation evidence in these categories:

- **Automated regressions:** context/provenance, tri-state profile permissions, autonomous admission/budgets and owner isolation, todo ownership/file links, workspace metadata/migration, history invalidation, workspace boundaries, snapshots, search fallback/cancellation and safe error reporting.
- **Offline native runtime:** synthetic model fixtures exercise native history/control behavior without real API keys. File/code tasks, Stop and restart/Continue also need application-level coverage.
- **Browser workflows:** check inline task/message association, the Files/Plans/Tasks drawer, file metadata/preview/upload/restore, todo ownership and artifact links, Stop/Continue, character switching, themes and narrow layouts.
- **Provider integration:** verify search and fetch results separately from model transport. A successful HTTP response alone does not prove a successful tool result; provider-only checks do not establish the full model-facing workflow.

For the synthetic, offline protocol fixture:

```powershell
./integrations/deepseek-harness/run-smoke.ps1 -Build
```

Omit `-Build` to reuse its image. Building requires network access; the fixture itself runs with a fake model, no real API keys and no container network.

Offline fixtures verify runtime behavior; real-provider autonomy and action relevance require separate acceptance.

Countdown timers live in memory. One-time dated reminders persist across restarts, appear in the same timer panel, and stay listed until dismissed. An overdue reminder is marked missed when the server next starts. Dated reminders use a local notice at expiry without calling a model. Receipts do not make external effects exactly-once across a crash; stopping a run does not roll back completed effects. This integration does not provide browser/desktop automation, a subagent UI, a new character-state system, group-chat tools or a new installer. Office authoring is not a guaranteed capability.
