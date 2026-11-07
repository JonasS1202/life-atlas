# Module: developer

For users who write code. Installed with `atlas.py init --modules developer`.

## What it adds

| Part | Where | Purpose |
|---|---|---|
| Procedure `adopt-repo` | `brain/_atlas/procedures/adopt-repo.md` | Bring a repository under Atlas: project page with `path:` and aliases, "Never miss" list, path rules, optional move into `ROOT/code/` |
| Skill `/atlas-adopt-repo` | `brain/_atlas/skills/atlas-adopt-repo/` | trigger for the procedure |
| Template `repo.md` | `brain/_atlas/templates/repo.md` | project page for a repository |
| Script shelf | `brain/howto/scripts/README.md` | reusable scripts with a table: what, how to call, last tested |

## How routing into a repository works (from the one Atlas chat)

1. The user writes "continue in budget-planner" or "program X in the shop app". The prompt hook matches project names,
   aliases, folder names and paths against the registry (the frontmatter of `brain/projects/*`) and tells the agent
   once per chat: `Atlas route -> project '…'`.
2. The agent reads the project page and the repository's own agent files, then works there like a normal coding agent.
3. For repositories inside `ROOT/code/` the agents also load a generated path rule
   (`.claude/rules/atlas-<slug>.md`, `.agents/rules/atlas-<slug>.md`) whenever they touch files there, and Claude
   Code loads the repository's own `CLAUDE.md` natively at that moment. Nothing of a project is loaded before it is
   routed to.

No file is written into the repository itself: its own `CLAUDE.md`, `AGENTS.md`, `.claude/` and `.agents/` belong to
its team (charter §5).

## Rules it adds to the way Atlas works

- Compare a live system with the repository's default branch on the remote, not with a local working copy that may be
  on a feature branch.
- Before writing a script, look in `brain/howto/scripts/`. A script that will be needed again goes there with a line
  in its README; when you fix one while using it, test it on the real case and update *Last tested*.
- Repositories are adopted when the user works in them repeatedly, not in advance.
