"""Unit tests for okf.py: body hashing, actor/timestamp validation, tier derivation.

Covers §3.1 (tag grammar), §3.3 (body hash) and §3.4 (trust tier derivation)
directly, rather than through the cache and lint layers that consume them.
"""

from __future__ import annotations

import unittest

from cadabby.okf import (
    canonicalize_tags,
    compute_body_hash,
    derive_trust_tier,
    is_actor_human,
    is_actor_machine,
    is_canonical_tag,
    is_valid_actor,
    is_valid_timestamp,
    tag_has_whitespace,
)

HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64


class TestBodyHash(unittest.TestCase):
    """§3.3. The hash is the anchor of every verification, so its normalization
    rules decide when a human's review survives an edit."""

    BASE = "# Title\n\nSome prose.\n"

    def test_prefix_and_shape(self):
        digest = compute_body_hash(self.BASE)
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(len(digest), len("sha256:") + 64)
        self.assertEqual(digest, digest.lower())

    def test_normalization_steps_do_not_change_the_hash(self):
        """Each §3.3 step, exercised in isolation against the same content."""
        base = compute_body_hash(self.BASE)
        for label, variant in {
            "CRLF line endings": "# Title\r\n\r\nSome prose.\r\n",
            "CR line endings": "# Title\r\rSome prose.\r",
            "trailing spaces": "# Title   \n\nSome prose.  \n",
            "trailing tabs": "# Title\t\n\nSome prose.\t\n",
            "leading blank lines": "\n\n# Title\n\nSome prose.\n",
            "trailing blank lines": "# Title\n\nSome prose.\n\n\n",
            "no final newline": "# Title\n\nSome prose.",
        }.items():
            with self.subTest(label):
                self.assertEqual(compute_body_hash(variant), base)

    def test_meaningful_edits_change_the_hash(self):
        base = compute_body_hash(self.BASE)
        for label, variant in {
            "one word changed": "# Title\n\nSome prose!\n",
            "interior blank line added": "# Title\n\n\nSome prose.\n",
            "leading indent added": "# Title\n\n  Some prose.\n",
            "case changed": "# title\n\nSome prose.\n",
        }.items():
            with self.subTest(label):
                self.assertNotEqual(compute_body_hash(variant), base)

    def test_empty_and_whitespace_only_bodies_agree(self):
        self.assertEqual(compute_body_hash(""), compute_body_hash("\n  \n\t\n"))

    def test_hash_is_byte_oriented_not_unicode_normalized(self):
        """NFC and NFD 'café' hash differently.

        Deliberate: §3.3 specifies a UTF-8 byte hash with no normalization form,
        so the hash tracks the file as stored. Worth pinning — adding NFC
        normalization later would silently invalidate every stored attestation.
        """
        self.assertNotEqual(compute_body_hash("café"), compute_body_hash("café"))


class TestActorValidation(unittest.TestCase):
    """§3.4. Actor strings gate who may claim what, so the pattern is normative."""

    def test_valid_actors(self):
        for actor in (
            "human:owner",
            "agent:claude-opus-5",
            "process:ci",
            "agent:gemini-flash/1.5",  # version suffix, explicitly permitted
            "human:first.last",
            "agent:a_b-c.d/1",
        ):
            self.assertTrue(is_valid_actor(actor), actor)

    def test_invalid_actors(self):
        for actor in (
            "owner",                 # no scheme
            "robot:x",               # unknown scheme
            "human:",                # empty identifier
            "human: owner",          # whitespace
            "Human:owner",           # scheme is case-sensitive
            "agent:claude opus",
            "agent:emoji-\U0001f600",
            "",
            None,
            42,
            ["human:owner"],
        ):
            self.assertFalse(is_valid_actor(actor), repr(actor))

    def test_human_and_machine_partition(self):
        self.assertTrue(is_actor_human("human:owner"))
        self.assertFalse(is_actor_machine("human:owner"))

        for machine in ("agent:claude", "process:ci"):
            self.assertTrue(is_actor_machine(machine))
            self.assertFalse(is_actor_human(machine))


class TestTimestampValidation(unittest.TestCase):
    def test_valid_timestamps(self):
        for ts in ("2026-10-03T12:00:00Z", "2026-10-03T12:00:00.123Z", "2024-02-29T00:00:00Z"):
            self.assertTrue(is_valid_timestamp(ts), ts)

    def test_rejects_non_utc_and_malformed_shapes(self):
        for ts in (
            "2026-10-03T12:00:00+00:00",  # offset form; the subset requires 'Z'
            "2026-10-03 12:00:00Z",       # space separator
            "2026-10-03T12:00:00",        # no zone
            "2026-10-03",
            "",
            None,
            20261003,
        ):
            self.assertFalse(is_valid_timestamp(ts), repr(ts))

    def test_rejects_well_shaped_but_impossible_dates(self):
        """The regex alone admits these; a claim of RFC 3339 has to mean the calendar."""
        for ts in (
            "2026-13-01T00:00:00Z",  # month 13
            "2026-00-01T00:00:00Z",  # month 0
            "2026-10-45T00:00:00Z",  # day 45
            "2026-02-30T00:00:00Z",  # February 30
            "2025-02-29T00:00:00Z",  # not a leap year
            "2026-10-03T25:00:00Z",  # hour 25
            "2026-10-03T12:60:00Z",  # minute 60
        ):
            self.assertFalse(is_valid_timestamp(ts), ts)


class TestTagGrammar(unittest.TestCase):
    """§3.1. Whitespace is a storage contract, not a style rule (see §4.2)."""

    def test_canonical_tags_accepted(self):
        for tag in (
            "sqlite",
            "knowledge-base",
            "c-library",
            "trust/human-reviewed",
            "gpt4",
            "a",
            "日本語",  # non-ASCII: caseless, so canonical
            "français",
        ):
            self.assertTrue(is_canonical_tag(tag), f"{tag!r} should be canonical")

    def test_non_canonical_tags_rejected(self):
        for tag in (
            "Knowledge-Base",    # uppercase
            "machine learning",  # whitespace
            "snake_case",        # underscore is not a separator here
            "-leading",
            "trailing-",
            "double--hyphen",
            "/leading-slash",
            "trailing/",
            "a//b",
            "tag!",
            "",
            None,
            True,
            42,
        ):
            self.assertFalse(is_canonical_tag(tag), f"{tag!r} should not be canonical")

    def test_tag_has_whitespace(self):
        for tag in ("machine learning", "tab\tsep", "new\nline", " leading", "trailing "):
            self.assertTrue(tag_has_whitespace(tag), repr(tag))
        for tag in ("machine-learning", "trust/human-reviewed", ""):
            self.assertFalse(tag_has_whitespace(tag), repr(tag))

    def test_canonicalize_lowercases_and_deduplicates_in_first_seen_order(self):
        self.assertEqual(
            canonicalize_tags(["Storage", "SQLite", "storage", "STORAGE", "c-library"]),
            ["storage", "sqlite", "c-library"],
        )

    def test_canonicalize_drops_empties_and_tolerates_absent_tags(self):
        self.assertEqual(canonicalize_tags(["", "sqlite", ""]), ["sqlite"])
        self.assertEqual(canonicalize_tags([]), [])
        self.assertEqual(canonicalize_tags(None), [])
        self.assertEqual(canonicalize_tags("not-a-list"), [])

    def test_canonicalize_raises_on_unrepairable_tags(self):
        for bad in (["machine learning"], ["ok", "also bad"], [True], [42]):
            with self.assertRaises(ValueError):
                canonicalize_tags(bad)

    def test_canonicalize_output_is_always_canonical(self):
        """Whatever survives canonicalization must satisfy the grammar."""
        for tag in canonicalize_tags(["Storage", "TRUST/Human-Reviewed", "gpt4"]):
            self.assertTrue(is_canonical_tag(tag), tag)


class TestTrustTierDerivation(unittest.TestCase):
    """§3.4. Trust is bound to content: the tier is a function of `of:` vs. the
    current body hash, never of the file's name or age."""

    def test_unverified_when_there_is_nothing_to_weigh(self):
        for label, verified in {
            "absent": None,
            "empty list": [],
            "not a list": "human:owner",
            "no attestation records": ["just a string", 42],
        }.items():
            with self.subTest(label):
                self.assertEqual(derive_trust_tier(verified, HASH), "unverified")

    def test_human_outranks_machine(self):
        self.assertEqual(
            derive_trust_tier(
                [{"by": "agent:claude", "of": HASH}, {"by": "human:owner", "of": HASH}],
                HASH,
            ),
            "human-reviewed",
        )

    def test_machine_confirmed_when_only_machines_are_bound(self):
        for actor in ("agent:claude", "process:ci"):
            with self.subTest(actor):
                self.assertEqual(derive_trust_tier([{"by": actor, "of": HASH}], HASH), "machine-confirmed")

    def test_drift_demotes_to_stale_regardless_of_who_signed(self):
        """The central invariant: editing prose must not leave a human endorsement standing."""
        self.assertEqual(derive_trust_tier([{"by": "human:owner", "of": OTHER_HASH}], HASH), "stale-verified")

    def test_stale_human_does_not_suppress_a_fresh_machine_attestation(self):
        self.assertEqual(
            derive_trust_tier(
                [{"by": "human:owner", "of": OTHER_HASH}, {"by": "agent:claude", "of": HASH}],
                HASH,
            ),
            "machine-confirmed",
        )

    def test_broken_records_are_debt_not_absence(self):
        """An attestation that never bound is surfaced as stale, not silently dropped.

        Reporting 'unverified' here would hide a malformed record behind a tier
        that looks like an ordinary new note; lint reports it as
        VERIFICATION_UNBOUND / ACTOR_MALFORMED in parallel.
        """
        for label, verified in {
            "empty record": [{}],
            "missing of:": [{"by": "human:owner"}],
            "missing by:": [{"of": HASH}],
            "malformed actor": [{"by": "owner", "of": HASH}],
            "of: is not a hash": [{"by": "human:owner", "of": ""}],
        }.items():
            with self.subTest(label):
                self.assertEqual(derive_trust_tier(verified, HASH), "stale-verified")

    def test_forged_human_cannot_be_smuggled_through_a_malformed_actor(self):
        self.assertEqual(derive_trust_tier([{"by": "human owner", "of": HASH}], HASH), "stale-verified")
        self.assertEqual(derive_trust_tier([{"by": "Human:owner", "of": HASH}], HASH), "stale-verified")

    def test_tier_tracks_the_hash_it_is_given(self):
        """Same records, different current hash: verified here, stale there."""
        records = [{"by": "human:owner", "of": HASH}]
        self.assertEqual(derive_trust_tier(records, HASH), "human-reviewed")
        self.assertEqual(derive_trust_tier(records, OTHER_HASH), "stale-verified")

    def test_end_to_end_binding_against_a_real_body_hash(self):
        body = "# Note\n\nOriginal prose.\n"
        records = [{"by": "human:owner", "of": compute_body_hash(body)}]
        self.assertEqual(derive_trust_tier(records, compute_body_hash(body)), "human-reviewed")

        # Cosmetic edits are normalized away by §3.3, so trust survives them.
        self.assertEqual(
            derive_trust_tier(records, compute_body_hash("# Note\r\n\r\nOriginal prose.   \r\n\n")),
            "human-reviewed",
        )
        # A substantive edit does not.
        self.assertEqual(
            derive_trust_tier(records, compute_body_hash("# Note\n\nRewritten prose.\n")),
            "stale-verified",
        )


if __name__ == "__main__":
    unittest.main()
