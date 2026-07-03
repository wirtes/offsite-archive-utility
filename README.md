# Offsite Archive Utility

A small local web UI for rotating offsite backup disks on macOS. Configure one or more source directories or volumes, configure several backup disks, plug in a disk, then kick off `rsync` from the browser.

## Run

```bash
python3 app.py
```

Open http://127.0.0.1:8585.

The first run creates `config.json` next to `app.py`. You can edit it directly or use the Configuration section in the web UI.

## How It Backs Up

Each enabled source is synchronized into its own destination folder:

```text
/Volumes/Offsite-A/Backups/This-Mac/<source-id>/
```

That means each rotating disk can hold a complete copy of all configured sources without different source directories merging into each other.

The default run button starts in dry-run mode. Uncheck **Dry run** when you are ready to write changes to the disk.

## Config Fields

- `rsync_path`: Path to the `rsync` executable, usually `/usr/bin/rsync`.
- `rsync_options`: Arguments passed to every rsync run.
- `exclude_patterns`: Patterns passed as `--exclude`.
- `sources`: Directories or mounted volumes to copy.
- `backup_disks`: Rotating disks with mount paths and destination subdirectories.

## Notes

- Keep disk `mount_path` values aligned with the volume names macOS shows under `/Volumes`.
- The app binds to `127.0.0.1` by default, so it is only reachable from the local machine.
- Only one backup job runs at a time.
