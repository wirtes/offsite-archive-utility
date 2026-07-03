#!/usr/bin/env python3
"""Local web UI for rotating offsite rsync backups on macOS."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
RSYNC_VERSION = "--version"
RSYNC_PROGRESS_RE = re.compile(
    r"^\s*(?P<transferred>[\d.,]+[A-Za-z]*)\s+"
    r"(?P<percent>\d+)%\s+"
    r"(?P<speed>\S+/s)\s+"
    r"(?P<eta>\S+)"
    r"(?:\s+\(xfer#(?P<xfer>\d+),\s+to-check=(?P<remaining>\d+)/(?P<total>\d+)\))?"
)


@dataclass
class Job:
    id: str
    disk_id: str
    disk_name: str
    dry_run: bool
    started_at: float
    status: str = "running"
    returncode: int | None = None
    commands: list[list[str]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    ended_at: float | None = None
    current_source_index: int = 0
    total_sources: int = 0
    current_item: str = ""
    current_file_percent: int | None = None
    transferred: str = ""
    speed: str = ""
    eta: str = ""
    xfer_count: int | None = None
    to_check_remaining: int | None = None
    to_check_total: int | None = None

    def to_dict(self) -> dict[str, Any]:
        item_percent = item_progress_percent(self)
        bar_percent = progress_bar_percent(self, item_percent)
        return {
            "id": self.id,
            "disk_id": self.disk_id,
            "disk_name": self.disk_name,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "started_at_label": timestamp(self.started_at),
            "ended_at": self.ended_at,
            "ended_at_label": timestamp(self.ended_at) if self.ended_at else "",
            "status": self.status,
            "returncode": self.returncode,
            "commands": self.commands,
            "command": self.commands[0] if self.commands else [],
            "command_label": "\n".join(format_command(command) for command in self.commands),
            "progress": {
                "current_source_index": self.current_source_index,
                "total_sources": self.total_sources,
                "current_item": self.current_item,
                "current_file_percent": self.current_file_percent,
                "transferred": self.transferred,
                "speed": self.speed,
                "eta": self.eta,
                "xfer_count": self.xfer_count,
                "to_check_remaining": self.to_check_remaining,
                "to_check_total": self.to_check_total,
                "item_percent": item_percent,
                "bar_percent": bar_percent,
                "indeterminate": bar_percent is None and self.status == "running",
                "label": progress_label(self, item_percent),
                "detail": progress_detail(self, item_percent),
                "meta": progress_meta(self, item_percent),
            },
            "log": self.log[-500:],
        }


class BackupState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.lock = threading.RLock()
        self.config = load_config(config_path)
        self.jobs: dict[str, Job] = {}
        self.active_job_id: str | None = None

    def save_config(self, config: dict[str, Any]) -> None:
        validate_config(config)
        with self.lock:
            self.config = config
            self.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def start_job(self, disk_id: str, dry_run: bool) -> Job:
        with self.lock:
            if self.active_job_id and self.jobs[self.active_job_id].status == "running":
                raise ValueError("A backup is already running.")

            config = self.config
            disk = find_disk(config, disk_id)
            if not disk:
                raise ValueError("Unknown backup disk.")
            if not disk_is_available(disk):
                raise ValueError("That backup disk is not mounted at its configured path.")

            commands = build_rsync_commands(config, disk, dry_run)
            job = Job(
                id=str(uuid.uuid4()),
                disk_id=disk["id"],
                disk_name=disk["name"],
                dry_run=dry_run,
                started_at=time.time(),
                commands=commands,
                total_sources=len(commands),
            )
            job.log.append(f"[{timestamp()}] Starting backup to {disk['name']}")
            for command in commands:
                job.log.append("$ " + format_command(command))
            self.jobs[job.id] = job
            self.active_job_id = job.id

        thread = threading.Thread(target=run_job, args=(self, job.id), daemon=True)
        thread.start()
        return job


def timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(value or time.time()).strftime("%Y-%m-%d %H:%M:%S")


def format_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def item_progress_percent(job: Job) -> int | None:
    if job.to_check_remaining is None or not job.to_check_total:
        return None
    completed = max(job.to_check_total - job.to_check_remaining, 0)
    return min(100, max(0, round((completed / job.to_check_total) * 100)))


def progress_label(job: Job, item_percent: int | None = None) -> str:
    if job.status == "completed":
        return "Backup completed"
    if job.status == "failed":
        return "Backup failed"
    if job.current_file_percent is not None and job.current_item:
        return f"Copying {job.current_item} ({job.current_file_percent}%)"
    if job.current_item:
        return f"Scanning {job.current_item}"
    if job.current_source_index and job.total_sources:
        return f"Running source {job.current_source_index} of {job.total_sources}"
    return "Preparing backup"


def update_job_progress_from_line(job: Job, line: str) -> None:
    progress_match = RSYNC_PROGRESS_RE.match(line)
    if progress_match:
        job.current_file_percent = int(progress_match.group("percent"))
        job.transferred = progress_match.group("transferred")
        job.speed = progress_match.group("speed")
        job.eta = progress_match.group("eta")
        if progress_match.group("xfer"):
            job.xfer_count = int(progress_match.group("xfer"))
        if progress_match.group("remaining") and progress_match.group("total"):
            job.to_check_remaining = int(progress_match.group("remaining"))
            job.to_check_total = int(progress_match.group("total"))
        return

    candidate = line.strip()
    if is_progress_item_line(candidate):
        job.current_item = candidate
        job.current_file_percent = None


def is_progress_item_line(line: str) -> bool:
    if not line or line.startswith("[") or line.startswith("$ "):
        return False
    if line.startswith("rsync(") or line.startswith("warning:"):
        return False
    if "Operation not permitted" in line or "codec can't decode" in line:
        return False
    return line not in {"./", "."}


def default_config() -> dict[str, Any]:
    return {
        "rsync_path": "/usr/bin/rsync",
        "rsync_options": ["-aE", "--delete", "--progress", "--human-readable"],
        "exclude_patterns": [
            ".DS_Store",
            ".Trash/",
            ".Trashes/",
            ".Spotlight-V100/",
            ".DocumentRevisions-V100/",
            ".TemporaryItems/",
            ".fseventsd/",
            "node_modules/",
            ".git/",
        ],
        "sources": [
            {
                "id": "home-documents",
                "label": "Documents",
                "path": str(Path.home() / "Documents"),
                "enabled": True,
            }
        ],
        "backup_disks": [
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
            {
                "id": "offsite-c",
                "name": "Offsite Disk C",
                "mount_path": "/Volumes/Offsite-C",
                "destination_subdir": "Backups/This-Mac",
            },
        ],
    }


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        config = default_config()
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return config

    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object.")
    for key in ("sources", "backup_disks"):
        if not isinstance(config.get(key), list):
            raise ValueError(f"Config field '{key}' must be a list.")

    source_ids = set()
    for source in config["sources"]:
        require_fields(source, ("id", "label", "path"), "source")
        validate_path_segment(source["id"], "source id")
        if source["id"] in source_ids:
            raise ValueError(f"Duplicate source id: {source['id']}")
        source_ids.add(source["id"])

    disk_ids = set()
    for disk in config["backup_disks"]:
        require_fields(disk, ("id", "name", "mount_path", "destination_subdir"), "backup disk")
        validate_path_segment(disk["id"], "backup disk id")
        validate_relative_path(disk["destination_subdir"], "destination_subdir")
        if disk["id"] in disk_ids:
            raise ValueError(f"Duplicate backup disk id: {disk['id']}")
        disk_ids.add(disk["id"])


def require_fields(item: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"Each {label} must be an object.")
    for field_name in fields:
        if not str(item.get(field_name, "")).strip():
            raise ValueError(f"Each {label} needs a non-empty '{field_name}'.")


def validate_path_segment(value: str, label: str) -> None:
    path = Path(str(value))
    if path.is_absolute() or "/" in str(value) or str(value) in {".", ".."}:
        raise ValueError(f"{label} must be a simple name without slashes.")


def validate_relative_path(value: str, label: str) -> None:
    path = Path(str(value))
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a relative path without '.' or '..' segments.")


def find_disk(config: dict[str, Any], disk_id: str) -> dict[str, Any] | None:
    return next((disk for disk in config["backup_disks"] if disk["id"] == disk_id), None)


def disk_is_available(disk: dict[str, Any]) -> bool:
    path = Path(disk["mount_path"])
    return path.exists() and path.is_dir()


def source_status(source: dict[str, Any]) -> dict[str, Any]:
    path = Path(source["path"]).expanduser()
    return {
        **source,
        "exists": path.exists(),
        "enabled": bool(source.get("enabled", True)),
    }


def disk_status(disk: dict[str, Any]) -> dict[str, Any]:
    mount_path = Path(disk["mount_path"])
    destination = mount_path / disk["destination_subdir"]
    return {
        **disk,
        "available": disk_is_available(disk),
        "destination": str(destination),
        "destination_exists": destination.exists(),
    }


def build_rsync_commands(config: dict[str, Any], disk: dict[str, Any], dry_run: bool) -> list[list[str]]:
    rsync_path = str(config.get("rsync_path") or "/usr/bin/rsync")
    options = [str(option) for option in config.get("rsync_options", [])]
    if dry_run and "--dry-run" not in options and "-n" not in options:
        options.append("--dry-run")

    base_command = [rsync_path, *options]
    for pattern in config.get("exclude_patterns", []):
        if str(pattern).strip():
            base_command.extend(["--exclude", str(pattern)])

    commands: list[list[str]] = []
    destination_root = Path(disk["mount_path"]) / disk["destination_subdir"]
    for source in config["sources"]:
        if not source.get("enabled", True):
            continue
        path = Path(source["path"]).expanduser()
        if not path.exists():
            raise ValueError(f"Source does not exist: {path}")
        source_destination = destination_root / source["id"]
        if not dry_run:
            source_destination.mkdir(parents=True, exist_ok=True)
        commands.append([*base_command, str(path) + "/", str(source_destination) + "/"])
    if not commands:
        raise ValueError("No enabled sources are configured.")
    return commands


def run_job(state: BackupState, job_id: str) -> None:
    with state.lock:
        job = state.jobs[job_id]
        commands = job.commands

    try:
        failed_returncode = 0
        for index, command in enumerate(commands, start=1):
            with state.lock:
                job = state.jobs[job_id]
                job.current_source_index = index
                job.total_sources = len(commands)
                job.current_item = ""
                job.current_file_percent = None
                job.transferred = ""
                job.speed = ""
                job.eta = ""
                job.log.append(f"[{timestamp()}] Source {index} of {len(commands)}")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                with state.lock:
                    job = state.jobs[job_id]
                    clean_line = line.rstrip()
                    update_job_progress_from_line(job, clean_line)
                    job.log.append(clean_line)
            returncode = process.wait()
            if returncode != 0:
                failed_returncode = returncode
                break
        with state.lock:
            job = state.jobs[job_id]
            job.returncode = failed_returncode
            job.status = "completed" if failed_returncode == 0 else "failed"
            if job.status == "completed":
                job.current_file_percent = 100
            job.ended_at = time.time()
            job.log.append(f"[{timestamp()}] rsync exited with code {failed_returncode}")
            state.active_job_id = None
    except Exception as exc:  # noqa: BLE001 - user-facing local tool
        with state.lock:
            job = state.jobs[job_id]
            job.returncode = -1
            job.status = "failed"
            job.ended_at = time.time()
            job.log.append(f"[{timestamp()}] Error: {exc}")
            state.active_job_id = None


def parse_config_form(body: bytes) -> dict[str, Any]:
    data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    config = default_config()
    config["rsync_path"] = data.get("rsync_path", ["/usr/bin/rsync"])[0].strip() or "/usr/bin/rsync"
    config["rsync_options"] = split_lines_or_args(data.get("rsync_options", [""])[0])
    config["exclude_patterns"] = split_nonempty_lines(data.get("exclude_patterns", [""])[0])
    config["sources"] = parse_source_rows(data)
    config["backup_disks"] = parse_rows(data, "disk", ("id", "name", "mount_path", "destination_subdir"))
    return config


def split_lines_or_args(value: str) -> list[str]:
    lines = split_nonempty_lines(value)
    if len(lines) > 1:
        return lines
    return shlex.split(value) if value.strip() else []


def split_nonempty_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def parse_rows(data: dict[str, list[str]], prefix: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    ids = data.get(f"{prefix}_id", [])
    rows: list[dict[str, Any]] = []
    for index in range(len(ids)):
        row: dict[str, Any] = {}
        for field_name in fields:
            values = data.get(f"{prefix}_{field_name}", [])
            value = values[index] if index < len(values) else ""
            if field_name == "enabled":
                row[field_name] = value == "on"
            else:
                row[field_name] = value.strip()
        if any(str(row.get(field_name, "")).strip() for field_name in fields if field_name != "enabled"):
            rows.append(row)
    return rows


def parse_source_rows(data: dict[str, list[str]]) -> list[dict[str, Any]]:
    ids = data.get("source_id", [])
    rows: list[dict[str, Any]] = []
    for index in range(len(ids)):
        row = {
            "id": ids[index].strip(),
            "label": data.get("source_label", [""] * len(ids))[index].strip(),
            "path": data.get("source_path", [""] * len(ids))[index].strip(),
            "enabled": data.get(f"source_enabled_{index}", ["off"])[0] == "on",
        }
        if any(str(row.get(field_name, "")).strip() for field_name in ("id", "label", "path")):
            rows.append(row)
    return rows


class BackupRequestHandler(BaseHTTPRequestHandler):
    state: BackupState

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(render_page(self.state))
        elif parsed.path == "/favicon.svg":
            self.send_svg(FAVICON_SVG)
        elif parsed.path == "/api/state":
            self.send_json(public_state(self.state))
        elif parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with self.state.lock:
                job = self.state.jobs.get(job_id)
            if not job:
                self.send_error_json("Job not found.", HTTPStatus.NOT_FOUND)
                return
            self.send_json(job.to_dict())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if parsed.path == "/start":
            data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            disk_id = data.get("disk_id", [""])[0]
            dry_run = data.get("dry_run", ["off"])[0] == "on"
            try:
                job = self.state.start_job(disk_id, dry_run)
                self.redirect(f"/?job={job.id}")
            except ValueError as exc:
                self.send_html(render_page(self.state, error=str(exc)), HTTPStatus.BAD_REQUEST)
        elif parsed.path == "/config":
            try:
                config = parse_config_form(body)
                self.state.save_config(config)
                self.redirect("/?saved=1")
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_html(render_page(self.state, error=str(exc)), HTTPStatus.BAD_REQUEST)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def send_html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_svg(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_error_json(self, message: str, status: HTTPStatus) -> None:
        self.send_json({"error": message}, status)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, format_: str, *args: Any) -> None:
        print(f"[{timestamp()}] {self.address_string()} {format_ % args}")


def public_state(state: BackupState) -> dict[str, Any]:
    with state.lock:
        config = state.config
        jobs = [job.to_dict() for job in state.jobs.values()]
        active_job = state.jobs.get(state.active_job_id) if state.active_job_id else None
    return {
        "sources": [source_status(source) for source in config["sources"]],
        "backup_disks": [disk_status(disk) for disk in config["backup_disks"]],
        "jobs": sorted(jobs, key=lambda item: item["started_at"], reverse=True),
        "active_job": active_job.to_dict() if active_job else None,
    }


def render_page(state: BackupState, error: str = "") -> str:
    with state.lock:
        config = json.loads(json.dumps(state.config))
        jobs = sorted(state.jobs.values(), key=lambda job: job.started_at, reverse=True)
        active_job = state.jobs.get(state.active_job_id) if state.active_job_id else None

    sources = [source_status(source) for source in config["sources"]]
    disks = [disk_status(disk) for disk in config["backup_disks"]]
    source_rows = "\n".join(render_source_row(source, index) for index, source in enumerate(sources))
    disk_rows = "\n".join(render_disk_row(disk) for disk in disks)
    disk_cards = "\n".join(render_disk_card(disk, bool(active_job)) for disk in disks)
    job_cards = "\n".join(render_job_card(job) for job in jobs[:10]) or "<p class='muted'>No backup jobs yet.</p>"
    active_job_id = active_job.id if active_job else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <title>Offsite Archive Utility</title>
  <style>{CSS}</style>
</head>
<body data-active-job="{escape(active_job_id)}">
  <header>
    <div>
      <h1>Offsite Archive Utility</h1>
      <p>Rotate backup disks, plug one in, and synchronize the configured sources with rsync.</p>
    </div>
    <div class="status-pill">{len(sources)} sources · {len(disks)} disks</div>
  </header>

  <main>
    {f"<section class='alert'>{escape(error)}</section>" if error else ""}

    <section class="panel">
      <div class="section-heading">
        <h2>Run Backup</h2>
        <span class="muted">Mounted disks are ready to run.</span>
      </div>
      <div class="disk-grid">{disk_cards}</div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <h2>Activity</h2>
        <span id="refresh-status" class="muted">Updates every 2 seconds</span>
      </div>
      <div id="jobs">{job_cards}</div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <h2>Configuration</h2>
        <span class="muted">Saved in {escape(str(state.config_path))}</span>
      </div>
      <form method="post" action="/config">
        <label>
          rsync path
          <input name="rsync_path" value="{escape(config.get("rsync_path", "/usr/bin/rsync"))}">
        </label>

        <label>
          rsync options
          <textarea name="rsync_options" rows="3">{escape(" ".join(config.get("rsync_options", [])))}</textarea>
        </label>

        <label>
          exclude patterns
          <textarea name="exclude_patterns" rows="4">{escape(chr(10).join(config.get("exclude_patterns", [])))}</textarea>
        </label>

        <h3>Sources</h3>
        <div class="table-wrap">
          <table id="sources-table">
            <thead><tr><th>Enabled</th><th>ID</th><th>Label</th><th>Path</th><th></th></tr></thead>
            <tbody>{source_rows}</tbody>
          </table>
        </div>
        <button type="button" class="secondary" data-add-row="source">Add source</button>

        <h3>Backup disks</h3>
        <div class="table-wrap">
          <table id="disks-table">
            <thead><tr><th>ID</th><th>Name</th><th>Mount path</th><th>Destination subdir</th><th></th></tr></thead>
            <tbody>{disk_rows}</tbody>
          </table>
        </div>
        <button type="button" class="secondary" data-add-row="disk">Add disk</button>

        <div class="actions">
          <button type="submit">Save configuration</button>
        </div>
      </form>
    </section>
  </main>
  <script>{JS}</script>
</body>
</html>"""


def render_source_row(source: dict[str, Any], index: int) -> str:
    checked = "checked" if source.get("enabled", True) else ""
    badge = "<span class='ok'>Found</span>" if source["exists"] else "<span class='bad'>Missing</span>"
    return f"""<tr>
  <td><input type="checkbox" name="source_enabled_{index}" {checked}></td>
  <td><input name="source_id" value="{escape(source["id"])}"></td>
  <td><input name="source_label" value="{escape(source["label"])}"></td>
  <td><input name="source_path" value="{escape(source["path"])}"><div class="row-note">{badge}</div></td>
  <td><button type="button" class="icon" data-remove-row>Remove</button></td>
</tr>"""


def render_disk_row(disk: dict[str, Any]) -> str:
    badge = "<span class='ok'>Mounted</span>" if disk["available"] else "<span class='bad'>Not mounted</span>"
    return f"""<tr>
  <td><input name="disk_id" value="{escape(disk["id"])}"></td>
  <td><input name="disk_name" value="{escape(disk["name"])}"></td>
  <td><input name="disk_mount_path" value="{escape(disk["mount_path"])}"><div class="row-note">{badge}</div></td>
  <td><input name="disk_destination_subdir" value="{escape(disk["destination_subdir"])}"></td>
  <td><button type="button" class="icon" data-remove-row>Remove</button></td>
</tr>"""


def render_disk_card(disk: dict[str, Any], running: bool) -> str:
    disabled = "disabled" if running or not disk["available"] else ""
    status_class = "ok" if disk["available"] else "bad"
    status_text = "Mounted" if disk["available"] else "Not mounted"
    return f"""<article class="disk-card" data-disk-id="{escape(disk["id"])}" data-available="{str(disk["available"]).lower()}">
  <div>
    <h3>{escape(disk["name"])}</h3>
    <p>{escape(disk["destination"])}</p>
    <span class="{status_class}">{status_text}</span>
  </div>
  <form method="post" action="/start">
    <input type="hidden" name="disk_id" value="{escape(disk["id"])}">
    <label class="check"><input type="checkbox" name="dry_run"> Dry run</label>
    <button type="submit" {disabled}>Run rsync</button>
  </form>
</article>"""


def render_job_card(job: Job) -> str:
    log = escape("\n".join(job.log[-80:]))
    dry = "dry run" if job.dry_run else "live"
    item_percent = item_progress_percent(job)
    bar_percent = progress_bar_percent(job, item_percent)
    bar_width = 100 if bar_percent is None else bar_percent
    return f"""<article class="job" data-job-id="{escape(job.id)}">
  <div class="job-head">
    <div><strong>{escape(job.disk_name)}</strong><span>{escape(timestamp(job.started_at))} · {dry}</span></div>
    <span class="job-status {escape(job.status)}">{escape(job.status)}</span>
  </div>
  <div class="progress-panel">
    <div class="progress-top">
      <strong>{escape(progress_label(job, item_percent))}</strong>
      <span>{escape(progress_detail(job, item_percent))}</span>
    </div>
    <div class="progress-track" aria-label="Backup progress">
      <div class="progress-fill {escape("indeterminate" if bar_percent is None and job.status == "running" else "")}" style="width: {bar_width}%"></div>
    </div>
    <div class="progress-meta">{escape(progress_meta(job, item_percent))}</div>
  </div>
  <code>{escape(chr(10).join(format_command(command) for command in job.commands))}</code>
  <pre>{log}</pre>
</article>"""


def progress_bar_percent(job: Job, item_percent: int | None) -> int | None:
    if job.status == "completed":
        return 100
    if job.status == "failed":
        if item_percent is not None:
            return item_percent
        if job.current_file_percent is not None:
            return job.current_file_percent
        return 100
    if item_percent is not None:
        return item_percent
    return job.current_file_percent


def progress_detail(job: Job, item_percent: int | None) -> str:
    parts = []
    if job.current_source_index and job.total_sources:
        parts.append(f"Source {job.current_source_index} of {job.total_sources}")
    if item_percent is not None:
        parts.append(f"{item_percent}% of known items")
    elif job.current_file_percent is not None:
        parts.append(f"{job.current_file_percent}% of current file")
    return " · ".join(parts) or "Waiting for rsync"


def progress_meta(job: Job, item_percent: int | None) -> str:
    parts = []
    if job.transferred:
        parts.append(f"Transferred {job.transferred}")
    if job.speed:
        parts.append(job.speed)
    if job.eta:
        parts.append(f"ETA {job.eta}")
    if job.xfer_count is not None:
        parts.append(f"{job.xfer_count} files transferred")
    if item_percent is not None and job.to_check_remaining is not None and job.to_check_total is not None:
        parts.append(f"{job.to_check_remaining} of {job.to_check_total} items left")
    return " · ".join(parts) or "rsync is starting up"


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


CSS = """
:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #17202a;
  --muted: #687383;
  --line: #d9dee7;
  --accent: #176d6a;
  --accent-dark: #0e4f4d;
  --danger: #b42318;
  --ok: #16703f;
  --shadow: 0 1px 2px rgba(20, 32, 44, 0.06);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); }
header {
  display: flex; align-items: center; justify-content: space-between; gap: 24px;
  padding: 28px clamp(18px, 4vw, 48px); border-bottom: 1px solid var(--line); background: #fff;
}
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 6px; font-size: 28px; }
h2 { margin-bottom: 0; font-size: 20px; }
h3 { margin: 24px 0 10px; font-size: 16px; }
header p, .muted { color: var(--muted); }
main { width: min(1180px, calc(100vw - 32px)); margin: 24px auto 56px; }
.panel {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 20px; margin-bottom: 18px; box-shadow: var(--shadow);
}
.section-heading {
  display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 18px;
}
.status-pill, .ok, .bad, .job-status {
  display: inline-flex; align-items: center; min-height: 26px; border-radius: 999px;
  padding: 3px 10px; font-size: 13px; font-weight: 650;
}
.status-pill { background: #eef3f7; color: #324255; white-space: nowrap; }
.ok { background: #e8f5ee; color: var(--ok); }
.bad { background: #fff0ef; color: var(--danger); }
.alert {
  border: 1px solid #f0b8b4; background: #fff5f4; color: var(--danger);
  border-radius: 8px; padding: 12px 14px; margin-bottom: 18px;
}
.disk-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.disk-card {
  border: 1px solid var(--line); border-radius: 8px; padding: 16px;
  display: flex; flex-direction: column; justify-content: space-between; gap: 16px;
}
.disk-card h3 { margin: 0 0 6px; }
.disk-card p { color: var(--muted); overflow-wrap: anywhere; margin-bottom: 10px; }
.disk-card form { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.check { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); }
label { display: block; margin-bottom: 14px; font-weight: 650; }
input, textarea {
  width: 100%; margin-top: 6px; padding: 9px 10px; border: 1px solid var(--line);
  border-radius: 6px; font: inherit; color: var(--ink); background: #fff;
}
textarea { resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
input[type="checkbox"] { width: 18px; height: 18px; margin: 0; }
button {
  border: 0; border-radius: 6px; padding: 9px 13px; background: var(--accent);
  color: #fff; font-weight: 700; cursor: pointer; min-height: 38px;
}
button:hover { background: var(--accent-dark); }
button:disabled { background: #a8b4c0; cursor: not-allowed; }
button.secondary, button.icon { background: #edf2f5; color: #263442; }
button.secondary:hover, button.icon:hover { background: #dfe8ee; }
button.icon { min-height: 34px; padding: 7px 10px; }
.actions { margin-top: 18px; display: flex; justify-content: flex-end; }
.table-wrap { width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; min-width: 760px; }
th, td { padding: 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
tr:last-child td { border-bottom: 0; }
th { color: var(--muted); font-size: 13px; }
.row-note { margin-top: 7px; }
.job { border: 1px solid var(--line); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.job-head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.job-head span { display: block; color: var(--muted); font-size: 13px; margin-top: 2px; }
.job-status.running { background: #eef3f7; color: #2b5872; }
.job-status.completed { background: #e8f5ee; color: var(--ok); }
.job-status.failed { background: #fff0ef; color: var(--danger); }
.progress-panel {
  background: #f8fafb; border: 1px solid var(--line); border-radius: 8px;
  padding: 12px; margin-bottom: 10px;
}
.progress-top {
  display: flex; align-items: baseline; justify-content: space-between; gap: 14px;
  margin-bottom: 9px;
}
.progress-top strong { overflow-wrap: anywhere; }
.progress-top span, .progress-meta { color: var(--muted); font-size: 13px; }
.progress-track {
  height: 12px; border-radius: 999px; background: #dce5ea; overflow: hidden;
  box-shadow: inset 0 0 0 1px rgba(22, 35, 45, 0.05);
}
.progress-fill {
  height: 100%; min-width: 8px; border-radius: inherit; background: var(--accent);
  transition: width 220ms ease;
}
.progress-fill.indeterminate {
  width: 100%; min-width: 100%;
  background: linear-gradient(90deg, #dce5ea 0%, var(--accent) 35%, #dce5ea 70%);
  background-size: 220% 100%;
  animation: progress-scan 1.25s linear infinite;
}
.progress-meta { margin-top: 8px; }
@keyframes progress-scan {
  from { background-position: 140% 0; }
  to { background-position: -80% 0; }
}
code {
  display: block; background: #f3f6f8; border-radius: 6px; padding: 9px 10px;
  margin-bottom: 10px; overflow-x: auto; white-space: pre;
}
pre {
  background: #101820; color: #edf5f7; border-radius: 6px; padding: 12px;
  overflow: auto; max-height: 340px; margin: 0; line-height: 1.45;
}
@media (max-width: 720px) {
  header, .section-heading, .job-head { align-items: flex-start; flex-direction: column; }
  .disk-card form { align-items: stretch; flex-direction: column; }
  button { width: 100%; }
}
"""


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="bg" x1="10" y1="6" x2="54" y2="58" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#22313f"/>
      <stop offset="1" stop-color="#0c171c"/>
    </linearGradient>
    <linearGradient id="brass" x1="18" y1="16" x2="48" y2="52" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#e7c06d"/>
      <stop offset="1" stop-color="#b9872d"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="14" fill="url(#bg)"/>
  <path d="M18 20.5C18 17.5 20.5 15 23.5 15h17c3 0 5.5 2.5 5.5 5.5v23c0 3-2.5 5.5-5.5 5.5h-17c-3 0-5.5-2.5-5.5-5.5v-23Z" fill="#f7f4ea"/>
  <path d="M22 22c0-1.7 1.3-3 3-3h14c1.7 0 3 1.3 3 3v9H22v-9Z" fill="#263946"/>
  <path d="M22 31h20v11.5c0 1.4-1.1 2.5-2.5 2.5h-15c-1.4 0-2.5-1.1-2.5-2.5V31Z" fill="url(#brass)"/>
  <path d="M26 37h12" stroke="#fff9e8" stroke-width="3" stroke-linecap="round"/>
  <path d="M32 17v31" stroke="#0c171c" stroke-opacity=".16" stroke-width="2"/>
  <circle cx="43.5" cy="44.5" r="3.5" fill="#15242c"/>
</svg>"""


JS = """
function rowHtml(type) {
  if (type === "source") {
    return `<tr>
      <td><input type="checkbox" name="source_enabled" checked></td>
      <td><input name="source_id" value=""></td>
      <td><input name="source_label" value=""></td>
      <td><input name="source_path" value=""></td>
      <td><button type="button" class="icon" data-remove-row>Remove</button></td>
    </tr>`;
  }
  return `<tr>
    <td><input name="disk_id" value=""></td>
    <td><input name="disk_name" value=""></td>
    <td><input name="disk_mount_path" value="/Volumes/"></td>
    <td><input name="disk_destination_subdir" value="Backups/This-Mac"></td>
    <td><button type="button" class="icon" data-remove-row>Remove</button></td>
  </tr>`;
}

document.addEventListener("click", (event) => {
  const addButton = event.target.closest("[data-add-row]");
  if (addButton) {
    const type = addButton.dataset.addRow;
    const table = document.querySelector(type === "source" ? "#sources-table tbody" : "#disks-table tbody");
    table.insertAdjacentHTML("beforeend", rowHtml(type));
    reindexSourceCheckboxes();
  }
  const removeButton = event.target.closest("[data-remove-row]");
  if (removeButton) {
    removeButton.closest("tr").remove();
    reindexSourceCheckboxes();
  }
});

document.addEventListener("submit", () => reindexSourceCheckboxes());

function reindexSourceCheckboxes() {
  document.querySelectorAll("#sources-table tbody tr").forEach((row, index) => {
    const checkbox = row.querySelector('input[type="checkbox"]');
    if (checkbox) checkbox.name = `source_enabled_${index}`;
  });
}

async function refreshJobs() {
  const response = await fetch("/api/state", { cache: "no-store" });
  if (!response.ok) return;
  const state = await response.json();
  const active = state.active_job;
  document.querySelector("#refresh-status").textContent =
    active ? `Running ${active.disk_name}` : "Updates every 2 seconds";
  updateDiskButtons(state);
  if (state.jobs.length === 0) return;
  const jobs = state.jobs.map((job) => `
    <article class="job" data-job-id="${job.id}">
      <div class="job-head">
        <div><strong>${escapeHtml(job.disk_name)}</strong><span>${escapeHtml(job.started_at_label)} · ${job.dry_run ? "dry run" : "live"}</span></div>
        <span class="job-status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span>
      </div>
      ${renderProgress(job)}
      <code>${escapeHtml(job.command_label)}</code>
      <pre>${escapeHtml(job.log.join("\\n"))}</pre>
    </article>`).join("");
  document.querySelector("#jobs").innerHTML = jobs;
}

function renderProgress(job) {
  const progress = job.progress || {};
  const barPercent = progress.bar_percent ?? 100;
  const fillClass = progress.indeterminate ? "progress-fill indeterminate" : "progress-fill";
  return `<div class="progress-panel">
    <div class="progress-top">
      <strong>${escapeHtml(progress.label || "Preparing backup")}</strong>
      <span>${escapeHtml(progress.detail || "Waiting for rsync")}</span>
    </div>
    <div class="progress-track" aria-label="Backup progress">
      <div class="${fillClass}" style="width: ${barPercent}%"></div>
    </div>
    <div class="progress-meta">${escapeHtml(progress.meta || "rsync is starting up")}</div>
  </div>`;
}

function updateDiskButtons(state) {
  const disksById = new Map(state.backup_disks.map((disk) => [disk.id, disk]));
  document.querySelectorAll(".disk-card").forEach((card) => {
    const disk = disksById.get(card.dataset.diskId);
    const available = disk ? disk.available : card.dataset.available === "true";
    const button = card.querySelector('button[type="submit"]');
    if (button) button.disabled = Boolean(state.active_job) || !available;
    card.dataset.available = String(available);
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

setInterval(refreshJobs, 2000);
refreshJobs();
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LAN-accessible rsync web UI for rotating offsite disks.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to JSON config file.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8585")), help="Bind port.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state = BackupState(args.config)
    BackupRequestHandler.state = state
    server = ThreadingHTTPServer((args.host, args.port), BackupRequestHandler)
    print(f"Offsite Archive Utility running at http://{args.host}:{args.port}")
    print(f"Config: {state.config_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
