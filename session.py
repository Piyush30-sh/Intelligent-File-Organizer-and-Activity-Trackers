"""
session.py — Authentication, session management, and free-trial enforcement.

Provides a single SessionManager instance that the GUI imports.
All state is backed by the MongoDB database (database.py).

Usage
-----
    from session import session_mgr

    # Attempt login
    ok, msg = session_mgr.login("alice", "password123")

    # Check status
    if session_mgr.is_logged_in:
        settings = session_mgr.user_settings  # auto-loaded on login

    # Free trial (monitoring without login)
    can, msg = session_mgr.try_free_monitoring()
"""

from __future__ import annotations

import database as db


class SessionManager:
    """Singleton-style session tracker (one user at a time)."""

    def __init__(self):
        self._user: dict | None = None          # users row
        self._settings: dict | None = None      # user_settings row
        self._is_logged_in: bool = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def is_logged_in(self) -> bool:
        return self._is_logged_in

    @property
    def user(self) -> dict | None:
        return self._user

    @property
    def user_id(self) -> int | None:
        return self._user["id"] if self._user else None

    @property
    def username(self) -> str:
        return self._user["username"] if self._user else ""

    @property
    def user_settings(self) -> dict:
        if self._settings is None:
            return {
                "monitored_folder": "",
                "organize_folder": "",
                "last_login": "",
                "extra_json": "{}",
            }
        return dict(self._settings)

    def register(self, username: str, password: str) -> tuple[bool, str]:
        """Create a new user account. Returns (success, message)."""
        username = username.strip()
        if not username:
            return False, "Username cannot be empty."
        if len(username) < 3:
            return False, "Username must be at least 3 characters."
        if not password:
            return False, "Password cannot be empty."
        if len(password) < 4:
            return False, "Password must be at least 4 characters."

        ok = db.create_user(username, password)
        if ok:
            db.log_audit(None, "register", f"New user registered: {username}")
            return True, "Account created successfully! You can now log in."
        return False, "Username already exists. Please choose another."


    def login(self, username: str, password: str) -> tuple[bool, str]:
        """Authenticate and load user data. Returns (success, message)."""
        username = username.strip()
        if not username or not password:
            return False, "Please enter both username and password."

        user = db.authenticate_user(username, password)
        if user is None:
            return False, "Invalid username or password."

        self._user = user
        self._settings = db.load_user_settings(user["id"])
        self._is_logged_in = True
        db.log_audit(user["id"], "login", f"User '{username}' logged in")
        return True, f"Welcome back, {username}!"

    def logout(self):
        """Clear session state."""
        if self._user:
            db.log_audit(self._user["id"], "logout", f"User '{self.username}' logged out")
        self._user = None
        self._settings = None
        self._is_logged_in = False

    def save_setting(self, key: str, value: str):
        """Persist a single setting and update the local cache."""
        if not self._is_logged_in or not self._user:
            return
        db.save_user_setting(self._user["id"], key, value)
        if self._settings:
            self._settings[key] = value

    def reload_settings(self):
        """Force-reload settings from DB."""
        if self._user:
            self._settings = db.load_user_settings(self._user["id"])

    # ------------------------------------------------------------------
    # Free trial (monitoring without login)
    # ------------------------------------------------------------------
    def try_free_monitoring(self) -> tuple[bool, str]:
        """Attempt to use one free monitoring try.
        Returns (allowed, message).
        """
        info = db.get_free_trial_info()
        if info["blocked"]:
            return False, (
                f"Free trial exhausted ({info['max_attempts']}/{info['max_attempts']} used).\n"
                "Please log in or create an account to continue monitoring."
            )
        ok = db.consume_free_trial()
        if ok:
            remaining = info["max_attempts"] - info["attempts_used"] - 1
            db.log_audit(None, "free_trial", f"Free monitoring attempt used. {remaining} remaining.")
            return True, f"Free trial: {remaining} attempt(s) remaining."
        return False, "Free trial exhausted. Please log in to continue."

    def free_trial_remaining(self) -> int:
        """Return how many free tries are left (0 = blocked)."""
        info = db.get_free_trial_info()
        return max(0, info["max_attempts"] - info["attempts_used"])

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------
    def audit(self, event_type: str, detail: str):
        """Write an audit-log row tied to the current user (or None)."""
        db.log_audit(self.user_id, event_type, detail)


# Module-level singleton
session_mgr = SessionManager()
