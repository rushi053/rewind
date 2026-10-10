"""Project configuration stored as JSON in the store directory."""

import copy
import json

DEFAULTS = {
    "max_file_size_mb": 50,
    "include_git": True,
    "watch": {
        "interval": 1.0,
        "debounce": 2.0,
        "max_wait": 30.0,
    },
    "guard": {
        "mode": "block",  # block | snapshot | warn | off
        "allow": [],  # fnmatch patterns matched against the whole command
        "disable": [],  # rule ids to turn off
    },
    "gc": {
        "keep_all_hours": 24,
        "hourly_days": 7,
        "daily_days": 30,
    },
}

DESCRIPTIONS = {
    "max_file_size_mb": "Files larger than this are skipped (and reported by `rewind doctor`).",
    "include_git": "Snapshot the .git directory too, so a corrupted or reset repo can be restored.",
    "watch.interval": "Seconds between polls in `rewind watch` / `rewind run`.",
    "watch.debounce": "Seconds of quiet before the watcher takes a snapshot.",
    "watch.max_wait": "Snapshot anyway after this many seconds of continuous changes.",
    "guard.mode": "What the seatbelt does with a destructive command: block | snapshot | warn | off.",
    "guard.allow": "Commands matching these glob patterns are never flagged.",
    "guard.disable": "Seatbelt rule ids to turn off (see `rewind guard rules`).",
    "gc.keep_all_hours": "Keep every snapshot younger than this.",
    "gc.hourly_days": "Then keep one snapshot per hour for this many days.",
    "gc.daily_days": "Then keep one snapshot per day for this many days; older ones are deleted.",
}


def merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, path):
        self.path = path
        self.user = {}
        if path.exists():
            try:
                self.user = json.loads(path.read_text("utf-8") or "{}")
            except ValueError as exc:
                raise ConfigError("%s is not valid JSON: %s" % (path, exc))
        self.data = merge(DEFAULTS, self.user)

    def get(self, key, default=None):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key, raw_value):
        if key not in DESCRIPTIONS:
            raise ConfigError("unknown config key %r (see `rewind config`)" % key)
        value = parse_value(raw_value)
        default = self._default(key)
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError("%s expects true or false" % key)
        elif isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError("%s expects a number" % key)
        elif isinstance(default, list):
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                raise ConfigError("%s expects a list" % key)
        if key == "guard.mode" and value not in ("block", "snapshot", "warn", "off"):
            raise ConfigError("guard.mode must be one of block, snapshot, warn, off")
        node = self.user
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self.save()

    def unset(self, key):
        node = self.user
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.get(part, {})
        node.pop(parts[-1], None)
        self.save()

    def append(self, key, value):
        current = list(self.get(key) or [])
        if value not in current:
            current.append(value)
        self.set(key, json.dumps(current))

    def save(self):
        self.path.write_text(json.dumps(self.user, indent=2, sort_keys=True) + "\n", "utf-8")
        self.data = merge(DEFAULTS, self.user)

    def _default(self, key):
        node = DEFAULTS
        for part in key.split("."):
            node = node[part]
        return node

    def flat(self):
        out = []
        for key in DESCRIPTIONS:
            out.append((key, self.get(key), self._default(key)))
        return out


def parse_value(raw):
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw
