"""
Minimal JSON-backed persistence for per-guild bot settings, including a
capped history of recently sent trivia facts (used both to show "what have
you sent me lately" and to quiz from that history).
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

SETTINGS_PATH = Path(__file__).parent / "data" / "settings.json"
_lock = Lock()

MAX_HISTORY = 50


def _load() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def set_trivia_channel(guild_id: int, channel_id: int) -> None:
    with _lock:
        data = _load()
        data[str(guild_id)] = {**data.get(str(guild_id), {}), "trivia_channel_id": channel_id}
        _save(data)


def get_trivia_channel(guild_id: int) -> int | None:
    data = _load()
    entry = data.get(str(guild_id))
    return entry.get("trivia_channel_id") if entry else None


def all_trivia_channels() -> dict[int, int]:
    """Returns {guild_id: channel_id} for every guild that has a channel set."""
    data = _load()
    result = {}
    for guild_id, entry in data.items():
        if entry.get("trivia_channel_id"):
            result[int(guild_id)] = entry["trivia_channel_id"]
    return result


def add_trivia_history(guild_id: int, kind: str, record: dict) -> None:
    """Appends a sent-trivia record for a guild, keeping only the most
    recent MAX_HISTORY entries per kind ('areacode' or 'county')."""
    with _lock:
        data = _load()
        entry = data.get(str(guild_id), {})
        history = entry.get("trivia_history", [])
        history.append({
            "kind": kind,
            "record": record,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        })
        # cap each kind independently so a burst of one type can't push the
        # other type out of the retained history
        by_kind: dict[str, list] = {}
        for h in history:
            by_kind.setdefault(h.get("kind", "areacode"), []).append(h)
        trimmed = []
        for k, items in by_kind.items():
            trimmed.extend(items[-MAX_HISTORY:])
        trimmed.sort(key=lambda h: h["sent_at"])
        entry["trivia_history"] = trimmed
        data[str(guild_id)] = entry
        _save(data)


def get_trivia_history(guild_id: int, kind: Optional[str] = None) -> list[dict]:
    """Returns [{"kind": ..., "record": {...}, "sent_at": iso_string}, ...],
    oldest first. Pass kind='areacode' or kind='county' to filter."""
    data = _load()
    entry = data.get(str(guild_id), {})
    history = entry.get("trivia_history", [])
    if kind is None:
        return history
    return [h for h in history if h.get("kind", "areacode") == kind]
