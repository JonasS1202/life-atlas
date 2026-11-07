# Projects

Undertakings with a goal and an end: moving house, a renovation, a job change, a trip, a code repository. One folder per
project with a `README.md` (templates: `_atlas/templates/project.md`, `_atlas/templates/repo.md`). A project links to
the areas and people it touches; facts that outlive the project belong in the area. Finished projects stay with
`status: done`.

**Routing.** The frontmatter of each project page is the registry Atlas routes with: the folder name, the title,
`aliases:` (every name the user uses for it) and `path:` (the folder on this computer, for code). When a message names a
project, the hook tells the agent once per chat to read that page; nothing of a project is loaded before that. Keep
names distinct; `atlas.py check` warns about ambiguous ones. Repositories inside `ROOT/code/` also get a path rule
(`atlas.py routes --execute`).

| Project | Status | Page |
|---|---|---|
