"""The installed wheel must behave like the documented CLI: ``petron experiment e001`` needs its
fixture, which lives outside ``src`` in a source checkout. (Review finding: installed-wheel behavior.)

Builds a real wheel. Interpreter with a recent setuptools: ``PETRON_BUILD_PYTHON`` (offline, no build
isolation); otherwise this interpreter with normal build isolation (needs network). If neither can
build, the test is skipped and reported as not run."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_wheel(into: Path) -> Path:
    work = into / "src"
    work.mkdir()
    for name in ("pyproject.toml", "LICENSE", "README.md", "src", "fixtures", "brain"):
        source = ROOT / name
        (shutil.copytree if source.is_dir() else shutil.copy)(source, work / name,
                                                              **({"ignore": shutil.ignore_patterns("__pycache__")}
                                                                 if source.is_dir() else {}))
    python = os.environ.get("PETRON_BUILD_PYTHON")
    command = [python or sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(into / "out"), "."]
    if python:
        command.insert(command.index("-q"), "--no-build-isolation")
    done = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=300)
    wheels = sorted((into / "out").glob("*.whl")) if (into / "out").exists() else []
    if done.returncode != 0 or not wheels:
        raise unittest.SkipTest("cannot build a wheel here (no pip/setuptools>=77 reachable): not run")
    return wheels[0]


class InstalledWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="petron-wheel-"))
        try:
            cls.wheel = build_wheel(cls.tmp)
        except BaseException:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise
        cls.installed = cls.tmp / "installed"
        with zipfile.ZipFile(cls.wheel) as archive:
            cls.names = archive.namelist()
            archive.extractall(cls.installed)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_wheel_carries_the_e001_fixture(self):
        for expected in ("petron/fixtures/e001/mission.json", "petron/fixtures/e001/project/calc/stats.py",
                         "petron/fixtures/e001/project/tests/test_stats.py"):
            self.assertIn(expected, self.names)

    def test_brain_assets_are_packaged_and_readable_from_an_installed_wheel(self):
        for expected in ("index.html", "brain.js", "live.js", "brain-texture.webp", "brain-mask.webp"):
            self.assertIn("petron/brain_assets/" + expected, self.names)
        script = (
            "from petron.brain_bridge import BRAIN_DIR, _live_index; "
            "assert all((BRAIN_DIR / n).is_file() for n in "
            "('index.html', 'brain.js', 'live.js', 'brain-texture.webp', 'brain-mask.webp')); "
            "assert 'live.js' in _live_index((BRAIN_DIR / 'index.html').read_text())"
        )
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(self.installed),
               "HOME": str(self.tmp), "PETRON_HOME": str(self.tmp / "home")}
        done = subprocess.run([sys.executable, "-c", script], cwd=self.tmp, env=env,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_documented_e001_command_works_from_an_installed_copy_outside_the_repo(self):
        cwd = self.tmp / "elsewhere"
        cwd.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(self.installed),
               "PETRON_HOME": str(self.tmp / "home"), "HOME": str(self.tmp)}
        done = subprocess.run([sys.executable, "-m", "petron", "experiment", "e001", "--sandbox", "none",
                               "--approver", "wheel-test"], cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=120)
        self.assertNotIn("Traceback", done.stderr)
        self.assertNotIn("FileNotFoundError", done.stdout + done.stderr)
        self.assertIn("# PETRON mission report", done.stdout)
        # Without an isolation backend nothing is verified, so the mission must fail closed (exit 1).
        self.assertEqual(done.returncode, 1, done.stdout[-400:])
        self.assertIn("isolation_unavailable", done.stdout)


if __name__ == "__main__":
    unittest.main()
