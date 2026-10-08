"""Unit tests for restricted YAML subset frontmatter parser and canonical writer.

Tests acceptance criteria from §3.2 and §10.
"""

from __future__ import annotations

import unittest

from cadabby.frontmatter import (
    FrontmatterParseError,
    FrontmatterSerializeError,
    parse_frontmatter,
    parse_scalar,
    serialize_frontmatter,
    split_comment,
)
from cadabby.okf import canonicalize_tags


class TestFrontmatterParser(unittest.TestCase):
    def test_split_comment(self):
        self.assertEqual(split_comment("title: foo # a comment"), ("title: foo ", "# a comment"))
        self.assertEqual(split_comment('title: "foo # bar" # trailing'), ('title: "foo # bar" ', "# trailing"))
        self.assertEqual(split_comment("title: 'foo # bar'"), ("title: 'foo # bar'", None))
        self.assertEqual(split_comment("# pure comment line"), ("", "# pure comment line"))

    def test_parse_scalar(self):
        self.assertEqual(parse_scalar("true", 1), True)
        self.assertEqual(parse_scalar("false", 1), False)
        self.assertEqual(parse_scalar("null", 1), None)
        self.assertEqual(parse_scalar("active", 1), "active")
        self.assertEqual(parse_scalar('"hello world"', 1), "hello world")
        self.assertEqual(parse_scalar('"hello\\nworld"', 1), "hello\nworld")
        self.assertEqual(parse_scalar('"with \\"quotes\\""', 1), 'with "quotes"')
        self.assertEqual(parse_scalar("'single quoted'", 1), "single quoted")
        self.assertEqual(parse_scalar("'it''s ok'", 1), "it's ok")

    def test_valid_okf_frontmatter(self):
        sample = """---
type: concept
title: "Epistemic Trust Tiers"
description: "Hierarchical classification of knowledge ground truth"
status: active # currently maintained
tags:
  - knowledge-base
  - sqlite
sources:
  - raw/paper.pdf
  - raw/notes.txt
verified:
  - by: human:owner
    at: '2026-10-03T12:00:00Z'
    method: manual-review
    of: 'sha256:9f2b7c1e4a8d05f3b6c29e7d1a4f80b35c6e9d2a7f41b8c035e6d9a2f74b1c80'
generated:
  by: agent:claude-opus-5
  at: '2026-10-03T11:45:00Z'
---
# Epistemic Trust Tiers

Content of the note goes here.
"""
        data, body = parse_frontmatter(sample)
        self.assertEqual(data["type"], "concept")
        self.assertEqual(data["title"], "Epistemic Trust Tiers")
        self.assertEqual(data["description"], "Hierarchical classification of knowledge ground truth")
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["tags"], ["knowledge-base", "sqlite"])
        self.assertEqual(data["sources"], ["raw/paper.pdf", "raw/notes.txt"])
        self.assertEqual(len(data["verified"]), 1)
        self.assertEqual(data["verified"][0]["by"], "human:owner")
        self.assertEqual(data["verified"][0]["at"], "2026-10-03T12:00:00Z")
        self.assertEqual(data["verified"][0]["method"], "manual-review")
        self.assertEqual(data["verified"][0]["of"], "sha256:9f2b7c1e4a8d05f3b6c29e7d1a4f80b35c6e9d2a7f41b8c035e6d9a2f74b1c80")
        self.assertEqual(data["generated"]["by"], "agent:claude-opus-5")
        self.assertEqual(data["generated"]["at"], "2026-10-03T11:45:00Z")
        self.assertTrue(body.startswith("# Epistemic Trust Tiers"))

    def test_rejections(self):
        # Flow style
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\ntags: [a, b]\n---\n")
        self.assertIn("Flow style", ctx.exception.message)

        # Flow style mapping
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\nauthor: {name: Bob}\n---\n")
        self.assertIn("Flow style", ctx.exception.message)

        # Multi-line scalar (|)
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\ndescription: |\n  multi\n  line\n---\n")
        self.assertIn("Multi-line", ctx.exception.message)

        # Tags (!)
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\ntype: !custom entity\n---\n")
        self.assertIn("tags are not permitted", ctx.exception.message)

        # Anchors and aliases (& / *)
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\ntitle: &anchor My Title\n---\n")
        self.assertIn("Anchors and aliases", ctx.exception.message)

        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\ntitle: *anchor\n---\n")
        self.assertIn("Anchors and aliases", ctx.exception.message)

        # Sequences of sequences
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\nmatrix:\n  - - 1\n---\n")
        self.assertIn("Sequences of sequences", ctx.exception.message)

        # Mappings nested > 2 levels
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\nlevel1:\n  level2:\n    level3: bad\n---\n")
        self.assertIn("nested more than two levels", ctx.exception.message)

        # Unterminated frontmatter
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\ntype: concept\ntitle: test\n")
        self.assertIn("Unterminated frontmatter", ctx.exception.message)

        # Unclosed quote
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter('---\ntitle: "Unclosed string\n---\n')
        self.assertIn("Unclosed", ctx.exception.message)

        # Duplicate top-level key
        with self.assertRaises(FrontmatterParseError) as ctx:
            parse_frontmatter("---\ntitle: one\ntitle: two\n---\n")
        self.assertIn("Duplicate", ctx.exception.message)

    def test_canonical_serializer_and_round_trip(self):
        original = {
            "title": "Epistemic Trust Tiers",
            "type": "concept",
            "status": "active",
            "description": "Hierarchical classification of knowledge",
            "tags": ["knowledge-base", "sqlite"],
            "sources": ["raw/paper.pdf"],
            "verified": [
                {
                    "by": "human:owner",
                    "at": "2026-10-03T12:00:00Z",
                    "method": "manual-review",
                    "of": "sha256:9f2b7c1e4a8d05f3b6c29e7d1a4f80b35c6e9d2a7f41b8c035e6d9a2f74b1c80",
                }
            ],
            "generated": {
                "by": "agent:claude-opus-5",
                "at": "2026-10-03T11:45:00Z",
            },
            "custom_field": "extra value",
        }
        body = "# Notes\n\nSome body text."

        serialized = serialize_frontmatter(original, body)

        # Verify key ordering: 'type' must come before 'title', which must come before 'custom_field'
        type_pos = serialized.find("type: concept")
        title_pos = serialized.find("title: \"Epistemic Trust Tiers\"")
        custom_pos = serialized.find("custom_field: \"extra value\"")
        self.assertTrue(0 < type_pos < title_pos < custom_pos)

        # Verify single-quoted timestamp and hash
        self.assertIn("at: '2026-10-03T12:00:00Z'", serialized)
        self.assertIn("of: 'sha256:9f2b7c1e4a8d05f3b6c29e7d1a4f80b35c6e9d2a7f41b8c035e6d9a2f74b1c80'", serialized)

        # Round-trip parse
        reparsed_data, reparsed_body = parse_frontmatter(serialized)
        self.assertEqual(reparsed_data, original)
        self.assertEqual(reparsed_body.strip(), body.strip())


class TestSubsetBoundarySymmetry(unittest.TestCase):
    """The writer must refuse exactly what the parser refuses (§3.2)."""

    IN_SUBSET = {
        "nested mapping": "---\ngenerated:\n  by: agent:x\n  at: '2026-01-01T00:00:00Z'\n---\nbody\n",
        "sequence of scalars": "---\ntags:\n  - a\n  - b\n---\nbody\n",
        "sequence of flat mappings": "---\nverified:\n  - by: human:a\n    of: 'sha256:1'\n---\nbody\n",
    }

    OUT_OF_SUBSET = {
        "three-level mapping": "---\na:\n  b:\n    c: v\n---\nbody\n",
        "sequence under a mapping": "---\na:\n  b:\n    - x\n---\nbody\n",
        "mapping inside a sequence item": "---\nv:\n  - by: a\n    meta:\n      k: z\n---\nbody\n",
        "sequence of sequences": "---\na:\n  - - x\n---\nbody\n",
        "flow style": "---\na: [x, y]\n---\nbody\n",
    }

    def test_parser_accepts_every_documented_shape(self):
        for label, src in self.IN_SUBSET.items():
            with self.subTest(label):
                fm, _ = parse_frontmatter(src)
                self.assertTrue(fm)
                # And the writer can re-emit what the parser accepted.
                self.assertEqual(parse_frontmatter(serialize_frontmatter(fm, "body\n"))[0], fm)

    def test_parser_rejects_out_of_subset_shapes(self):
        for label, src in self.OUT_OF_SUBSET.items():
            with self.subTest(label):
                with self.assertRaises(FrontmatterParseError):
                    parse_frontmatter(src)

    def test_writer_raises_rather_than_emitting_unreadable_frontmatter(self):
        """Coercing an out-of-subset value would destroy it and orphan the note.

        A Python repr round-trips through no parser: the file would be recorded
        with parse_error and vanish from search while the write reported success.
        """
        for label, data in {
            "mapping nested two deep": {"type": "concept", "provenance": {"import": {"tool": "zotero"}}},
            "sequence inside a mapping": {"type": "concept", "provenance": {"authors": ["a", "b"]}},
            "sequence of sequences": {"type": "concept", "matrix": [["x"]]},
            "mapping inside a sequence item": {"type": "concept", "verified": [{"by": "a", "meta": {"k": "v"}}]},
        }.items():
            with self.subTest(label):
                with self.assertRaises(FrontmatterSerializeError):
                    serialize_frontmatter(data, "# Body\n")

    def test_serialize_error_names_the_offending_key_path(self):
        with self.assertRaises(FrontmatterSerializeError) as ctx:
            serialize_frontmatter({"type": "concept", "provenance": {"import": {"tool": "zotero"}}}, "# Body\n")
        self.assertIn("provenance.import", str(ctx.exception))

        with self.assertRaises(FrontmatterSerializeError) as ctx:
            serialize_frontmatter({"verified": [{"by": "a", "meta": {"k": "v"}}]}, "# Body\n")
        self.assertIn("verified[].meta", str(ctx.exception))

    def test_quoted_sequence_scalars_with_colons_round_trip(self):
        original = {
            "type": "concept",
            "sources": ["https://example.com/a:b", "note: with colon"],
        }
        serialized = serialize_frontmatter(original, "# Body\n")
        reparsed, _ = parse_frontmatter(serialized)
        self.assertEqual(reparsed, original)

    def test_mixed_scalar_and_mapping_sequence_rejected_both_ways(self):
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\nitems:\n  - scalar_first\n  - key: val\n---\n")
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\nitems:\n  - key: val\n  - scalar_second\n---\n")
        with self.assertRaises(FrontmatterSerializeError):
            serialize_frontmatter({"items": ["scalar_first", {"key": "val"}]}, "# Body\n")

    def test_writer_rejects_invalid_key_names(self):
        for bad_data in (
            {"bad key": "val"},
            {"generated": {"bad.key": "val"}},
            {"verified": [{"bad:key": "val"}]},
        ):
            with self.assertRaises(FrontmatterSerializeError):
                serialize_frontmatter(bad_data, "# Body\n")



class TestTagGrammar(unittest.TestCase):
    """Enforcement of the §3.1 tag grammar in the writer.

    The grammar itself is covered in test_okf.py; these cover the write path.
    """

    def test_writer_lowercases_and_deduplicates_preserving_order(self):
        serialized = serialize_frontmatter(
            {"type": "concept", "tags": ["Storage", "SQLite", "storage", "c-library"]},
            "# Body\n",
        )
        reparsed, _ = parse_frontmatter(serialized)
        self.assertEqual(reparsed["tags"], ["storage", "sqlite", "c-library"])

    def test_writer_refuses_whitespace_bearing_tag(self):
        """Lowercasing cannot repair a space, and writing it corrupts the --tag facet."""
        with self.assertRaises(ValueError) as ctx:
            serialize_frontmatter({"type": "concept", "tags": ["machine learning"]}, "# Body\n")
        self.assertIn("whitespace", str(ctx.exception))

    def test_writer_does_not_mutate_caller_frontmatter(self):
        original = {"type": "concept", "tags": ["Storage", "storage"]}
        serialize_frontmatter(original, "# Body\n")
        self.assertEqual(original["tags"], ["Storage", "storage"])

    def test_tag_filter_is_exact_under_the_grammar(self):
        """The space-joined `instr` predicate (§4.2) is only exact because of §3.1.

        A conformant tag list cannot produce the false positive that a
        whitespace-bearing tag would.
        """
        stored = " ".join(canonicalize_tags(["machine-learning", "sqlite"]))
        self.assertEqual(stored, "machine-learning sqlite")

        def matches(probe: str) -> bool:
            return f" {probe} " in f" {stored} "

        self.assertTrue(matches("machine-learning"))
        self.assertTrue(matches("sqlite"))
        self.assertFalse(matches("machine"))
        self.assertFalse(matches("learning"))


class TestWriterParserRoundTrip(unittest.TestCase):
    """C30: whatever the canonical writer emits, the parser reads back unchanged."""

    SAMPLES = (
        "plain",
        "Notes on [RFC 9110]",
        "{braces} and [brackets]",
        "key: value inside",
        "trailing colon:",
        "a#b and a # comment-like",
        "it's \"quoted\" text",
        "line\u2028separator",
        "form\ffeed and \vvtab",
        "https://example.com/a?b=c#frag",
        "2026-10-07T12:00:00Z",
        "sha256:abc",
        "true",
        "null",
        "~",
        "- dash lead",
        "&anchor-like",
        "*alias-like",
        "!tag-like",
        "| pipe lead",
        "> angle lead",
        "multi\nline",
        "tab\tseparated",
        "back\\slash",
        "",
        " padded ",
    )

    def test_writer_output_always_reparses(self):
        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                data = {
                    "title": sample,
                    "sources": [sample, "raw/x.md"],
                    "verified": [{"by": "agent:x", "method": sample}],
                }
                parsed, _ = parse_frontmatter(serialize_frontmatter(data))
                self.assertEqual(parsed["title"], sample)
                self.assertEqual(parsed["sources"], [sample, "raw/x.md"])
                self.assertEqual(parsed["verified"], [{"by": "agent:x", "method": sample}])

    def test_colon_in_bare_sequence_item_stays_scalar(self):
        parsed, _ = parse_frontmatter(
            "---\nsources:\n  - https://example.com/a\n  - agent:foo\n"
            "verified:\n  - by: agent:x\n    at: '2026-10-07T12:00:00Z'\n---\n"
        )
        self.assertEqual(parsed["sources"], ["https://example.com/a", "agent:foo"])
        self.assertEqual(parsed["verified"], [{"by": "agent:x", "at": "2026-10-07T12:00:00Z"}])

    def test_top_level_key_without_space_is_rejected(self):
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\ntitle:Foo\n---\n")

    def test_tilde_is_a_string(self):
        parsed, _ = parse_frontmatter("---\ntitle: ~\n---\n")
        self.assertEqual(parsed["title"], "~")

    def test_writer_refuses_non_string_scalars(self):
        from datetime import datetime

        for bad in (3, 1.5, datetime(2026, 1, 1), object()):
            with self.subTest(value=bad):
                with self.assertRaises(FrontmatterSerializeError) as ctx:
                    serialize_frontmatter({"title": "ok", "priority": bad})
                self.assertIn("priority", str(ctx.exception))

    def test_writer_preserves_top_level_null(self):
        text = serialize_frontmatter({"title": "x", "superseded_by": None})
        self.assertIn("superseded_by: null", text)
        parsed, _ = parse_frontmatter(text)
        self.assertIn("superseded_by", parsed)
        self.assertIsNone(parsed["superseded_by"])

    def test_writer_refuses_empty_mapping_item(self):
        with self.assertRaises(FrontmatterSerializeError):
            serialize_frontmatter({"verified": [{}]})


if __name__ == "__main__":
    unittest.main()
