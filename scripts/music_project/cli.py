from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import TEMPLATE_VERSION, TOOL_VERSION
from .analysis import analyze_audio
from .assets import safe_import
from .io import (
    atomic_write_json,
    atomic_write_toml,
    find_root,
    load_config,
    load_toml,
    now_iso,
    resolve_inside,
    sha256_file,
    validate_identifier,
)
from .records import (
    new_track,
    record_review,
    record_rights,
    register_edit,
    register_export,
    register_generation,
    register_render,
    seal_record,
)
from .release import freeze_release, record_publication
from .retention import apply_plan, build_plan, plan_as_json
from .validation import format_report, load_records, validate


def _actor(value: str) -> str:
    if value:
        return value
    result = subprocess.run(
        ["git", "config", "user.name"], capture_output=True, text=True
    )
    actor = result.stdout.strip() if result.returncode == 0 else ""
    actor = actor or os.environ.get("USER", "")
    if not actor:
        raise ValueError("actor is required; pass --actor")
    return actor


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _effective_provider(root: Path, track_id: str, explicit: str) -> str:
    if explicit:
        return explicit
    track = load_toml(root / "tracks" / track_id / "track.toml")
    track_provider = track.get("default_provider")
    if isinstance(track_provider, str) and track_provider:
        return track_provider
    project = load_config(root).get("project")
    if not isinstance(project, dict) or not isinstance(project.get("default_provider"), str):
        raise ValueError("project.default_provider must be configured")
    provider = str(project["default_provider"])
    if not provider:
        raise ValueError("project.default_provider must be configured")
    return provider


def _ignored_copy_path(relative: Path) -> bool:
    parts = relative.parts
    return bool(
        ".git" in parts
        or "__pycache__" in parts
        or parts[:2] == ("releases", ".staging")
        or parts[:2] == ("assets", "candidates-local") and relative.name != ".gitkeep"
    )


def initialize_project(target: Path, project_id: str, title: str, actor: str) -> Path:
    source = _source_root()
    target = target.expanduser().resolve()
    if target != source and source in target.parents:
        raise ValueError("initialization target must not be nested inside the template source")
    target.mkdir(parents=True, exist_ok=True)
    conflicts: list[str] = []
    files: list[tuple[Path, Path]] = []
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        if _ignored_copy_path(relative) or not source_path.is_file():
            continue
        destination = target / relative
        files.append((source_path, destination))
        if destination.exists() and destination.read_bytes() != source_path.read_bytes():
            conflicts.append(relative.as_posix())
    if conflicts:
        raise FileExistsError(
            "initialization would overwrite existing files: " + ", ".join(conflicts)
        )
    for source_path, destination in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(source_path, destination)

    if not (target / ".git").exists():
        subprocess.run(["git", "init", "-b", "main"], cwd=target, check=True)
    subprocess.run(["git", "lfs", "install", "--local"], cwd=target, check=True)

    config_path = target / "music.toml"
    config = load_toml(config_path)
    config["template_version"] = TEMPLATE_VERSION
    config["tool_version"] = TOOL_VERSION
    config["created_with"] = f"music-project-template {TOOL_VERSION}"
    config["project"]["project_id"] = project_id
    config["project"]["title"] = title
    atomic_write_toml(config_path, config)

    portfolio_path = target / "portfolio.toml"
    portfolio = load_toml(portfolio_path)
    portfolio["created_at"] = now_iso()
    portfolio["created_by"] = actor
    portfolio["title"] = title
    atomic_write_toml(portfolio_path, portfolio)
    return target


def _hash_text(root: Path, track_id: str, kind: str, item_id: str) -> Path:
    directories = {"prompt": "prompts", "lyrics": "lyrics"}
    directory = directories[kind]
    validate_identifier(track_id, "track-id")
    validate_identifier(item_id, f"{kind}-id")
    record_directory = resolve_inside(root / "tracks" / track_id / directory, root)
    record_path = resolve_inside(record_directory / f"{item_id}.toml", root)
    if not record_path.is_file():
        raise FileNotFoundError(record_path)
    record = load_toml(record_path)
    text_path = root / str(record.get("text_path") or "")
    text_path = text_path.resolve(strict=True)
    text_path.relative_to(root.resolve())
    record["sha256"] = sha256_file(text_path)
    record["status"] = "sealed"
    atomic_write_toml(record_path, seal_record(record))
    return record_path


def _status(root: Path) -> dict[str, Any]:
    records, parse_issues = load_records(root)
    counts: dict[str, int] = {}
    tracks: dict[str, str] = {}
    for _, record in records:
        record_type = str(record.get("record_type") or "unknown")
        counts[record_type] = counts.get(record_type, 0) + 1
        if record_type == "track":
            tracks[str(record.get("track_id"))] = str(record.get("workflow_state"))
    report = validate(root, "minimal")
    return {
        "tool_version": TOOL_VERSION,
        "record_counts": counts,
        "tracks": tracks,
        "minimal_validation": report["status"],
        "parse_issues": len(parse_issues),
    }


def _new_report_output(root: Path, raw_path: str, kind: str) -> Path:
    output = resolve_inside(Path(raw_path), root)
    if output.suffix.lower() != ".json":
        raise ValueError("report output must use a .json extension")
    relative = output.relative_to(root.resolve())
    if kind == "retention":
        allowed = len(relative.parts) == 3 and relative.parts[:2] == (
            "reports",
            "retention",
        )
    elif kind == "analysis":
        allowed = (
            len(relative.parts) == 4
            and relative.parts[0] == "tracks"
            and relative.parts[2] == "analysis"
            and relative.parts[1] != "_template"
        )
        if allowed:
            validate_identifier(relative.parts[1], "track-id")
            allowed = (root / "tracks" / relative.parts[1] / "track.toml").is_file()
    else:
        raise ValueError(f"unknown report kind: {kind}")
    if not allowed:
        raise ValueError(f"{kind} output is outside its designated report directory")
    if output.exists():
        raise FileExistsError(f"report output already exists: {relative.as_posix()}")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music.py")
    parser.add_argument("--root", default="", help="project root; defaults to discovery")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="copy a versioned template snapshot")
    init_parser.add_argument("target")
    init_parser.add_argument("--project-id", required=True)
    init_parser.add_argument("--title", required=True)
    init_parser.add_argument("--actor", default="")

    track_parser = subparsers.add_parser("new-track")
    track_parser.add_argument("--track-id", required=True)
    track_parser.add_argument("--title", required=True)
    track_parser.add_argument("--actor", default="")

    hash_text_parser = subparsers.add_parser("hash-text")
    hash_text_parser.add_argument("--track-id", required=True)
    hash_text_parser.add_argument("--kind", choices=("prompt", "lyrics"), required=True)
    hash_text_parser.add_argument("--id", required=True)

    generation = subparsers.add_parser("register-generation")
    generation.add_argument("--track-id", required=True)
    generation.add_argument("--actor", default="")
    generation.add_argument("--provider", default="", help="explicit provider override")
    generation.add_argument("--operation", default="create")
    generation.add_argument("--occurred-at", default="")
    generation.add_argument("--model", required=True)
    generation.add_argument("--object-id", required=True)
    generation.add_argument("--plan", default="")
    generation.add_argument("--prompt-ref", required=True)
    generation.add_argument("--lyrics-ref", default="")
    generation.add_argument("--terms-snapshot-ref", default="")
    generation.add_argument("--parent-ref", action="append", default=[])
    generation.add_argument("--input-ref", action="append", default=[])
    generation.add_argument("--output-ref", action="append", default=[])
    generation.add_argument("--provider-data", action="append", default=[])

    edit = subparsers.add_parser("register-edit")
    edit.add_argument("--track-id", required=True)
    edit.add_argument("--actor", default="")
    edit.add_argument("--provider", default="", help="explicit provider override")
    edit.add_argument("--operation", required=True)
    edit.add_argument("--occurred-at", default="")
    edit.add_argument("--parent-ref", action="append", required=True)
    edit.add_argument("--input-ref", action="append", default=[])
    edit.add_argument("--output-ref", action="append", default=[])
    edit.add_argument("--provider-data", action="append", default=[])

    export = subparsers.add_parser("register-export")
    export.add_argument("--track-id", required=True)
    export.add_argument("--actor", default="")
    export.add_argument("--provider", default="", help="explicit provider override")
    export.add_argument("--operation", required=True)
    export.add_argument("--occurred-at", default="")
    export.add_argument("--parent-ref", action="append", required=True)
    export.add_argument("--input-ref", action="append", default=[])
    export.add_argument("--output-ref", action="append", required=True)
    export.add_argument("--provider-data", action="append", default=[])

    render = subparsers.add_parser("register-render")
    render.add_argument("--track-id", required=True)
    render.add_argument("--actor", default="")
    render.add_argument("--toolchain", required=True)
    render.add_argument("--parent-ref", action="append", required=True)
    render.add_argument("--input-ref", action="append", default=[])
    render.add_argument("--output-ref", action="append", default=[])
    render.add_argument("--notes", default="")

    review = subparsers.add_parser("record-review")
    review.add_argument("--track-id", required=True)
    review.add_argument("--actor", default="")
    review.add_argument("--subject-ref", required=True)
    review.add_argument(
        "--subject-sha256",
        default="",
        help="SHA-256 of the exact reviewed media; required for a release listening gate",
    )
    review.add_argument("--decision", required=True)
    review.add_argument("--blind-label", default="")
    review.add_argument("--timestamp-notes", default="")

    rights = subparsers.add_parser("record-rights")
    rights.add_argument("--track-id", required=True)
    rights.add_argument("--actor", default="")
    rights.add_argument("--subject-ref", required=True)
    rights.add_argument("--source-type", required=True)
    rights.add_argument("--human-status", required=True)
    rights.add_argument("--private-locator", default="")
    rights.add_argument("--evidence-sha256", default="")
    rights.add_argument("--public-note", default="")

    importer = subparsers.add_parser("safe-import")
    importer.add_argument("source")
    importer.add_argument("--track-id", required=True)
    importer.add_argument("--bucket", choices=("inputs", "selected", "stems", "milestones"), required=True)
    importer.add_argument("--role", required=True)
    importer.add_argument("--actor", default="")

    analyzer = subparsers.add_parser("analyze-audio")
    analyzer.add_argument("source")
    analyzer.add_argument(
        "--output", required=True, help="new tracks/<track-id>/analysis/<name>.json path"
    )

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--level", choices=("minimal", "post", "release"), default="minimal")
    validate_parser.add_argument("--json", action="store_true")

    subparsers.add_parser("status")

    retention_plan = subparsers.add_parser("retention-plan")
    retention_plan.add_argument("--track-id", default="")
    retention_plan.add_argument(
        "--output", default="", help="new reports/retention/<name>.json path"
    )

    retention_apply = subparsers.add_parser("retention-apply")
    retention_apply.add_argument("--plan", required=True)
    retention_apply.add_argument("--actor", default="")
    retention_apply.add_argument("--confirm", action="store_true")

    freeze = subparsers.add_parser("freeze-release")
    freeze.add_argument("--track-id", required=True)
    freeze.add_argument("--master", required=True)
    freeze.add_argument("--title", required=True)
    freeze.add_argument("--actor", default="")
    freeze.add_argument("--release-id", default="")
    freeze.add_argument("--listening-gate", action="append", required=True)
    freeze.add_argument("--rights-ref", action="append", default=[])
    freeze.add_argument("--policy-snapshot-ref", action="append", required=True)
    freeze.add_argument("--override-reason", default="")
    freeze.add_argument("--override-risk", action="append", default=[])
    freeze.add_argument("--confirm", action="store_true")

    publication = subparsers.add_parser("record-publication")
    publication.add_argument("--release-id", required=True)
    publication.add_argument("--actor", default="")
    publication.add_argument("--release-url", required=True)
    publication.add_argument("--asset-url", required=True)
    publication.add_argument("--asset-name", required=True)
    publication.add_argument("--asset-sha256", required=True)
    publication.add_argument("--confirm", action="store_true")

    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("--to", required=True)
    migrate.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            target = initialize_project(
                Path(args.target), args.project_id, args.title, _actor(args.actor)
            )
            print(target)
            return 0
        root = Path(args.root).expanduser().resolve() if args.root else find_root()

        if args.command == "new-track":
            print(new_track(root, args.track_id, args.title, _actor(args.actor)))
        elif args.command == "hash-text":
            print(_hash_text(root, args.track_id, args.kind, args.id))
        elif args.command == "register-generation":
            print(
                register_generation(
                    root,
                    track_id=args.track_id,
                    actor=_actor(args.actor),
                    provider=_effective_provider(root, args.track_id, args.provider),
                    operation=args.operation,
                    occurred_at=args.occurred_at,
                    model=args.model,
                    object_id=args.object_id,
                    plan=args.plan,
                    prompt_ref=args.prompt_ref,
                    lyrics_ref=args.lyrics_ref,
                    terms_snapshot_ref=args.terms_snapshot_ref,
                    parent_refs=args.parent_ref,
                    input_refs=args.input_ref,
                    output_refs=args.output_ref,
                    provider_data=args.provider_data,
                )
            )
        elif args.command == "register-edit":
            print(
                register_edit(
                    root,
                    track_id=args.track_id,
                    actor=_actor(args.actor),
                    provider=_effective_provider(root, args.track_id, args.provider),
                    operation=args.operation,
                    occurred_at=args.occurred_at,
                    parent_refs=args.parent_ref,
                    input_refs=args.input_ref,
                    output_refs=args.output_ref,
                    provider_data=args.provider_data,
                )
            )
        elif args.command == "register-export":
            print(
                register_export(
                    root,
                    track_id=args.track_id,
                    actor=_actor(args.actor),
                    provider=_effective_provider(root, args.track_id, args.provider),
                    operation=args.operation,
                    occurred_at=args.occurred_at,
                    parent_refs=args.parent_ref,
                    input_refs=args.input_ref,
                    output_refs=args.output_ref,
                    provider_data=args.provider_data,
                )
            )
        elif args.command == "register-render":
            print(
                register_render(
                    root,
                    track_id=args.track_id,
                    actor=_actor(args.actor),
                    toolchain=args.toolchain,
                    parent_refs=args.parent_ref,
                    input_refs=args.input_ref,
                    output_refs=args.output_ref,
                    notes=args.notes,
                )
            )
        elif args.command == "record-review":
            print(
                record_review(
                    root,
                    track_id=args.track_id,
                    actor=_actor(args.actor),
                    subject_ref=args.subject_ref,
                    subject_sha256=args.subject_sha256,
                    decision=args.decision,
                    blind_label=args.blind_label,
                    timestamp_notes=args.timestamp_notes,
                )
            )
        elif args.command == "record-rights":
            print(
                record_rights(
                    root,
                    track_id=args.track_id,
                    actor=_actor(args.actor),
                    subject_ref=args.subject_ref,
                    source_type=args.source_type,
                    human_status=args.human_status,
                    private_locator=args.private_locator,
                    evidence_sha256=args.evidence_sha256,
                    public_note=args.public_note,
                )
            )
        elif args.command == "safe-import":
            result = safe_import(
                root,
                Path(args.source),
                bucket=args.bucket,
                track_id=args.track_id,
                actor=_actor(args.actor),
                role=args.role,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "analyze-audio":
            report = analyze_audio(
                root,
                Path(args.source),
                _new_report_output(root, args.output, "analysis"),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "validate":
            report = validate(root, args.level)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_report(report))
            return 0 if report["status"] == "PASS" else 1
        elif args.command == "status":
            print(json.dumps(_status(root), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "retention-plan":
            plan = build_plan(root, args.track_id or None)
            if args.output:
                atomic_write_json(
                    _new_report_output(root, args.output, "retention"), plan
                )
            print(plan_as_json(plan))
        elif args.command == "retention-apply":
            if not args.confirm:
                raise ValueError("retention-apply requires --confirm; run retention-plan first")
            plan_path = Path(args.plan)
            if not plan_path.is_absolute():
                plan_path = root / plan_path
            plan_path = resolve_inside(plan_path, root, must_exist=True)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    apply_plan(
                        root, plan, _actor(args.actor), confirmed=args.confirm
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "freeze-release":
            path, status = freeze_release(
                root,
                track_id=args.track_id,
                master=Path(args.master),
                title=args.title,
                actor=_actor(args.actor),
                listening_gate_refs=args.listening_gate,
                rights_refs=args.rights_ref,
                policy_snapshot_refs=args.policy_snapshot_ref,
                release_id=args.release_id,
                override_reason=args.override_reason,
                override_risks=args.override_risk,
                confirmed=args.confirm,
            )
            print(f"{status} {path}")
        elif args.command == "record-publication":
            print(
                record_publication(
                    root,
                    release_id=args.release_id,
                    actor=_actor(args.actor),
                    release_url=args.release_url,
                    asset_url=args.asset_url,
                    asset_name=args.asset_name,
                    asset_sha256=args.asset_sha256,
                    confirmed=args.confirm,
                )
            )
        elif args.command == "migrate":
            if args.to == "0.1":
                print("schema 0.1 is current; no changes")
            else:
                raise ValueError("no migration path is implemented for the requested schema")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
