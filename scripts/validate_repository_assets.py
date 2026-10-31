"""Validate checked-in Pipeline Guard configuration and plugin manifests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from scripts.validate_config import ConfigValidationError, validate_config

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_IGNORED_DIRECTORIES = {".git", ".venv", "reports", "__pycache__"}


class RepositoryAssetError(ValueError):
    """Raised when a checked-in configuration or plugin manifest is invalid."""


def _find_manifests(filename: str) -> list[Path]:
    """Find manifests without traversing generated or virtual-environment files."""
    matches: list[Path] = []
    for current_root, directories, files in os.walk(_REPOSITORY_ROOT):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in _IGNORED_DIRECTORIES
        )
        if filename in files:
            matches.append(Path(current_root) / filename)
    return sorted(matches)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepositoryAssetError(f"{path}: invalid or unreadable JSON") from exc
    if not isinstance(value, dict):
        raise RepositoryAssetError(f"{path}: top-level JSON value must be an object")
    return value


def _validate_plugin_manifest(path: Path) -> None:
    manifest = _load_json_object(path)
    for field in ("name", "version", "description"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RepositoryAssetError(f"{path}: '{field}' must be a non-empty string")

    plugin_root = (
        path.parent.parent if path.parent.name == ".claude-plugin" else path.parent
    )
    for field in ("agents", "skills", "commands"):
        value = manifest.get(field, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise RepositoryAssetError(f"{path}: '{field}' must be an array of paths")
        for relative_path in value:
            if (
                relative_path.startswith("./")
                and not (plugin_root / relative_path[2:]).exists()
            ):
                raise RepositoryAssetError(
                    f"{path}: '{field}' references missing path '{relative_path}'"
                )


def _validate_marketplace_manifest(path: Path) -> None:
    manifest = _load_json_object(path)
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise RepositoryAssetError(f"{path}: 'name' must be a non-empty string")
    plugins = manifest.get("plugins")
    if not isinstance(plugins, list):
        raise RepositoryAssetError(f"{path}: 'plugins' must be an array")
    for index, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            raise RepositoryAssetError(f"{path}: plugins[{index}] must be an object")
        source = plugin.get("source")
        if isinstance(source, str) and source.startswith("./"):
            if not (_REPOSITORY_ROOT / source[2:]).exists():
                raise RepositoryAssetError(
                    f"{path}: plugins[{index}] references missing source '{source}'"
                )


def validate_repository_assets() -> tuple[int, int]:
    """Validate every checked-in preset, project config and plugin manifest."""
    config_paths = sorted(
        path
        for directory in ("presets", "projects")
        for path in (_REPOSITORY_ROOT / "config" / directory).glob("*.yaml")
    )
    if not config_paths:
        raise RepositoryAssetError(
            "config/presets and config/projects contain no YAML files"
        )

    for path in config_paths:
        try:
            validate_config(path)
        except ConfigValidationError as exc:
            raise RepositoryAssetError(str(exc)) from exc

    plugin_manifests = _find_manifests("plugin.json")
    marketplace_manifests = _find_manifests("marketplace.json")
    if not plugin_manifests:
        raise RepositoryAssetError("no plugin.json manifest was found")

    for path in plugin_manifests:
        _validate_plugin_manifest(path)
    for path in marketplace_manifests:
        _validate_marketplace_manifest(path)

    return len(config_paths), len(plugin_manifests) + len(marketplace_manifests)


def main() -> int:
    try:
        config_count, manifest_count = validate_repository_assets()
    except RepositoryAssetError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        "Validated "
        f"{config_count} YAML configs and {manifest_count} plugin/marketplace manifests."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
