"""Unit tests for graph neighborhood and co-citation analytics (§5.3, §9)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cadabby.cache import VaultCache
from cadabby.graph import (
    LinkTargetIndex,
    extract_wikilinks,
    get_note_graph,
    resolve_link_target,
    strip_code_spans,
)
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


class TestExtractWikilinks(unittest.TestCase):
    def test_bare_wikilink(self):
        text = "This note references [[Machine-Learning]] directly."
        links = extract_wikilinks(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_raw, "Machine-Learning")
        self.assertEqual(links[0].target_stem, "Machine-Learning")
        self.assertIsNone(links[0].anchor)
        self.assertIsNone(links[0].alias)
        self.assertEqual(links[0].occurrences, 1)

    def test_wikilink_with_anchor(self):
        text = "Jump to [[Storage-Architecture#B-Tree-Indexes]]."
        links = extract_wikilinks(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_raw, "Storage-Architecture#B-Tree-Indexes")
        self.assertEqual(links[0].target_stem, "Storage-Architecture")
        self.assertEqual(links[0].anchor, "B-Tree-Indexes")
        self.assertIsNone(links[0].alias)

    def test_wikilink_with_alias(self):
        text = "Check out [[Epistemic-Trust-Tiers|Trust Model]]."
        links = extract_wikilinks(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_raw, "Epistemic-Trust-Tiers")
        self.assertEqual(links[0].target_stem, "Epistemic-Trust-Tiers")
        self.assertIsNone(links[0].anchor)
        self.assertEqual(links[0].alias, "Trust Model")

    def test_wikilink_with_anchor_and_alias(self):
        text = "Detailed in [[Storage-Architecture#LSM-Trees|Log-Structured Merge Trees]]."
        links = extract_wikilinks(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_raw, "Storage-Architecture#LSM-Trees")
        self.assertEqual(links[0].target_stem, "Storage-Architecture")
        self.assertEqual(links[0].anchor, "LSM-Trees")
        self.assertEqual(links[0].alias, "Log-Structured Merge Trees")

    def test_multiple_occurrences_increment_count(self):
        text = (
            "First mention of [[SQLite]].\n"
            "Some intermediate discussion.\n"
            "Second mention of [[SQLite]] again.\n"
            "Third mention of [[SQLite]]."
        )
        links = extract_wikilinks(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_raw, "SQLite")
        self.assertEqual(links[0].occurrences, 3)

    def test_code_spans_and_fences_ignored(self):
        text = (
            "Real link to [[Real-Note]].\n\n"
            "Inline code: `[[NotALink]]` should be ignored.\n\n"
            "Fenced code block:\n"
            "```markdown\n"
            "[[FencedNote]] is not a real link.\n"
            "```\n\n"
            "Another real link to [[Another-Note]]."
        )
        links = extract_wikilinks(text)
        stems = [l.target_stem for l in links]
        self.assertEqual(stems, ["Real-Note", "Another-Note"])

    def test_strip_code_spans_utility(self):
        raw = "Text `code block` more text ```fenced``` end."
        clean = strip_code_spans(raw)
        self.assertNotIn("`", clean)
        self.assertIn("Text", clean)
        self.assertIn("more text", clean)
        self.assertIn("end.", clean)

    def test_subpath_wikilinks(self):
        text = "Links to [[customers/acme/README]] and [[projects/apollo/rfc-001#Design]]."
        links = extract_wikilinks(text)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].target_stem, "customers/acme/README")
        self.assertEqual(links[1].target_stem, "projects/apollo/rfc-001")
        self.assertEqual(links[1].anchor, "Design")

    def test_media_embeds_and_multiline_links_ignored(self):
        text = (
            "Embedded diagram ![[architecture.png]] and ![[diagram.svg|300]].\n"
            "Multiline bracket [[\nBroken-Line\n]] is not a link.\n"
            "Real link [[Valid-Target]].\n"
        )
        links = extract_wikilinks(text)
        self.assertEqual([l.target_stem for l in links], ["Valid-Target"])

    def test_tilde_fences_and_multi_backtick_spans_ignored(self):
        text = (
            "Double backtick: ``[[InsideDoubleBacktick]]`` and `` `[[StillCode]]` ``.\n"
            "~~~\n"
            "[[InsideTildeFence]]\n"
            "~~~\n"
            "Outside [[Actual-Link]].\n"
        )
        links = extract_wikilinks(text)
        self.assertEqual([l.target_stem for l in links], ["Actual-Link"])

    def test_table_escaped_pipe_wikilinks(self):
        text = (
            "| Constraint | Source |\n"
            "| :--- | :--- |\n"
            "| Residency | [[customers/northwind/Discovery-Call\\|Discovery Call]] |\n"
            "| Latency | [[Storage-Architecture#LSM-Trees\\|LSM Section]] |\n"
        )
        links = extract_wikilinks(text)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].target_raw, "customers/northwind/Discovery-Call")
        self.assertEqual(links[0].target_stem, "customers/northwind/Discovery-Call")
        self.assertIsNone(links[0].anchor)
        self.assertEqual(links[0].alias, "Discovery Call")
        self.assertEqual(links[1].target_raw, "Storage-Architecture#LSM-Trees")
        self.assertEqual(links[1].target_stem, "Storage-Architecture")
        self.assertEqual(links[1].anchor, "LSM-Trees")
        self.assertEqual(links[1].alias, "LSM Section")


class TestLinkTargetIndex(unittest.TestCase):
    def test_link_target_index(self):
        cids = [
            "wiki/concepts/Epistemic-Trust-Tiers",
            "wiki/entities/SQLite",
            "wiki/guides/Getting-Started",
            "raw/vaswani2017.pdf",
        ]
        resolver = LinkTargetIndex(cids)

        # 1. Exact match
        self.assertEqual(resolver.resolve("wiki/concepts/Epistemic-Trust-Tiers"), "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(resolver.resolve("raw/vaswani2017.pdf"), "raw/vaswani2017.pdf")

        # 2. Match without "wiki/" prefix
        self.assertEqual(resolver.resolve("concepts/Epistemic-Trust-Tiers"), "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(resolver.resolve("entities/SQLite"), "wiki/entities/SQLite")

        # 3. Path suffix match
        self.assertEqual(resolver.resolve("guides/Getting-Started"), "wiki/guides/Getting-Started")

        # 4. Note stem match
        self.assertEqual(resolver.resolve("Epistemic-Trust-Tiers"), "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(resolver.resolve("SQLite"), "wiki/entities/SQLite")

        # 5. Non-existent target returns None
        self.assertIsNone(resolver.resolve("NonExistentNote"))

        # Verify resolve_link_target backward-compatible wrapper
        self.assertEqual(resolve_link_target("SQLite", cids), "wiki/entities/SQLite")


if __name__ == "__main__":
    unittest.main()
