"""An MCP server is code. It must be authenticated before it is started, and what is started must
be exactly what was authenticated. (Review finding: untrusted MCP server executed on the host.)

The impostor package below writes a marker file the moment it is imported; the tests prove the
marker never appears unless the operator pinned that exact code."""

import importlib.util
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from petron.connectors.pexisos import (ConnectorError, FabricConfig, FabricConnector, digest_source,
                                       stage_source)

HAVE_MCP = importlib.util.find_spec("mcp") is not None
ANALYST = "demo:principal:analyst"


def impostor(root: Path, marker: Path) -> None:
    package = root / "pexislink"
    package.mkdir()
    (package / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('host code executed')\n")
    (package / "__main__.py").write_text(textwrap.dedent(f'''
        open({str(marker)!r}, 'a').write(' / main')
        raise SystemExit("impostor")
    '''))


class ImpostorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-impostor-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.marker = self.tmp / "protected-host-marker"      # stands in for $HOME/.ssh or similar
        self.src = self.tmp / "src"
        self.src.mkdir()
        impostor(self.src, self.marker)

    def run_connector(self, pin):
        return FabricConnector(FabricConfig(ANALYST, self.src, timeout_s=15, pexislink_src_sha256=pin)) \
            .run([("list_entities", {})])

    def refused(self, pin):
        with self.assertRaises(ConnectorError) as caught:
            self.run_connector(pin)
        return caught.exception.code

    @unittest.skipUnless(HAVE_MCP, "mcp not installed")
    def test_unpinned_server_is_never_started(self):
        self.assertEqual(self.refused(None), "source_not_pinned")
        self.assertFalse(self.marker.exists(), "impostor code ran on the host before authorization")

    @unittest.skipUnless(HAVE_MCP, "mcp not installed")
    def test_wrong_pin_is_never_started(self):
        self.assertEqual(self.refused("0" * 64), "source_hash_mismatch")
        self.assertFalse(self.marker.exists())

    @unittest.skipUnless(HAVE_MCP, "mcp not installed")
    def test_malformed_pin_is_refused(self):
        for pin in ("", "abc", "G" * 64, "A" * 64, 123):
            self.assertEqual(self.refused(pin), "source_pin_invalid", pin)
        self.assertFalse(self.marker.exists())

    @unittest.skipUnless(HAVE_MCP, "mcp not installed")
    def test_source_changed_after_pinning_is_never_started(self):
        pin = digest_source(self.src)
        (self.src / "pexislink" / "extra.py").write_text("# added after the operator pinned the tree\n")
        self.assertEqual(self.refused(pin), "source_hash_mismatch")
        self.assertFalse(self.marker.exists())

    @unittest.skipUnless(HAVE_MCP, "mcp not installed")
    def test_symlink_in_the_server_tree_is_never_started(self):
        outside = self.tmp / "outside.py"
        outside.write_text("print('outside')")
        os.symlink(outside, self.src / "pexislink" / "link.py")
        self.assertEqual(self.refused("0" * 64), "source_invalid")
        self.assertFalse(self.marker.exists())

    def test_an_operator_who_pins_code_authorizes_exactly_that_code(self):
        """Authenticity, not sandboxing: pinned code is trusted code. The boundary is the pin."""
        pin = digest_source(self.src)
        staged = stage_source(self.src, pin)
        self.addCleanup(staged.cleanup)
        self.assertTrue((Path(staged.name) / "pexislink" / "__init__.py").is_file())
        self.assertFalse(self.marker.exists(), "staging must only copy, never import")

    def test_staged_copy_is_the_authenticated_bytes_and_immune_to_later_edits(self):
        pin = digest_source(self.src)
        staged = stage_source(self.src, pin)
        self.addCleanup(staged.cleanup)
        before = (Path(staged.name) / "pexislink" / "__main__.py").read_bytes()
        (self.src / "pexislink" / "__main__.py").write_text("print('swapped after authentication')\n")
        self.assertEqual((Path(staged.name) / "pexislink" / "__main__.py").read_bytes(), before)
        mode = (Path(staged.name) / "pexislink" / "__main__.py").stat().st_mode & 0o777
        self.assertEqual(mode & 0o222, 0, "staged code must be read-only")
        self.assertEqual(Path(staged.name).stat().st_mode & 0o077, 0, "staging area must be private")

    def test_digest_is_stable_and_content_sensitive(self):
        first = digest_source(self.src)
        self.assertEqual(first, digest_source(self.src))
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        (self.src / "pexislink" / "__init__.py").write_text("# changed\n")
        self.assertNotEqual(first, digest_source(self.src))


class CliPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-clipin-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.marker = self.tmp / "marker"
        self.src = self.tmp / "src"
        self.src.mkdir()
        impostor(self.src, self.marker)

    def cli(self, *argv):
        import contextlib
        import io
        from petron.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_digest_command_prints_the_pin_without_starting_anything(self):
        code, out, _ = self.cli("fabric", "digest", "--pexislink-src", str(self.src))
        self.assertEqual(code, 0)
        self.assertIn(digest_source(self.src), out)
        self.assertFalse(self.marker.exists())

    @unittest.skipUnless(HAVE_MCP, "mcp not installed")
    def test_probe_without_a_pin_is_refused_and_starts_nothing(self):
        code, _, err = self.cli("fabric", "probe", "--principal", ANALYST, "--pexislink-src", str(self.src))
        self.assertEqual(code, 1)
        self.assertIn("source_not_pinned", err)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
