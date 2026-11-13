"""Direct unit tests for `core/tools/atlas_lib/graph_engine.py` (Python 3 stdlib only)."""
import os
import shutil
import stat
import sys
import time
import unittest

sys.dont_write_bytecode = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, "core", "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from atlas_lib import graph_engine  # noqa: E402

UNIT_BASE = os.path.join(os.path.expanduser("~"), ".atlas-tests")


def _rmtree(path):
    def unlock(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if os.path.exists(path):
        shutil.rmtree(path, onerror=unlock)


class GraphEngineUnit(unittest.TestCase):
    def setUp(self):
        os.makedirs(UNIT_BASE, exist_ok=True)
        self.brain = os.path.join(UNIT_BASE, f"unit-{self._testMethodName}-{int(time.time() * 1000)}")
        os.makedirs(self.brain, exist_ok=True)

    def tearDown(self):
        _rmtree(self.brain)

    def write_page(self, rel, content):
        full = os.path.join(self.brain, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        return full

    def test_code_masking_headings_and_wikilink_compilation(self):
        self.write_page(
            "people/erika-beispiel.md",
            "---\nsummary: Steuerberaterin\ntags: [person]\naliases: [Kanzlei Beispiel]\n---\n"
            "# Erika Beispiel\n\n"
            "```python\n# Fake Heading Inside Code\nmatrix = [[1, 2], [3, 4]]\n```\n\n"
            "## Honorar & Fristen\n\nStundensatz vereinbart.\n",
        )
        src_text = (
            "---\nsummary: Einkommensteuer\ntags: [finance]\n---\n"
            "# Steuer 2026\n\n"
            "Rückfragen gehen an [[Erika Beispiel#Honorar & Fristen|Steuerkanzlei]].\n"
            "Inline code `[[Erika Beispiel]]` and `[broken](nope.md)` must stay untouched.\n\n"
            "~~~bash\n# Another fake heading\necho \"[[Erika Beispiel]]\" [bad](missing.md)\n~~~\n"
        )
        self.write_page("areas/steuern/erklaerung.md", src_text)

        idx = graph_engine.sync_index(self.brain)
        conn = graph_engine.open_graph_db(self.brain)
        try:
            anchors = [
                r["anchor"]
                for r in conn.execute("SELECT anchor FROM headings WHERE rel = 'people/erika-beispiel.md'")
            ]
        finally:
            conn.close()

        self.assertIn("erika-beispiel", anchors)
        self.assertIn("honorar-fristen", anchors)
        self.assertNotIn("fake-heading-inside-code", anchors)

        rels = {
            "people/erika-beispiel.md": os.path.join(self.brain, "people/erika-beispiel.md"),
            "areas/steuern/erklaerung.md": os.path.join(self.brain, "areas/steuern/erklaerung.md"),
        }
        res = graph_engine.validate_and_analyze_brain(self.brain, os.path.dirname(self.brain), rels, idx=idx)
        self.assertEqual(res["broken"], [])
        self.assertEqual(res["broken_anchors"], [])
        self.assertEqual(res["wikilink_count"], 1)

        compiled, count = res["wikilink_files"]["areas/steuern/erklaerung.md"]
        self.assertEqual(count, 1)
        self.assertIn("[Steuerkanzlei](../../people/erika-beispiel.md#honorar-fristen)", compiled)
        self.assertIn("`[[Erika Beispiel]]`", compiled)
        self.assertIn('echo "[[Erika Beispiel]]" [bad](missing.md)', compiled)

    def test_dual_tokenizer_compounds_short_tokens_and_corrupt_db_self_heal(self):
        self.write_page(
            "areas/vertraege/mobilfunk.md",
            "---\nsummary: Mobilfunktarif O2\ntags: [contract]\n---\n"
            "# Mobilfunk\n\nNotiz zur Kundenrückgewinnung im Herbst sowie KV-Beitrag.\n",
        )
        graph_engine.sync_index(self.brain)

        # Compound substring via trigram + umlaut folding
        ranked_comp, _ = graph_engine.search_pages(self.brain, ["rückgewinnung"])
        self.assertEqual(ranked_comp, ["areas/vertraege/mobilfunk.md"])
        ranked_ascii, _ = graph_engine.search_pages(self.brain, ["rueckgewinnung"])
        self.assertEqual(ranked_ascii, ["areas/vertraege/mobilfunk.md"])

        # Short 2-char token search ("O2", "KV")
        ranked_o2, _ = graph_engine.search_pages(self.brain, ["O2"])
        self.assertEqual(ranked_o2, ["areas/vertraege/mobilfunk.md"])
        ranked_kv, _ = graph_engine.search_pages(self.brain, ["KV"])
        self.assertEqual(ranked_kv, ["areas/vertraege/mobilfunk.md"])

        # Corrupt SQLite file with garbage bytes -> must self-heal automatically without raising
        db_file = graph_engine.default_db_path(self.brain)
        with open(db_file, "wb") as fh:
            fh.write(b"NOT_A_VALID_SQLITE3_DATABASE_FILE_AT_ALL_0123456789")
        healed_idx = graph_engine.sync_index(self.brain)
        self.assertIn("areas/vertraege/mobilfunk.md", healed_idx)
        ranked_after, _ = graph_engine.search_pages(self.brain, ["rückgewinnung"])
        self.assertEqual(ranked_after, ["areas/vertraege/mobilfunk.md"])

    def test_specificity_1hop_ranking_and_hub_suppression(self):
        self.write_page("INDEX.md", "# Index\n\n- [Finanzen](areas/finanzen/README.md)\n")
        self.write_page(
            "areas/finanzen/README.md",
            "---\nsummary: Finanzen Hub\ntags: [finance]\n---\n# Finanzen\n\n- [Giro](giro.md)\n- [Depot](depot.md)\n",
        )
        self.write_page(
            "areas/finanzen/giro.md",
            "---\nsummary: Hauptkonto\ntags: [finance]\nrelated: [people/max-mustermann.md]\n---\n"
            "# Girokonto\n\nSiehe [Hub](README.md), [Index](../../INDEX.md) und [Depot](depot.md).\n",
        )
        self.write_page(
            "areas/finanzen/depot.md",
            "---\nsummary: Weltportfolio\ntags: [finance]\n---\n# Depot\n\nVerknüpft mit [A](a.md), [B](b.md), [C](c.md).\n",
        )
        for ch in ("a", "b", "c"):
            self.write_page(f"areas/finanzen/{ch}.md", f"---\nsummary: Sub {ch}\ntags: [finance]\n---\n# {ch}\n")
        self.write_page(
            "people/max-mustermann.md",
            "---\nsummary: Bankberater\ntags: [person]\n---\n# Max Mustermann\n",
        )

        graph_engine.sync_index(self.brain)
        neighbors = graph_engine.expand_1hop_neighbors(self.brain, ["areas/finanzen/giro.md"], max_neighbors=5)
        n_rels = [n[0] for n in neighbors]
        # Structural hubs (INDEX.md, areas/finanzen/README.md) must never appear
        self.assertNotIn("INDEX.md", n_rels)
        self.assertNotIn("areas/finanzen/README.md", n_rels)
        # Specific related low-degree leaf (`people/max-mustermann.md`) must outrank higher-degree body neighbor (`depot.md`)
        self.assertEqual(n_rels[0], "people/max-mustermann.md")
        self.assertIn("areas/finanzen/depot.md", n_rels)
        self.assertGreater(neighbors[0][1], neighbors[1][1])


if __name__ == "__main__":
    unittest.main()
