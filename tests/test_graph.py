"""Unit tests for graph neighborhood and co-citation analytics (§5.3, §9)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cadabby.cache import VaultCache
from cadabby.graph import get_note_graph, resolve_link_target
from cadabby.vault import Vault


class TestGraphAnalytics(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

        # Build a small test vault with known link structure
        # A links to B, C
        # D links to B, C (co-citation for B and C!)
        # B links to E (2-hop forward from A)
        # F links to A (2-hop backlink from B)
        (self.root / "wiki" / "concepts").mkdir(parents=True, exist_ok=True)
        (self.root / "wiki" / "entities").mkdir(parents=True, exist_ok=True)

        def write_note(rel_path: str, title: str, body: str):
            p = self.root / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                f"---\n"
                f"title: {title}\n"
                f"type: concept\n"
                f"description: Description for {title}\n"
                f"status: active\n"
                f"---\n\n"
                f"# {title}\n\n"
                f"{body}\n",
                "utf-8",
            )

        write_note("wiki/concepts/NoteA.md", "Note A", "Links to [[NoteB]] and [[NoteC]].")
        write_note("wiki/concepts/NoteB.md", "Note B", "Links to [[NoteE]].")
        write_note("wiki/concepts/NoteC.md", "Note C", "Just content here.")
        write_note("wiki/concepts/NoteD.md", "Note D", "Also links to [[NoteB]] and [[NoteC]].")
        write_note("wiki/concepts/NoteE.md", "Note E", "Terminal note.")
        write_note("wiki/concepts/NoteF.md", "Note F", "Links to [[NoteA]].")

        self.vault = Vault(self.root)
        self.cache = VaultCache(self.vault)
        self.cache.scan()

    def tearDown(self):
        self.cache.close()
        self.tmp_dir.cleanup()

    def test_graph_neighborhood_and_co_citations(self):
        conn = self.cache.get_connection()

        # Query NoteB
        graph_b = get_note_graph(conn, "wiki/concepts/NoteB")
        self.assertIsNotNone(graph_b)
        self.assertEqual(graph_b["note"]["cid"], "wiki/concepts/NoteB")

        # 1-hop forward: NoteB -> NoteE
        fwd_targets = [fl["target_cid"] for fl in graph_b["forward_links"]]
        self.assertEqual(fwd_targets, ["wiki/concepts/NoteE"])

        # 1-hop backlinks: NoteA -> NoteB, NoteD -> NoteB
        backlink_sources = sorted([bl["source_cid"] for bl in graph_b["backlinks"]])
        self.assertEqual(backlink_sources, ["wiki/concepts/NoteA", "wiki/concepts/NoteD"])

        # 2-hop backlinks: NoteF -> NoteA -> NoteB
        two_hop_back = [th["source_cid"] for th in graph_b["two_hop_backlinks"]]
        self.assertIn("wiki/concepts/NoteF", two_hop_back)

        # Co-citations: NoteB and NoteC are co-cited by both NoteA and NoteD
        co_cites = {cc["cid"]: cc["count"] for cc in graph_b["co_citations"]}
        self.assertIn("wiki/concepts/NoteC", co_cites)
        self.assertEqual(co_cites["wiki/concepts/NoteC"], 2)

        # Query NoteA
        graph_a = get_note_graph(conn, "wiki/concepts/NoteA")
        self.assertIsNotNone(graph_a)
        # 2-hop forward: NoteA -> NoteB -> NoteE
        two_hop_fwd = [th["target_cid"] for th in graph_a["two_hop_forward"]]
        self.assertIn("wiki/concepts/NoteE", two_hop_fwd)

        # Bibliographic coupling: NoteA and NoteD both cite NoteB and NoteC
        bib = {bc["cid"]: bc["count"] for bc in graph_a["bibliographic_coupling"]}
        self.assertIn("wiki/concepts/NoteD", bib)
        self.assertEqual(bib["wiki/concepts/NoteD"], 2)


if __name__ == "__main__":
    unittest.main()
