"""Regressions for the PR #21 audit findings (PR21-01 .. PR21-13).

Every fixture is synthetic: no real dependency, credential or proprietary code.
Each test names the audit finding it pins, so a later change that reopens a
leak path fails here with that identifier.
"""

from pathlib import Path
import gzip
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_open_core_boundary as boundary  # noqa: E402
import check_public_artifacts as artifacts  # noqa: E402
import open_core_names as names  # noqa: E402

PRIVATE_SPELLINGS = (
    "pexisos-enterprise", "pexisos_enterprise", "pexisos.enterprise", "PexisOS.Cloud",
    "pexisos---enterprise", "pexisos__cloud", "pexisos_-_cloud", "PEXISOS-CLOUD",
)
LEGITIMATE_NAMES = ("pexislink", "pexisos-cloudy", "pexisoscloud", "my-pexisos-cloud-adapter",
                    "pexislink-cloud", "cloudpickle", "enterprise-tools")


def _wheel(root: Path, entries: dict[str, str], name: str = "pexislink-0.1.0-py3-none-any.whl") -> Path:
    path = root / name
    with zipfile.ZipFile(path, "w") as archive:
        for filename, content in entries.items():
            archive.writestr(filename, content)
    return path


def _sdist(root: Path, entries: dict[str, str], name: str = "pexislink-0.1.0.tar.gz") -> Path:
    path = root / name
    with tarfile.open(path, "w:gz") as archive:
        for filename, content in entries.items():
            raw = content.encode("utf-8")
            info = tarfile.TarInfo(filename)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return path


CLEAN_WHEEL = {
    "pexislink/__init__.py": "__version__ = '0.1.0'",
    "pexislink-0.1.0.dist-info/METADATA": "Metadata-Version: 2.4\nName: pexislink\nVersion: 0.1.0\n"
                                          "Requires-Dist: cantools==44.1.0; extra == \"can\"\n",
    "pexislink-0.1.0.dist-info/entry_points.txt": "[console_scripts]\npexislink = pexislink.cli:main\n",
}


class NameNormalizationTests(unittest.TestCase):
    """PR21-01: PEP 503 names must not slip past an ad-hoc regex."""

    def test_every_equivalent_spelling_is_private(self):
        for spelling in PRIVATE_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertTrue(names.is_private_distribution(spelling))

    def test_legitimate_names_are_not_blocked(self):
        for name in LEGITIMATE_NAMES:
            with self.subTest(name=name):
                self.assertFalse(names.is_private_distribution(name))
                self.assertFalse(names.mentions_private_name(name))

    def test_requirement_strings_are_parsed_before_comparison(self):
        for requirement in ("pexisos.enterprise==1.0", "PexisOS.Cloud[full]>=1; python_version>'3'",
                            "pexisos---enterprise @ https://example.invalid/x.whl", "  pexisos__cloud ~= 2"):
            with self.subTest(requirement=requirement):
                self.assertTrue(names.mentions_private_name(requirement))

    def test_packaging_fields_reject_all_spellings(self):
        for spelling in PRIVATE_SPELLINGS:
            pyproject = {
                "build-system": {"requires": ["setuptools>=77", f"{spelling}>=1"]},
                "project": {"dependencies": [f"{spelling}==0.1"],
                            "optional-dependencies": {"x": [f"{spelling}[extra]; os_name=='posix'"]}},
                "dependency-groups": {"dev": [spelling]},
            }
            with self.subTest(spelling=spelling):
                found = boundary.inspect_packaging(pyproject)
                for field in ("build-system", "project.dependencies", "project.optional-dependencies",
                              "dependency-groups"):
                    self.assertIn(f"private_packaging_reference:{field}", found)

    def test_legitimate_packaging_passes(self):
        pyproject = {"build-system": {"requires": ["setuptools>=77.0.3"]},
                     "project": {"dependencies": [], "optional-dependencies": {"mcp": ["mcp==2.2.0"]},
                                 "scripts": {"pexislink": "pexislink.cli:main"}},
                     "tool": {"setuptools": {"packages": {"find": {"where": ["src"]}}}}}
        self.assertEqual(boundary.inspect_packaging(pyproject), [])


class TrackedPathTests(unittest.TestCase):
    """PR21-02: private modules as files, at the root, or as gitlinks."""

    def test_single_file_and_root_private_modules_are_rejected(self):
        paths = ["src/pexisos_cloud.py", "src/pexisos_enterprise.py", "pexisos_cloud.py",
                 "pexisos_enterprise/__init__.py", "src/pexisos_cloud.cpython-312-x86_64-linux-gnu.so",
                 "scripts/pexisos_enterprise.pth", "examples/nested/pexisos_cloud/__init__.py"]
        found = boundary.inspect_tracked_paths(paths)
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(f"private_tracked_path:{path}", found)

    def test_similar_public_names_are_allowed(self):
        self.assertEqual(boundary.inspect_tracked_paths(
            ["src/pexisos_cloudy.py", "docs/pexisos-cloud-boundary.md", "src/pexislink/cloud_sync.py"]), [])

    def test_gitlinks_and_symlinks_are_rejected(self):
        entries = [("160000", "src/pexisos_cloud"), ("160000", "vendor/lib"),
                   ("120000", "src/pexislink/host_probe.py"), ("100644", "src/pexislink/core.py"),
                   ("100755", "scripts/tool.py")]
        found = boundary.inspect_tracked_entries(entries)
        self.assertIn("gitlink_tracked_path:src/pexisos_cloud", found)
        self.assertIn("gitlink_tracked_path:vendor/lib", found)
        self.assertIn("symlink_tracked_path:src/pexislink/host_probe.py", found)
        self.assertIn("private_tracked_path:src/pexisos_cloud", found)
        self.assertFalse(any("core.py" in f or "tool.py" in f for f in found), found)

    def test_repository_check_end_to_end(self):
        """A committed src/pexisos_enterprise.py fails the real repository check."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            git = ["git", "-C", td, "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run(git + ["init", "-q"], check=True)
            (repo / "pyproject.toml").write_text('[project]\nname = "pexislink"\nversion = "0"\n')
            (repo / "src").mkdir()
            (repo / "src/pexisos_enterprise.py").write_text("X = 1\n")
            subprocess.run(git + ["add", "."], check=True)
            findings = boundary.check_repository(repo)
            self.assertIn("private_tracked_path:src/pexisos_enterprise.py", findings)


class ArchiveMemberTests(unittest.TestCase):
    """PR21-03: private modules and metadata inside wheels and sdists."""

    LEAKS = ("pexisos_enterprise.py", "pexisos_cloud.cpython-312-x86_64-linux-gnu.so",
             "pexisos_cloud.abi3.so", "pexisos_cloud.pyd", "pexisos_enterprise.pth",
             "pexisos_cloud-1.0.dist-info/METADATA", "pexisos.cloud-1.0.dist-info/RECORD",
             "pexislink/_vendor/pexisos_cloud.py", "pexisos_enterprise-1.0.data/purelib/x.py",
             "PexisOS_Cloud.py")

    def test_wheel_rejects_every_private_member_form(self):
        with tempfile.TemporaryDirectory() as td:
            for leak in self.LEAKS:
                with self.subTest(member=leak):
                    path = _wheel(Path(td), {**CLEAN_WHEEL, leak: "synthetic"})
                    self.assertIn(f"private_archive_path:{leak}", artifacts.inspect_archive(path))

    def test_sdist_rejects_single_file_private_module(self):
        with tempfile.TemporaryDirectory() as td:
            path = _sdist(Path(td), {"pexislink-0.1.0/PKG-INFO": "Name: pexislink\n",
                                     "pexislink-0.1.0/src/pexisos_cloud.py": "synthetic"})
            self.assertIn("private_archive_path:pexislink-0.1.0/src/pexisos_cloud.py",
                          artifacts.inspect_archive(path))

    def test_similar_public_members_are_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            path = _wheel(Path(td), {**CLEAN_WHEEL, "pexislink/pexisos_cloudy.py": "ok"})
            self.assertEqual(artifacts.inspect_archive(path), [])


class DistributionMetadataTests(unittest.TestCase):
    """PR21-06 (and PR21-05 at artifact level): what the wheel declares, not only what pyproject says."""

    def test_private_requires_dist_is_rejected(self):
        for requirement in ("pexisos.enterprise==1.0", 'PexisOS.Cloud>=1; extra == "ent"', "pexisos---enterprise"):
            metadata = f"Metadata-Version: 2.4\nName: pexislink\nVersion: 0.1.0\nRequires-Dist: {requirement}\n"
            with self.subTest(requirement=requirement), tempfile.TemporaryDirectory() as td:
                path = _wheel(Path(td), {**CLEAN_WHEEL, "pexislink-0.1.0.dist-info/METADATA": metadata})
                found = artifacts.inspect_archive(path)
                self.assertIn("private_metadata_reference:pexislink-0.1.0.dist-info/METADATA", found)

    def test_private_entry_point_is_rejected(self):
        entry_points = "[gui_scripts]\nlaunch = pexisos_cloud:main\n"
        with tempfile.TemporaryDirectory() as td:
            path = _wheel(Path(td), {**CLEAN_WHEEL, "pexislink-0.1.0.dist-info/entry_points.txt": entry_points})
            self.assertIn("private_metadata_reference:pexislink-0.1.0.dist-info/entry_points.txt",
                          artifacts.inspect_archive(path))

    def test_sdist_metadata_is_inspected(self):
        with tempfile.TemporaryDirectory() as td:
            path = _sdist(Path(td), {
                "pexislink-0.1.0/PKG-INFO": "Name: pexislink\nRequires-Dist: PexisOS.Cloud\n",
                "pexislink-0.1.0/src/pexislink.egg-info/requires.txt": "pexisos.enterprise\n",
            })
            found = artifacts.inspect_archive(path)
            self.assertIn("private_metadata_reference:pexislink-0.1.0/PKG-INFO", found)
            self.assertIn("private_metadata_reference:pexislink-0.1.0/src/pexislink.egg-info/requires.txt", found)

    def test_clean_metadata_passes(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(artifacts.inspect_archive(_wheel(Path(td), CLEAN_WHEEL)), [])

    def test_oversized_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            huge = "Name: pexislink\n" + "X-Pad: " + "a" * (artifacts._MAX_METADATA_BYTES + 1) + "\n"
            path = _wheel(Path(td), {**CLEAN_WHEEL, "pexislink-0.1.0.dist-info/METADATA": huge})
            self.assertIn("metadata_size_limit:pexislink-0.1.0.dist-info/METADATA", artifacts.inspect_archive(path))


class DynamicDependencyFileTests(unittest.TestCase):
    """PR21-06: setuptools dynamic dependency files are read, not just named."""

    @staticmethod
    def _pyproject(dependencies=None, optional=None):
        dynamic = {}
        if dependencies is not None:
            dynamic["dependencies"] = {"file": dependencies}
        if optional is not None:
            dynamic["optional-dependencies"] = {"ent": {"file": optional}}
        return {"project": {"name": "pexislink", "dynamic": list(dynamic)},
                "tool": {"setuptools": {"dynamic": dynamic}}}

    def test_private_requirement_in_dynamic_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.in").write_text("# comment\ncantools==44.1.0\npexisos.enterprise==1.0\n")
            (root / "extra.in").write_text("PexisOS.Cloud>=1\n")
            found = boundary.inspect_packaging(self._pyproject(["requirements.in"], ["extra.in"]), root)
            self.assertIn("private_packaging_reference:tool.setuptools.dynamic.dependencies.file", found)
            self.assertIn("private_packaging_reference:tool.setuptools.dynamic.optional-dependencies.file", found)

    def test_clean_dynamic_file_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.in").write_text("cantools==44.1.0\n")
            self.assertEqual(boundary.inspect_packaging(self._pyproject("requirements.in"), root), [])

    def test_missing_escaping_or_unchecked_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            (Path(td) / "outside.txt").write_text("cantools\n")
            (root / "link.txt").symlink_to(Path(td) / "outside.txt")
            for ref in ("missing.txt", "../outside.txt", "/etc/hostname", "link.txt"):
                with self.subTest(ref=ref):
                    found = boundary.inspect_packaging(self._pyproject([ref]), root)
                    self.assertIn("unreadable_dynamic_dependency_file:tool.setuptools.dynamic.dependencies.file",
                                  found)
            self.assertIn("unreadable_dynamic_dependency_file:tool.setuptools.dynamic.dependencies.file",
                          boundary.inspect_packaging(self._pyproject(["requirements.in"])))



class DynamicImportFormTests(unittest.TestCase):
    """PR21-04: every literal spelling of a dynamic import is resolved."""

    FORMS = (
        'import importlib\nimportlib.import_module(name="pexisos_cloud.client")\n',
        '__import__(name="pexisos_enterprise")\n',
        'from importlib import import_module as im\nim(name="pexisos_cloud")\n',
        'import importlib\nimportlib.import_module(".client", package="pexisos_cloud")\n',
        'import builtins\nbuiltins.__import__("pexisos_enterprise")\n',
        'import importlib\nimportlib.__import__("pexisos_cloud")\n',
        'import importlib\nimportlib.import_module("pexisos.enterprise")\n',
    )

    def test_every_form_is_reported(self):
        for source in self.FORMS:
            with self.subTest(source=source):
                found = boundary.inspect_imports(source, "src/pexislink/x.py")
                self.assertTrue(any("private_dynamic_import" in f for f in found), found)

    def test_public_dynamic_imports_are_allowed(self):
        source = ('import importlib\nimportlib.import_module(name="pexislink.fabric")\n'
                  'importlib.import_module(".core", package="pexislink")\n__import__("json")\n')
        self.assertEqual(boundary.inspect_imports(source, "src/pexislink/x.py"), [])


class EntryPointFieldTests(unittest.TestCase):
    """PR21-05: gui-scripts, entry-points and any tool table are inspected."""

    def test_gui_scripts_and_tool_tables_are_inspected(self):
        pyproject = {"project": {"gui-scripts": {"launch": "pexisos_cloud:main"},
                                 "entry-points": {"pexislink.plugins": {"x": "PexisOS.Cloud.plugin:run"}}},
                     "tool": {"uv": {"sources": {"pexisos-cloud": {"index": "private"}}}}}
        found = boundary.inspect_packaging(pyproject)
        for field in ("project.gui-scripts", "project.entry-points", "tool.uv"):
            self.assertIn(f"private_packaging_reference:{field}", found)


class SdistRootTests(unittest.TestCase):
    """PR21-07: a private tarball cannot hide its root by matching the archive name."""

    def test_private_root_named_like_the_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for root_name in ("pexisos_cloud", "private", "enterprise", "pexisos_enterprise-1.0"):
                with self.subTest(root=root_name):
                    path = _sdist(Path(td), {f"{root_name}/engine.py": "synthetic"}, f"{root_name}.tar.gz")
                    self.assertTrue(any(f.startswith("private_archive_path:") for f in artifacts.inspect_archive(path)))


class ArchiveLimitTests(unittest.TestCase):
    """PR21-08: limits stop the scan early instead of after reading the whole archive."""

    def test_member_count_limit_stops_tar_iteration(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pexislink-0.1.0.tar"
            with tarfile.open(path, "w") as archive:
                for index in range(artifacts._MAX_ENTRIES + 5):
                    archive.addfile(tarfile.TarInfo(f"pexislink-0.1.0/f{index}"), io.BytesIO(b""))
            self.assertEqual(artifacts.inspect_archive(path), ["archive_member_count_limit"])

    def test_declared_size_limit_stops_before_decompressing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pexislink-0.1.0.tar.gz"
            info = tarfile.TarInfo("pexislink-0.1.0/big.bin")
            info.size = artifacts._MAX_UNCOMPRESSED + 1
            # Header only, body absent: a scanner that read the member would hit EOF.
            with gzip.open(path, "wb") as handle:
                handle.write(info.tobuf(format=tarfile.PAX_FORMAT) + b"\0" * 512)
            self.assertEqual(artifacts.inspect_archive(path), ["archive_uncompressed_size_limit"])

    def test_oversized_archive_file_is_refused_unread(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pexislink-0.1.0-py3-none-any.whl"
            with path.open("wb") as handle:
                handle.truncate(artifacts._MAX_ARCHIVE_BYTES + 1)
            self.assertEqual(artifacts.inspect_archive(path), ["archive_size_limit"])


class DiagnosticEscapingTests(unittest.TestCase):
    """PR21-09: crafted names cannot forge log lines, CI commands or terminal escapes."""

    FORGED = "evil\n::error file=README.md::forged\x1b[31m"

    def test_archive_member_names_are_escaped(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pexislink-0.1.0-py3-none-any.whl"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("pexislink/__init__.py", "")
                info = zipfile.ZipInfo(self.FORGED)
                info.external_attr = (0o120777 << 16)
                archive.writestr(info, "x")
            found = artifacts.inspect_archive(path)
            self.assertTrue(found)
            for line in found:
                self.assertNotIn("\n", line)
                self.assertNotIn("\x1b", line)
                self.assertFalse(line.startswith("::"))

    def test_tracked_path_and_import_diagnostics_are_escaped(self):
        for line in boundary.inspect_tracked_paths([f"src/pexisos_cloud/{self.FORGED}.py"]):
            self.assertNotIn("\n", line)
            self.assertNotIn("\x1b", line)
        for line in boundary.inspect_imports("import pexisos_cloud\n", f"src/{self.FORGED}.py"):
            self.assertNotIn("\n", line)


class WholeTreeScanTests(unittest.TestCase):
    """PR21-11: packages beside src/pexislink/ (and other tracked Python) are scanned."""

    def test_sibling_package_private_import_fails_repository_check(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            git = ["git", "-C", td, "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run(git + ["init", "-q"], check=True)
            (repo / "pyproject.toml").write_text('[project]\nname = "pexislink"\nversion = "0"\n')
            (repo / "src/pexislink_extras").mkdir(parents=True)
            (repo / "src/pexislink_extras/__init__.py").write_text("import pexisos_cloud\n")
            subprocess.run(git + ["add", "."], check=True)
            findings = boundary.check_repository(repo)
            self.assertIn("src/pexislink_extras/__init__.py:1:private_import:pexisos_cloud", findings)

    def test_tracked_symlink_is_refused_and_not_read(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run(git + ["init", "-q"], check=True)
            (repo / "pyproject.toml").write_text('[project]\nname = "pexislink"\nversion = "0"\n')
            outside = Path(td) / "outside.py"
            outside.write_text("import pexisos_cloud\n")
            (repo / "probe.py").symlink_to(outside)
            subprocess.run(git + ["add", "."], check=True)
            findings = boundary.check_repository(repo)
            self.assertIn("symlink_tracked_path:probe.py", findings)
            self.assertFalse(any("private_import" in f for f in findings), findings)

if __name__ == "__main__":
    unittest.main()
