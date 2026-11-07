"""Test Atlas for developing Atlas: create or update a separate Atlas from this working copy.

    python -I dev/sandbox.py [--root PATH] [--fresh]

- First run: `atlas.py init` from this checkout into PATH (default: an `Atlas-dev` folder next to the Atlas that
  contains this checkout, else ~/Atlas-dev), with the developer module and demo content, wired for Claude Code and
  Antigravity in project scope (only inside PATH; your real Atlas is not touched).
- Later runs: update it with `core-apply` from this checkout, exactly the path every user takes with /atlas-update,
  so every change also tests the update.
- `--fresh` deletes nothing: it renames the old test Atlas to `<PATH>.old-<time>` and starts over.

Then open PATH as a project in Claude Code or as a workspace in Antigravity and try the change live.
"""
import argparse
import datetime as dt
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(REPO, "core", "tools", "atlas.py")

DEMO = {
    "areas/versicherungen/README.md": "---\nsummary: Versicherungen\ntags: [contract]\n---\n# Versicherungen\n\n"
                                      "| Page | What |\n|---|---|\n| [Kfz](kfz.md) | Kfz-Versicherung |\n",
    "areas/versicherungen/kfz.md": "---\nsummary: Kfz-Haftpflicht bei der Demo-Versicherung, 400 EUR/Jahr\n"
                                   "tags: [contract, auto]\naliases: [Autoversicherung]\ndue:\n"
                                   "  - {soon} Kfz kündbar (Demo)\n---\n# Kfz-Versicherung\n\nVertrag DEMO-4711.\n",
    "people/erika-muster.md": "---\nsummary: Erika Muster, Schwester (Demo)\ntags: [person]\naliases: [Erika]\n---\n"
                              "# Erika Muster\n\nDemo-Person.\n",
    "projects/demo-shop/README.md": "---\nsummary: Demo-Onlineshop zum Testen des Routings\ntags: [project]\n"
                                    "aliases: [Demoshop]\npath: code/demo-shop\n---\n# Project: Demo-Shop\n\n"
                                    "## Never miss\n\n- Demo only.\n",
}


def run(*args, check=True):
    res = subprocess.run([sys.executable, "-I", *args], capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    out = (res.stdout + res.stderr).strip()
    if out:
        print(out)
    if check and res.returncode:
        sys.exit(res.returncode)
    return res.returncode


def default_root():
    # checkout at <ROOT>/code/life-atlas -> sibling <ROOT>-dev
    parent = os.path.dirname(os.path.dirname(REPO))
    if os.path.basename(os.path.dirname(REPO)) == "code" and os.path.isdir(os.path.join(parent, "brain")):
        return parent + "-dev"
    return os.path.join(os.path.expanduser("~"), "Atlas-dev")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=default_root())
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    brain = os.path.join(root, "brain")
    if args.fresh and os.path.exists(root):
        old = f"{root}.old-{dt.datetime.now():%Y%m%d-%H%M%S}"
        os.rename(root, old)
        print(f"old test Atlas kept as {old}")
    if not os.path.isdir(os.path.join(brain, "_atlas")):
        run(TOOL, "--root", root, "init", "--source", REPO, "--name", "Dev", "--language", "Deutsch",
            "--modules", "developer", "--execute")
        soon = (dt.date.today() + dt.timedelta(days=10)).isoformat()
        for rel, text in DEMO.items():
            p = os.path.join(brain, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text.replace("{soon}", soon))
        with open(os.path.join(brain, "tags.md"), "a", encoding="utf-8", newline="\n") as fh:
            fh.write("| **auto** | Car | kfz, fahrzeug, pkw |\n")
        os.makedirs(os.path.join(root, "code", "demo-shop", ".git"), exist_ok=True)
        for agent in ("claude", "antigravity"):
            run(os.path.join(brain, "_atlas", "tools", "atlas.py"), "--root", root, "wire", "--agent", agent,
                "--execute")
        print(f"test Atlas created: {root}")
    else:
        print("updating the test Atlas from this working copy (the same path users take with /atlas-update):")
        run(os.path.join(brain, "_atlas", "tools", "atlas.py"), "--root", root, "core-apply", "--source", REPO,
            "--execute")
    for agent in ("claude", "antigravity"):
        run(os.path.join(brain, "_atlas", "tools", "atlas.py"), "--root", root, "probe", "--agent", agent, check=False)
    print(f"\nOpen {root} as a project (Claude Code) or workspace (Antigravity) to try it live.")


if __name__ == "__main__":
    main()
