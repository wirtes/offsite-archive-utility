import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app import BackupState, Job, build_rsync_commands, default_config, load_config, public_state, render_disk_card, render_job_card, render_page, run_job, update_job_progress_from_line


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

    def test_rsync_command_removes_appledouble_generating_e_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            mount = root / "BackupDisk"
            source.mkdir()
            mount.mkdir()

            command = build_rsync_commands(config_for(source), disk_for(mount), dry_run=True)[0]

            self.assertIn("-a", command)
            self.assertNotIn("-aE", command)
            self.assertNotIn("-E", command)

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

    def test_load_config_removes_appledouble_generating_e_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config = config_for(Path(tmp) / "source")
            config["rsync_options"] = ["-aE", "-E", "-vE", "--delete"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            loaded = load_config(config_path)
            saved = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(["-a", "-v"], loaded["rsync_options"])
            self.assertEqual(["-a", "-v"], saved["rsync_options"])

    def test_sources_table_labels_id_as_backup_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = BackupState(Path(tmp) / "config.json")

            html = render_page(state)

            self.assertIn("Subdirectory on backup disk", html)
            self.assertIn('placeholder="backup-subdirectory"', html)
            self.assertIn("<th>Delete</th>", html)
            self.assertIn('name="source_delete_0"', html)
            self.assertNotIn("<th>Label</th>", html)
            self.assertIn("ID is the stable internal key", html)

    def test_source_delete_defaults_to_off(self) -> None:
        config = default_config()

        self.assertFalse(config["sources"][0]["delete"])

    def test_render_page_shows_only_one_previous_job_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = BackupState(Path(tmp) / "config.json")
            state.jobs["oldest"] = job_for("oldest", "Oldest Job", 100)
            state.jobs["middle"] = job_for("middle", "Middle Job", 200)
            state.jobs["newest"] = job_for("newest", "Newest Job", 300)

            html = render_page(state)

            self.assertIn('data-job-id="newest"', html)
            self.assertNotIn('data-job-id="middle"', html)
            self.assertNotIn('data-job-id="oldest"', html)

    def test_render_page_includes_backup_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = BackupState(Path(tmp) / "config.json")
            state.config["backup_disks"] = [
                {
                    "id": "offsite-a",
                    "name": "Offsite Disk A",
                    "mount_path": "/Volumes/Offsite-A",
                    "destination_subdir": "Backups/This-Mac",
                },
                {
                    "id": "offsite-b",
                    "name": "Offsite Disk B",
                    "mount_path": "/Volumes/Offsite-B",
                    "destination_subdir": "Backups/This-Mac",
                },
            ]
            state.jobs["a"] = job_for("a", "Offsite Disk A", 100, disk_id="offsite-a")
            state.jobs["b"] = job_for("b", "Offsite Disk B", 200, disk_id="offsite-b")

            html = render_page(state)

            self.assertIn('id="backup-timeline"', html)
            self.assertIn("Backup timeline", html)
            self.assertIn("Offsite Disk A", html)
            self.assertIn("Offsite Disk B", html)

    def test_public_state_shows_running_job_and_one_previous_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = BackupState(Path(tmp) / "config.json")
            state.jobs["old"] = job_for("old", "Old Job", 100)
            state.jobs["previous"] = job_for("previous", "Previous Job", 200)
            active = job_for("active", "Active Job", 300, status="running")
            state.jobs["active"] = active
            state.active_job_id = "active"

            payload = public_state(state)

            self.assertEqual(["Active Job", "Previous Job"], [job["disk_name"] for job in payload["jobs"]])

    def test_public_state_timeline_uses_full_job_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = BackupState(Path(tmp) / "config.json")
            state.config["backup_disks"] = [
                {
                    "id": "offsite-a",
                    "name": "Offsite Disk A",
                    "mount_path": "/Volumes/Offsite-A",
                    "destination_subdir": "Backups/This-Mac",
                }
            ]
            state.jobs["old"] = job_for("old", "Offsite Disk A", 100, disk_id="offsite-a")
            state.jobs["middle"] = job_for("middle", "Offsite Disk A", 200, disk_id="offsite-a")
            state.jobs["new"] = job_for("new", "Offsite Disk A", 300, disk_id="offsite-a")

            payload = public_state(state)

            self.assertEqual(1, len(payload["jobs"]))
            self.assertEqual(3, len(payload["timeline"]["rows"][0]["events"]))

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

    def test_delete_flag_is_only_added_for_sources_that_enable_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            mount = root / "BackupDisk"
            source.mkdir()
            mount.mkdir()
            config = config_for(source)
            config["rsync_options"] = ["-a", "--delete"]

            command = build_rsync_commands(config, disk_for(mount), dry_run=True)[0]

            self.assertNotIn("--delete", command)

            config["sources"][0]["delete"] = True
            command = build_rsync_commands(config, disk_for(mount), dry_run=True)[0]

            self.assertIn("--delete", command)
            self.assertLess(command.index("--delete"), len(command) - 2)

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

    def test_running_job_highlights_active_command(self) -> None:
        job = Job(
            id="job-one",
            disk_id="offsite-a",
            disk_name="Offsite A",
            dry_run=False,
            started_at=0,
            status="running",
            commands=[
                ["/usr/bin/rsync", "/source-one/", "/dest-one/"],
                ["/usr/bin/rsync", "/source-two/", "/dest-two/"],
            ],
            current_source_index=2,
            total_sources=2,
        )

        payload = job.to_dict()
        html = render_job_card(job)

        self.assertEqual(1, payload["active_command_index"])
        self.assertIn('class="command-line active"', html)
        self.assertIn("/source-two/", html)

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
            history = json.loads((Path(tmp) / "backup_history.json").read_text(encoding="utf-8"))
            self.assertEqual("job-one", history["jobs"][0]["id"])

    def test_timeline_history_persists_across_state_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            state = BackupState(config_path)
            state.config["backup_disks"] = [
                {
                    "id": "offsite-a",
                    "name": "Offsite Disk A",
                    "mount_path": "/Volumes/Offsite-A",
                    "destination_subdir": "Backups/This-Mac",
                }
            ]
            job = job_for("historic", "Offsite Disk A", 100, disk_id="offsite-a")
            job.ended_at = 120
            state.record_job_history_locked(job)

            restored_state = BackupState(config_path)
            restored_state.config["backup_disks"] = state.config["backup_disks"]
            payload = public_state(restored_state)

            self.assertEqual("historic", payload["timeline"]["rows"][0]["events"][0]["id"])

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
        self.assertEqual("37.43MB/s · 7 of 20 items left", progress["meta"])
        self.assertNotIn("Transferred", progress["meta"])
        self.assertNotIn("files transferred", progress["meta"])
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
                "delete": False,
            }
        ],
        "backup_disks": [],
    }


def job_for(job_id: str, disk_name: str, started_at: float, status: str = "completed", disk_id: str | None = None) -> Job:
    return Job(
        id=job_id,
        disk_id=disk_id or f"disk-{job_id}",
        disk_name=disk_name,
        dry_run=False,
        started_at=started_at,
        status=status,
    )


def disk_for(mount: Path) -> dict:
    return {
        "id": "offsite-a",
        "name": "Offsite A",
        "mount_path": str(mount),
        "destination_subdir": "Backups/This-Mac",
    }


if __name__ == "__main__":
    unittest.main()
