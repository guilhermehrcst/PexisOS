"""Synthetic malicious and valid public distributions; no credentials used."""

from pathlib import Path
import io
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_public_artifacts import inspect_archive


class PublicArtifactsTests(unittest.TestCase):
    def _wheel(self, root, entries):
        path = root / "pexislink-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for filename, content in entries.items():
                archive.writestr(filename, content)
        return path

    def _sdist(self, root, entries):
        path = root / "pexislink-0.1.0.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for filename, content in entries.items():
                info = tarfile.TarInfo(filename)
                raw = content.encode("utf-8")
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        return path

    def test_rejects_private_archive_paths_and_path_traversal(self):
        entries = {
            "pexisos_enterprise/engine.py": "SECRET_MARKER_391",
            "private/analysis.py": "algorithm",
            "../outside.txt": "not allowed",
            ".env": "DUMMY_SECRET_1234",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for archive in (self._wheel(root, entries), self._sdist(root, entries)):
                issues = inspect_archive(archive)
                self.assertGreaterEqual(len(issues), 4, issues)
                self.assertTrue(any("path_traversal" in issue for issue in issues), issues)
                self.assertTrue(any("private_archive_path" in issue for issue in issues), issues)
                self.assertNotIn("SECRET_MARKER_391", "\n".join(issues))
                self.assertNotIn("DUMMY_SECRET_1234", "\n".join(issues))

    def test_accepts_clean_pexislink_wheel(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._wheel(Path(td), {
                "pexislink/__init__.py": "__version__ = '0.1.0'",
                "pexislink-0.1.0.dist-info/METADATA": "Name: pexislink",
            })
            self.assertEqual(inspect_archive(path), [])

    def test_accepts_clean_sdist_root_and_rejects_embedded_private(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            clean = self._sdist(root, {
                "pexislink-0.1.0/src/pexislink/__init__.py": "# ok",
                "pexislink-0.1.0/LICENSE": "Apache License",
            })
            self.assertEqual(inspect_archive(clean), [])
            malicious = self._sdist(root, {
                "pexislink-0.1.0/src/pexislink/__init__.py": "# ok",
                "pexislink-0.1.0/src/pexisos_cloud/secret.py": "secret",
            })
            self.assertTrue(any("private_archive_path" in issue for issue in inspect_archive(malicious)))

    def test_error_never_prints_archive_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self._wheel(root, {"enterprise/plan.py": "NEVER_LOG_THIS_FAKE_SECRET"})
            self.assertNotIn("NEVER_LOG_THIS_FAKE_SECRET", "\n".join(inspect_archive(path)))

    def test_rejects_private_only_sdist_without_misreading_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self._sdist(root, {"private/engine.py": "secret"})
            self.assertTrue(any("private_archive_path" in line for line in inspect_archive(archive)))

    def test_refuses_directory_only_archives(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            zip_path = root / "pexislink-0.1.0-py3-none-any.whl"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("pexislink/", "")
            self.assertTrue(any("empty_archive" in f for f in inspect_archive(zip_path)))

    def test_refuses_empty_corrupted_and_unknown_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            empty = self._wheel(root, {})
            self.assertTrue(inspect_archive(empty))
            empty.write_bytes(b"corrupt archive bytes")
            self.assertTrue(inspect_archive(empty))
            txt = root / "text.txt"
            txt.write_text("not archive")
            self.assertTrue(inspect_archive(txt))

    def test_refuses_unsafe_tar_links_and_windows_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tar_path = root / "pexislink-0.1.0.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tar:
                info = tarfile.TarInfo("pexislink-0.1.0/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../private/keys"
                tar.addfile(info)
            self.assertTrue(any("unsupported_member_type" in issue for issue in inspect_archive(tar_path)))
            path = self._wheel(root, {r"C:\secret\file.py": "fake", "pexislink/__init__.py": "# ok"})
            self.assertTrue(any("absolute_archive_path" in issue for issue in inspect_archive(path)))


if __name__ == "__main__":
    unittest.main()
