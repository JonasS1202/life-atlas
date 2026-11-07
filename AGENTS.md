# life-atlas source repository

This repository is the source of Atlas, not an installed Atlas.

- If the user wants Atlas **installed** (or repaired): read `INSTALL.md` and `core/charter.md` completely and follow
  `INSTALL.md`.
- If the user wants to **work on Atlas itself** (Atlas Dev): read `CONTRIBUTING.md` and follow its loop: change here,
  `python -I tests/run.py`, try it in the test Atlas (`python -I dev/sandbox.py`), release (VERSION, CHANGELOG,
  `core/upgrades/`), push, then `/atlas-update` in the real Atlas. Never edit an installed `brain/_atlas/` by hand.
- Layout: `core/` becomes `ROOT/brain/_atlas/`, `skeleton/` the grown part of `ROOT/brain/`, `modules/<name>/core` and
  `modules/<name>/home` are added by `atlas.py init --modules`. Tool and hook use the Python standard library only and
  fail open. Wiring and hook formats change only after checking the official sources in `core/adapters.md`.
