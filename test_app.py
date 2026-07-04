import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app import BackupState, Job, build_rsync_commands, default_config, load_config, render_disk_card, render_job_card, render_page, run_job, update_job_progress_from_line


class RsyncCommandTests(unittest.TestCase):
    def test_default_config_excludes_appledouble_files(self) -> None:
        self.assertIn("._*", default_config()["exclude_patterns"])

    def test_rsync_command_always_excludes_appledouble_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            mount = root / "BackupDisk"
            source.mkdir()
            mount.mkdir()

            command = build_rsync_commands(config_for(source), disk_for(mount), dry_run=True)[0]

            self.assertIn("--exclude", command)
            self.assertIn("._*", command)

    def test_load_config_adds_builtin_excludes_to_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config = config_for(Path(tmp) / "source")
            config["exclude_patterns"] = [".DS_Store"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            loaded = load_config(config_path)
            saved = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertIn("._*", loaded["exclude_patterns"])
            self.assertIn("._*", saved["exclude_patterns"])

    def test_sources_table_labels_id_as_backup_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = BackupState(Path(tmp) / "config.json")

            html = render_page(state)

            self.assertIn("Subdirectory on backup disk", html)
            self.assertIn('placeholder="backup-subdirectory"', html)
            self.assertNotIn("<th>Label</th>", html)
            self.assertIn("ID is the stable internal key", html)

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
        self.assertIn("data-disk-status", html)
        self.assertIn('class="refresh-interval"', html)
        self.assertIn('value="30000"', html)
        self.assertIn('value="60000"', html)
        self.assertNotIn('value="60001"', html)
        self.assertNotIn('value="300000"', html)

    def test_job_log_renders_collapsed_details(self) -> None:
        job = Job(
            id="job-one",
            disk_id="offsite-a",
            disk_name="Offsite A",
            dry_run=False,
            started_at=0,
            commands=[["/usr/bin/rsync", "/source/", "/dest/"]],
            log=["line one"],
        )

        html = render_job_card(job)

        self.assertIn('<details class="log-details">', html)
        self.assertIn("<summary>Activity log</summary>", html)
        self.assertNotIn("<details open", html)

    def test_job_log_replaces_undecodable_rsync_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            state = BackupState(config_path)
            job = Job(
                id="job-one",
                disk_id="offsite-a",
                disk_name="Offsite A",
                dry_run=False,
                started_at=0,
                commands=[["/usr/bin/rsync", "/source/", "/dest/"]],
            )
            state.jobs[job.id] = job
            state.active_job_id = job.id

            class FakeProcess:
                stdout = iter(["valid line\n", "bad byte \ufffd\n"])

                def wait(self) -> int:
                    return 0

            with patch("app.subprocess.Popen", return_value=FakeProcess()) as popen:
                run_job(state, job.id)

            self.assertEqual(popen.call_args.kwargs["errors"], "replace")
            self.assertEqual(state.jobs[job.id].status, "completed")
            self.assertIn("bad byte", "\n".join(state.jobs[job.id].log))

    def test_rsync_progress_line_updates_job_progress(self) -> None:
        job = Job(
            id="job-one",
            disk_id="offsite-a",
            disk_name="Offsite A",
            dry_run=False,
            started_at=0,
        )

        update_job_progress_from_line(job, "Movies/example.mkv")
        update_job_progress_from_line(
            job,
            "     1489011  42%   37.43MB/s   00:00:04 (xfer#3, to-check=7/20)",
        )

        progress = job.to_dict()["progress"]
        self.assertEqual(progress["current_item"], "Movies/example.mkv")
        self.assertEqual(progress["current_file_percent"], 42)
        self.assertEqual(progress["speed"], "37.43MB/s")
        self.assertEqual(progress["xfer_count"], 3)
        self.assertEqual(progress["to_check_remaining"], 7)
        self.assertEqual(progress["to_check_total"], 20)
        self.assertEqual(progress["item_percent"], 35)
        self.assertIn("35% of known items left", progress["detail"])
        self.assertFalse(progress["indeterminate"])

    def test_running_job_without_numbers_is_indeterminate(self) -> None:
        job = Job(
            id="job-one",
            disk_id="offsite-a",
            disk_name="Offsite A",
            dry_run=False,
            started_at=0,
            current_source_index=1,
            total_sources=2,
        )

        progress = job.to_dict()["progress"]

        self.assertIsNone(progress["bar_percent"])
        self.assertTrue(progress["indeterminate"])
        self.assertEqual(progress["label"], "Running source 1 of 2")


def config_for(source: Path) -> dict:
    return {
        "rsync_path": "/usr/bin/rsync",
        "rsync_options": ["-aE", "--delete"],
        "exclude_patterns": [],
        "sources": [
            {
                "id": "source-one",
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
