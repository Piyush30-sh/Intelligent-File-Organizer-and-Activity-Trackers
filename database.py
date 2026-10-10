"""
database.py — MongoDB persistence layer for Smart File Organizer.

Collections
-----------
users          – user login (SHA-256 hashed passwords)
counters       – auto-increment ID sequences
free_trials    – per-machine free monitoring attempts (max 2)
user_settings  – user-specific monitored folder, preferences
user_logs      – per-user audit trail

All public functions are thread-safe (pymongo is thread-safe by design).
"""

import os
import hashlib
from datetime import datetime

from pymongo import MongoClient, DESCENDING

# ---------------------------------------------------------------------------
# MongoDB connection settings
# ---------------------------------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "smart_file_organizer")

_client: MongoClient | None = None
_db = None


def _get_db():
    """Return the MongoDB database handle (lazy singleton)."""
    global _client, _db
    if _db is None:
        if not MONGO_URI:
            raise RuntimeError("Set the MONGO_URI environment variable before starting the application.")
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = _client[MONGO_DB_NAME]
    return _db


# ---------------------------------------------------------------------------
# Auto-increment helper  (sequential integer IDs like a relational DB)
# ---------------------------------------------------------------------------
def _next_id(collection_name: str) -> int:
    """Return the next sequential integer id for *collection_name*."""
    db = _get_db()
    result = db.counters.find_one_and_update(
        {"_id": collection_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return result["seq"]


# ---------------------------------------------------------------------------
# Schema initialisation (called once at startup)
# ---------------------------------------------------------------------------
def init_db():
    """Create indexes / unique constraints. Safe to call repeatedly."""
    db = _get_db()

    # users — unique case-insensitive username
    db.users.create_index(
        [("username_lower", 1)],
        unique=True,
        name="uq_username_lower",
    )

    # free_trials — unique machine_id
    db.free_trials.create_index(
        [("machine_id", 1)],
        unique=True,
        name="uq_machine_id",
    )

    # user_settings — unique user_id
    db.user_settings.create_index(
        [("user_id", 1)],
        unique=True,
        name="uq_user_id",
    )

    # user_logs — fast lookup by user
    db.user_logs.create_index(
        [("user_id", 1), ("id", DESCENDING)],
        name="idx_user_logs_user",
    )


# ---------------------------------------------------------------------------
# Password hashing (SHA-256)
# ---------------------------------------------------------------------------
def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------
def create_user(username: str, password: str) -> bool:
    """Register a new user. Returns True on success, False if username taken."""
    db = _get_db()
    pw_hash = _hash_password(password)
    user_id = _next_id("users")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        db.users.insert_one({
            "id": user_id,
            "username": username,
            "username_lower": username.lower(),
            "password_hash": pw_hash,
            "created_at": now,
        })
        db.user_settings.insert_one({
            "id": _next_id("user_settings"),
            "user_id": user_id,
            "monitored_folder": "",
            "organize_folder": "",
            "last_login": "",
            "theme": "dark",
            "extra_json": "{}",
        })
        return True
    except Exception:
        # Duplicate username (unique index violation)
        return False


def authenticate_user(username: str, password: str):
    """Return user dict if credentials valid, else None."""
    db = _get_db()
    pw_hash = _hash_password(password)
    user = db.users.find_one({
        "username_lower": username.lower(),
        "password_hash": pw_hash,
    })
    if user:
        # update last_login in settings
        db.user_settings.update_one(
            {"user_id": user["id"]},
            {"$set": {"last_login": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}},
        )
        return {
            "id": user["id"],
            "username": user["username"],
            "password_hash": user["password_hash"],
            "created_at": user["created_at"],
        }
    return None


def get_user_by_id(user_id: int):
    """Return user dict or None."""
    db = _get_db()
    user = db.users.find_one({"id": user_id})
    if user:
        return {
            "id": user["id"],
            "username": user["username"],
            "password_hash": user["password_hash"],
            "created_at": user["created_at"],
        }
    return None


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------
def load_user_settings(user_id: int) -> dict:
    """Load all stored settings for a user. Returns dict with defaults."""
    db = _get_db()
    row = db.user_settings.find_one({"user_id": user_id})
    if row:
        return {
            "id": row.get("id"),
            "user_id": row["user_id"],
            "monitored_folder": row.get("monitored_folder", ""),
            "organize_folder": row.get("organize_folder", ""),
            "last_login": row.get("last_login", ""),
            "theme": row.get("theme", "dark"),
            "extra_json": row.get("extra_json", "{}"),
        }
    return {
        "user_id": user_id,
        "monitored_folder": "",
        "organize_folder": "",
        "last_login": "",
        "theme": "dark",
        "extra_json": "{}",
    }


def save_user_setting(user_id: int, key: str, value: str):
    """Update a single field in user_settings."""
    allowed = {"monitored_folder", "organize_folder", "extra_json", "theme"}
    if key not in allowed:
        return
    db = _get_db()
    db.user_settings.update_one(
        {"user_id": user_id},
        {"$set": {key: value}},
    )


# ---------------------------------------------------------------------------
# Free trial tracking  (machine_id = a hash of hostname + username)
# ---------------------------------------------------------------------------
def _machine_id() -> str:
    """Deterministic per-machine identifier (no PII stored)."""
    raw = f"{os.environ.get('COMPUTERNAME', '')}-{os.environ.get('USERNAME', '')}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def get_free_trial_info() -> dict:
    """Return {'attempts_used': int, 'max_attempts': int, 'blocked': bool}."""
    db = _get_db()
    mid = _machine_id()
    row = db.free_trials.find_one({"machine_id": mid})
    if row:
        return {
            "attempts_used": row["attempts_used"],
            "max_attempts": row["max_attempts"],
            "blocked": row["attempts_used"] >= row["max_attempts"],
        }
    # First time — create document
    db.free_trials.insert_one({
        "id": _next_id("free_trials"),
        "machine_id": mid,
        "attempts_used": 0,
        "max_attempts": 2,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return {"attempts_used": 0, "max_attempts": 2, "blocked": False}


def consume_free_trial() -> bool:
    """Use one free trial attempt. Returns True if successful, False if blocked."""
    db = _get_db()
    mid = _machine_id()
    row = db.free_trials.find_one({"machine_id": mid})
    if not row:
        db.free_trials.insert_one({
            "id": _next_id("free_trials"),
            "machine_id": mid,
            "attempts_used": 1,
            "max_attempts": 2,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return True
    if row["attempts_used"] >= row["max_attempts"]:
        return False
    db.free_trials.update_one(
        {"machine_id": mid},
        {"$inc": {"attempts_used": 1}},
    )
    return True


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def log_audit(user_id, event_type: str, detail: str):
    """Append one document to user_logs (user_id may be None for anonymous)."""
    db = _get_db()
    db.user_logs.insert_one({
        "id": _next_id("user_logs"),
        "user_id": user_id,
        "event_type": event_type,
        "detail": detail,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def get_user_logs(user_id: int, limit: int = 200) -> list:
    """Return recent audit documents for a user."""
    db = _get_db()
    cursor = db.user_logs.find(
        {"user_id": user_id},
        {"_id": 0},
    ).sort("id", DESCENDING).limit(limit)
    return list(cursor)
