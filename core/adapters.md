# Adapters: how Atlas plugs into each agent

Verified: **2026-11-07** against the official docs listed below. `/atlas-update` compares this page with the current
official documentation and proposes changes (charter §7). Anything marked *unconfirmed* must be tested on the real
machine (`atlas.py probe`, a fresh chat) and fixed here when it turns out differently.

## Official sources (the only basis for changes)

| Agent | Sources |
|---|---|
| Claude Code (Anthropic) | `code.claude.com/docs` (memory, skills, hooks, settings, permissions; index: `code.claude.com/docs/llms.txt`), `docs.claude.com`, `docs.anthropic.com` |
| Antigravity (Google) | `antigravity.google/docs` (rules, skills, hooks, mcp, settings, customization-discovery, migration), `ai.google.dev/gemini-api/docs/antigravity-agent` |
| Atlas itself | the `life-atlas` repository (`state.json` → `upstream`) |

Blogs, forums and videos are hints only. On 2026-11-07 third-party pages still showed outdated Antigravity paths
(`.agent/` instead of `.agents/`) and an unofficial removal date for workflows.

## How to tell which agent you are

- **Claude Code**: tools named like `Read`, `Edit`, `Bash`/`PowerShell`, `Skill`; config in `.claude/` and `~/.claude/`;
  project instructions from `CLAUDE.md`.
- **Antigravity**: the Antigravity app, IDE or CLI (Gemini models); config in `.agents/` and `~/.gemini/`; rules from
  `AGENTS.md`/`GEMINI.md`.

## Default: project scope (ROOT is the project / workspace)

`atlas.py wire --agent <claude|antigravity>` (scope `project`) writes into ROOT only:

| Atlas needs | Claude Code | Antigravity |
|---|---|---|
| Router in every chat | `ROOT/CLAUDE.md` with `@brain/ROUTER.md` (relative import; max import depth 4) | `ROOT/GEMINI.md` with `@[Atlas router](brain/ROUTER.md)` (include syntax documented for `AGENTS.md`, `GEMINI.md`, `.agents/rules/*.md`; relative to the rule file). Fallback `--router-mode copy`: router copied between `atlas:router` markers, kept in sync by the session hook. Limit 24,000 bytes per rule file after includes |
| Why these file names | Claude Code reads `CLAUDE.md`, and since v2.1.277 also `AGENTS.md`; using only `CLAUDE.md` keeps the router from loading twice | Antigravity reads `AGENTS.md` and `GEMINI.md`; using `GEMINI.md` keeps Claude Code from loading it |
| Skills | `ROOT/.claude/skills/<name>/SKILL.md` | `ROOT/.agents/skills/<name>/SKILL.md` (`description` required) |
| Hooks | `ROOT/.claude/settings.json` → `hooks.SessionStart`, `UserPromptSubmit`, `Stop` | `ROOT/.agents/hooks.json` → hook `"atlas"`: `SessionStart`, `PreInvocation`, `Stop` |
| Project rules, loaded only when the repo is touched | `ROOT/.claude/rules/atlas-<slug>.md` with `paths: - "code/<repo>/**"` (patterns relative to the working folder). Claude Code also loads the repo's own `CLAUDE.md` natively when it reads files in `code/<repo>/` | `ROOT/.agents/rules/atlas-<slug>.md` with `trigger: glob`, `globs: code/<repo>/**`. Nested `AGENTS.md` are **not** loaded natively (only workspace folder and parents): the router tells the agent to read them |
| Projects outside ROOT | no path rules (patterns are relative); routing by the hook; `wire --grant` adds the folder to `permissions.additionalDirectories` plus `Edit`/`Read` allow rules (`//abs` form, **unconfirmed** for Windows drive letters) | routing by the hook; whether the agent may read and edit folders outside the workspace is **unconfirmed**; prefer `ROOT/code/` |
| Memory | `autoMemoryDirectory` → `ROOT/brain/_atlas/memory` (absolute path; confirmed in project settings 2026-11-07); first 200 lines / 25 KB of `MEMORY.md` load at start | no documented memory redirect; the router names `brain/_atlas/memory/MEMORY.md`; Antigravity's `/learn` (manual, writes rules/skills, not facts) is steered into the brain by the router and adopted by `atlas.py adopt` |
| Subagents | `ROOT/.claude/agents/atlas-search.md`, `atlas-maintainer.md` (big searches and weekly maintenance outside the main chat) | none documented; done in the main chat |
| On the phone | Claude app with Remote Control of the session on the computer: the session runs in ROOT, hooks and brain work as usual | Antigravity app connected to the instance on the computer: same |
| Workflows | – | deprecated in favour of skills |

Optional **user scope** (`--scope user`): the same router, hooks and skills in `~/.claude/` and `~/.gemini/`
(`~/.gemini/AGENTS.md`, `~/.gemini/config/hooks.json`, `~/.gemini/config/skills/`), so Atlas is also present in chats
opened elsewhere. No path rules (they are relative to each chat's folder). Not needed when every chat starts in ROOT.

## Hook protocol

| | Claude Code | Antigravity |
|---|---|---|
| Session start | stdin `session_id`, `cwd`, `source`; output `hookSpecificOutput.additionalContext` | stdin `conversationId`, `workspacePaths`; output `injectSteps: [{systemMessage: {systemMessage: …}}]` |
| Each user message | `UserPromptSubmit`: stdin `prompt`, `cwd`, `session_id`; output `hookSpecificOutput.additionalContext` | `PreInvocation` runs before **every** model call; Atlas acts only on `invocationNum == 0`; stdin `lastUserInput`; output `injectSteps: [{ephemeralMessage: …}]` |
| End of turn | `Stop`: stdin `stop_hook_active`, `last_assistant_message`, `transcript_path`, `session_id`; continue with `{"decision": "block", "reason": …}`; built-in loop guard, cap 8 | `Stop`: stdin `terminationReason`, `fullyIdle`, `lastUserInput`, `transcriptPath`; continue with `{"decision": "continue", "reason": …}`; no loop guard: Atlas keeps one in `state.json` |
| Command shell | Git Bash on Windows by default (PowerShell without Git Bash) | **unconfirmed**; `wire` writes absolute paths without spaces (8.3 short paths on Windows) so any shell works |

What the session hook always does, in both agents: print `Atlas · ok` (the heartbeat the router checks for), pull the
brain from its remote (≤ 8 s, aborts cleanly on conflict), then add the daily briefing or only what is due today.
If a future agent version drops or renames these events, the heartbeat goes missing and the router starts heal mode
(`procedures/update.md` §0).

## What `wire` writes, and how it is undone

Every changed file is backed up to `brain/_atlas/migrations/<run>/wiring-backup/` first. Blocks are marked
`<!-- atlas:start -->` … `<!-- atlas:end -->`; hook entries are identified by `atlas_hook.py` (Claude) or the key
`atlas` (Antigravity); skills and rules by their folder or `atlas-` prefix. `atlas.py unwire --agent …` removes exactly
these parts. Content outside them is never touched; an unreadable JSON file stops the wiring.

## Open checks (fill in after the first real install)

- [ ] Antigravity: does `@[Atlas router](brain/ROUTER.md)` in `ROOT/GEMINI.md` load the router? If not: `wire --router-mode copy`.
- [ ] Antigravity: do `trigger: glob` rules fire for `code/<repo>/**` when the agent edits files there?
- [ ] Antigravity: can the agent read and edit folders outside the workspace (projects not in `code/`)?
- [ ] Antigravity: which shell runs hook commands on Windows; is `ephemeralMessage` visible to the model in that turn?
- [ ] Antigravity: transcript format for the stop hook's work detection (currently a text heuristic).
- [ ] Claude Code: `paths:` rules and nested `CLAUDE.md` load when files in `code/<repo>/` are read (`/context`).
