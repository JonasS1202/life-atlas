# Atlas — your whole life, mapped. Your agent keeps the map.

> **AI agent reading this?** The user wants Atlas installed (or repaired). Read [`INSTALL.md`](INSTALL.md) and
> [`core/charter.md`](core/charter.md) completely, then follow `INSTALL.md` step by step. Phase 1 asks the user for
> consent before anything is changed.

Atlas turns your AI coding agent (Claude Code or Google Antigravity) into the conductor of your life's knowledge and
work: conversations, thoughts, finances, insurance, health, home, people, and all your code projects. You open **one
folder**, start a chat, and talk. Atlas routes every request to the right place, works there like a normal coding agent
when it is code, keeps what was new, and looks after itself in the background.

You don't clone it. You tell your agent:

```
Install Atlas from https://github.com/JonasS1202/life-atlas
```

The agent reads this repository, checks the current official documentation of your agent (agents change fast), looks
at your computer with your permission, proposes a structure for *your* life, builds it after your go (existing notes,
agent memories, scattered documents and code repositories included), and ends with a short interview to get to know you.

## What it feels like

- **Open `C:\Atlas`, start a chat, talk.** The first chat of the day starts with a 3–6 line briefing: what is due,
  what waits in the inbox, one question about something Atlas is missing. Later chats stay quiet unless something is
  due today.
- **"Continue in the shop app."** Atlas routes into that repository once and from then on codes like a normal agent.
- **"Remember: the car insurance can be cancelled until 30 November."** Filed on the right page with a deadline; you
  are reminded in time.
- **"No, that's wrong."** A correction becomes a lesson Atlas keeps.
- **On the phone** the Claude or Antigravity app connected to your computer works the same way.
- **Once a week** Atlas tidies itself after your first request and tells you in three lines what it did.

## What you get

| | |
|---|---|
| **One entry point** | Open `C:\Atlas` (or `~/Atlas`) as your project in Claude Code or workspace in Antigravity. No switching folders. |
| **Routing at any scale** | Project names, aliases, folders and paths are matched against a registry; tested with 300 projects and 20,000 pages. The always-loaded part does not grow with them. |
| **Normal coding inside a project** | After routing, the agent reads the project page and the repository's own `CLAUDE.md`/`AGENTS.md`; project rules load only when that project is touched. |
| **Findable by design** | Every page has a one-line `summary:` and `tags:` from a fixed vocabulary with synonyms. Search reads headers first (`atlas.py find`, grep) and opens pages last. |
| **Token budget** | Always loaded: a router of about 1,000 tokens and short skill descriptions. Hooks print nothing when nothing matches. `/atlas-check` measures it. |
| **Learns like you would** | Clear rule for what is worth keeping, learning signals ("remember", "from now on", corrections) that are never ignored, `/atlas-learn` to review a chat. Better than Antigravity's `/learn`: facts too, one brain for both agents, and whatever `/learn` writes elsewhere is adopted. |
| **Daily log** | Each `Atlas:` line lands in `journal/<year>/<date>.md`: "what did we do last Tuesday" has an answer, at no token cost. |
| **Looks after itself** | Weekly maintenance: scripts fix the mechanics (links, tag synonyms, caches), the model only judges what is left. |
| **Personal** | Briefing, questions (per briefing, evenings, weekly, off), reminders, maintenance cadence, tone: defaults you change in plain words. Safety rules stay fixed. |
| **Knows you** | `self/` holds who you are. `/atlas-deep`, only on request, is a reflective conversation about values, motives and goals; kept only with your consent. |
| **Several devices** | The brain is pulled at the start of a chat and pushed at the end; project paths are device-independent; `atlas.py code-sync` brings your repos to a new machine. |
| **Self-healing** | Every chat starts with `Atlas · ok`. If an agent update breaks the wiring, Atlas notices, researches the official docs, prepares and tests the fix, and applies it after one yes. |

## How it works

```
C:\Atlas\                       ← open this folder; start every chat here
   CLAUDE.md                     Claude Code entry: imports brain/ROUTER.md
   GEMINI.md                     Antigravity entry: includes brain/ROUTER.md
   .claude/  .agents/            hooks, skills, subagents, path rules per project (generated)
   code/                         your repositories (optional; projects may also live elsewhere)
   brain/                        the knowledge base (Git)
      ROUTER.md                  always loaded: route first, keep what lasts, guard
      INDEX.md  settings.md  tags.md  glossary.md
      self/  people/  areas/  projects/  howto/  skills/  journal/  inbox/
      _atlas/                    the core: charter, hooks, tool, procedures, templates, memory notepad
      _secrets/                  keys you gave Atlas: never versioned, never searched
```

Session start: heartbeat, pull, briefing (once a day). Each message: route to a project (once per chat), learning
signals, matching pages with their one-line summary, or nothing. End of a turn: daily log, commit, push, and a reminder
to file what was learned (always after a learning signal, otherwise throttled).

## Safety, plainly

- **You decide what is stored.** Atlas keeps what you give it, including API keys (in `brain/_secrets/`, never
  committed or pushed). It forgets what you tell it not to keep. If something looks odd to store, it asks once.
- **Consent first.** The install starts by telling you what will be read and changed, and waits for your yes.
- **No credential harvesting.** Browser passwords, cookies, keychains, private keys and `.env` files are never opened on
  Atlas's own initiative. Other devices on your network are never probed unless you ask.
- **Nothing is deleted.** Moves run from a written plan, per section after your go, with checksums, a journal and an
  undo command. Cloud-sync folders, system and application folders and the inside of Git repositories are off limits.
- **Everything is reversible.** The brain is a Git repository; wiring files are backed up and `atlas.py unwire` removes
  them again.
- **Your data, your provider.** Pages are read by the model of the agent you use. With a Git remote they also live at
  that provider (use a private repository). Deep-mode insights are stored only with your consent.

## Requirements

- Claude Code and/or Google Antigravity
- Git and Python 3.8+ (standard library only); the installer offers to install them. Without Python Atlas runs in
  *rule mode*: no hooks.
- Windows, macOS or Linux

## Commands

| In chat | What it does |
|---|---|
| `/atlas-brief` | what is due, in 3–6 lines |
| `/atlas-sort` | file the inbox (and the cloud inbox), or bring a messy folder into Atlas |
| `/atlas-learn` | review this chat: file facts, propose rules and skills |
| `/atlas-interview` | a short round of questions so Atlas gets to know you |
| `/atlas-deep` | deep mode, only when you ask |
| `/atlas-maintain` | the weekly self-maintenance, on demand |
| `/atlas-check` | health and token budget |
| `/atlas-update` | agent changes, core updates, self-healing |
| `/atlas-adopt-repo` | (developer module) bring a repository under Atlas |

The engine: `python -I brain/_atlas/tools/atlas.py <init|wire|unwire|routes|projects|index|find|probe|check|deadlines|inventory|move|verify|undo|mark|adopt|code-sync|core-diff|core-apply|state>`.

## Updates and development

`/atlas-update` brings a new version into your Atlas without breaking what has grown there: a three-way comparison
keeps your local changes, merges where both sides changed, updates the router's core block but never your personal
part, adds new settings with defaults, re-wires and tests, and rolls back by itself on failure.

Maintainers (push rights to this repository) get **Atlas Dev**: the repository as a project inside their own Atlas, a
test suite (`python -I tests/run.py`) and a separate test Atlas (`python -I dev/sandbox.py`). See
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Repository layout

| Path | |
|---|---|
| `INSTALL.md` | the installation runbook for the agent (new install, second device, repair) |
| `core/` | becomes `brain/_atlas/`: charter, adapters, hooks, tool, procedures, templates, skills, subagents, upgrades |
| `skeleton/` | becomes the grown part of `brain/` (index, settings, tags, glossary, folder guides) |
| `modules/` | optional modules (`developer` included) |
| `tests/`, `dev/` | test suite and test Atlas for developing Atlas |
| `CHANGELOG.md`, `CONTRIBUTING.md` | what changed per version; how to develop and release |

## Status

Version 0.4.0. Built from a setup in daily professional use, generalised for every part of life. The Antigravity wiring
follows the official docs as of 2026-11-07; open checks are listed in [`core/adapters.md`](core/adapters.md).
