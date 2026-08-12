# MochiBot Architecture

MochiBot is a single-owner companion running as one Python process with SQLite
storage. Its primary design goal is a consistent relationship with minimal
setup, not broad provider or multi-user infrastructure.

## Boundaries

- **Transports** (`mochi/transport/`) adapt Telegram or WeChat messages into
  `IncomingMessage` values and deliver `ChatResult` values. They do not own
  conversation decisions.
- **Main runtime** (`mochi/ai_client.py`) owns user-visible reasoning,
  personality, conversation context, model calls, and tool orchestration.
- **Runtime entries** (`mochi/main_runtime.py`) describe system-owned
  situations, such as bedtime and Weekly curation, that need Main's semantic
  judgment without inventing a user message.
- **Heartbeat** (`mochi/heartbeat.py`) owns sleep gates and independent durable
  clocks for autonomous situations. It creates runtime entries but does not
  interpret observer facts or author companion responses.
- **Skills** (`mochi/skills/`) contain feature behavior and deterministic tool
  operations. Transports and heartbeat call them only through Main or the skill
  registry.
- **Observers** (`mochi/observers/`) are read-only factual producers. Each
  source projects only bounded, provenance-safe unresolved facts; omission on
  a later successful source scan resolves them. Their cached safe views expose
  only allowlisted, bounded fields and can be read without collecting again or
  consuming Attention state.
- **Persistence** (`mochi/db.py`) stores conversation, memory, configuration,
  usage, and tool execution facts.

Dependency direction is transport/heartbeat -> Main -> skills and persistence.
Skills do not import transports or orchestration.

Main 可通过 resident `look_around` 读取 Observer 已有缓存的安全视图。
该工具只读，不触发采集、外部请求或 Attention 状态变化；Free Time 默认
仍不注入生活上下文，只有 Main 主动查看时才获得有界事实。

Main is the semantic judge and author of its actions. The harness gives Main a
lived situation, relevant facts and relationship context, available
capabilities, and the deterministic consequences of using them; it does not
script what Main should say or prescribe a semantic sequence of actions. Hard
commands are reserved for actual safety, protocol, authorization, and data
integrity boundaries. Personality-free Lite work such as classification,
extraction, and validation remains deterministic and may use strict schemas and
output contracts.

## Main conversation flow

1. A transport receives an owner message and creates an `IncomingMessage`.
2. Main loads canonical Core, then the Agent contract, recent conversation, relevant memory, diary,
   and the tools available for that turn.
3. Main calls the configured Main model and executes requested tools through the
   shared policy and execution ledger.
4. The transport delivers the result. The assistant turn is persisted only
   after delivery is confirmed; only then can a complete ordinary conversation
   wake summary and memory extraction.

## Model and provider boundary

The product has exactly two model roles. **Main** owns every personality-bearing
or user-visible situation, including conversation and autonomous runtime
entries. **Lite** is explicitly assigned to personality-free classification,
extraction, and validation. If Lite is not assigned, semantic pre-routing is
skipped rather than borrowing Main and pretending a cheap tier exists.

Official chat brands are OpenAI/GPT, DeepSeek, Anthropic/Claude, and Gemini.
DeepSeek and Gemini use their official OpenAI-compatible endpoints through the
OpenAI adapter; Anthropic uses its own adapter. Embedding is optional and off by
default. OpenAI, Alibaba Cloud Bailian, and Azure AI Foundry embedding use the same
OpenAI-compatible embedding adapter.

## Tool availability boundary

Each provider round receives one immutable tool-availability snapshot. The
provider schema and dispatch allowlist are derived from that same snapshot, so
a tool cannot execute merely because it exists in the global registry.

Tool metadata uses `resident`, `routed`, or `on_demand`. Resident tools enter
the turn directly; the Lite pre-router sees only routed skills; and
`request_tools` may add enabled, configured, transport-compatible routed or
on-demand tools for a later provider round. It never authorizes another call in
the same provider response and never mutates the global registry. `locked`
controls only whether the owner may disable a skill. Concrete deny rules, rate
limits, state-change facts, recoverability, and receipts remain execution
contracts rather than an abstract risk taxonomy.

## Bedtime flow

Explicit goodnight messages and heartbeat-detected silence both create a
`MainRuntimeEntry(kind="bedtime")`. The entry adds a lived sleep-transition
situation to the normal Main context; it does not run a separate bedtime agent
or maintenance prompt.

The heartbeat atomically claims the sleep transition so explicit and silent
triggers cannot produce two goodnights. Main may use the abilities available in
the turn, then leaves a natural bedtime response. The transport confirms
delivery before the assistant turn is stored, and the heartbeat completes the
transition to sleeping even when model or delivery work fails.

## Nightly and Weekly memory flow

Nightly is deterministic housekeeping. After the configured maintenance hour,
heartbeat claims the logical date in `scheduled_runs`; Diary rollover, Core
size audit, trash retention, and log cleanup run without a
model. A failed claim can retry, while a successful date cannot run twice.

After Monday Nightly succeeds, heartbeat claims the ISO week and creates
`MainRuntimeEntry(kind="weekly_maintenance")`. Weekly runs silently through the
same Main prompt and tool loop, but receives an entry-scoped surface rather than
ordinary chat tools:

- an exact receipt-backed patch operation for the free-text Core, with snapshots;
- one atomic curation batch over only the rendered Memory Items and same-user
  evidence messages.

Weekly context contains the previous seven logical days of archived Diary,
recent conversation context, at most 40 new Memory Items, and at most 40
text-related older items. Counts and truncation flags are explicit; unseen rows
are never in scope. Memory edits compare both content and update time, and
Memory/Trash/FTS/vector/KG invalidation commits as one SQLite transaction.
Weekly's final model text is discarded and no synthetic chat history is stored.
Successful Core patches record a content-hash ISO-week receipt in the canonical
Core store, so a later failure can retry projection or curation without
offering the Core mutation again or retaining an extra copy of Core.

Memory Items are authoritative facts with bounded user-message provenance.
Their knowledge-graph relationships are derived by incremental projection after
item writes; Nightly never scans conversations or asks a model to reinterpret
memory. A committed Weekly batch records projection state so projection retries
cannot replay the curation transaction.

Conversation context uses a durable per-user rolling summary. Every configured
batch of complete ordinary user/assistant turns is combined with the previous
summary by personality-free Lite, and SQLite advances the cursor only after a
successful result. Until then Main receives the durable previous summary, every
unsummarized complete turn, and the recent role-true window. Context reset starts
a clean summary epoch and rejects any in-flight result from the old epoch.

Memory extraction is another independent Lite coordinator. It consumes fixed
batches of complete eligible normal-chat turns, requires evidence IDs from
same-user messages in that exact batch, optionally embeds candidates before the
transaction, then commits Memory Items and its cursor atomically. Derived KG
projection has separate retry state and cannot roll back authoritative items.
FTS/LIKE is always the text recall path; vectors only add candidates when an
embedding is available, and recent-only rows are never semantic recall filler.

## Self Reminder flow

`manage_reminder(kind="self")` stores a private future intent, not a prewritten
user notification. At the scheduled time, the reminder scheduler claims the
row and creates `MainRuntimeEntry(kind="self_reminder")`; Main sees current
Core, conversation, Diary, and the capabilities available on the pinned
transport, without a synthetic user message.

Main may act, prepare a user-visible result, or finish with `[SKIP]`. Tool-only
success and skip are terminal outcomes that require no transport delivery. A
deliverable result is serialized before any external send. Text and stickers
are checkpointed independently, so ordinary retry resumes components not yet
checkpointed. A crash after transport success but before its SQLite checkpoint
can duplicate that one component; transport and SQLite provide at-least-once,
not exactly-once, delivery. A stable turn ledger prevents restart from
re-entering Main after any tool attempt, avoiding duplicate side effects at the
cost of conservatively ending an interrupted turn. Assistant
history is written idempotently after delivery and marked processed so an
assistant-only system turn does not enter memory extraction.

Ordinary `notify` reminders remain authorized, deterministic deliveries. Their
voice rendering is prepared once and persisted; transport retry reuses that
outbox. SQLite claims and leases prevent concurrent workers, while the external
send boundary remains at-least-once because transport and SQLite cannot commit
atomically.

Autonomous Free Time/Attention delivery is more conservative: a transport
timeout is terminally audited as `delivery_unknown` and is not automatically
retried, because an accepted-but-unacknowledged proactive message is more likely
to annoy through duplication than to require guaranteed delivery.

## Free Time and Attention flow

Heartbeat keeps two independent clocks. Free Time is randomized, unassigned
companion time; Attention runs periodically and can be advanced by a changed
observer fact without moving the Free Time clock. Sleeping and long-silence
pause gates run before observer or model work.

Both situations enter the standard Main personality and Agent First tool loop.
Free Time deliberately excludes Agenda, Diary, summaries, history-derived
temporal context, recent role messages, auto-recall, and semantic routing.
Attention receives bounded unresolved facts plus Agenda, Diary, conversation
summary, temporal context, and role-true recent history. Both start with
resident tools and may request other tools; neither inherits a sticky routed
skill.

Observers own factual source state, Main owns meaning/action/expression, and
the transport owns delivery. A Main skip does not resolve observer facts.
Heartbeat stores the prepared result before delivery, so model generation and
tool effects do not repeat after a transport failure. History and proactive
delivery logs are written only after successful text delivery. SQLite cannot
commit atomically with an external transport, so a crash at that boundary can
still duplicate one component: autonomous delivery is honestly at-least-once.
Daily limits and cooldowns constrain delivery only; they do not filter topics
or decide what facts mean.
