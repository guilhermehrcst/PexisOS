"""The public Pages site: a visual-only PETRON Brain at the root and the Console at /console/.

These tests pin what may be published. The builder takes a fixed allowlist of Brain files and then checks
the result; a build that would publish an operational capability, a broken reference or an absolute path
must refuse, not warn.
"""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_pages_site", ROOT / "scripts" / "build_pages_site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)

CONSOLE_PAGE = ('<!doctype html><html><head><meta charset="utf-8"><title>PexisOS Console</title>'
                '<script type="module" src="./assets/app.js"></script>'
                '<link rel="stylesheet" href="./assets/app.css"></head><body><div id="root"></div></body></html>')


class SiteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pages-site-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.console = self.tmp / "console-dist"
        (self.console / "assets").mkdir(parents=True)
        (self.console / "index.html").write_text(CONSOLE_PAGE)
        (self.console / "assets" / "app.js").write_text("console.log(1)")
        (self.console / "assets" / "app.css").write_text("body{}")
        self.brain = self.tmp / "brain"
        shutil.copytree(site.BRAIN_SOURCE, self.brain)        # the real approved sources, editable per test
        self.out = self.tmp / "site"

    def build(self):
        site.build(self.console, self.out, brain_source=self.brain)

    def refuses(self, fragment):
        with self.assertRaises(site.SiteError) as caught:
            self.build()
        self.assertIn(fragment, str(caught.exception))


class LayoutTests(SiteCase):
    def test_the_published_tree_is_exactly_the_brain_the_console_and_nojekyll(self):
        self.build()
        self.assertEqual({p.name for p in self.out.iterdir()},
                         {"index.html", "brain.js", "brain-texture.webp", "brain-mask.webp", "console", ".nojekyll"})
        self.assertTrue((self.out / "console" / "index.html").is_file())
        self.assertTrue((self.out / "console" / "assets" / "app.js").is_file())

    def test_no_operational_part_of_petron_is_copied(self):
        for leftover in ("README.md", "__init__.py"):
            self.assertTrue((self.brain / leftover).exists())       # present in the source directory ...
        self.build()
        published = {str(p.relative_to(self.out)) for p in self.out.rglob("*") if p.is_file()}
        for forbidden in ("live.js", "README.md", "__init__.py"):   # ... but never published
            self.assertNotIn(forbidden, published)
        self.assertFalse(any(name.endswith((".py", ".sqlite3", ".db")) for name in published))

    def test_the_brain_page_is_static_visual_and_locked_down(self):
        self.build()
        page = (self.out / "index.html").read_text()
        self.assertIn("connect-src 'none'", page)
        self.assertIn("default-src 'none'", page)
        self.assertIn('<meta name="petron-palette" content="spectrum">', page)
        self.assertEqual(page.count("<script"), 1)
        self.assertIn('<script src="./brain.js" defer></script>', page)
        for capability in ("live.js", "petron-input", "petron-mode", "EventSource", "fetch(", "/api/"):
            self.assertNotIn(capability, page + (self.out / "brain.js").read_text(), capability)

    def test_building_twice_gives_the_same_site(self):
        self.build()
        first = {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
        self.build()
        second = {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
        self.assertEqual(first, second)

    def test_the_cli_builds_and_verifies(self):
        self.assertEqual(site.main(["--console-dist", str(self.console), "--out", str(self.out)]), 0)
        self.assertEqual(site.main(["--verify-only", "--out", str(self.out)]), 0)


class RefusalTests(SiteCase):
    def test_a_missing_console_build_stops_the_build(self):
        shutil.rmtree(self.console)
        self.refuses("console build not found")

    def test_a_missing_approved_brain_file_stops_the_build(self):
        (self.brain / "brain-mask.webp").unlink()
        self.refuses("approved Brain file missing")

    def test_a_brain_that_reaches_the_bridge_is_refused(self):
        for label, injection in {
            "live.js": '<script src="./live.js" defer></script>',
            "event stream": "<script>new EventSource('/x')</script>",
            "api path": '<a href="/api/missions">x</a>',
            "bridge input": '<textarea id="petron-input"></textarea>',
        }.items():
            with self.subTest(label):
                shutil.copyfile(site.BRAIN_SOURCE / "index.html", self.brain / "index.html")
                html = (self.brain / "index.html").read_text().replace("</main>", injection + "</main>")
                (self.brain / "index.html").write_text(html)
                with self.assertRaises(site.SiteError):
                    self.build()

    def test_a_network_call_in_the_brain_script_is_refused(self):
        shutil.copyfile(site.BRAIN_SOURCE / "brain.js", self.brain / "brain.js")
        (self.brain / "brain.js").write_text((self.brain / "brain.js").read_text() + "\nfetch('/api/snapshot');\n")
        self.refuses("operational capability")

    def test_a_brain_page_that_already_has_a_csp_is_refused_not_duplicated(self):
        html = (self.brain / "index.html").read_text().replace(
            '<meta charset="utf-8">', '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="x">')
        (self.brain / "index.html").write_text(html)
        self.refuses("no existing CSP")

    def test_an_absolute_console_asset_path_is_refused(self):
        (self.console / "index.html").write_text(CONSOLE_PAGE.replace('src="./assets/app.js"', 'src="/assets/app.js"'))
        self.refuses("absolute")

    def test_a_console_reference_to_a_missing_file_is_refused(self):
        (self.console / "assets" / "app.css").unlink()
        self.refuses("does not resolve")

    def test_a_console_without_assets_is_refused(self):
        shutil.rmtree(self.console / "assets")
        (self.console / "index.html").write_text("<html><body>x</body></html>")
        self.refuses("assets/")

    def test_verify_refuses_extra_files_at_the_root(self):
        self.build()
        (self.out / "notes.txt").write_text("not for the public")
        with self.assertRaises(site.SiteError) as caught:
            site.verify(self.out)
        self.assertIn("extra", str(caught.exception))

    def test_verify_refuses_an_operational_file_anywhere_in_the_tree(self):
        self.build()
        (self.out / "console" / "assets" / "brain_bridge.py").write_text("x")
        with self.assertRaises(site.SiteError) as caught:
            site.verify(self.out)
        self.assertIn("forbidden file", str(caught.exception))

    def test_verify_refuses_a_brain_without_its_csp(self):
        self.build()
        page = (self.out / "index.html").read_text().replace("connect-src 'none'", "connect-src *")
        (self.out / "index.html").write_text(page)
        with self.assertRaises(site.SiteError):
            site.verify(self.out)

    def test_the_cli_exits_non_zero_when_refusing(self):
        shutil.rmtree(self.console)
        self.assertEqual(site.main(["--console-dist", str(self.console), "--out", str(self.out)]), 1)

    def test_two_brain_script_tags_are_refused(self):
        tag = '<script src="./brain.js" defer></script>'
        self.build()
        page = (self.out / "index.html").read_text()
        (self.out / "index.html").write_text(page.replace(tag, tag + tag))
        with self.assertRaises(site.SiteError) as caught:
            site.verify(self.out)
        self.assertIn("exactly one", str(caught.exception))

    def test_a_reference_that_leaves_the_site_is_refused_even_if_the_file_exists_beside_it(self):
        self.build()
        (self.tmp / "README.md").write_text("outside the artifact")           # exists next to site/, not in it
        page = (self.out / "index.html").read_text().replace("</main>", '<a href="../README.md">x</a></main>')
        (self.out / "index.html").write_text(page)
        with self.assertRaises(site.SiteError) as caught:
            site.verify(self.out)
        self.assertIn("outside the site", str(caught.exception))


class OutputDirectoryTests(SiteCase):
    """`rmtree` runs on the --out directory, so it is resolved and checked before anything is deleted."""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "fake-repo"                      # stands in for the checkout the CLI runs from
        for sub in ("apps", "scripts"):
            (self.repo / sub).mkdir(parents=True)
            (self.repo / sub / "keep.txt").write_text("source")
        (self.repo / "README.md").write_text("source")
        original_repo, original_cwd = site.REPO, Path.cwd()
        site.REPO = self.repo.resolve()
        self.addCleanup(lambda: setattr(site, "REPO", original_repo))
        self.addCleanup(lambda: __import__("os").chdir(original_cwd))
        __import__("os").chdir(self.repo)

    def assert_refused_and_untouched(self, out):
        with self.assertRaises(site.SiteError):
            site.build(self.console, Path(out), brain_source=self.brain)
        for kept in ("apps/keep.txt", "scripts/keep.txt", "README.md"):
            self.assertTrue((self.repo / kept).is_file(), f"{kept} was deleted by --out {out}")

    def test_a_relative_path_to_the_repository_root_is_refused_before_deleting(self):
        self.assert_refused_and_untouched(".")

    def test_a_relative_path_to_a_source_directory_is_refused_before_deleting(self):
        self.assert_refused_and_untouched("apps")

    def test_a_path_that_reaches_the_root_through_dotdot_is_refused(self):
        self.assert_refused_and_untouched("apps/..")

    def test_the_parent_of_the_repository_is_refused(self):
        self.assert_refused_and_untouched("..")

    def test_an_output_that_contains_the_console_build_is_refused(self):
        with self.assertRaises(site.SiteError):
            site.build(self.console, self.tmp, brain_source=self.brain)
        self.assertTrue((self.console / "index.html").is_file())

    def test_a_non_empty_directory_that_is_not_an_earlier_build_is_refused(self):
        stranger = self.tmp / "stranger"
        stranger.mkdir()
        (stranger / "precious.txt").write_text("not a site")
        with self.assertRaises(site.SiteError):
            site.build(self.console, stranger, brain_source=self.brain)
        self.assertTrue((stranger / "precious.txt").is_file())

    def test_a_relative_output_name_and_an_earlier_build_are_still_accepted(self):
        site.build(self.console, Path("site"), brain_source=self.brain)
        site.build(self.console, Path("site"), brain_source=self.brain)        # rebuild over its own output
        self.assertTrue((self.repo / "site" / "console" / "index.html").is_file())


class WorkflowTests(unittest.TestCase):
    """The Pages workflow must publish the assembled site, build it with the checked script, and never from a PR."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github" / "workflows" / "pages.yml").read_text()

    def test_it_publishes_the_assembled_site_not_the_console_alone(self):
        self.assertIn("path: site", self.text)
        self.assertNotIn("path: apps/console/dist", self.text)
        self.assertIn("scripts/build_pages_site.py", self.text)

    def test_a_change_to_the_brain_files_or_the_builder_triggers_it(self):
        for path in ("apps/petron/brain/**", "scripts/build_pages_site.py", "apps/console/**"):
            self.assertIn(f'"{path}"', self.text, path)

    def test_deploy_stays_limited_to_main(self):
        self.assertIn("github.event_name != 'pull_request' && github.ref == 'refs/heads/main'", self.text)
        self.assertEqual(self.text.count("pages: write"), 1)

    def test_the_site_is_verified_before_upload(self):
        self.assertLess(self.text.index("--verify-only"), self.text.index("upload-pages-artifact"))

    def test_actions_stay_pinned_by_commit(self):
        for line in self.text.splitlines():
            if "uses:" in line:
                self.assertRegex(line, r"@[0-9a-f]{40}", line)


class ApprovedBrainSourceTests(unittest.TestCase):
    def test_the_source_default_palette_is_still_adaptive_for_the_local_runtime(self):
        """Only the published copy declares spectrum; the local bridge keeps its runtime-driven default."""
        page = (site.BRAIN_SOURCE / "index.html").read_text()
        self.assertNotIn("petron-palette", page)
        self.assertEqual(site.PUBLIC_PALETTE, "spectrum")

    def test_the_inventory_still_marks_the_textures_review_required(self):
        """Publishing is an owner decision; it is not an IP approval, and the inventory must not say it is."""
        data = json.loads((ROOT / "docs" / "ip" / "public-source-inventory.json").read_text())
        rules = {r["path"]: r for r in data["rules"]}
        for name in ("brain-texture.webp", "brain-mask.webp"):
            rule = rules[f"apps/petron/brain/{name}"]
            self.assertEqual((rule["classification"], rule["ownership_review"]), ("review-required", "pending"))
        self.assertEqual(data["legal_signoff"], "not_recorded")


if __name__ == "__main__":
    unittest.main()
