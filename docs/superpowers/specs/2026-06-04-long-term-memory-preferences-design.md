# Long-Term Memory And Preferences - Design

**Date:** 2026-06-04
**Status:** Draft for user review

## Goal

Give Jarvis durable personalization and automatic recall without letting the
agent silently rewrite its own behavior.

The feature has two separate memory lanes:

- **Approved preferences:** standing behavioral instructions that shape future
  runs. These require explicit approval before activation.
- **Automatic recall memories:** searchable prior-context summaries that help
  Jarvis remember earlier conversations, decisions, commands, errors, and
  project context. These are automatically retrieved when relevant, but they do
  not become standing instructions.

## Background

Jarvis already persists conversations, messages, triggers, audit events,
schedules, MCP server state, actions, and settings in SQLite. The original v1
architecture intentionally deferred long-term memory and RAG, but kept the
runtime shape compatible with richer memory later: short-lived agent runs,
durable database-backed state, and a single `AgentRunner` path for Discord,
scheduled, and dashboard manual invocations.

The Action Inbox feature also created a useful safety precedent. Jarvis can
pause work that would alter state, show the operator an explicit pending item,
and resume after a decision. Long-term preferences should use the same
operator-controlled pattern because they alter future behavior.

## Decisions

1. **Separate preferences from recall.** Preferences are instructions; recall
   memories are context. They are stored, displayed, injected, and governed
   differently.
2. **Require approval for behavior-shaping preferences.** Jarvis may propose a
   preference, but only an approved preference is injected as standing behavior.
3. **Use automatic recall, not a search command.** Before each run, Jarvis
   searches prior memory across all channels and injects the most relevant
   context automatically.
4. **Use vector recall in v1.** Jarvis will store summary embeddings in SQLite
   using `sqlite-vec` rather than starting with keyword-only search or adding an
   external vector database.
5. **Index summaries first, with raw transcript fallback.** Jarvis embeds
   compact memory summaries and selected evidence snippets for the automatic
   vector recall path. It keeps raw conversation messages in the existing
   transcript store and can use them as an exact fallback when a user asks for
   precise prior wording, quotes, commands, or error text. It does not embed
   every raw message in v1.
6. **Cross-channel recall is the default.** Discord, dashboard, and scheduled
   memories are all eligible for recall. Channel metadata is stored so future
   filtering remains possible.
7. **Retrieved memories are not truth or policy.** Current user instructions
   beat retrieved memories. Approved preferences beat retrieved memories.
8. **Recall failures must not block runs.** Memory should make Jarvis more
   useful, but a broken embedding or retrieval path should not take the
   assistant offline.

## User Experience

### Approved Preferences

Preferences are concise standing rules such as:

- "Prefer concise answers."
- "Ask before making destructive finance changes."
- "For YNAB reconciliation, check the external portal before adjusting YNAB."

Active preferences are injected into every run in a `Standing preferences`
section. They can be viewed, edited, archived, or approved from the dashboard.
Jarvis may propose new preferences when a conversation contains reusable
behavioral guidance, but proposed preferences remain inactive until approved.

### Automatic Recall

Recall memories are prior-context records such as:

- "We diagnosed the Gmail MCP OAuth issue and narrowed it to the upstream MCP
  tool service rather than Jarvis token storage."
- "PR #18 added the Action Inbox and validated `/actions`, `/mcp`, and
  `/settings` after Dockge deploy."
- "In prior YNAB work, the workflow used external account portals before YNAB
  adjustments."

These memories help Jarvis continue extended work and answer questions about
earlier conversations. They are injected only when retrieval says they are
relevant to the current prompt.

When the user asks for exact prior wording, Jarvis should use the retrieved
summary as a pointer back to the source conversation and then inspect the raw
messages for precise quotes, commands, or error text. The raw transcript is a
fallback evidence source, not the primary automatic recall index.

### Evidence Snippets

Each recall memory can include a small number of exact snippets. Examples:

- command text
- error text
- file path
- PR or issue number
- URL
- short decision quote
- identifier such as a branch, model, account, or service name

Evidence snippets improve exact recall without indexing every raw message. They
also help Jarvis decide which source conversation to inspect when the user asks
for precise prior wording.

## Data Model

### `memory_preferences`

Stores approved or pending standing preferences.

Core fields:

- `id`
- `content`
- `status` (`pending`, `active`, `archived`, `rejected`)
- `source` (`dashboard`, `agent_proposal`, `migration`, or `manual_seed`)
- `created_at`
- `updated_at`
- `approved_at`
- `archived_at`

Only `status=active` preferences are injected into runs.

### `memory_entries`

Stores recallable summaries.

Core fields:

- `id`
- `conversation_id`
- `source_channel_kind`
- `source_channel_ref`
- `summary`
- `topics`
- `entities`
- `status` (`active` or `archived`)
- `created_at`
- `updated_at`
- `last_recalled_at`

The `topics` and `entities` fields are structured JSON lists that support
dashboard browsing and future ranking improvements.

### `memory_evidence`

Stores exact snippets attached to a memory entry.

Core fields:

- `id`
- `memory_entry_id`
- `kind` (`command`, `error`, `url`, `file_path`, `decision`, `quote`,
  `identifier`, or `note`)
- `label`
- `content`
- `created_at`

Evidence snippets are optional. They should be short and high signal.

### `memory_vectors`

Stores summary embeddings in a `sqlite-vec` virtual table keyed to
`memory_entries.id`.

The searchable embedding text is the summary plus compact topic/entity labels,
not the full raw transcript.

### `memory_recall_events`

Stores explainability records for each retrieval.

Core fields:

- `id`
- `conversation_id`
- `trigger_id`
- `memory_entry_id`
- `score`
- `rank`
- `created_at`

These rows let the dashboard show which memories were used for a run and help
debug surprising recall.

## Runtime Behavior

### Before A Run

1. Load active approved preferences.
2. Embed the incoming user prompt or scheduled prompt.
3. Query `sqlite-vec` for relevant active memory entries across all channels.
4. Apply deterministic filters:
   - skip archived entries;
   - require a minimum relevance threshold;
   - cap injected memories at five entries in v1;
   - keep ranking stable when scores tie.
5. Record `memory_recall_events` for injected memories.
6. Assemble the prompt with this order:
   - system prompt;
   - `Standing preferences`;
   - `Relevant prior context`;
   - trigger-specific context, such as schedule timezone/date context;
   - current user prompt.

The prior-context section must explicitly say that retrieved memories are
possibly relevant context, not standing instructions.

### After A Run

1. Persist messages and audit events through the existing path.
2. Summarize meaningful runs into a compact `memory_entries` row.
3. Extract topics, entities, and a small number of evidence snippets.
4. Embed the summary and store it in `memory_vectors`.
5. If the run contains a durable behavioral preference candidate, create a
   preference proposal instead of silently activating it.

Summarization and embedding happen after the user-facing answer. Failure should
not change the completed answer.

### Preference Promotion

Jarvis may notice that a recall memory or current conversation looks like a
preference. Examples:

- "Always check the portal first for this workflow."
- "Use this model for coding tasks."
- "Do not send emails without showing me the draft."

Those become pending preference proposals. The dashboard should present them in
the same approve/reject style as Action Inbox items, but preferences have their
own storage and dashboard view because they are not MCP tool approvals and do
not resume an interrupted SDK run.

## Prompt Boundary

The prompt assembly must preserve a clear hierarchy:

1. System/developer/runtime safety instructions.
2. Approved preferences.
3. Retrieved prior context.
4. Current trigger context.
5. Current user prompt.

Conflict handling:

- The current user prompt beats retrieved memory.
- Approved preferences beat retrieved memory.
- Retrieved memory never creates permission to perform a destructive action.
- Retrieved memory should not be phrased as something Jarvis must always do.
- If retrieved memory is stale or uncertain, Jarvis should qualify it.

## Dashboard

Add `/memory` with these sections:

1. **Preferences**
   - active preferences;
   - pending preference proposals;
   - edit/archive/approve/reject controls.
2. **Recall Memories**
   - recent memory entries;
   - topics/entities;
   - evidence snippets;
   - archive control for stale or wrong entries.
3. **Recall Debugging**
   - recent recall events;
   - which conversation/run recalled which memories;
   - similarity score and rank.

Conversation detail pages should show memories recalled for that run. This is
important because automatic vector recall can otherwise feel opaque.

## Failure Handling

- If preference loading fails, emit an audit event and continue without
  preferences.
- If prompt embedding fails before recall, emit an audit event and continue
  without recalled memories.
- If vector search fails, emit an audit event and continue without recalled
  memories.
- If `sqlite-vec` is unavailable, Jarvis should start with preferences enabled
  and vector recall disabled, with a visible dashboard warning.
- If summarization or post-run embedding fails, emit an audit event and leave
  the user-facing answer untouched.
- If a memory entry is wrong or stale, archive it from `/memory`; archived
  entries are excluded from recall.

Add audit event types for memory lifecycle and recall failures so failures do
not disappear into logs only.

## Testing Strategy

### Repository And Migration Tests

- Create, list, update, and archive preferences.
- Create, list, update, and archive memory entries.
- Attach and list evidence snippets.
- Record recall events.
- Verify archived memories are excluded from recall.
- Verify the memory migration creates all tables and can downgrade cleanly.
- Verify the vector table setup works in tests, using a fake embedding provider.

### Prompt Assembly Tests

- Active preferences appear in the `Standing preferences` section.
- Retrieved memories appear in the `Relevant prior context` section.
- Evidence snippets are included only with their memory entry.
- Exact-recall prompts can use a retrieved memory entry to inspect the source
  conversation transcript.
- Current prompt remains last.
- Empty preferences/memories do not produce noisy empty sections.
- Conflicting retrieved context is labeled as context, not instruction.

### Agent Runner Tests

- Memory recall runs before `Runner.run`.
- Retrieved memories are present in the prompt passed to the SDK.
- The run continues if recall fails.
- Post-run summarization is invoked after a successful run.
- Post-run summarization failure does not change the user-facing output.

### Web Tests

- `/memory` lists active preferences and pending proposals.
- `/memory` lists recent recall memories with evidence snippets.
- Archiving a memory removes it from future recall.
- Conversation detail shows recalled memories for that conversation.

### Verification

Expected local verification:

- `uv run ruff check jarvis tests`
- focused repository, prompt, runner, web, and migration tests
- `uv run pytest -q`
- browser smoke for `/memory`, conversation detail recall display, and existing
  `/actions`, `/mcp`, and `/settings` pages after implementation

## Non-Goals

- No external vector database in v1.
- No embedding of every raw message in v1; raw messages remain available as
  source transcripts for explicit exact-recall requests.
- No full memory curation workbench in v1.
- No multi-user memory isolation beyond current single-user Jarvis assumptions.
- No automatic activation of behavioral preferences.
- No guarantee that recalled memories are complete or authoritative.

## Implementation Constraints

The implementation plan should use these concrete boundaries:

- Use an injected embedding provider interface so tests can supply a fake
  deterministic embedding provider.
- Use the configured OpenAI-compatible LLM endpoint for production embeddings
  unless configuration explicitly overrides it.
- Load and probe `sqlite-vec` at startup. If the extension cannot load, keep
  preferences enabled and mark automatic vector recall unavailable.
- Run post-run summarization in an in-process async task after the user-facing
  response is persisted. Do not add an external queue in v1.
- Use structured summarizer output with `summary`, `topics`, `entities`, and
  `evidence` fields.
- Add explicit audit event types for preference proposal, preference approval,
  memory summary creation, memory recall, and memory failure.
- Keep dashboard write routes under `/memory`, separate from `/actions`.
