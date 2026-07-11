"""Transactional SQLite storage for the desktop companion."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from .profile import (
    MAX_DEVICES,
    SCHEMA_VERSION,
    ProfileValidationError,
    default_profile,
    validate_command,
    validate_device,
    validate_identifier,
    validate_metadata,
    validate_profile,
    validate_transport,
)


DATABASE_SCHEMA_VERSION = 1
IDENTITY_KEYS = ("address", "host", "uuid", "service_id", "identifier")


class DatabaseError(RuntimeError):
    """Base class for expected companion database failures."""


class NotFoundError(DatabaseError):
    """Requested record does not exist."""


class ConflictError(DatabaseError):
    """Requested mutation conflicts with the current state."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _slug(value):
    characters = []
    separator = False
    for character in value:
        lowered = character.lower()
        if ("a" <= lowered <= "z") or ("0" <= lowered <= "9"):
            if separator and characters:
                characters.append("-")
            characters.append(lowered)
            separator = False
        else:
            separator = True
    return ("".join(characters) or "device")[:32]


def _next_id(name, existing):
    base = _slug(name)
    candidate = base
    suffix = 2
    while candidate in existing:
        ending = "-" + str(suffix)
        candidate = base[: 32 - len(ending)] + ending
        suffix += 1
    return candidate


def _copy(value):
    return json.loads(_json(value))


class CompanionDatabase:
    """Thread-safe, parameterized access to the companion SQLite database."""

    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize()

    def close(self):
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @contextmanager
    def _transaction(self):
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _initialize(self):
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > DATABASE_SCHEMA_VERSION:
            raise DatabaseError("database was created by a newer companion")
        with self._transaction():
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    sort_order INTEGER NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    transport_metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands (
                    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    command_json TEXT NOT NULL,
                    PRIMARY KEY (device_id, name),
                    UNIQUE (device_id, sort_order)
                );
                CREATE TABLE IF NOT EXISTS discoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity TEXT NOT NULL,
                    name TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    transport_metadata TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    UNIQUE (transport, identity)
                );
                CREATE INDEX IF NOT EXISTS discoveries_last_seen
                    ON discoveries(last_seen DESC, id ASC);
                """
            )
            self._connection.execute(
                "PRAGMA user_version = " + str(DATABASE_SCHEMA_VERSION)
            )
        count = self._connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        if count == 0:
            self.import_profile(default_profile())

    def _export_profile_locked(self):
        rows = self._connection.execute(
            """SELECT id, name, device_type, transport, transport_metadata
               FROM devices ORDER BY sort_order"""
        ).fetchall()
        devices = []
        for row in rows:
            command_rows = self._connection.execute(
                """SELECT name, command_json FROM commands
                   WHERE device_id = ? ORDER BY sort_order""",
                (row["id"],),
            ).fetchall()
            buttons = {
                command_row["name"]: json.loads(command_row["command_json"])
                for command_row in command_rows
            }
            devices.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "type": row["device_type"],
                    "transport": row["transport"],
                    "transport_metadata": json.loads(row["transport_metadata"]),
                    "buttons": buttons,
                }
            )
        active = self._connection.execute(
            "SELECT value FROM metadata WHERE key = ?", ("active_device",)
        ).fetchone()
        if not devices or active is None:
            raise DatabaseError("database profile is incomplete")
        return validate_profile(
            {
                "schema": SCHEMA_VERSION,
                "active_device": active["value"],
                "devices": devices,
            }
        )

    def export_profile(self):
        with self._lock:
            return self._export_profile_locked()

    def _replace_profile_locked(self, profile):
        created = {
            row["id"]: row["created_at"]
            for row in self._connection.execute(
                "SELECT id, created_at FROM devices"
            ).fetchall()
        }
        now = _utc_now()
        self._connection.execute("DELETE FROM devices")
        self._connection.execute(
            """INSERT INTO metadata(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            ("active_device", profile["active_device"]),
        )
        for device_order, device in enumerate(profile["devices"]):
            self._connection.execute(
                """INSERT INTO devices(
                       id, sort_order, name, device_type, transport,
                       transport_metadata, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    device["id"],
                    device_order,
                    device["name"],
                    device["type"],
                    device["transport"],
                    _json(device["transport_metadata"]),
                    created.get(device["id"], now),
                    now,
                ),
            )
            for command_order, (name, command) in enumerate(
                device["buttons"].items()
            ):
                self._connection.execute(
                    """INSERT INTO commands(
                           device_id, name, sort_order, command_json
                       ) VALUES (?, ?, ?, ?)""",
                    (device["id"], name, command_order, _json(command)),
                )

    def import_profile(self, profile):
        canonical = validate_profile(profile)
        with self._lock, self._transaction():
            self._replace_profile_locked(canonical)
        return self.export_profile()

    def _mutate_profile(self, mutation):
        with self._lock:
            profile = self._export_profile_locked()
            result = mutation(profile)
            canonical = validate_profile(profile)
            with self._transaction():
                self._replace_profile_locked(canonical)
            return _copy(result(canonical) if callable(result) else result)

    @staticmethod
    def _find_device(profile, device_id):
        for device in profile["devices"]:
            if device["id"] == device_id:
                return device
        return None

    @staticmethod
    def _summary(device, active_device):
        return {
            "id": device["id"],
            "name": device["name"],
            "type": device["type"],
            "transport": device["transport"],
            "transport_metadata": _copy(device["transport_metadata"]),
            "button_count": len(device["buttons"]),
            "active": device["id"] == active_device,
        }

    def list_devices(self):
        profile = self.export_profile()
        return [
            self._summary(device, profile["active_device"])
            for device in profile["devices"]
        ]

    def get_device(self, device_id):
        device_id = validate_identifier(device_id, "device id", 32)
        profile = self.export_profile()
        device = self._find_device(profile, device_id)
        if device is None:
            raise NotFoundError("device does not exist")
        result = _copy(device)
        result["active"] = device_id == profile["active_device"]
        return result

    def create_device(self, payload):
        if not isinstance(payload, dict):
            raise ProfileValidationError("device payload must be an object")
        allowed = {
            "id",
            "name",
            "type",
            "transport",
            "transport_metadata",
            "buttons",
            "active",
        }
        unknown = set(payload).difference(allowed)
        if unknown:
            raise ProfileValidationError(
                "unsupported device field: " + sorted(unknown)[0]
            )
        if not isinstance(payload.get("name"), str):
            raise ProfileValidationError("device name must be text")
        if "active" in payload and not isinstance(payload["active"], bool):
            raise ProfileValidationError("active must be boolean")

        def mutation(profile):
            existing = {device["id"] for device in profile["devices"]}
            requested_id = payload.get("id")
            identifier = (
                validate_identifier(requested_id, "device id", 32)
                if requested_id is not None
                else _next_id(payload["name"], existing)
            )
            if identifier in existing:
                raise ConflictError("device id already exists")
            device = validate_device(
                {
                    "id": identifier,
                    "name": payload["name"],
                    "type": payload.get("type", "generic"),
                    "transport": payload.get("transport", "ir"),
                    "transport_metadata": payload.get("transport_metadata", {}),
                    "buttons": payload.get("buttons", {}),
                }
            )
            profile["devices"].append(device)
            if payload.get("active", True):
                profile["active_device"] = identifier
            return lambda canonical: self._summary(
                self._find_device(canonical, identifier), canonical["active_device"]
            )

        return self._mutate_profile(mutation)

    def update_device(self, device_id, patch):
        device_id = validate_identifier(device_id, "device id", 32)
        if not isinstance(patch, dict) or not patch:
            raise ProfileValidationError("device patch must be a non-empty object")
        allowed = {"name", "type", "transport", "transport_metadata", "active"}
        unknown = set(patch).difference(allowed)
        if unknown:
            raise ProfileValidationError(
                "unsupported device field: " + sorted(unknown)[0]
            )
        if "active" in patch and not isinstance(patch["active"], bool):
            raise ProfileValidationError("active must be boolean")

        def mutation(profile):
            device = self._find_device(profile, device_id)
            if device is None:
                raise NotFoundError("device does not exist")
            for key in ("name", "type", "transport", "transport_metadata"):
                if key in patch:
                    device[key] = patch[key]
            if patch.get("active") is True:
                profile["active_device"] = device_id
            elif patch.get("active") is False and profile["active_device"] == device_id:
                raise ConflictError("choose another active device instead")
            return lambda canonical: self._summary(
                self._find_device(canonical, device_id), canonical["active_device"]
            )

        return self._mutate_profile(mutation)

    def delete_device(self, device_id):
        device_id = validate_identifier(device_id, "device id", 32)

        def mutation(profile):
            index = next(
                (
                    index
                    for index, device in enumerate(profile["devices"])
                    if device["id"] == device_id
                ),
                None,
            )
            if index is None:
                raise NotFoundError("device does not exist")
            if len(profile["devices"]) == 1:
                deleted = profile["devices"][0]
                replacement = default_profile()
                profile.clear()
                profile.update(replacement)
                return deleted
            deleted = profile["devices"].pop(index)
            if profile["active_device"] == device_id:
                replacement = min(index, len(profile["devices"]) - 1)
                profile["active_device"] = profile["devices"][replacement]["id"]
            return deleted

        return self._mutate_profile(mutation)

    def get_buttons(self, device_id):
        return self.get_device(device_id)["buttons"]

    def put_button(self, device_id, name, command):
        device_id = validate_identifier(device_id, "device id", 32)
        if not isinstance(name, str) or not name or len(name) > 48:
            raise ProfileValidationError("button name has an invalid length")
        command = validate_command(command)

        def mutation(profile):
            device = self._find_device(profile, device_id)
            if device is None:
                raise NotFoundError("device does not exist")
            device["buttons"][name] = command
            return command

        return self._mutate_profile(mutation)

    def delete_button(self, device_id, name):
        device_id = validate_identifier(device_id, "device id", 32)
        if not isinstance(name, str) or not name or len(name) > 48:
            raise ProfileValidationError("button name has an invalid length")

        def mutation(profile):
            device = self._find_device(profile, device_id)
            if device is None:
                raise NotFoundError("device does not exist")
            if name not in device["buttons"]:
                raise NotFoundError("button does not exist")
            return device["buttons"].pop(name)

        return self._mutate_profile(mutation)

    @staticmethod
    def _validate_discovery(payload):
        if not isinstance(payload, dict):
            raise ProfileValidationError("discovery payload must be an object")
        allowed = {
            "identity",
            "name",
            "type",
            "transport",
            "transport_metadata",
        }
        unknown = set(payload).difference(allowed)
        if unknown:
            raise ProfileValidationError(
                "unsupported discovery field: " + sorted(unknown)[0]
            )
        name = payload.get("name")
        if not isinstance(name, str) or not name or len(name) > 48:
            raise ProfileValidationError("device name has an invalid length")
        device_type = validate_identifier(
            payload.get("type", "generic"), "device type", 24
        )
        transport = validate_transport(payload.get("transport"))
        metadata = validate_metadata(payload.get("transport_metadata", {}))
        identity = payload.get("identity")
        if identity is not None:
            if (
                not isinstance(identity, str)
                or not identity
                or len(identity) > 160
                or any(ord(character) < 32 for character in identity)
            ):
                raise ProfileValidationError("discovery identity is invalid")
        else:
            identity = None
            for key in IDENTITY_KEYS:
                if key in metadata:
                    identity = key + "=" + str(metadata[key])
                    break
            if identity is None:
                material = _json({"name": name, "metadata": metadata}).encode("utf-8")
                identity = "sha256=" + hashlib.sha256(material).hexdigest()
        return {
            "identity": identity,
            "name": name,
            "type": device_type,
            "transport": transport,
            "transport_metadata": metadata,
        }

    @staticmethod
    def _discovery_row(row):
        return {
            "id": row["id"],
            "identity": row["identity"],
            "name": row["name"],
            "type": row["device_type"],
            "transport": row["transport"],
            "transport_metadata": json.loads(row["transport_metadata"]),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "controllable": False,
        }

    def list_discoveries(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM discoveries ORDER BY last_seen DESC, id ASC"
            ).fetchall()
            return [self._discovery_row(row) for row in rows]

    def get_discovery(self, discovery_id):
        if isinstance(discovery_id, bool) or not isinstance(discovery_id, int):
            raise ProfileValidationError("discovery id must be an integer")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM discoveries WHERE id = ?", (discovery_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("discovery does not exist")
            return self._discovery_row(row)

    def upsert_discovery(self, payload):
        discovery = self._validate_discovery(payload)
        now = _utc_now()
        with self._lock, self._transaction():
            self._connection.execute(
                """INSERT INTO discoveries(
                       identity, name, device_type, transport,
                       transport_metadata, first_seen, last_seen
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(transport, identity) DO UPDATE SET
                       name = excluded.name,
                       device_type = excluded.device_type,
                       transport_metadata = excluded.transport_metadata,
                       last_seen = excluded.last_seen""",
                (
                    discovery["identity"],
                    discovery["name"],
                    discovery["type"],
                    discovery["transport"],
                    _json(discovery["transport_metadata"]),
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM discoveries WHERE transport = ? AND identity = ?",
                (discovery["transport"], discovery["identity"]),
            ).fetchone()
            result = self._discovery_row(row)
        return result

    def delete_discovery(self, discovery_id):
        discovery = self.get_discovery(discovery_id)
        with self._lock, self._transaction():
            self._connection.execute(
                "DELETE FROM discoveries WHERE id = ?", (discovery_id,)
            )
        return discovery

    def clear_discoveries(self):
        with self._lock, self._transaction():
            count = self._connection.execute(
                "SELECT COUNT(*) FROM discoveries"
            ).fetchone()[0]
            self._connection.execute("DELETE FROM discoveries")
        return count

    @staticmethod
    def _same_metadata(first, second):
        for key in IDENTITY_KEYS:
            if key in first and key in second and first[key] == second[key]:
                return True
        return first == second

    def save_discoveries(self, discovery_ids=None, save_all=False):
        """Save one, several, or all discoveries as badge device entries."""

        if not isinstance(save_all, bool):
            raise ProfileValidationError("all must be boolean")
        if save_all:
            if discovery_ids not in (None, []):
                raise ProfileValidationError("choose ids or all, not both")
            discoveries = self.list_discoveries()
        else:
            if not isinstance(discovery_ids, list) or not discovery_ids:
                raise ProfileValidationError("ids must be a non-empty list")
            if len(discovery_ids) > MAX_DEVICES:
                raise ProfileValidationError("too many discovery ids")
            seen = set()
            discoveries = []
            for discovery_id in discovery_ids:
                if (
                    isinstance(discovery_id, bool)
                    or not isinstance(discovery_id, int)
                    or discovery_id < 1
                ):
                    raise ProfileValidationError("discovery id must be positive")
                if discovery_id not in seen:
                    discoveries.append(self.get_discovery(discovery_id))
                    seen.add(discovery_id)
        if not discoveries:
            return []

        def mutation(profile):
            selected_ids = []
            existing_ids = {device["id"] for device in profile["devices"]}
            for discovery in discoveries:
                found = None
                for device in profile["devices"]:
                    if device["transport"] == discovery["transport"] and self._same_metadata(
                        device["transport_metadata"],
                        discovery["transport_metadata"],
                    ):
                        found = device
                        break
                if found is None:
                    if len(profile["devices"]) >= MAX_DEVICES:
                        raise ConflictError("badge device limit would be exceeded")
                    identifier = _next_id(discovery["name"], existing_ids)
                    existing_ids.add(identifier)
                    found = {
                        "id": identifier,
                        "name": discovery["name"],
                        "type": discovery["type"],
                        "transport": discovery["transport"],
                        "transport_metadata": discovery["transport_metadata"],
                        "buttons": {},
                    }
                    profile["devices"].append(found)
                else:
                    found["name"] = discovery["name"]
                    found["type"] = discovery["type"]
                    found["transport_metadata"] = discovery["transport_metadata"]
                selected_ids.append(found["id"])
            return lambda canonical: [
                self._summary(
                    self._find_device(canonical, identifier),
                    canonical["active_device"],
                )
                for identifier in selected_ids
            ]

        return self._mutate_profile(mutation)
