"""
Minimal JSON-backed persistence for per-guild bot settings.
"""
import json
from pathlib import Path
from threading import Lock

SETTINGS_PATH = Path(__file__).parent / "data" / "settings.json"
_lock = Lock()


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
