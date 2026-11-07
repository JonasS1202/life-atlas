---
summary: Entry point of the brain: how to search, the map, where things go
tags: [atlas]
---
# Atlas · Index

Stand: {{DATE}}. Entry point to everything Atlas knows about {{USER_NAME}}. Atlas keeps this page current and small:
when an area or rule appears or moves, this page changes in the same turn. Write it in {{LANGUAGE}}; keep the structure.

## How to search (cheap first)

1. **Pull the terms** out of the request, including screenshots: names, places, numbers (contract, customer, policy,
   plate, invoice), projects, devices, error texts, dates.
2. **Search the headers first, open pages last.** Every page starts with `summary:` and `tags:`.
   - `python -I _atlas/tools/atlas.py find <words>`: one line per page (path, summary, tags), best first;
   - or grep the headers directly: `^tags:.*versicherung`, `^summary:.*kfz`, `^aliases:.*lena`;
   - tag meanings and synonyms: `tags.md`. Then open only the two or three pages that fit.
3. **Where to look**, if you know the kind of thing:
   - unknown term or nickname → `glossary.md`; a person → `people/`; the user themself → `self/`;
   - a part of life (money, insurance, health, home, car, work …) → `areas/<area>/`;
   - an undertaking with an end, a code repository → `projects/<project>/`;
   - "how do I / how do we …", preferences, rules the user set → `howto/`; how Atlas should behave → `settings.md`;
   - "when did …", "what did we decide …", a conversation → `journal/` (daily logs: `journal/<year>/<date>.md`).
4. **Many terms or unknown ground**: hand the search to a subagent if the agent has one (Claude: `atlas-search`).
5. **Hits are leads, not truth.** Verify against the real source before claiming or changing anything.
6. **Nothing found** means new ground: look at it, then file the result.

## Map

| Folder | Content | Entry |
|---|---|---|
| `self/` | Who the user is: profile, values, goals, patterns | `self/README.md` |
| `settings.md` | How Atlas behaves: briefing, questions, rhythms, style (the user changes it in plain words) | – |
| `tags.md` | Tag vocabulary with synonyms | – |
| `glossary.md` | Terms, abbreviations, nicknames | – |
| `people/` | One page per person | `people/README.md` |
| `areas/` | Areas of life; each has a `README.md` | `areas/README.md` |
| `projects/` | Undertakings with an end, code repositories (routing registry) | `projects/README.md` |
| `howto/` | Routines, tools, preferences, rules the user set; `howto/scripts/` | `howto/README.md` |
| `skills/` | The user's own skills, wired into both agents | `skills/README.md` |
| `journal/<year>/` | Dated: daily logs, conversations, events, decisions, findings | `journal/README.md` |
| `inbox/` | Drop zone, filed by `/atlas-sort` | – |
| `_atlas/` | Core: charter, hooks, tools, procedures, templates, memory notepad | `_atlas/charter.md` |
| `_secrets/` | Credentials the user gave. Not versioned, not searched by hooks | – |
| `_archive/` | Superseded. Not searched, not authoritative | – |

## Where things go

| New knowledge | Goes to |
|---|---|
| About the user themself (values, goals, how they decide, health basics they share) | `self/` |
| How Atlas should behave (rhythm, questions, tone) | `settings.md` |
| A person (family, friend, doctor, advisor, colleague) | `people/<first-last>.md` |
| A contract, account, policy, subscription, device, vehicle, property | its own page in the matching `areas/<area>/` |
| A whole new part of life | new `areas/<area>/README.md`, plus a row in the map above |
| Something with a goal and an end | `projects/<project>/README.md`, linking to the areas it touches |
| A routine, a tool's quirk, a preference, a rule the user set ("never store X"), a lesson from a correction | `howto/` |
| A procedure Atlas should be able to run again | `skills/<name>/SKILL.md` (via `/atlas-learn`) |
| A reusable script | `howto/scripts/` plus a line in its `README.md` |
| A conversation, event, decision, finding with a date | `journal/<year>/<YYYY-MM-DD>_<topic>.md` |
| A term, abbreviation, nickname | `glossary.md` |
| A date that matters (notice period, renewal, appointment) | `due:` in the frontmatter of the page it belongs to |
| A password, PIN, API key the user gave | `_secrets/<topic>.env`; the page notes where |
| A loose thought that fits nowhere yet | the closest area page; if several pile up, a new area |

## Projects

Not listed here (this page must stay small). The project list is `projects/README.md`; the routing registry is the
frontmatter of each project page (`path:`, `aliases:`), which the hook reads. `atlas.py projects` prints it.

## Conventions

- Frontmatter on every page: `summary:` (one line, ≤ 120 characters: what is on the page) and `tags:` (from `tags.md`).
  Optional: `aliases:` (other names; drives routing and hints), `due:` (dated items), `path:`/`remote:` (projects).
- One page per thing; extend an existing page before creating a new one. Link with relative Markdown links.
- At the top: `Stand: YYYY-MM-DD`, or dated sections. Sources with date (conversation, document, the user).
- Correct or delete what is outdated; do not leave it next to the new version. Git keeps history.
