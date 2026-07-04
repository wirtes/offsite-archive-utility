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
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_HISTORY_PATH = APP_DIR / "backup_history.json"
RSYNC_VERSION = "--version"
BUILT_IN_EXCLUDE_PATTERNS = ("._*",)
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
            "command_labels": [format_command(command) for command in self.commands],
            "active_command_index": max(0, self.current_source_index - 1) if self.status == "running" and self.current_source_index else None,
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
    def __init__(self, config_path: Path, history_path: Path | None = None):
        self.config_path = config_path
        self.history_path = history_path or config_path.with_name(DEFAULT_HISTORY_PATH.name)
        self.lock = threading.RLock()
        self.config = load_config(config_path)
        self.history = load_history(self.history_path)
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

    def record_job_history_locked(self, job: Job) -> None:
        record = job_history_record(job)
        self.history = [item for item in self.history if item.get("id") != job.id]
        self.history.append(record)
        self.history.sort(key=lambda item: float(item.get("started_at", 0)))
        save_history(self.history_path, self.history)


def timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(value or time.time()).strftime("%Y-%m-%d %H:%M:%S")


def load_history(history_path: Path) -> list[dict[str, Any]]:
    if not history_path.exists():
        return []
    with history_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get("jobs", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id") and item.get("disk_id")]


def save_history(history_path: Path, history: list[dict[str, Any]]) -> None:
    history_path.write_text(json.dumps({"jobs": history}, indent=2) + "\n", encoding="utf-8")


def job_history_record(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "disk_id": job.disk_id,
        "disk_name": job.disk_name,
        "dry_run": job.dry_run,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "status": job.status,
        "returncode": job.returncode,
    }


def format_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def item_progress_percent(job: Job) -> int | None:
    if job.to_check_remaining is None or not job.to_check_total:
        return None
    return min(100, max(0, round((job.to_check_remaining / job.to_check_total) * 100)))


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
        "rsync_options": ["-a", "--progress", "--human-readable"],
        "exclude_patterns": [
            ".DS_Store",
            "._*",
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
                "id": "documents",
                "path": str(Path.home() / "Documents"),
                "enabled": True,
                "delete": False,
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
    if normalize_appledouble_skip_config(config):
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    validate_config(config)
    return config


def normalize_appledouble_skip_config(config: dict[str, Any]) -> bool:
    excludes_changed = ensure_builtin_excludes(config)
    options_changed = remove_appledouble_generating_options_from_config(config)
    delete_changed = remove_global_delete_option_from_config(config)
    return excludes_changed or options_changed or delete_changed


def ensure_builtin_excludes(config: dict[str, Any]) -> bool:
    patterns = config.setdefault("exclude_patterns", [])
    if not isinstance(patterns, list):
        return False

    changed = False
    existing = {str(pattern).strip() for pattern in patterns}
    for pattern in BUILT_IN_EXCLUDE_PATTERNS:
        if pattern not in existing:
            patterns.append(pattern)
            changed = True
    return changed


def remove_appledouble_generating_options_from_config(config: dict[str, Any]) -> bool:
    options = config.get("rsync_options", [])
    if not isinstance(options, list):
        return False

    cleaned_options = remove_appledouble_generating_options(options)
    if cleaned_options == options:
        return False

    config["rsync_options"] = cleaned_options
    return True


def remove_appledouble_generating_options(options: list[Any]) -> list[str]:
    cleaned: list[str] = []
    for option in options:
        option = str(option)
        if option == "-E":
            continue
        if option.startswith("-") and not option.startswith("--") and "E" in option[1:]:
            stripped_flags = option[1:].replace("E", "")
            if stripped_flags:
                cleaned.append("-" + stripped_flags)
            continue
        cleaned.append(option)
    return cleaned


def remove_global_delete_option_from_config(config: dict[str, Any]) -> bool:
    options = config.get("rsync_options", [])
    if not isinstance(options, list):
        return False

    cleaned_options = [str(option) for option in options if str(option) != "--delete"]
    if cleaned_options == options:
        return False

    config["rsync_options"] = cleaned_options
    return True


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object.")
    for key in ("sources", "backup_disks"):
        if not isinstance(config.get(key), list):
            raise ValueError(f"Config field '{key}' must be a list.")

    source_ids = set()
    for source in config["sources"]:
        require_fields(source, ("id", "path"), "source")
        validate_path_segment(source["id"], "source subdirectory on backup disk")
        if source["id"] in source_ids:
            raise ValueError(f"Duplicate source subdirectory on backup disk: {source['id']}")
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
        "delete": bool(source.get("delete", False)),
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
    options = [option for option in remove_appledouble_generating_options(config.get("rsync_options", [])) if option != "--delete"]
    if dry_run and "--dry-run" not in options and "-n" not in options:
        options.append("--dry-run")

    exclude_patterns = list(config.get("exclude_patterns", [])) + list(BUILT_IN_EXCLUDE_PATTERNS)

    base_command = [rsync_path, *options]
    seen_excludes: set[str] = set()
    for pattern in exclude_patterns:
        pattern = str(pattern).strip()
        if pattern and pattern not in seen_excludes:
            base_command.extend(["--exclude", pattern])
            seen_excludes.add(pattern)

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
        source_command = [*base_command]
        if source.get("delete", False):
            source_command.append("--delete")
        commands.append([*source_command, str(path) + "/", str(source_destination) + "/"])
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
            state.record_job_history_locked(job)
    except Exception as exc:  # noqa: BLE001 - user-facing local tool
        with state.lock:
            job = state.jobs[job_id]
            job.returncode = -1
            job.status = "failed"
            job.ended_at = time.time()
            job.log.append(f"[{timestamp()}] Error: {exc}")
            state.active_job_id = None
            state.record_job_history_locked(job)


def parse_config_form(body: bytes) -> dict[str, Any]:
    data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    config = default_config()
    config["rsync_path"] = data.get("rsync_path", ["/usr/bin/rsync"])[0].strip() or "/usr/bin/rsync"
    config["rsync_options"] = split_lines_or_args(data.get("rsync_options", [""])[0])
    config["exclude_patterns"] = split_nonempty_lines(data.get("exclude_patterns", [""])[0])
    config["sources"] = parse_source_rows(data)
    config["backup_disks"] = parse_rows(data, "disk", ("id", "name", "mount_path", "destination_subdir"))
    normalize_appledouble_skip_config(config)
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
            "path": data.get("source_path", [""] * len(ids))[index].strip(),
            "enabled": data.get(f"source_enabled_{index}", ["off"])[0] == "on",
            "delete": data.get(f"source_delete_{index}", ["off"])[0] == "on",
        }
        if any(str(row.get(field_name, "")).strip() for field_name in ("id", "path")):
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
        active_job = state.jobs.get(state.active_job_id) if state.active_job_id else None
        jobs = [job.to_dict() for job in visible_activity_jobs(state.jobs.values(), active_job)]
        timeline = backup_timeline(timeline_history(state), config["backup_disks"])
    return {
        "sources": [source_status(source) for source in config["sources"]],
        "backup_disks": [disk_status(disk) for disk in config["backup_disks"]],
        "jobs": jobs,
        "active_job": active_job.to_dict() if active_job else None,
        "timeline": timeline,
    }


def visible_activity_jobs(jobs: Iterable[Job], active_job: Job | None) -> list[Job]:
    sorted_jobs = sorted(jobs, key=lambda job: job.started_at, reverse=True)
    if not active_job:
        return sorted_jobs[:1]

    visible = [active_job]
    for job in sorted_jobs:
        if job.id != active_job.id:
            visible.append(job)
            break
    return visible


def timeline_history(state: BackupState) -> list[dict[str, Any]]:
    by_id = {str(item["id"]): dict(item) for item in state.history}
    for job in state.jobs.values():
        by_id[job.id] = job_history_record(job)
    return sorted(by_id.values(), key=lambda item: float(item.get("started_at", 0)))


def backup_timeline(history: Iterable[dict[str, Any]], disks: list[dict[str, Any]]) -> dict[str, Any]:
    all_events = sorted(history, key=lambda item: float(item.get("started_at", 0)))
    if not all_events:
        return {"rows": [], "start_label": "", "end_label": ""}

    start = float(all_events[0].get("started_at", 0))
    end = float(all_events[-1].get("started_at", start))
    span = max(end - start, 1)
    disk_names = {disk["id"]: disk["name"] for disk in disks}
    for event in all_events:
        disk_names.setdefault(str(event.get("disk_id", "")), str(event.get("disk_name", "Unknown disk")))

    rows = []
    for disk_id, disk_name in disk_names.items():
        events = []
        for event in all_events:
            if event.get("disk_id") != disk_id:
                continue
            started_at = float(event.get("started_at", 0))
            events.append(
                {
                    "id": event.get("id", ""),
                    "label": timestamp(started_at),
                    "status": event.get("status", "completed"),
                    "dry_run": bool(event.get("dry_run", False)),
                    "position": round(((started_at - start) / span) * 100, 2) if len(all_events) > 1 else 50,
                }
            )
        rows.append({"disk_id": disk_id, "disk_name": disk_name, "events": events})

    return {
        "rows": rows,
        "start_label": timestamp(start),
        "end_label": timestamp(end),
    }


def render_backup_timeline(timeline: dict[str, Any]) -> str:
    rows = timeline.get("rows", [])
    if not any(row.get("events") for row in rows):
        return """<div id="backup-timeline" class="timeline">
  <div class="timeline-head"><h3>Backup timeline</h3></div>
  <p class="muted">No backup history yet.</p>
</div>"""

    rendered_rows = []
    for row in rows:
        events = "".join(
            f"""<span class="timeline-dot {escape(event["status"])}" style="left: {escape(event["position"])}%" title="{escape(event["label"])} · {escape(event["status"])}{' · dry run' if event["dry_run"] else ''}"></span>"""
            for event in row.get("events", [])
        )
        empty = "<span class='timeline-empty'>No runs yet</span>" if not row.get("events") else ""
        rendered_rows.append(
            f"""<div class="timeline-row">
  <div class="timeline-label">{escape(row["disk_name"])}</div>
  <div class="timeline-track">{events}{empty}</div>
</div>"""
        )

    return f"""<div id="backup-timeline" class="timeline">
  <div class="timeline-head">
    <h3>Backup timeline</h3>
    <span>{escape(timeline.get("start_label", ""))} to {escape(timeline.get("end_label", ""))}</span>
  </div>
  {''.join(rendered_rows)}
</div>"""


def render_page(state: BackupState, error: str = "") -> str:
    with state.lock:
        config = json.loads(json.dumps(state.config))
        active_job = state.jobs.get(state.active_job_id) if state.active_job_id else None
        jobs = visible_activity_jobs(state.jobs.values(), active_job)
        timeline = backup_timeline(timeline_history(state), config["backup_disks"])

    sources = [source_status(source) for source in config["sources"]]
    disks = [disk_status(disk) for disk in config["backup_disks"]]
    source_rows = "\n".join(render_source_row(source, index) for index, source in enumerate(sources))
    disk_rows = "\n".join(render_disk_row(disk) for disk in disks)
    disk_cards = "\n".join(render_disk_card(disk, bool(active_job)) for disk in disks)
    job_cards = "\n".join(render_job_card(job) for job in jobs) or "<p class='muted'>No backup jobs yet.</p>"
    timeline_html = render_backup_timeline(timeline)
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
        <div class="heading-actions">
          <span class="muted">Mounted disks are ready to run.</span>
          <button type="button" class="secondary compact" id="refresh-disks">Refresh disks</button>
        </div>
      </div>
      <div class="disk-grid">{disk_cards}</div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <h2>Activity</h2>
        <span id="refresh-status" class="muted">{escape("Backup running" if active_job else "Idle")}</span>
      </div>
      <div id="jobs">{job_cards}</div>
      {timeline_html}
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

        <div class="config-zone sources-zone">
          <div class="zone-heading">
            <h3>Sources</h3>
            <p>Read only. These locations are scanned and copied from; rsync will not write changes here.</p>
          </div>
          <div class="table-wrap">
            <table id="sources-table">
              <thead><tr><th>Enabled</th><th>Delete</th><th>Subdirectory on backup disk</th><th>Path</th><th></th></tr></thead>
              <tbody>{source_rows}</tbody>
            </table>
          </div>
          <button type="button" class="secondary" data-add-row="source">Add source</button>
        </div>

        <div class="config-zone disks-zone">
          <div class="zone-heading">
            <h3>Backup disks</h3>
            <p>Write target. These mounted destinations will be written to and may have files deleted when --delete is enabled.</p>
          </div>
          <p class="field-note">ID is the stable internal key used by the tool. Name is the human-friendly display name shown in the Run Backup and Activity sections.</p>
          <div class="table-wrap">
            <table id="disks-table">
              <thead><tr><th>ID</th><th>Name</th><th>Mount path</th><th>Destination subdir</th><th></th></tr></thead>
              <tbody>{disk_rows}</tbody>
            </table>
          </div>
          <button type="button" class="secondary" data-add-row="disk">Add disk</button>
        </div>

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
    delete_checked = "checked" if source.get("delete", False) else ""
    badge = "<span class='ok'>Found</span>" if source["exists"] else "<span class='bad'>Missing</span>"
    return f"""<tr>
  <td><input type="checkbox" name="source_enabled_{index}" data-source-enabled {checked}></td>
  <td><input type="checkbox" name="source_delete_{index}" data-source-delete {delete_checked}></td>
  <td><input name="source_id" value="{escape(source["id"])}" placeholder="backup-subdirectory"></td>
  <td><input name="source_path" value="{escape(source["path"])}"><div class="row-note">{badge}</div></td>
  <td><button type="button" class="icon" data-remove-row>Remove</button></td>
</tr>"""


def render_disk_row(disk: dict[str, Any]) -> str:
    badge = "<span class='ok'>Mounted</span>" if disk["available"] else "<span class='bad'>Not mounted</span>"
    return f"""<tr>
  <td><input name="disk_id" value="{escape(disk["id"])}"></td>
  <td><input name="disk_name" value="{escape(disk["name"])}"></td>
  <td><input name="disk_mount_path" value="{escape(disk["mount_path"])}"><div class="row-note" data-disk-row-status="{escape(disk["id"])}">{badge}</div></td>
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
    <span class="{status_class}" data-disk-status>{status_text}</span>
  </div>
  <form method="post" action="/start">
    <input type="hidden" name="disk_id" value="{escape(disk["id"])}">
    <label class="check"><input type="checkbox" name="dry_run"> Dry run</label>
    <label class="refresh-control">
      Refresh
      <select class="refresh-interval">
        <option value="1000">1 second</option>
        <option value="2000" selected>2 seconds</option>
        <option value="5000">5 seconds</option>
        <option value="10000">10 seconds</option>
        <option value="30000">30 seconds</option>
        <option value="60000">60 seconds</option>
      </select>
    </label>
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
  {render_command_list(job)}
  <details class="log-details">
    <summary>Activity log</summary>
    <pre>{log}</pre>
  </details>
</article>"""


def render_command_list(job: Job) -> str:
    if not job.commands:
        return "<div class=\"command-list\"></div>"

    active_index = job.current_source_index - 1 if job.status == "running" and job.current_source_index else None
    lines = []
    for index, command in enumerate(job.commands):
        active_class = " active" if active_index == index else ""
        lines.append(f"""<code class="command-line{active_class}">{escape(format_command(command))}</code>""")
    return f"""<div class="command-list">{''.join(lines)}</div>"""


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
        parts.append(f"{item_percent}% of known items left")
    elif job.current_file_percent is not None:
        parts.append(f"{job.current_file_percent}% of current file")
    return " · ".join(parts) or "Waiting for rsync"


def progress_meta(job: Job, item_percent: int | None) -> str:
    parts = []
    if job.speed:
        parts.append(job.speed)
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
.heading-actions { display: inline-flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }
.refresh-control {
  display: inline-flex; align-items: center; gap: 8px; margin: 0; color: var(--muted);
  font-size: 13px; font-weight: 650; white-space: nowrap;
}
.refresh-control select {
  width: auto; min-width: 118px; margin: 0; padding: 7px 30px 7px 10px;
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
button.compact { width: auto; min-height: 34px; padding: 7px 10px; }
.actions { margin-top: 18px; display: flex; justify-content: flex-end; }
.table-wrap { width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; min-width: 760px; }
th, td { padding: 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
tr:last-child td { border-bottom: 0; }
th { color: var(--muted); font-size: 13px; }
.row-note { margin-top: 7px; }
.config-zone {
  border: 1px solid var(--line); border-radius: 8px; padding: 14px; margin-top: 18px;
}
.config-zone h3 { margin: 0; }
.zone-heading {
  display: flex; align-items: baseline; justify-content: space-between; gap: 18px; margin-bottom: 12px;
}
.zone-heading p { margin: 0; font-size: 13px; font-weight: 650; }
.field-note { margin: -4px 0 12px; color: var(--muted); font-size: 13px; }
.sources-zone { background: #eef8f1; border-color: #b9dec5; }
.sources-zone .zone-heading p { color: #17603a; }
.disks-zone { background: #fff1ef; border-color: #efb8b0; }
.disks-zone .zone-heading p { color: #a5362b; }
.config-zone .secondary { margin-top: 12px; }
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
.command-list { margin-bottom: 10px; }
.command-line,
code {
  display: block; background: #f3f6f8; border-radius: 6px; padding: 9px 10px;
  margin-bottom: 6px; overflow-x: auto; white-space: pre;
}
.command-line.active {
  background: #fff2a8;
  box-shadow: inset 0 0 0 1px #e6c84f;
}
pre {
  background: #101820; color: #edf5f7; border-radius: 6px; padding: 12px;
  overflow: auto; max-height: 340px; margin: 0; line-height: 1.45;
}
.log-details {
  border: 1px solid var(--line); border-radius: 8px; background: #fff; overflow: hidden;
}
.log-details summary {
  cursor: pointer; padding: 10px 12px; font-weight: 700; color: #263442;
  list-style-position: inside;
}
.log-details pre { border-radius: 0; }
.timeline {
  border-top: 1px solid var(--line); margin-top: 16px; padding-top: 16px;
}
.timeline-head {
  display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  margin-bottom: 12px;
}
.timeline-head h3 { margin: 0; font-size: 16px; }
.timeline-head span { color: var(--muted); font-size: 13px; }
.timeline-row {
  display: grid; grid-template-columns: minmax(140px, 220px) 1fr; gap: 14px;
  align-items: center; margin-bottom: 12px;
}
.timeline-label {
  color: #263442; font-weight: 700; overflow-wrap: anywhere;
}
.timeline-track {
  position: relative; height: 18px; border-radius: 999px; background: #edf2f5;
  box-shadow: inset 0 0 0 1px rgba(22, 35, 45, 0.06);
}
.timeline-track::before {
  content: ""; position: absolute; left: 0; right: 0; top: 8px; height: 2px;
  background: #cbd6df;
}
.timeline-dot {
  position: absolute; top: 50%; width: 12px; height: 12px; border-radius: 50%;
  transform: translate(-50%, -50%); background: var(--accent); border: 2px solid #fff;
  box-shadow: 0 0 0 1px rgba(22, 35, 45, 0.18);
}
.timeline-dot.running { background: #2b5872; }
.timeline-dot.completed { background: var(--ok); }
.timeline-dot.failed { background: var(--danger); }
.timeline-empty {
  position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
  color: var(--muted); font-size: 12px;
}
@media (max-width: 720px) {
  header, .section-heading, .job-head, .zone-heading, .heading-actions { align-items: flex-start; flex-direction: column; }
  .disk-card form { align-items: stretch; flex-direction: column; }
  .timeline-head, .timeline-row { display: flex; align-items: stretch; flex-direction: column; }
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
      <td><input type="checkbox" name="source_enabled" data-source-enabled checked></td>
      <td><input type="checkbox" name="source_delete" data-source-delete></td>
      <td><input name="source_id" value="" placeholder="backup-subdirectory"></td>
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
    const enabled = row.querySelector("[data-source-enabled]");
    const deleteFiles = row.querySelector("[data-source-delete]");
    if (enabled) enabled.name = `source_enabled_${index}`;
    if (deleteFiles) deleteFiles.name = `source_delete_${index}`;
  });
}

async function refreshState({ manual = false } = {}) {
  const response = await fetch("/api/state", { cache: "no-store" });
  if (!response.ok) return;
  const state = await response.json();
  const activityState = captureActivityState();
  const active = state.active_job;
  isBackupRunning = Boolean(active);
  updateDiskCards(state);
  renderJobs(state.jobs, activityState);
  renderTimeline(state.timeline);
  syncRefreshSelectors();
  document.querySelector("#refresh-status").textContent = active
    ? `Running ${active.disk_name}`
    : (manual ? "Disk status refreshed" : "Idle");
  scheduleActiveRefresh();
}

function renderJobs(jobs, activityState = captureActivityState()) {
  if (jobs.length === 0) {
    document.querySelector("#jobs").innerHTML = "<p class='muted'>No backup jobs yet.</p>";
    return;
  }
  const html = jobs.map((job) => `
    <article class="job" data-job-id="${job.id}">
      <div class="job-head">
        <div><strong>${escapeHtml(job.disk_name)}</strong><span>${escapeHtml(job.started_at_label)} · ${job.dry_run ? "dry run" : "live"}</span></div>
        <span class="job-status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span>
      </div>
      ${renderProgress(job)}
      ${renderCommandList(job)}
      <details class="log-details">
        <summary>Activity log</summary>
        <pre>${escapeHtml(job.log.join("\\n"))}</pre>
      </details>
    </article>`).join("");
  document.querySelector("#jobs").innerHTML = html;
  restoreActivityState(activityState);
  syncRefreshSelectors();
}

function renderCommandList(job) {
  const commands = job.command_labels || [];
  if (commands.length === 0) return `<div class="command-list"></div>`;
  const activeIndex = job.active_command_index;
  const lines = commands.map((command, index) => {
    const activeClass = activeIndex === index ? " active" : "";
    return `<code class="command-line${activeClass}">${escapeHtml(command)}</code>`;
  }).join("");
  return `<div class="command-list">${lines}</div>`;
}

function renderTimeline(timeline) {
  const wrap = document.querySelector("#backup-timeline");
  if (!wrap) return;
  const rows = (timeline && timeline.rows) || [];
  if (!rows.some((row) => row.events && row.events.length)) {
    wrap.innerHTML = `<div class="timeline-head"><h3>Backup timeline</h3></div><p class="muted">No backup history yet.</p>`;
    return;
  }
  const dateRange = `${escapeHtml(timeline.start_label || "")} to ${escapeHtml(timeline.end_label || "")}`;
  const rowHtml = rows.map((row) => {
    const events = (row.events || []).map((event) => {
      const title = `${event.label} · ${event.status}${event.dry_run ? " · dry run" : ""}`;
      return `<span class="timeline-dot ${escapeHtml(event.status)}" style="left: ${Number(event.position) || 0}%" title="${escapeHtml(title)}"></span>`;
    }).join("");
    const empty = events ? "" : "<span class='timeline-empty'>No runs yet</span>";
    return `<div class="timeline-row">
      <div class="timeline-label">${escapeHtml(row.disk_name)}</div>
      <div class="timeline-track">${events}${empty}</div>
    </div>`;
  }).join("");
  wrap.innerHTML = `<div class="timeline-head"><h3>Backup timeline</h3><span>${dateRange}</span></div>${rowHtml}`;
}

function captureActivityState() {
  const state = {
    windowY: window.scrollY,
    logs: new Map(),
  };
  document.querySelectorAll(".job").forEach((jobEl) => {
    const jobId = jobEl.dataset.jobId;
    const details = jobEl.querySelector(".log-details");
    const log = jobEl.querySelector(".log-details pre");
    if (jobId && details) {
      state.logs.set(jobId, {
        open: details.open,
        scrollTop: log ? log.scrollTop : 0,
        scrollLeft: log ? log.scrollLeft : 0,
      });
    }
  });
  return state;
}

function restoreActivityState(state) {
  state.logs.forEach((logState, jobId) => {
    const jobEl = Array.from(document.querySelectorAll(".job")).find((candidate) => candidate.dataset.jobId === jobId);
    const details = jobEl ? jobEl.querySelector(".log-details") : null;
    const log = jobEl ? jobEl.querySelector(".log-details pre") : null;
    if (details) details.open = logState.open;
    if (log) {
      log.scrollTop = logState.scrollTop;
      log.scrollLeft = logState.scrollLeft;
    }
  });
  window.scrollTo({ top: state.windowY, left: window.scrollX, behavior: "auto" });
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

function updateDiskCards(state) {
  const disksById = new Map(state.backup_disks.map((disk) => [disk.id, disk]));
  document.querySelectorAll(".disk-card").forEach((card) => {
    const disk = disksById.get(card.dataset.diskId);
    const available = disk ? disk.available : card.dataset.available === "true";
    const button = card.querySelector('button[type="submit"]');
    if (button) button.disabled = Boolean(state.active_job) || !available;
    const status = card.querySelector("[data-disk-status]");
    if (status) {
      status.textContent = available ? "Mounted" : "Not mounted";
      status.className = available ? "ok" : "bad";
    }
    card.dataset.available = String(available);
  });
  document.querySelectorAll("[data-disk-row-status]").forEach((statusWrap) => {
    const disk = disksById.get(statusWrap.dataset.diskRowStatus);
    if (!disk) return;
    statusWrap.innerHTML = disk.available ? "<span class='ok'>Mounted</span>" : "<span class='bad'>Not mounted</span>";
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

let refreshTimer = null;
let isBackupRunning = Boolean(document.body.dataset.activeJob);
let refreshIntervalValue = window.localStorage.getItem("offsite-refresh-interval") || "2000";
if (refreshIntervalValue === "60001") refreshIntervalValue = "60000";
if (!["1000", "2000", "5000", "10000", "30000", "60000"].includes(refreshIntervalValue)) {
  refreshIntervalValue = "2000";
}

function scheduleActiveRefresh() {
  if (refreshTimer) window.clearTimeout(refreshTimer);
  refreshTimer = null;
  syncRefreshSelectors();
  if (!isBackupRunning) return;
  const interval = refreshIntervalMs();
  refreshTimer = window.setTimeout(() => refreshState(), interval);
}

function refreshStatusText() {
  const interval = refreshIntervalMs();
  if (interval >= 60000) return `Updates every ${interval / 60000} minute${interval === 60000 ? "" : "s"}`;
  return `Updates every ${interval / 1000} seconds`;
}

function refreshIntervalMs() {
  return Number(refreshIntervalValue || 2000);
}

function syncRefreshSelectors() {
  document.querySelectorAll(".refresh-interval").forEach((selector) => {
    selector.value = refreshIntervalValue;
  });
}

document.addEventListener("change", (event) => {
  const selector = event.target.closest(".refresh-interval");
  if (selector) {
    refreshIntervalValue = selector.value;
    window.localStorage.setItem("offsite-refresh-interval", refreshIntervalValue);
    syncRefreshSelectors();
    if (isBackupRunning) {
      scheduleActiveRefresh();
      refreshState();
    }
  }
});

document.querySelector("#refresh-disks")?.addEventListener("click", () => {
  refreshState({ manual: true });
});

syncRefreshSelectors();
if (isBackupRunning) {
  scheduleActiveRefresh();
  refreshState();
}
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
