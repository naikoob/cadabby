"""Unit tests for restricted YAML subset frontmatter parser and canonical writer.

Tests acceptance criteria from §3.2 and §10.
"""

from __future__ import annotations

import unittest

from cadabby.frontmatter import (
    FrontmatterParseError,
    format_scalar,
    parse_frontmatter,
    parse_scalar,
    serialize_frontmatter,
    split_comment,
    split_frontmatter,
)


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


if __name__ == "__main__":
    unittest.main()
