"""Obsidian core-Templates interop (§7.5).

A template is a pre-note: valid only once its placeholders are substituted.
`title: {{title}}` is a flow mapping and `tags: []` a flow sequence, both
outside the restricted subset (§3.2), so a template folder walked as a
cognitive domain turns `lint` permanently red. These tests hold the two halves
of the contract -- the engine skips the folder, and `scaffold_note` can read a
body out of it -- plus the line between them: frontmatter is never templated.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from cadabby.adapters.memory_storage import InMemoryLedger, InMemoryNoteStorage
from cadabby.cli import cmd_init
from cadabby.errors import EXIT_ENVIRONMENT, EXIT_OK, INVALID_ARGUMENT, NOT_FOUND, classify
from cadabby.frontmatter import parse_frontmatter
from cadabby.lint import run_vault_lint
from cadabby.ops import ScaffoldNoteUseCase, scaffold_note
from cadabby.vault import Vault, obsidian_template_dir


from tests.helpers import DummyArgs

# Exactly what Obsidian's core Templates plugin produces: the title placeholder
# parses as a flow mapping, the empty tag list as a flow sequence.
OBSIDIAN_TEMPLATE = """---
type: concept
title: {{title}}
tags: []
created: {{date:YYYY-MM-DD}}
---

# {{title}}

## Summary

## Open questions
"""


class TemplateVaultCase(unittest.TestCase):
    """A vault with wiki/, raw/ and log/, and helpers to configure Obsidian."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        for sub in ("wiki", "raw", "log"):
            (self.vault_dir / sub).mkdir(parents=True)
        self.vault = Vault(self.vault_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def configure_templates(self, folder):
        """Write the core plugin's settings file naming `folder`."""
        obs = self.vault_dir / ".obsidian"
        obs.mkdir(exist_ok=True)
        (obs / "templates.json").write_text(
            json.dumps({"folder": folder, "dateFormat": "YYYY-MM-DD"}), "utf-8"
        )

    def write_template(self, rel, content=OBSIDIAN_TEMPLATE):
        path = self.vault_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
        return path


class TestTemplateFolderIsNotADomain(TemplateVaultCase):
    """§2.2, §7.5. The declared template folder is excluded from note discovery."""

    def test_a_declared_template_folder_is_not_a_cognitive_domain(self):
        self.configure_templates("templates")
        self.write_template("templates/concept.md")
        self.assertNotIn("templates", self.vault.discover_domains())

    def test_templates_do_not_reach_the_note_walk(self):
        self.configure_templates("templates")
        self.write_template("templates/concept.md")
        walked = [p.name for p, _, _ in self.vault.iter_domain_notes()]
        self.assertEqual(walked, [])

    def test_a_template_folder_does_not_turn_lint_red(self):
        # The reported symptom: FRONTMATTER_UNPARSEABLE on templates/concept.md
        # at the `title: {{title}}` line, making `cadabby lint` exit 1 forever.
        self.configure_templates("templates")
        self.write_template("templates/concept.md")
        self.assertEqual(run_vault_lint(self.vault), [])

    def test_without_the_setting_the_folder_is_an_ordinary_domain(self):
        # No .obsidian/templates.json means nothing was declared, and §2.2's
        # rule stands: a non-reserved top-level directory is a domain. Users
        # with a real `templates/` domain must not silently lose it.
        self.write_template("templates/concept.md")
        self.assertIn("templates", self.vault.discover_domains())

    def test_a_nested_template_folder_is_pruned_but_its_parent_survives(self):
        self.configure_templates("meta/templates")
        self.write_template("meta/templates/concept.md")
        self.write_template(
            "meta/Charter.md",
            "---\ntype: concept\ntitle: Charter\n---\n\n# Charter\n",
        )
        self.assertIn("meta", self.vault.discover_domains())
        walked = [p.name for p, _, _ in self.vault.iter_domain_notes()]
        self.assertEqual(walked, ["Charter.md"])

    def test_foam_templates_are_invisible_without_configuration(self):
        # Foam stores templates in `.foam/templates/`, and the dot prefix
        # already keeps them out of domain discovery. Asserted so the
        # behavior is deliberate rather than a lucky side effect.
        self.write_template(".foam/templates/new-note.md")
        self.assertNotIn(".foam", self.vault.discover_domains())
        self.assertEqual(run_vault_lint(self.vault), [])


class TestTemplateSettingIsReadDefensively(TemplateVaultCase):
    """§7.5. A third-party config must not be able to break the vault."""

    def test_absent_settings_file_yields_no_template_dir(self):
        self.assertIsNone(self.vault.template_dir())

    def test_malformed_json_fails_safe(self):
        obs = self.vault_dir / ".obsidian"
        obs.mkdir(exist_ok=True)
        (obs / "templates.json").write_text("{not json", "utf-8")
        self.assertIsNone(self.vault.template_dir())

    def test_settings_that_are_not_an_object_fail_safe(self):
        # A bare array or string would make the `folder` lookup raise
        # AttributeError and surface as INTERNAL -- blaming the engine for
        # someone else's config.
        obs = self.vault_dir / ".obsidian"
        obs.mkdir(exist_ok=True)
        for payload in ("[1, 2, 3]", '"templates"', "null", "7"):
            with self.subTest(payload=payload):
                (obs / "templates.json").write_text(payload, "utf-8")
                self.assertIsNone(self.vault.template_dir())

    def test_a_missing_or_empty_folder_key_yields_nothing(self):
        for payload in ({}, {"folder": ""}, {"folder": "   "}, {"folder": None}, {"folder": 7}):
            with self.subTest(payload=payload):
                obs = self.vault_dir / ".obsidian"
                obs.mkdir(exist_ok=True)
                (obs / "templates.json").write_text(json.dumps(payload), "utf-8")
                self.assertIsNone(self.vault.template_dir())

    def test_an_escaping_or_absolute_folder_is_refused(self):
        # Honoring these would exclude a directory outside the vault, or
        # silently exclude nothing while looking like it worked.
        for folder in ("../outside", "/etc", "a/../../outside", ".."):
            with self.subTest(folder=folder):
                self.configure_templates(folder)
                self.assertIsNone(self.vault.template_dir())

    def test_a_resolvable_folder_is_returned_absolute(self):
        self.configure_templates("templates")
        self.assertEqual(self.vault.template_dir(), (self.vault_dir / "templates").resolve())

    def test_a_folder_symlinked_out_of_the_vault_is_refused(self):
        # The case the string checks cannot see: the setting names a plain
        # relative folder, and only resolve() reveals it leaves the vault.
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.vault_dir / "templates").symlink_to(outside, target_is_directory=True)
        self.configure_templates("templates")
        self.assertIsNone(self.vault.template_dir())

    def test_a_symlinked_vault_root_still_resolves_its_template_folder(self):
        # Vault resolves its root in __post_init__, but `init` reaches this
        # function by another route, so the containment check has to normalize
        # the root itself. Unresolved, every parent comparison below misses and
        # a symlinked vault silently loses its exclusion.
        self.configure_templates("templates")
        (self.vault_dir / "templates").mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.vault_dir, target_is_directory=True)
        self.assertEqual(
            obsidian_template_dir(link), (self.vault_dir / "templates").resolve()
        )

    def test_a_folder_named_like_a_root_path_is_not_taken_as_one(self):
        # '/etc' survives strip('/') as 'etc'; honoring it would exclude
        # <vault>/etc rather than refusing the setting.
        (self.vault_dir / "etc").mkdir()
        self.configure_templates("/etc")
        self.assertIsNone(self.vault.template_dir())
        self.assertIn("etc", self.vault.discover_domains())


class TestScaffoldFromTemplate(TemplateVaultCase):
    """§5.1, §7.5. Templates seed the body; frontmatter stays engine-owned."""

    def setUp(self):
        super().setUp()
        self.configure_templates("templates")
        self.write_template("templates/concept.md")

    def test_the_template_body_seeds_the_note_with_the_title_substituted(self):
        path = scaffold_note(
            vault=self.vault,
            title="Flash Attention",
            type_="concept",
            description="d",
            template="concept",
        )
        _, body = parse_frontmatter(path.read_text("utf-8"))
        self.assertIn("# Flash Attention", body)
        self.assertIn("## Open questions", body)
        self.assertNotIn("{{title}}", body)

    def test_the_templates_own_frontmatter_is_discarded(self):
        # The engine's OKF block is authoritative. A template that could set
        # `verified` or `generated` would let a typo mint notes whose trust
        # tier asserts more than the vault can back (§3.4).
        path = scaffold_note(
            vault=self.vault,
            title="Flash Attention",
            type_="concept",
            description="d",
            template="concept",
        )
        fm, body = parse_frontmatter(path.read_text("utf-8"))
        self.assertEqual(fm["title"], "Flash Attention")
        self.assertEqual(fm["generated"]["by"], "agent:unknown")
        self.assertNotIn("created", fm)
        self.assertNotIn("{{date:YYYY-MM-DD}}", body)

    def test_a_note_scaffolded_from_a_template_lints_clean(self):
        scaffold_note(
            vault=self.vault,
            title="Flash Attention",
            type_="concept",
            description="d",
            template="concept",
        )
        codes = [f.code for f in run_vault_lint(self.vault) if f.severity == "error"]
        self.assertEqual(codes, [])

    def test_the_name_may_carry_the_extension(self):
        path = scaffold_note(
            vault=self.vault, title="A", type_="concept", description="d", template="concept.md"
        )
        self.assertIn("## Summary", path.read_text("utf-8"))

    def test_body_and_template_together_are_refused(self):
        with self.assertRaises(ValueError) as ctx:
            scaffold_note(
                vault=self.vault,
                title="A",
                type_="concept",
                description="d",
                body="# Mine",
                template="concept",
            )
        self.assertEqual(classify(ctx.exception).code, INVALID_ARGUMENT)

    def test_a_missing_template_is_not_found(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            scaffold_note(
                vault=self.vault, title="A", type_="concept", description="d", template="nope"
            )
        self.assertEqual(classify(ctx.exception).code, NOT_FOUND)

    def test_a_traversing_template_name_is_refused(self):
        # Asserted as INVALID_ARGUMENT specifically. NOT_FOUND would also stop
        # the read, but it would mean the name merely missed rather than that
        # the engine refused to leave the template folder, and the guard could
        # then be deleted without a test noticing.
        secret = Path(self.tmp.name) / "secret.md"
        secret.write_text("# secret\n", "utf-8")
        for name in ("../../secret", "../secret", "/etc/passwd"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as ctx:
                    scaffold_note(
                        vault=self.vault,
                        title="A",
                        type_="concept",
                        description="d",
                        template=name,
                    )
                self.assertEqual(classify(ctx.exception).code, INVALID_ARGUMENT)

    def test_a_degenerate_name_stays_inside_the_template_folder(self):
        # '..' is not refused, because it never escapes: it becomes the
        # literal filename '...md' inside the template folder and simply
        # misses. Pinned so the NOT_FOUND is understood as containment
        # working rather than the traversal guard having been skipped.
        with self.assertRaises(FileNotFoundError) as ctx:
            scaffold_note(
                vault=self.vault, title="A", type_="concept", description="d", template=".."
            )
        self.assertEqual(classify(ctx.exception).code, NOT_FOUND)
        self.assertIn("templates/", str(ctx.exception))

    def test_requesting_a_template_with_none_configured_says_so(self):
        (self.vault_dir / ".obsidian" / "templates.json").unlink()
        with self.assertRaises(FileNotFoundError) as ctx:
            scaffold_note(
                vault=self.vault, title="A", type_="concept", description="d", template="concept"
            )
        self.assertIn("template folder", str(ctx.exception))


class TestStarterTemplatesAreOptIn(unittest.TestCase):
    """§7.5, §7.6. `init --obsidian-templates` writes the folder it declares."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, **kwargs):
        """Run `cadabby init` against the scratch vault, swallowing its chatter."""
        opts = {
            "target_path": self.vault_dir,
            "vault": None,
            "name": "v",
            "force": False,
            "obsidian": False,
            "obsidian_templates": False,
            **kwargs,
        }
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cmd_init(DummyArgs(**opts))

    def insert_template(self, name, title):
        """Do what Obsidian does: copy a template in and substitute {{title}}."""
        body = (self.vault_dir / "templates" / name).read_text("utf-8")
        dest = self.vault_dir / "wiki" / f"{title.replace(' ', '-')}.md"
        dest.write_text(body.replace("{{title}}", title), "utf-8")
        return dest

    def test_plain_obsidian_init_writes_no_templates(self):
        # The opt-in guarantee. Reserving `templates/` in every Obsidian vault
        # would silently cost a user who wants it as a cognitive domain.
        self.assertEqual(self.init(obsidian=True), EXIT_OK)
        self.assertFalse((self.vault_dir / "templates").exists())
        self.assertFalse((self.vault_dir / ".obsidian" / "templates.json").exists())

    def test_the_flag_writes_the_folder_and_declares_it_together(self):
        # Declaring a folder without populating it would reserve a name while
        # nothing used it; populating without declaring would leave pre-notes
        # in a live cognitive domain. Neither half is correct alone.
        self.assertEqual(self.init(obsidian_templates=True), EXIT_OK)
        declared = obsidian_template_dir(self.vault_dir)
        self.assertEqual(declared, (self.vault_dir / "templates").resolve())
        self.assertTrue(sorted(declared.glob("*.md")))

    def test_the_flag_implies_obsidian(self):
        # Starter templates are useless to a vault Obsidian cannot open.
        self.assertEqual(self.init(obsidian_templates=True), EXIT_OK)
        self.assertTrue((self.vault_dir / ".obsidian" / "app.json").is_file())

    def test_the_declared_folder_is_the_one_the_engine_skips(self):
        # Closes the loop: the setting init writes is read back by the same
        # function the scan uses, so the folder cannot be declared and walked.
        self.init(obsidian_templates=True)
        vault = Vault(self.vault_dir)
        self.assertNotIn("templates", vault.discover_domains())
        self.assertEqual(run_vault_lint(vault), [])

    def test_inserting_a_starter_leaves_exactly_the_field_it_cannot_fill(self):
        # The core design claim. Obsidian's plugin has no input prompt, so
        # `description` cannot be templated. It is left empty on purpose: a
        # placeholder like "TODO" would lint clean and quietly seed a required
        # epistemic field -- one that feeds BM25 and index.md -- with filler.
        # A loud FIELD_MISSING naming the note is the honest outcome.
        self.init(obsidian_templates=True)
        self.insert_template("Concept.md", "Flash Attention")
        errors = [f for f in run_vault_lint(Vault(self.vault_dir)) if f.severity == "error"]
        self.assertEqual([f.code for f in errors], ["FIELD_MISSING"])
        self.assertIn("description", errors[0].message)

    def test_filling_that_field_clears_the_error(self):
        self.init(obsidian_templates=True)
        note = self.insert_template("Concept.md", "Flash Attention")
        note.write_text(
            note.read_text("utf-8").replace("description:\n", 'description: "IO-aware"\n'), "utf-8"
        )
        errors = [f for f in run_vault_lint(Vault(self.vault_dir)) if f.severity == "error"]
        self.assertEqual(errors, [])

    def test_every_starter_yields_a_parseable_note_once_the_title_is_filled(self):
        # Guards the shipped assets themselves: a starter that survives §3.2
        # only by luck would fail at the one moment it is used.
        self.init(obsidian_templates=True)
        for src in sorted((self.vault_dir / "templates").glob("*.md")):
            with self.subTest(template=src.name):
                note = self.insert_template(src.name, f"Title {src.stem}")
                fm, _ = parse_frontmatter(note.read_text("utf-8"))
                self.assertEqual(fm["title"], f"Title {src.stem}")
                self.assertIn(fm["type"], ("concept", "moc"))

    def test_shipped_starters_never_carry_epistemic_frontmatter(self):
        # A starter that set `verified` or `generated` would let every note a
        # human creates in Obsidian claim an attestation nobody made (§3.4).
        self.init(obsidian_templates=True)
        for src in sorted((self.vault_dir / "templates").glob("*.md")):
            with self.subTest(template=src.name):
                note = self.insert_template(src.name, "T")
                fm, _ = parse_frontmatter(note.read_text("utf-8"))
                self.assertNotIn("verified", fm)
                self.assertNotIn("generated", fm)

    def test_the_starters_also_drive_scaffold(self):
        # The human path and the agent path read the same files, which is what
        # makes "convergence" more than a slogan.
        self.init(obsidian_templates=True)
        path = scaffold_note(
            vault=Vault(self.vault_dir),
            title="Ring Attention",
            type_="concept",
            description="d",
            template="Concept",
        )
        self.assertIn("# Ring Attention", path.read_text("utf-8"))

    def test_an_existing_declaration_wins_and_is_left_alone(self):
        # Read, do not mirror: a user who already pointed Obsidian somewhere
        # gets the starters there and keeps their own settings file intact.
        self.init(obsidian=True)
        settings = self.vault_dir / ".obsidian" / "templates.json"
        settings.write_text(json.dumps({"folder": "meta/templates", "dateFormat": "L"}), "utf-8")
        self.assertEqual(self.init(obsidian_templates=True), EXIT_OK)
        self.assertEqual(json.loads(settings.read_text("utf-8"))["dateFormat"], "L")
        self.assertTrue((self.vault_dir / "meta" / "templates" / "Concept.md").is_file())
        self.assertFalse((self.vault_dir / "templates").exists())

    def test_force_does_not_redirect_an_existing_declaration(self):
        # --force overwrites user-owned files with shipped defaults, and this
        # is the one it must not: resetting the setting to `templates` while
        # the starters went to `meta/templates` leaves the declared folder
        # empty and the populated one a live domain full of pre-notes, which
        # is lint red in a vault the user only asked to refresh.
        self.init(obsidian=True)
        settings = self.vault_dir / ".obsidian" / "templates.json"
        settings.write_text(json.dumps({"folder": "meta/templates"}), "utf-8")
        self.assertEqual(self.init(obsidian_templates=True, force=True), EXIT_OK)
        self.assertEqual(json.loads(settings.read_text("utf-8"))["folder"], "meta/templates")
        self.assertEqual(run_vault_lint(Vault(self.vault_dir)), [])

    def test_a_templates_folder_already_holding_notes_is_refused(self):
        # The failure this whole design exists to avoid: declaring the folder
        # would drop a cognitive domain out of the vault with no error at all.
        self.init(obsidian=True)
        existing = self.vault_dir / "templates" / "Real-Note.md"
        existing.parent.mkdir(parents=True)
        existing.write_text(
            '---\ntype: concept\ntitle: "Real Note"\ndescription: "d"\nstatus: active\n'
            "---\n# Real Note\n",
            "utf-8",
        )
        self.assertEqual(self.init(obsidian_templates=True), EXIT_ENVIRONMENT)
        self.assertFalse((self.vault_dir / ".obsidian" / "templates.json").exists())
        self.assertEqual([p.name for p in (self.vault_dir / "templates").glob("*.md")],
                         ["Real-Note.md"])
        self.assertIn("templates", Vault(self.vault_dir).discover_domains())

    def test_re_running_leaves_an_edited_starter_untouched(self):
        # Templates carry content rather than naming behavior, so they are
        # user-owned (§7.6): unlike a shim, regenerating one destroys an edit.
        self.init(obsidian_templates=True)
        edited = self.vault_dir / "templates" / "Concept.md"
        edited.write_text("# mine\n", "utf-8")
        self.assertEqual(self.init(obsidian_templates=True), EXIT_OK)
        self.assertEqual(edited.read_text("utf-8"), "# mine\n")


class TestTemplatesRequireAVault(unittest.TestCase):
    """§9.1. Templates are vault layout, so the hermetic path cannot serve them."""

    def test_the_vaultless_use_case_refuses_a_template(self):
        # ScaffoldNoteUseCase is driven through in-memory adapters with no
        # vault in unit tests. A template name has nowhere to resolve against
        # there, and saying so beats an AttributeError on None.
        uc = ScaffoldNoteUseCase(InMemoryNoteStorage(), InMemoryLedger())
        with self.assertRaises(ValueError) as ctx:
            uc.execute(title="A", type_="concept", description="d", template="concept")
        self.assertEqual(classify(ctx.exception).code, INVALID_ARGUMENT)
        self.assertIn("vault", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
