from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def find_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "music.toml").is_file():
            return candidate
    raise RuntimeError("not inside a music-project-template repository")


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_config(root: Path) -> dict[str, Any]:
    return load_toml(root / "music.toml")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: dict[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "seal"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _toml_key(value: str) -> str:
    if value.replace("_", "a").replace("-", "a").isalnum() and value:
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        if any(isinstance(item, dict) for item in value):
            raise TypeError("array-of-table values are emitted separately")
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def toml_dumps(value: dict[str, Any]) -> str:
    lines: list[str] = []

    def emit_table(table: dict[str, Any], prefix: tuple[str, ...] = ()) -> None:
        scalar_items: list[tuple[str, Any]] = []
        child_tables: list[tuple[str, dict[str, Any]]] = []
        array_tables: list[tuple[str, list[dict[str, Any]]]] = []
        for key, item in table.items():
            if isinstance(item, dict):
                child_tables.append((key, item))
            elif isinstance(item, list) and item and all(
                isinstance(entry, dict) for entry in item
            ):
                array_tables.append((key, item))
            else:
                scalar_items.append((key, item))

        for key, item in scalar_items:
            lines.append(f"{_toml_key(key)} = {_toml_value(item)}")

        for key, child in child_tables:
            if lines and lines[-1] != "":
                lines.append("")
            full = (*prefix, key)
            lines.append("[" + ".".join(_toml_key(part) for part in full) + "]")
            emit_table(child, full)

        for key, entries in array_tables:
            full = (*prefix, key)
            for entry in entries:
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append("[[" + ".".join(_toml_key(part) for part in full) + "]]")
                emit_table(entry, full)

    emit_table(value)
    return "\n".join(lines).rstrip() + "\n"


def atomic_write_toml(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, toml_dumps(value))


def resolve_inside(path: Path, root: Path, *, must_exist: bool = False) -> Path:
    root = root.resolve()
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes repository root: {path}") from exc
    return resolved


def relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def validate_identifier(value: str, label: str = "identifier") -> str:
    if not value or len(value) > 128:
        raise ValueError(f"{label} must be 1-128 characters")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if value[0] not in allowed - {".", "_", "-"} or any(
        character not in allowed for character in value
    ):
        raise ValueError(
            f"{label} must start with a letter or number and contain only letters, numbers, dot, underscore, or hyphen"
        )
    return value


def parse_key_values(values: Iterable[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"expected KEY=VALUE, got: {value}")
        key, raw = value.split("=", 1)
        validate_identifier(key, "provider-data key")
        lowered = raw.lower()
        if lowered in {"true", "false"}:
            parsed[key] = lowered == "true"
        else:
            try:
                parsed[key] = int(raw)
            except ValueError:
                try:
                    parsed[key] = float(raw)
                except ValueError:
                    parsed[key] = raw
    return parsed
