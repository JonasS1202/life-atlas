# Procedure: adopt a repository (`/atlas-adopt-repo`, developer module)

Goal: from the Atlas chat, "work in <project>" routes straight into the repository, with its rules loaded only then.
Adopt a repository when the user works in it repeatedly, not in advance.

1. **Check the repository**: exists, is a Git repository, has a remote (`git -C <repo> remote -v`); note the default
   branch on the remote. Without a remote, say so and ask whether to adopt it anyway.
2. **Where it lives**: inside `<ROOT>/code/` is best: the agent may work there without permission prompts, Claude Code
   loads the repo's own `CLAUDE.md` natively when it touches files there, and both agents get a path rule for it. If
   the repository lives elsewhere, offer to move it into `code/` as a whole (plan + go, `_atlas/procedures/move.md`;
   check first that nothing else points to the old path, e.g. IDE workspaces, scripts, other repos' submodules).
   If it stays outside, Claude Code needs `atlas.py wire --agent claude --grant --execute` to work there without
   prompts (a permission change: ask first); Antigravity may not reach folders outside the workspace at all
   (`_atlas/adapters.md` → open checks), which is another reason for `code/`.
3. **Read what is there**: `README`, `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.claude/`, `.agents/`, `.cursor/rules`,
   `.cursorrules`, `CONTRIBUTING`, CI configuration. Do not change these files; they belong to the repository.
4. **Write the project page** `brain/projects/<slug>/README.md` from `_atlas/templates/repo.md`:
   - frontmatter `path:` (`code/<repo>` relative to ROOT for repositories in `code/`, so it is right on every device;
     absolute only outside ROOT), `remote:` (the clone URL, for `atlas.py code-sync`), `aliases:` with every name the
     user uses for it, `summary:` and `tags:`;
   - purpose in two or three sentences, how to build, test and deploy, who owns what;
   - **Never miss**: at most five hard rules whose violation costs real damage (production, data, money);
   - links to areas for shared facts (servers, accounts, services) instead of copying them.
   Long reference material goes to further pages in `brain/projects/<slug>/`, not into the README.
5. **Register it**: a row in `brain/projects/README.md`. Then `python -I <ROOT>/brain/_atlas/tools/atlas.py routes
   --execute` writes the path rules (`.claude/rules/atlas-<slug>.md`, `.agents/rules/atlas-<slug>.md`) for repositories
   inside ROOT. `atlas.py projects` shows the registry the hook routes with.
6. **Test the route**: `echo '{"prompt":"continue in <name>","cwd":"<ROOT>","session_id":"t"}' | <python> -I
   <ROOT>/brain/_atlas/hooks/atlas_hook.py claude prompt` must print `Atlas route -> project '<name>'`. Try the
   aliases too.
7. Commit and tell the user what was created, with the `Atlas:` line.

Writing an agent file *into* the repository (shared with its team) only on the user's explicit request.
