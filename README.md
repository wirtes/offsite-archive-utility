# Offsite Archive Utility

A small local web UI for rotating offsite backup disks on macOS. Configure one or more source directories or volumes, configure several backup disks, plug in a disk, then kick off `rsync` from the browser.

## Run

```bash
python3 app.py
```

Open http://127.0.0.1:8585 on the backup Mac, or use `http://<mac-lan-ip>:8585` from another device on the same local network.

The first run creates `config.json` next to `app.py`. You can edit it directly or use the Configuration section in the web UI. The app also keeps `backup_history.json` next to the config so the Activity timeline persists across restarts. Both files are ignored by git because they contain machine-specific paths/history; use `config.example.json` as the shareable template.

## How It Backs Up

Each enabled source is synchronized into its own destination folder:

```text
/Volumes/Offsite-A/Backups/This-Mac/<source-id>/
```

That means each rotating disk can hold a complete copy of all configured sources without different source directories merging into each other.

The default run button starts in dry-run mode. Uncheck **Dry run** when you are ready to write changes to the disk.

## Config Fields

- `rsync_path`: Path to the `rsync` executable, usually `/usr/bin/rsync`.
- `rsync_options`: Arguments passed to every rsync run. Keep `--delete` out of this global list; use the per-source Delete checkbox instead.
- `exclude_patterns`: Patterns passed as `--exclude`.
- `sources`: Directories or mounted volumes to copy. Each source `id` is used as the subdirectory on each backup disk. Set `delete` to `true` only when rsync should delete destination files for that source.
- `backup_disks`: Rotating disks with mount paths and destination subdirectories. Disk `id` is the stable internal key; disk `name` is the human-friendly display name.

## Notes

- Keep disk `mount_path` values aligned with the volume names macOS shows under `/Volumes`.
- When backing up an entire mounted volume, exclude macOS metadata folders such as `.Spotlight-V100/`, `.Trashes/`, `.DocumentRevisions-V100/`, `.TemporaryItems/`, and `.fseventsd/`, plus AppleDouble sidecar files like `._*`. They are system-managed and often produce permission warnings.
- The app binds to `0.0.0.0` by default, so it is reachable from other devices on your local network if macOS Firewall allows incoming connections.
- To restrict it to the backup Mac only, run `python3 app.py --host 127.0.0.1`.
- Only one backup job runs at a time.
- The per-source Delete checkbox enables `--delete` only for that source. It defaults off for new sources.
