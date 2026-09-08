"""Exercise the rendered installer extraction with real Unix ownership semantics."""
from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "archive ownership extraction requires Unix root")
class InstallerArchiveOwnershipTest(unittest.TestCase):
    def test_node_extraction_uses_installer_owner_instead_of_archive_uid(self):
        installer = Path(__file__).resolve().parents[1] / "pullwise_server/worker_node_installer.py"
        spec = importlib.util.spec_from_file_location("node_installer_fixture", installer)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        extraction = next(line.strip() for line in module.render().splitlines()
                          if line.strip().startswith("tar ") and "--strip-components=1" in line)
        with tempfile.TemporaryDirectory(prefix="pullwise-archive-owner-") as temporary:
            root = Path(temporary)
            archive_path = root / "node-fixture.tar.xz"
            destination = root / "node"
            destination.mkdir()
            contents = b"local ownership fixture; never executed\n"
            with tarfile.open(archive_path, "w:xz") as archive:
                entry = tarfile.TarInfo("node-fixture/bin/node")
                entry.uid = entry.gid = 12345
                entry.mode = 0o755
                entry.size = len(contents)
                archive.addfile(entry, io.BytesIO(contents))
            replacements = {"${TEMP_DIR}/${ARCHIVE}": str(archive_path), "$NODE_ROOT": str(destination)}
            command = [replacements.get(part, part) for part in shlex.split(extraction)]
            command[0] = shutil.which("tar") or "tar"
            subprocess.run(command, check=True, capture_output=True, timeout=10)
            extracted = destination / "bin/node"
            self.assertEqual(extracted.read_bytes(), contents)
            self.assertEqual(extracted.stat().st_uid, os.geteuid(),
                             "archive UID must not own the binary executed by the root Watcher")


if __name__ == "__main__":
    unittest.main()
