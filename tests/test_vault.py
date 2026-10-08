"""Unit tests for vault discovery, configuration, and CID/path mapping."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cadabby.constants import FILE_CONFIG
from cadabby.vault import (
    Vault,
    VaultConfigError,
    cid_to_path,
    default_vault_config,
    find_vault_root,
    load_vault_config,
    path_to_cid,
    path_to_layer,
)


class TestVault(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_find_vault_root_walkup(self):
        # Create vault structure
        (self.dir / FILE_CONFIG).write_text('{"vault_name": "test-vault"}', "utf-8")
        nested_dir = self.dir / "wiki" / "concepts" / "deep"
        nested_dir.mkdir(parents=True, exist_ok=True)

        found = find_vault_root(nested_dir)
        self.assertEqual(found, self.dir.resolve())

    def test_load_vault_config_deep_merge(self):
        cfg_file = self.dir / FILE_CONFIG
        cfg_file.write_text(
            '{"vault_name": "custom", "ranking": {"trust": {"human-reviewed": 3.0}}}',
            "utf-8",
        )
        cfg = load_vault_config(self.dir)
        self.assertEqual(cfg["vault_name"], "custom")
        # Custom value applied
        self.assertEqual(cfg["ranking"]["trust"]["human-reviewed"], 3.0)
        # Default value preserved
        self.assertEqual(cfg["ranking"]["trust"]["machine-confirmed"], 1.2)

    def test_load_vault_config_malformed_json_raises(self):
        cfg_file = self.dir / FILE_CONFIG
        cfg_file.write_text('{"vault_name": "broken", broken json...}', "utf-8")
        with self.assertRaises(VaultConfigError):
            load_vault_config(self.dir)

    def test_load_vault_config_nondict_raises(self):
        cfg_file = self.dir / FILE_CONFIG
        cfg_file.write_text('["not", "a", "dict"]', "utf-8")
        with self.assertRaises(VaultConfigError):
            load_vault_config(self.dir)

    def test_load_vault_config_mistyped_value_raises(self):
        # A null or mistyped value must fail at the config file rather than as
        # an opaque TypeError inside whichever consumer reads the key.
        cfg_file = self.dir / FILE_CONFIG
        cases = [
            ('{"raw_text_extensions": null}', "raw_text_extensions"),
            ('{"identities": null}', "identities"),
            ('{"log_rotate_bytes": "262144"}', "log_rotate_bytes"),
            ('{"log_rotate_bytes": true}', "log_rotate_bytes"),
            ('{"ranking": null}', "ranking"),
            ('{"ranking": {"trust": null}}', "ranking.trust"),
        ]
        for content, key in cases:
            with self.subTest(key=key):
                cfg_file.write_text(content, "utf-8")
                with self.assertRaises(VaultConfigError) as ctx:
                    load_vault_config(self.dir)
                self.assertIn(f"'{key}'", str(ctx.exception))

    def test_config_values_are_validated(self):
        """C36: right JSON type, wrong value, still fails at load and names the key (§2.6)."""
        cfg_file = self.dir / FILE_CONFIG
        cases = [
            ('{"integrity": "sha1"}', "integrity"),
            ('{"ranking": {"trust": {"human-reviewed": NaN}}}', "ranking.trust.human-reviewed"),
            ('{"ranking": {"status": {"active": Infinity}}}', "ranking.status.active"),
            ('{"raw_text_extensions": [".md", 3]}', "raw_text_extensions"),
            ('{"identities": {"human:alice": "alice@example.com"}}', "identities.human:alice"),
            ('{"identities": {"human:alice": [null]}}', "identities.human:alice"),
        ]
        for content, key in cases:
            with self.subTest(key=key, content=content):
                cfg_file.write_text(content, "utf-8")
                with self.assertRaises(VaultConfigError) as ctx:
                    load_vault_config(self.dir)
                self.assertIn(f"'{key}'", str(ctx.exception))
        cfg_file.write_text('{"integrity": "hash", "identities": {"human:alice": ["a@x.com"]}}', "utf-8")
        self.assertEqual(load_vault_config(self.dir)["integrity"], "hash")

    def test_load_vault_config_passes_through_unknown_keys(self):
        # Type checking is scoped to keys with defaults, so forward-compatible
        # additions are not rejected by an older build.
        cfg_file = self.dir / FILE_CONFIG
        cfg_file.write_text('{"future_key": null, "log_rotate_bytes": 999}', "utf-8")
        cfg = load_vault_config(self.dir)
        self.assertIsNone(cfg["future_key"])
        self.assertEqual(cfg["log_rotate_bytes"], 999)

    def test_every_default_config_key_is_read_by_something(self):
        """A shipped key that no code reads is a promise the engine breaks.

        The pass-through above is what makes this necessary: unknown keys do
        not raise, so a key the engine ships but never consults behaves
        exactly like one it does, and nothing tells the user their setting is
        inert. `obsidian.materialize_trust_tags` lived that way through
        0.3.1 -- defaulted, type-checked, documented in two spec sections,
        and read by no code path -- and was removed rather than implemented
        (§7.5).
        """
        src = Path(__file__).resolve().parent.parent / "src" / "cadabby"
        sources = "\n".join(p.read_text("utf-8") for p in src.rglob("*.py"))

        # `schema` is a format marker written for a future migration to read,
        # not a knob, so it has no consumer by design. It is listed here
        # rather than skipped silently because the migration story it implies
        # does not exist yet either.
        exempt = {"schema"}

        for key in default_vault_config("v"):
            if key in exempt:
                continue
            with self.subTest(key=key):
                self.assertTrue(
                    f'config["{key}"]' in sources or f"config['{key}']" in sources,
                    f"'{key}' is shipped in .cadabby.json defaults but nothing reads it",
                )

    def test_path_and_cid_mappings(self):
        # Wiki note
        wiki_rel = "wiki/concepts/Epistemic-Trust-Tiers.md"
        cid = path_to_cid(wiki_rel)
        self.assertEqual(cid, "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(cid_to_path(cid), wiki_rel)
        self.assertEqual(path_to_layer(wiki_rel), "wiki")

        # Raw file
        raw_rel = "raw/papers/attention.pdf"
        self.assertEqual(path_to_cid(raw_rel), raw_rel)
        self.assertEqual(cid_to_path(raw_rel), raw_rel)
        self.assertEqual(path_to_layer(raw_rel), "raw")

        # Vault.rel_path resolution regardless of cwd
        vault = Vault(self.dir)
        old_cwd = os.getcwd()
        try:
            # Change cwd to /tmp to ensure cwd != vault.root
            os.chdir(tempfile.gettempdir())
            # Relative path within vault
            self.assertEqual(vault.rel_path("wiki/concepts/Foo.md"), "wiki/concepts/Foo.md")
            # Absolute path within vault
            abs_note = self.dir / "wiki" / "concepts" / "Foo.md"
            self.assertEqual(vault.rel_path(abs_note), "wiki/concepts/Foo.md")
        finally:
            os.chdir(old_cwd)

    def test_find_vault_root_rejects_invalid_env_vault(self):
        # A set-but-invalid CADABBY_VAULT fails loudly rather than silently
        # falling back to the cwd or resolving to an enclosing ancestor vault.
        marked_ancestor = self.dir / "marked_ancestor"
        unmarked_child = marked_ancestor / "child"
        unmarked_child.mkdir(parents=True, exist_ok=True)
        (marked_ancestor / FILE_CONFIG).write_text('{"vault_name": "ancestor"}', "utf-8")

        for label, env_value in (
            ("nonexistent", str(self.dir / "gone")),
            ("unmarked child of a vault", str(unmarked_child)),
        ):
            with self.subTest(case=label), mock.patch.dict(os.environ, {"CADABBY_VAULT": env_value}):
                with self.assertRaises(FileNotFoundError) as ctx:
                    find_vault_root(None)
                self.assertIn("CADABBY_VAULT", str(ctx.exception))

    def test_find_vault_root_precedence(self):
        env_vault = self.dir / "env_vault"
        env_vault.mkdir(parents=True, exist_ok=True)
        (env_vault / FILE_CONFIG).write_text('{"vault_name": "env"}', "utf-8")

        explicit_vault = self.dir / "explicit_vault"
        explicit_vault.mkdir(parents=True, exist_ok=True)
        (explicit_vault / FILE_CONFIG).write_text('{"vault_name": "explicit"}', "utf-8")

        with mock.patch.dict(os.environ, {"CADABBY_VAULT": str(env_vault)}):
            # When explicit path is passed, it must take precedence over CADABBY_VAULT
            found_explicit = find_vault_root(explicit_vault)
            self.assertEqual(found_explicit, explicit_vault.resolve())

            # When explicit path is None, CADABBY_VAULT is used
            found_env = find_vault_root(None)
            self.assertEqual(found_env, env_vault.resolve())

    def test_note_path_violation_normalizes_slashes_and_rejects_leading_slash(self):
        from cadabby.vault import note_path_violation

        self.assertIsNotNone(note_path_violation("/wiki/Note.md"))
        self.assertIsNotNone(note_path_violation("\\wiki\\Note.md"))
        self.assertIn("template", note_path_violation("templates\\Concept.md", "templates") or "")
        self.assertIn("relative", note_path_violation("/templates/Concept.md", "templates") or "")

    def test_log_rotate_bytes_rejects_float_nan_and_non_positive(self):
        from cadabby.errors import VaultConfigError

        vdir = self.dir / "bad_rotate"
        vdir.mkdir(parents=True, exist_ok=True)
        for bad_raw in ('{"log_rotate_bytes": 1.5}', '{"log_rotate_bytes": NaN}', '{"log_rotate_bytes": 0}', '{"log_rotate_bytes": -10}'):
            (vdir / FILE_CONFIG).write_text(bad_raw, "utf-8")
            with self.subTest(bad_raw=bad_raw), self.assertRaises(VaultConfigError):
                load_vault_config(vdir)


if __name__ == "__main__":
    unittest.main()

