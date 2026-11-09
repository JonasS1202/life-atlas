# Procedure: what Atlas keeps, and `/atlas-learn`

## The test

**Would the user, or a later chat, otherwise have to find this out again or say it again?** Then keep it, now, in its
one place (`INDEX.md` → *Where things go*), with `summary:` and `tags:`. If not, do not.

## Keep

- **Life:** facts about people (who, relation, contact, birthdays), contracts and accounts (provider, numbers, cost,
  term, notice period), devices, places, health basics the user shares; amounts and dates that matter (`due:` on the
  page or in `brain/calendar/README.md` under `## Timeline`); appointments, vacations/blocked periods (`[blocked]`),
  race/macro goals (`[goal]`), planned workouts (`[workout]`), and open tasks (`## Open To-Dos` in
  `brain/calendar/README.md`); decisions and their reason; plans and goals; what the user said about themself (`self/`).
- **The user's ways:** preferences, rules ("from now on…", "never…"), corrections of Atlas (the lesson goes to `howto/`,
  the wrong page gets fixed), how they want answers.
- **Code (project page):** gotchas found while debugging, how to build/test/deploy, decisions and why, what was
  verified live, and equally what was proven *not* to work.
- **Findings** worth having again (research results, a comparison, an explanation the user asked to keep): journal or
  the area page.

## Do not keep

General knowledge, intermediate steps, things already recorded elsewhere (the repository, its README, Git history,
official docs: link instead), speculation (or mark it clearly as unverified), duplicates (update the existing page),
credentials the user did not hand over, anything the user said not to keep.

## Signals

The prompt hook flags explicit signals ("remember", "from now on", "never again") and corrections ("no, that is wrong").
After such a signal, keeping something is not optional; the stop hook insists. Without a signal, use the test.

## `/atlas-learn` (review a whole chat)

1. Go through the chat: what passed the test but was not filed yet?
2. **Facts:** file them directly, with summary and tags.
3. **Behaviour rules** (how to work, what to avoid): propose them; on yes, write them to `howto/<topic>.md`. A rule that
   every chat needs gets one line in the personal part of `ROUTER.md`, the detail stays in `howto/`.
4. **Procedures** worth repeating: propose a skill; on yes, write `brain/skills/<name>/SKILL.md` (`name`, a
   `description` of at most ~200 characters in quotes, then the steps) and run `atlas.py wire --agent <each> --execute`
   so both agents get it.
5. Report what was filed, with the `Atlas:` line.

Antigravity's own `/learn` writes rules and skills into `GEMINI.md`, `.agents/` or `~/.gemini/`. When it runs, steer its
proposal into the brain instead (`howto/`, `brain/skills/`). Whatever lands outside anyway is reported in the briefing
and moved in with `atlas.py adopt`.
