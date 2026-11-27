"""Qualification without real songs, models, or audio playback."""
import hashlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"bin"))
import _kara_stems as stems

class PolicyTests(unittest.TestCase):
    def generation(self, root, song):
        meta={"path":str(song),"bytes":6000,"mtime_ns":42,"model":stems.MODEL}
        token=hashlib.sha256(json.dumps(meta,sort_keys=True).encode()).hexdigest()
        directory=root/token
        directory.mkdir()
        (directory/"meta.json").write_text(json.dumps({"source":meta}))
        for name in stems.FILES:
            (directory/name).write_bytes(b"x"*6000)
        return directory

    def test_remove_only_owned_generations_and_dead_identity_pointers(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            a,b=root/"a.opus",root/"b.opus"
            keep=self.generation(root,a)
            remove=self.generation(root,b)
            unknown=root/("f"*64)
            unknown.mkdir()
            (unknown/"personal.txt").write_text("do not delete")
            registry=root/"identities";registry.mkdir()
            pointer=registry/("e"*64);pointer.write_text(remove.name)
            (root/"symlink").symlink_to(unknown)
            removed,count=stems.prune_stems(only={b},root=root)
            self.assertEqual((removed,count),({b},1))
            self.assertTrue(keep.is_dir())
            self.assertTrue((unknown/"personal.txt").is_file())
            self.assertTrue((root/"symlink").is_symlink())
            self.assertFalse(pointer.exists())
            self.assertFalse(remove.exists())

    def test_busy_lease_prevents_deletion(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            directory=self.generation(root,root/"a.opus")
            with stems.lease(root/"demucs.lock",wait=False):
                with self.assertRaises(RuntimeError):
                    stems.prune_stems(root=root)
            self.assertTrue(directory.exists())

    def test_reserve_and_cache_ceiling_are_enforced(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(stems.shutil,"disk_usage",return_value=SimpleNamespace(free=stems.MIN_DISK_FREE)):
                with self.assertRaises(RuntimeError): stems.admit_storage(root,60)
            with patch.object(stems.shutil,"disk_usage",return_value=SimpleNamespace(free=100*1024**3)):
                stems.admit_storage(root,60)
                with self.assertRaises(RuntimeError): stems.admit_storage(root,3600)

if __name__=="__main__":
    unittest.main()
