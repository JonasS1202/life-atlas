# Procedure: moving files outside Atlas safely

Used by the first install (concept phase) and by `/atlas-sort`. Charter §5 applies: plan, go, checksums, journal, undo,
never delete.

## The plan file

```json
{
  "title": "Move documents into Atlas areas",
  "sections": [
    {
      "name": "insurance-pdfs",
      "why": "Insurance letters scattered in Downloads and Desktop",
      "operations": [
        {"op": "move", "src": "~/Downloads/Kfz-Police-2025.pdf", "dst": "C:/Atlas/brain/areas/insurance/files/2025-01-10_kfz-police.pdf"},
        {"op": "copy", "src": "~/Documents/Steuer/2024", "dst": "C:/Atlas/brain/areas/finances/files/tax-2024"}
      ]
    }
  ]
}
```

- `op`: `move` (source leaves its place) or `copy` (source stays). A folder moves as a whole.
- One section per thing the user can approve on its own. Say per section why it moves.

## Steps

1. Write the plan to `<ROOT>/brain/_atlas/migrations/<YYYY-MM-DD>_<topic>/plan.json`.
2. Dry run, which also validates (missing sources, existing targets, protected places):
   `python -I <ROOT>/brain/_atlas/tools/atlas.py move --plan <plan.json>`
3. Show the user the sections in plain words (counts, sizes, from → to) and ask for a go **per section**.
4. Execute only the approved sections:
   `python -I <ROOT>/brain/_atlas/tools/atlas.py move --plan <plan.json> --section <name> --execute`
5. Verify: `atlas.py verify --journal <…/journal.jsonl>` must report every target OK.
6. Tell the user what moved, where the journal is, and how to undo
   (`atlas.py undo --journal <…> --execute`). Cross-volume moves leave the source renamed to `*.atlas-recovery-*`;
   only the user deletes those, after checking.
7. Update the pages that refer to the moved files.

## What the tool refuses (and you must not work around)

Sources or targets in cloud-sync folders, system and program folders, application data, the inside of Git repositories
(a repo only as a whole), existing targets (no overwrite), Atlas core. If a refusal seems wrong, explain it to the user
instead of moving by hand.

## Also leave in place

Files other programs point to (photo libraries, tax software data, game saves, mail stores), anything currently open or
in use, and anything whose purpose you cannot tell. When unsure, leave it and note it in the concept.
