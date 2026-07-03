import tempfile
import unittest
from pathlib import Path

from app import build_rsync_commands, render_disk_card


class RsyncCommandTests(unittest.TestCase):
    def test_dry_run_does_not_create_destination_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            mount = root / "BackupDisk"
            source.mkdir()
            mount.mkdir()

            config = config_for(source)
            disk = disk_for(mount)

            commands = build_rsync_commands(config, disk, dry_run=True)

            self.assertEqual(len(commands), 1)
            self.assertIn("--dry-run", commands[0])
            self.assertFalse((mount / "Backups" / "This-Mac" / "source-one").exists())

    def test_live_run_creates_destination_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            mount = root / "BackupDisk"
            source.mkdir()
            mount.mkdir()

            build_rsync_commands(config_for(source), disk_for(mount), dry_run=False)

            self.assertTrue((mount / "Backups" / "This-Mac" / "source-one").is_dir())

    def test_disk_card_defaults_dry_run_to_off(self) -> None:
        disk = {
            "id": "offsite-a",
            "name": "Offsite A",
            "destination": "/Volumes/Offsite-A/Backups/This-Mac",
            "available": True,
        }

        html = render_disk_card(disk, running=False)

        self.assertIn('name="dry_run"', html)
        self.assertNotIn('name="dry_run" checked', html)


def config_for(source: Path) -> dict:
    return {
        "rsync_path": "/usr/bin/rsync",
        "rsync_options": ["-aE", "--delete"],
        "exclude_patterns": [],
        "sources": [
            {
                "id": "source-one",
                "label": "Source One",
                "path": str(source),
                "enabled": True,
            }
        ],
        "backup_disks": [],
    }


def disk_for(mount: Path) -> dict:
    return {
        "id": "offsite-a",
        "name": "Offsite A",
        "mount_path": str(mount),
        "destination_subdir": "Backups/This-Mac",
    }


if __name__ == "__main__":
    unittest.main()
