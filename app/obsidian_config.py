"""Read strategy configuration from the Obsidian vault.

The Obsidian vault is the authoritative source for strategy rules.
This module reads the rule file and exposes the parsed config so the
workbench can sync thresholds, weights, and phase definitions without
code changes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def _resolve_vault_path() -> Path | None:
    """Return the Obsidian vault root, or None if not found."""
    candidates = [
        os.environ.get("OBSIDIAN_VAULT_PATH"),
        os.path.expandvars(r"%USERPROFILE%\Documents\Obsidian Vault"),
        os.path.expandvars(r"$HOME\Documents\Obsidian Vault"),
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            if path.is_dir():
                return path
    return None


_RULES_RELATIVE_PATH = Path("01-策略研究") / "擒龙策略规则.md"


def _parse_table(lines: list[str]) -> list[dict[str, str]]:
    """Parse a simple markdown table into a list of dicts."""
    rows: list[dict[str, str]] = []
    headers: list[str] = []
    for line in lines:
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line[1:-1].split("|")]
        if all(c.startswith("-") for c in cells if c):
            continue  # separator row
        if not headers:
            headers = cells
        else:
            row = {}
            for i, cell in enumerate(cells):
                if i < len(headers):
                    row[headers[i]] = cell
            rows.append(row)
    return rows


def _parse_number(value: str) -> float | None:
    """Extract a number from a cell, handling Chinese commas."""
    if not value:
        return None
    cleaned = value.replace(",", "").replace("，", "").replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _find_section(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Return (start, end) line indices for a markdown section."""
    pattern = re.compile(rf"^##\s+{re.escape(heading)}")
    start = None
    for i, line in enumerate(lines):
        if pattern.match(line):
            start = i
        elif start is not None and line.startswith("## "):
            return (start, i)
    if start is not None:
        return (start, len(lines))
    return None


def load_rules() -> dict[str, Any]:
    """Read strategy rules from the Obsidian vault and return a config dict.

    Returns a dict with keys matching the workbench settings schema.
    If the vault or file is not found, returns an empty dict.
    """
    vault = _resolve_vault_path()
    if not vault:
        return {"_obsidian_synced": False, "_obsidian_error": "vault not found"}

    rules_file = vault / _RULES_RELATIVE_PATH
    if not rules_file.is_file():
        return {"_obsidian_synced": False, "_obsidian_error": f"file not found: {rules_file}"}

    try:
        text = rules_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"_obsidian_synced": False, "_obsidian_error": f"read error: {exc}"}

    lines = text.split("\n")
    result: dict[str, Any] = {
        "_obsidian_synced": True,
        "_obsidian_file": str(rules_file),
        "_obsidian_mtime": rules_file.stat().st_mtime,
    }

    # Parse threshold config table
    threshold_section = _find_section(lines, "阈值配置")
    if threshold_section:
        table_lines = lines[threshold_section[0] : threshold_section[1]]
        for row in _parse_table(table_lines):
            param = row.get("参数", "")
            val = _parse_number(row.get("值", ""))
            if param == "threshold" and val is not None:
                result["rulebook_threshold"] = val
            elif param == "push_threshold" and val is not None:
                result["rulebook_push_threshold"] = val
            elif param == "max_push" and val is not None:
                result["max_push"] = int(val)
            elif param == "min_amount" and val is not None:
                result["min_amount"] = val
            elif param == "min_turnover" and val is not None:
                result["min_turnover"] = val

    # Parse mode weights
    modes_section = _find_section(lines, "模式权重")
    if modes_section:
        section_lines = lines[modes_section[0] : modes_section[1]]
        mode_weights: dict[str, dict[str, float]] = {}
        current_mode = None
        for line in section_lines:
            mode_match = re.match(r"^###\s+(.+)", line)
            if mode_match:
                mode_name = mode_match.group(1).strip().lower()
                # Map Chinese names
                mode_map = {"价值": "value", "成长": "growth", "趋势": "trend", "事件": "event"}
                current_mode = mode_map.get(mode_name, mode_name)
                mode_weights[current_mode] = {}
                continue
            if current_mode and line.startswith("|") and not line.startswith("|---"):
                cells = [c.strip() for c in line[1:-1].split("|")]
                if len(cells) >= 2:
                    factor_name = cells[0].strip()
                    factor_val = _parse_number(cells[1])
                    if factor_name and factor_val is not None:
                        mode_weights[current_mode][factor_name] = factor_val
        if mode_weights:
            result["mode_weights"] = mode_weights

    # Parse emotion phases
    phase_section = _find_section(lines, "情绪阶段")
    if phase_section:
        table_lines = lines[phase_section[0] : phase_section[1]]
        phases: list[dict[str, Any]] = []
        for row in _parse_table(table_lines):
            phase_name = row.get("阶段", "")
            score_range = row.get("情绪评分", "")
            action = row.get("操作", "")
            position = row.get("仓位", "")
            # Parse score range like "<14" or "14-25"
            low = high = None
            range_match = re.match(r"([<>]=?)\s*(\d+)|(\d+)\s*-\s*(\d+)", score_range)
            if range_match:
                if range_match.group(1):
                    op = range_match.group(1)
                    val = int(range_match.group(2))
                    low, high = (0, val) if op.startswith("<") else (val, 100)
                else:
                    low, high = int(range_match.group(3)), int(range_match.group(4))
            phases.append({
                "phase": phase_name,
                "score_low": low,
                "score_high": high,
                "action": action,
                "position": position,
            })
        if phases:
            result["emotion_phases"] = phases

    return result


def sync_to_scoring(obsidian_config: dict[str, Any]) -> dict[str, Any]:
    """Apply Obsidian config values to the in-memory scoring.RULEBOOK_CONFIG.

    Returns a dict describing what was updated.
    """
    from . import scoring

    updates: dict[str, Any] = {}
    config = scoring.RULEBOOK_CONFIG

    # Map Obsidian keys (prefixed) to scoring config keys (unprefixed)
    key_map: dict[str, str] = {
        "rulebook_threshold": "threshold",
        "rulebook_push_threshold": "push_threshold",
        "max_push": "max_push",
        "min_amount": "min_amount",
        "min_turnover": "min_turnover",
    }
    for obsidian_key, config_key in key_map.items():
        if obsidian_key in obsidian_config:
            old = config.get(config_key)
            new = obsidian_config[obsidian_key]
            if old != new:
                config[config_key] = new
                updates[config_key] = {"old": old, "new": new}

    if "mode_weights" in obsidian_config:
        old_weights = dict(config.get("mode_weights", {}))
        new_weights = obsidian_config["mode_weights"]
        merged = dict(old_weights)
        for mode, weights in new_weights.items():
            if mode in merged:
                merged[mode] = {**merged[mode], **weights}
        if merged != old_weights:
            config["mode_weights"] = merged
            updates["mode_weights"] = {"old": old_weights, "new": merged}

    return updates
