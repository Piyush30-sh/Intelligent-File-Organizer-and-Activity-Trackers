import os
import sys
import logging
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
import threading
from plyer import notification
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import hashlib
import time

# --- New modular imports ---
import database as db
from folder_guard import is_folder_safe, get_block_reason
from session import session_mgr

STABLE_TIME = 0.8
NOTIFY_BATCH_INTERVAL = 0.5
BG_RESIZE_DEBOUNCE_MS = 120
BG_RESIZE_SETTLE_MS = 280
LAYOUT_RESIZE_DEBOUNCE_MS = 60
DUP_SCAN_PROGRESS_UPDATE_MS = 120
DUP_SCAN_PROGRESS_STEP = 25
LIVE_LOG_FLUSH_MS = 45
ANIMATION_DELAY_SCALE = 1.12
ANIMATION_MINIMIZED_SCALE = 3.8
SETTINGS_PANEL_WIDTH = 360
SETTINGS_PANEL_HEIGHT = 242
SETTINGS_SWITCH_WIDTH = 46
SETTINGS_SWITCH_HEIGHT = 24
FILE_HASH_CHUNK_SIZE = 262144
LOW_ANIMATION_MODE = {"enabled": False}


def _anim_delay(normal_ms, low_ms=900):
    if LOW_ANIMATION_MODE.get("enabled"):
        return int(low_ms)
    delay = max(1, int(normal_ms * ANIMATION_DELAY_SCALE))
    try:
        root = tk._default_root
        if root is not None and root.winfo_exists() and str(root.state()) == "iconic":
            return max(delay, int(normal_ms * ANIMATION_MINIMIZED_SCALE))
    except Exception:
        pass
    return delay

# Notification branding
APP_NAME = "Intelligent FO & AT"
_NOTIFY_ICON = os.path.join(
    getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))),
    "activity.ico"
)
SAME_FOLDER_RENAME_WINDOW = 2.0

# Extensions treated as IDE-managed code/text files.
# For these, a longer quiet window + per-file cooldown prevents notification spam
# from auto-save features in VS Code, IntelliJ, PyCharm, etc.
IDE_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cpp", ".c", ".h", ".cc",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".dart",
    ".html", ".css", ".scss", ".less", ".json", ".xml", ".yaml", ".yml",
    ".md", ".sh", ".bat", ".ps1", ".sql", ".r", ".vue", ".svelte",
}
# Seconds of write-silence before promoting a code-file modification
IDE_DEBOUNCE_TIME = 5.0
# Minimum seconds between two notifications for the same code file.
# Suppresses repeated pings from every auto-save keystroke.
IDE_COOLDOWN = 15.0

IGNORED_EXTENSIONS = {".tmp", ".part", ".crdownload", ".lock"}
IGNORED_NAME_PATTERNS = ("~$", ".tmp", ".part", ".crdownload", "rf")

WINDOWS_PLACEHOLDER_NAMES = (
    "new text document",
    "new folder",
    "new bitmap image",
    "new microsoft"
)

# =========================
# LOGGING
# =========================
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/activity.log",
    level=logging.INFO,
    format="%(asctime)s - %(message)s"
)

# =========================
# DATABASE INIT
# =========================
db.init_db()

FRIENDLY = {
    "file_created": "File Created",
    "file_deleted": "File Deleted",
    "file_modified": "File Modified",
    "file_moved": "File Moved",
    "file_renamed": "File Renamed",
    "folder_created": "Folder Created",
    "folder_deleted": "Folder Deleted",
    "folder_moved": "Folder Moved",
    "folder_renamed": "Folder Renamed",
}

# =========================
# UTILITIES
# =========================
def file_hash(path):
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(FILE_HASH_CHUNK_SIZE), b""):
                h.update(c)
        return h.hexdigest()
    except Exception:
        return None

def _is_file_locked(path):
    """Return True if another process still has the file open/locked.
    On Windows most editors hold an exclusive write lock while saving.
    We try to open the file for writing; if it raises PermissionError the
    app has not released it yet.
    """
    try:
        with open(path, "r+b"):
            return False
    except (IOError, PermissionError, OSError):
        return True

def is_noise(path):
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1]
    name_no_ext = os.path.splitext(name)[0]
    
    if ext in IGNORED_EXTENSIONS:
        return True
    if any(p in name for p in IGNORED_NAME_PATTERNS):
        return True
    # Office temporary files (Excel, Word, etc.)
    if name.startswith("~") and (ext == ".tmp" or name.startswith("~$")):
        return True
    if "~rf" in name and name.endswith(".tmp"):
        return True
    # Excel 8-character hex temp files (like DF3A3600, 78BE2600)
    if len(name_no_ext) == 8 and all(c in "0123456789abcdef" for c in name_no_ext):
        return True
    return False

def is_windows_placeholder(path):
    name = os.path.basename(path).lower()
    return any(name.startswith(p) for p in WINDOWS_PLACEHOLDER_NAMES)

def _format_display_path(path, base_dir=None):
    name = os.path.basename(path)
    if base_dir:
        try:
            rel = os.path.relpath(path, base_dir)
            if not rel.startswith(".."):
                root_name = os.path.basename(base_dir.rstrip(os.sep))
                if rel == ".":
                    return f"{name} | {root_name}"
                return f"{name} | {root_name}{os.sep}{rel}"
        except Exception:
            pass
    return f"{name} | {path}"

def _format_event_item(event, item, base_dir=None):
    if isinstance(item, tuple):
        old, new = item
        return f"{_format_display_path(old, base_dir)} -> {_format_display_path(new, base_dir)}"
    return _format_display_path(item, base_dir)

def prepare_notification_message(paths, base_dir=None):
    names = [_format_event_item(None, p, base_dir) for p in paths]
    msg = ", ".join(names[:5])
    if len(names) > 5:
        msg += f", +{len(names)-5} more..."
    return msg[:250]

# =========================
# LIVE LOG WIDGET
# =========================
live_log_window = None
live_log_widget = None
log_history = []
live_log_pending = []
live_log_flush_job = None
event_filters = {
    "created": True,
    "deleted": True,
    "modified": True,
    "moved": True,
    "renamed": True,
}
object_filters = {
    "file": True,
    "folder": True,
}
event_colors = {
      "created": ("#142845", "#ffffff"),
    "deleted": ("#142845", "#ffffff"),
    "modified": ("#142845", "#ffffff"),
    "moved": ("#142845", "#ffffff"),
    "renamed": ("#142845", "#ffffff"),
    "bookmark": ("#0d2e4f", "#9fd3ff"),
}
paused_state = {"paused": False}
search_state = {"hits": [], "index": -1}

def live_log_available():
    return live_log_widget is not None and live_log_widget.winfo_exists()

# =========================
# IGNORE EVENTS
# =========================
# All files currently being moved by Organize Folder will be added here
ignore_events = set()

def open_live_log():
    global live_log_window, live_log_widget
    if live_log_window and tk.Toplevel.winfo_exists(live_log_window):
        live_log_window.lift()
        return
    live_log_window = tk.Toplevel()
    live_log_window.title("Live Activity Log")
    live_log_window.geometry("820x560")
    live_log_window.minsize(680, 420)

    log_bg = "#0f1f35"
    log_panel = "#142845"
    log_text = "#e6f0ff"
    log_muted = "#9fb4d1"
    live_log_window.configure(bg=log_bg)

    header = tk.Frame(live_log_window, bg=log_panel, highlightthickness=1, highlightbackground="#203d63")
    header.pack(fill="x", padx=16, pady=(14, 10))

    title = tk.Label(
        header,
        text="LIVE ACTIVITY LOG",
        font=("Segoe UI", 12, "bold"),
        fg=log_text,
        bg=log_panel
    )
    title.pack(pady=(10, 2))

    subtitle = tk.Label(
        header,
        text="Streaming file system events in real time",
        font=("Segoe UI", 9),
        fg=log_muted,
        bg=log_panel
    )
    subtitle.pack(pady=(0, 10))

    body = tk.Frame(live_log_window, bg=log_bg)
    body.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    # Controls row (filters + actions)
    controls = tk.Frame(body, bg=log_bg)
    controls.pack(fill="x", pady=(0, 8))

    filters_frame = tk.Frame(controls, bg=log_bg)
    filters_frame.pack(side="left", anchor="w")

    def refresh_log_view():
        global live_log_flush_job
        if not live_log_available():
            return
        if live_log_flush_job is not None:
            try:
                live_log_widget.after_cancel(live_log_flush_job)
            except Exception:
                pass
            live_log_flush_job = None
        live_log_pending.clear()
        live_log_widget.config(state="normal")
        live_log_widget.delete("1.0", tk.END)
        for evt_key, msg, obj_kind in log_history:
            if event_filters.get(evt_key, True) and object_filters.get(obj_kind, True):
                live_log_widget.insert(tk.END, msg + "\n", (evt_key, obj_kind))
        live_log_widget.see(tk.END)
        live_log_widget.config(state="disabled")
        search_state["hits"] = []
        search_state["index"] = -1

    def make_event_toggle(evt_key, label_text):
        var = tk.BooleanVar(value=event_filters.get(evt_key, True))

        def on_toggle():
            event_filters[evt_key] = var.get()
            refresh_log_view()
        chk = tk.Checkbutton(
            filters_frame,
            text=label_text,
            variable=var,
            onvalue=True,
            offvalue=False,
            fg=log_text,
            bg=log_bg,
            selectcolor="#1f3555",
            activebackground=log_bg,
            activeforeground=log_text,
            font=("Segoe UI", 9)
        )
        chk.configure(command=on_toggle)
        chk.pack(side="left", padx=(0, 10))

    def make_object_toggle(kind_key, label_text):
        var = tk.BooleanVar(value=object_filters.get(kind_key, True))

        def on_toggle():
            object_filters[kind_key] = var.get()
            refresh_log_view()
        chk = tk.Checkbutton(
            filters_frame,
            text=label_text,
            variable=var,
            onvalue=True,
            offvalue=False,
            fg=log_text,
            bg=log_bg,
            selectcolor="#1f3555",
            activebackground=log_bg,
            activeforeground=log_text,
            font=("Segoe UI", 9)
        )
        chk.configure(command=on_toggle)
        chk.pack(side="left", padx=(0, 10))

    make_event_toggle("created", "Created")
    make_event_toggle("deleted", "Deleted")
    make_event_toggle("modified", "Modified")
    make_event_toggle("moved", "Moved")
    make_event_toggle("renamed", "Renamed")
    make_object_toggle("file", "Files")
    make_object_toggle("folder", "Folders")

    actions = tk.Frame(controls, bg=log_bg)
    actions.pack(side="right", anchor="e")

    def toggle_pause():
        paused_state["paused"] = not paused_state["paused"]
        pause_btn.config(text="Resume" if paused_state["paused"] else "Pause")

    def clear_log():
        global live_log_flush_job
        if not live_log_available():
            return
        if live_log_flush_job is not None:
            try:
                live_log_widget.after_cancel(live_log_flush_job)
            except Exception:
                pass
            live_log_flush_job = None
        live_log_pending.clear()
        live_log_widget.config(state="normal")
        live_log_widget.delete("1.0", tk.END)
        live_log_widget.config(state="disabled")
        search_state["hits"] = []
        search_state["index"] = -1

    def add_bookmark():
        marker = f"{datetime.now().strftime('%H:%M:%S')} - --- BOOKMARK ---"
        if live_log_available():
            _insert_log_line("bookmark", marker, "file")

    def export_snapshot():
        if not live_log_available():
            return
        text = live_log_widget.get("1.0", tk.END).strip()
        if not text:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"log_snapshot_{ts}.txt"
        try:
            os.makedirs("logs", exist_ok=True)
        except Exception:
            pass
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialdir=os.path.abspath("logs"),
            initialfile=default_name,
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass
        live_log_window.clipboard_clear()
        live_log_window.clipboard_append(text)

    pause_btn = tk.Button(
        actions,
        text="Pause",
        command=toggle_pause,
        bg="#1c3357",
        fg=log_text,
        activebackground="#25446f",
        activeforeground=log_text,
        relief="flat",
        font=("Segoe UI", 9)
    )
    pause_btn.pack(side="left", padx=6)
    if paused_state.get("paused"):
        pause_btn.config(text="Resume")

    export_btn = tk.Button(
        actions,
        text="Export",
        command=export_snapshot,
        bg="#1c3357",
        fg=log_text,
        activebackground="#25446f",
        activeforeground=log_text,
        relief="flat",
        font=("Segoe UI", 9)
    )
    export_btn.pack(side="left", padx=6)

    bookmark_btn = tk.Button(
        actions,
        text="Bookmark",
        command=add_bookmark,
        bg="#1c3357",
        fg=log_text,
        activebackground="#25446f",
        activeforeground=log_text,
        relief="flat",
        font=("Segoe UI", 9)
    )
    bookmark_btn.pack(side="left", padx=6)

    clear_btn = tk.Button(
        actions,
        text="Clear",
        command=clear_log,
        bg="#1c3357",
        fg=log_text,
        activebackground="#25446f",
        activeforeground=log_text,
        relief="flat",
        font=("Segoe UI", 9)
    )
    clear_btn.pack(side="left", padx=6)

    # Search row
    search_row = tk.Frame(body, bg=log_bg)
    search_row.pack(fill="x", pady=(0, 8))
    search_label = tk.Label(search_row, text="Search:", fg=log_text, bg=log_bg, font=("Segoe UI", 9))
    search_label.pack(side="left")
    search_var = tk.StringVar()
    search_entry = tk.Entry(search_row, textvariable=search_var, bg=log_panel, fg=log_text, insertbackground=log_text, relief="flat", highlightthickness=1, highlightbackground="#203d63")
    search_entry.pack(side="left", fill="x", expand=True, padx=8)

    def refresh_search_tags(pattern):
        w = live_log_widget
        w.tag_remove("search_hit", "1.0", tk.END)
        w.tag_remove("search_active", "1.0", tk.END)
        search_state["hits"] = []
        search_state["index"] = -1
        if not pattern:
            return
        idx = "1.0"
        hits = []
        while True:
            idx = w.search(pattern, idx, nocase=True, stopindex=tk.END)
            if not idx:
                break
            end = f"{idx}+{len(pattern)}c"
            w.tag_add("search_hit", idx, end)
            hits.append((idx, end))
            idx = end
        search_state["hits"] = hits
        if hits:
            search_state["index"] = 0
            w.tag_add("search_active", hits[0][0], hits[0][1])
            w.see(hits[0][0])

    def next_search():
        hits = search_state.get("hits", [])
        if not hits:
            return
        search_state["index"] = (search_state.get("index", -1) + 1) % len(hits)
        idx = search_state["index"]
        live_log_widget.tag_remove("search_active", "1.0", tk.END)
        live_log_widget.tag_add("search_active", hits[idx][0], hits[idx][1])
        live_log_widget.see(hits[idx][0])

    def prev_search():
        hits = search_state.get("hits", [])
        if not hits:
            return
        search_state["index"] = (search_state.get("index", 0) - 1) % len(hits)
        idx = search_state["index"]
        live_log_widget.tag_remove("search_active", "1.0", tk.END)
        live_log_widget.tag_add("search_active", hits[idx][0], hits[idx][1])
        live_log_widget.see(hits[idx][0])

    search_btn = tk.Button(
        search_row,
        text="Find",
        command=lambda: refresh_search_tags(search_var.get().strip()),
        bg="#1c3357",
        fg=log_text,
        activebackground="#25446f",
        activeforeground=log_text,
        relief="flat",
        font=("Segoe UI", 9)
    )
    search_btn.pack(side="left", padx=6)
    prev_btn = tk.Button(
        search_row,
        text="Prev",
        command=prev_search,
        bg="#1c3357",
        fg=log_text,
        activebackground="#25446f",
        activeforeground=log_text,
        relief="flat",
        font=("Segoe UI", 9)
    )
    prev_btn.pack(side="left", padx=4)
    next_btn = tk.Button(
        search_row,
        text="Next",
        command=next_search,
        bg="#1c3357",
        fg=log_text,
        activebackground="#25446f",
        activeforeground=log_text,
        relief="flat",
        font=("Segoe UI", 9)
    )
    next_btn.pack(side="left", padx=4)

    search_entry.bind("<Return>", lambda _e: refresh_search_tags(search_var.get().strip()))

    # Log widget
    live_log_widget = scrolledtext.ScrolledText(
        body,
        state="disabled",
        bg=log_panel,
        fg=log_text,
        insertbackground=log_text,
        relief="flat",
        highlightthickness=1,
        highlightbackground="#203d63",
        font=("Consolas", 10)
    )
    live_log_widget.pack(fill="both", expand=True)

    live_log_widget.tag_config("recent", background="#1b2f4c")
    live_log_widget.tag_config("bookmark", background=event_colors.get("bookmark", ("#0d2e4f", "#9fd3ff"))[0], foreground=event_colors.get("bookmark", ("#0d2e4f", "#9fd3ff"))[1])
    live_log_widget.tag_config("search_hit", background="#243657", foreground=log_text)
    live_log_widget.tag_config("search_active", background="#2d6cdf", foreground="#ffffff")
    for evt_key, colors in event_colors.items():
        bg, fg = colors if isinstance(colors, (list, tuple)) and len(colors) >= 2 else (colors, log_text)
        live_log_widget.tag_config(evt_key, background=bg, foreground=fg)
    live_log_widget.tag_raise("search_active")

    # Render existing history according to current filters
    refresh_log_view()

    def animate_header_glow(i=0):
        if not live_log_window.winfo_exists():
            return
        colors = ["#e6f0ff", "#cfe3ff", "#b7d3ff", "#cfe3ff"]
        if not LOW_ANIMATION_MODE.get("enabled"):
            title.config(fg=colors[i % len(colors)])
            i += 1
        live_log_window.after(_anim_delay(180, 1000), lambda: animate_header_glow(i))

    animate_header_glow()

def log_gui(event, item, widget=None, base_dir=None):
    """Log to file and live log only if not ignored"""
    if paused_state.get("paused"):
        return
    if isinstance(item, tuple):
        if item[0] in ignore_events or item[1] in ignore_events:
            return
    elif item in ignore_events:
        return
    name = _format_event_item(event, item, base_dir)
    label = FRIENDLY.get(event, event)
    obj_kind = "folder" if event.startswith("folder_") else "file"
    merged_evt = event.split("_")[-1] if "_" in event else event
    if merged_evt not in ("created", "deleted", "modified", "moved", "renamed"):
        merged_evt = "created"
    msg = f"{datetime.now().strftime('%H:%M:%S')} - {label} - {name}"
    logging.info(msg)
    # Always record; UI render depends on filters and availability
    live_log_widget.after(0, lambda: append_live_log(merged_evt, msg, obj_kind)) if live_log_available() else append_live_log(merged_evt, msg, obj_kind)


def _apply_recent_highlight(widget, line_num):
    start = f"{line_num}.0"
    end = f"{line_num}.end"
    widget.tag_add("recent", start, end)
    widget.after(2200, lambda: widget.tag_remove("recent", start, end) if widget.winfo_exists() else None)


def _insert_log_line(evt, msg, obj_kind="file", highlight=True):
    if not live_log_available():
        return
    w = live_log_widget
    w.config(state="normal")
    tags = (evt, obj_kind)
    w.insert(tk.END, msg + "\n", tags)
    w.see(tk.END)
    line = int(w.index("end-1c").split(".")[0])
    w.config(state="disabled")
    if highlight:
        _apply_recent_highlight(w, line)


def _flush_live_log_queue():
    global live_log_flush_job, live_log_pending
    live_log_flush_job = None

    if not live_log_available() or not live_log_pending:
        return

    w = live_log_widget
    pending = live_log_pending
    live_log_pending = []

    w.config(state="normal")
    inserted = False
    for evt, msg, obj_kind in pending:
        if not event_filters.get(evt, True) or not object_filters.get(obj_kind, True):
            continue
        w.insert(tk.END, msg + "\n", (evt, obj_kind))
        inserted = True
    w.see(tk.END)
    line = int(w.index("end-1c").split(".")[0])
    w.config(state="disabled")
    if inserted:
        _apply_recent_highlight(w, line)


def append_live_log(evt, msg, obj_kind="file"):
    global live_log_flush_job
    # honor pause; append to history and insert when appropriate
    if paused_state["paused"]:
        return

    log_history.append((evt, msg, obj_kind))

    if not live_log_available():
        return
    if not event_filters.get(evt, True):
        return
    if not object_filters.get(obj_kind, True):
        return

    live_log_pending.append((evt, msg, obj_kind))
    if live_log_flush_job is None:
        live_log_flush_job = live_log_widget.after(LIVE_LOG_FLUSH_MS, _flush_live_log_queue)
    if len(live_log_pending) >= 120:
        if live_log_flush_job is not None:
            try:
                live_log_widget.after_cancel(live_log_flush_job)
            except Exception:
                pass
            live_log_flush_job = None
        _flush_live_log_queue()

# =========================
# FILE SYSTEM HANDLER
# =========================
class Handler(FileSystemEventHandler):
    def __init__(self, widget, base_dir=None):
        self.widget = widget
        self.base_dir = base_dir
        self.candidates = {}
        self.dir_candidates = {}
        self.finalized = set()
        self.hashes = {}
        self.recent_creates = {}
        self.recent_deletes = {}
        self.recent_dir_deletes = {}
        self.recent_folder_renames = []
        self.recent_folder_moves = []
        self.notified_moves = []  # Track recently notified moves for filtering
        self.reported_folders = set()  # Track folders we've already reported
        self.known_folders = set()  # Track all folder paths
        self.modification_buffer = {}  # path -> (hash, timestamp) for debouncing
        self.last_notified = {}  # path -> (hash, timestamp) — tracks last notification per file
        self.notify_buffer = {
            "file_created": set(),
            "file_deleted": set(),
            "file_modified": set(),
            "file_moved": set(),
            "file_renamed": set(),
            "folder_created": set(),
            "folder_deleted": set(),
            "folder_moved": set(),
            "folder_renamed": set()
        }
        self.buffer_lock = threading.Lock()
        self._prime_known_folders()
        self._prime_existing_files()  # Track existing files
        threading.Thread(target=self._finalizer, daemon=True).start()
        threading.Thread(target=self._notify_worker, daemon=True).start()
        threading.Thread(target=self._modification_debouncer, daemon=True).start()

    def _prime_known_folders(self):
        if not self.base_dir or not os.path.isdir(self.base_dir):
            return
        self.known_folders.add(self.base_dir)
        for root, dirs, _files in os.walk(self.base_dir):
            for d in dirs:
                self.known_folders.add(os.path.join(root, d))
    
    def _prime_existing_files(self):
        """Track all existing files when monitoring starts so modifications work on them."""
        if not self.base_dir or not os.path.isdir(self.base_dir):
            return
        for root, _dirs, files in os.walk(self.base_dir):
            for filename in files:
                filepath = os.path.join(root, filename)
                if not is_noise(filepath) and os.path.isfile(filepath):
                    self.finalized.add(filepath)
                    h = file_hash(filepath)
                    if h:
                        self.hashes[filepath] = h

    def _is_child_of_recent_rename(self, path, target="new"):
        now = time.time()
        keep = []
        for old_root, new_root, ts in self.recent_folder_renames:
            if now - ts <= SAME_FOLDER_RENAME_WINDOW:
                keep.append((old_root, new_root, ts))
                root = new_root if target == "new" else old_root
                if path.startswith(root + os.sep):
                    return True
        self.recent_folder_renames = keep
        return False

    def _is_rename_related_move(self, old_path, new_path):
        return self._is_child_of_recent_rename(old_path, target="old") and \
            self._is_child_of_recent_rename(new_path, target="new")

    def _is_child_of_recent_move(self, path):
        now = time.time()
        keep = []
        for new_root, ts in self.recent_folder_moves:
            if now - ts <= SAME_FOLDER_RENAME_WINDOW:
                keep.append((new_root, ts))
                if path.startswith(new_root + os.sep):
                    return True
        self.recent_folder_moves = keep
        return False

    def _finalizer(self):
        while True:
            time.sleep(0.2)
            now = time.time()
            for name, items in list(self.recent_deletes.items()):
                remaining = []
                for path, ts in items:
                    if now - ts <= SAME_FOLDER_RENAME_WINDOW:
                        remaining.append((path, ts))
                    else:
                        with self.buffer_lock:
                            self.notify_buffer["file_deleted"].add(path)
                if remaining:
                    self.recent_deletes[name] = remaining
                else:
                    self.recent_deletes.pop(name, None)
            for name, items in list(self.recent_dir_deletes.items()):
                remaining = []
                for path, ts in items:
                    if now - ts <= SAME_FOLDER_RENAME_WINDOW:
                        remaining.append((path, ts))
                    else:
                        with self.buffer_lock:
                            self.notify_buffer["folder_deleted"].add(path)
                if remaining:
                    self.recent_dir_deletes[name] = remaining
                else:
                    self.recent_dir_deletes.pop(name, None)
            if self.recent_folder_renames:
                self._is_child_of_recent_rename("", target="new")
            if self.recent_folder_moves:
                self._is_child_of_recent_move("")
            for path, ts in list(self.dir_candidates.items()):
                if not os.path.exists(path):
                    self.dir_candidates.pop(path, None)
                    continue
                if is_windows_placeholder(path):
                    if now - ts >= SAME_FOLDER_RENAME_WINDOW:
                        self.dir_candidates.pop(path, None)
                    continue
                if now - ts >= STABLE_TIME:
                    with self.buffer_lock:
                        self.notify_buffer["folder_created"].add(path)
                    self.reported_folders.add(path)
                    self.dir_candidates.pop(path, None)
            
            for path, ts in list(self.candidates.items()):
                if not os.path.exists(path):
                    self.candidates.pop(path, None)
                    continue
                if now - ts >= STABLE_TIME:
                    if is_windows_placeholder(path) or is_noise(path):
                        continue
                    self.finalized.add(path)
                    self.hashes[path] = file_hash(path)
                    self.recent_creates[path] = now
                    with self.buffer_lock:
                        self.notify_buffer["file_created"].add(path)
                    self.candidates.pop(path, None)

    def _notify_worker(self):
        while True:
            time.sleep(NOTIFY_BATCH_INTERVAL)
            with self.buffer_lock:
                if paused_state.get("paused"):
                    for key in self.notify_buffer:
                        self.notify_buffer[key].clear()
                    continue
                now = time.time()
                # Prune old notified moves
                if self.notified_moves:
                    self.notified_moves = [m for m in self.notified_moves if now - m[1] <= SAME_FOLDER_RENAME_WINDOW]

                # Pre-process: Filter out child creates of moved folders (handles event ordering)
                if self.notify_buffer["folder_moved"] or self.notified_moves:
                    # Normalize moved folder destinations and include recently notified moves
                    normalized_moves = set()
                    for p in self.notify_buffer["folder_moved"]:
                        dest = p[1] if isinstance(p, tuple) else p
                        normalized_moves.add(os.path.normpath(dest))
                    normalized_moves.update(m[0] for m in self.notified_moves)

                    # Remove child folder creates
                    remaining_folder_creates = set()
                    for fc_path in self.notify_buffer["folder_created"]:
                        norm_fc = os.path.normpath(fc_path)
                        is_child = False
                        for move_path in normalized_moves:
                            if norm_fc.startswith(move_path + os.sep):
                                is_child = True
                                break
                        if not is_child:
                            remaining_folder_creates.add(fc_path)
                    self.notify_buffer["folder_created"] = remaining_folder_creates

                    # Remove child file creates
                    remaining_file_creates = set()
                    for fc_path in self.notify_buffer["file_created"]:
                        norm_fc = os.path.normpath(fc_path)
                        is_child = False
                        for move_path in normalized_moves:
                            if norm_fc.startswith(move_path + os.sep):
                                is_child = True
                                break
                        if not is_child:
                            remaining_file_creates.add(fc_path)
                    self.notify_buffer["file_created"] = remaining_file_creates
                
                for evt, paths in self.notify_buffer.items():
                    # Filter out ignored events
                    filtered_paths = []
                    for p in paths:
                        if isinstance(p, tuple):
                            if p[0] in ignore_events or p[1] in ignore_events:
                                continue
                        elif p in ignore_events:
                            continue
                        filtered_paths.append(p)
                    # Remove them from ignore_events set
                    for p in paths:
                        if isinstance(p, tuple):
                            if p[0] in ignore_events:
                                ignore_events.remove(p[0])
                            if p[1] in ignore_events:
                                ignore_events.remove(p[1])
                        elif p in ignore_events:
                            ignore_events.remove(p)
                    if not filtered_paths:
                        continue
                    msg = prepare_notification_message(filtered_paths, self.base_dir)
                    if self.widget:
                        label = FRIENDLY.get(evt, evt.replace("_", " ").title())
                        self.widget.after(0, lambda l=label, m=msg: notification.notify(
                            title=f"📂 {APP_NAME} - {l}",
                            message=m,
                            app_name=APP_NAME,
                            app_icon=_NOTIFY_ICON,
                            timeout=5
                        ))
                    for p in filtered_paths:
                        log_gui(evt, p, self.widget, self.base_dir)
                        if evt == "folder_moved":
                            dest = p[1] if isinstance(p, tuple) else p
                            self.notified_moves.append((os.path.normpath(dest), now))
                    paths.clear()
    
    def _modification_debouncer(self):
        """Fire 'File Modified' exactly once per save session.

        Two modes depending on file type:

        Non-code files (Office, images, etc.):
          - Wait 1 s of write-silence, then wait until the writing process
            releases its exclusive lock, then notify once.

        IDE code files (.py, .js, .ts, .java, ...):
          - IDEs never take an exclusive lock, so lock-detection is useless.
          - Instead use a longer quiet window (IDE_DEBOUNCE_TIME) to absorb
            rapid auto-saves.
          - After notifying, enforce an IDE_COOLDOWN: if another save comes in
            before the cooldown expires AND the content is unchanged since the
            last notification, skip it silently.
          - If the content DID change after the cooldown, notify again.
        """
        DEBOUNCE_TIME = 1.0  # quiet window for non-code files (seconds)
        while True:
            time.sleep(0.5)
            if paused_state.get("paused"):
                with self.buffer_lock:
                    self.modification_buffer.clear()
                continue

            now = time.time()

            # Collect paths that have been quiet long enough
            candidates = []
            with self.buffer_lock:
                for path, (last_hash, last_time) in list(self.modification_buffer.items()):
                    ext = os.path.splitext(path)[1].lower()
                    is_code = ext in IDE_CODE_EXTENSIONS
                    quiet_needed = IDE_DEBOUNCE_TIME if is_code else DEBOUNCE_TIME
                    if now - last_time >= quiet_needed:
                        if not os.path.exists(path):
                            del self.modification_buffer[path]
                            continue
                        candidates.append((path, last_hash, is_code))

            # Do all file I/O outside the lock
            to_notify = []
            for path, last_hash, is_code in candidates:
                if is_code:
                    # --- IDE / code file path ---
                    current_hash = file_hash(path)
                    if not current_hash:
                        continue

                    with self.buffer_lock:
                        if path not in self.modification_buffer:
                            continue

                        prev_hash, prev_time = self.last_notified.get(path, (None, 0))

                        if current_hash != last_hash:
                            # File still changing — update buffer and keep waiting
                            self.modification_buffer[path] = (current_hash, time.time())
                            continue

                        # File is stable (quiet for IDE_DEBOUNCE_TIME)
                        if prev_hash is not None and current_hash == prev_hash:
                            # Content identical to last notification — silent discard
                            del self.modification_buffer[path]
                            self.hashes[path] = current_hash
                            continue

                        if prev_hash is not None and now - prev_time < IDE_COOLDOWN:
                            # Content changed but still within cooldown — keep waiting
                            self.modification_buffer[path] = (current_hash, time.time())
                            continue

                        # New content and cooldown passed (or first notification) → fire
                        to_notify.append(path)
                        del self.modification_buffer[path]
                        self.hashes[path] = current_hash
                        self.last_notified[path] = (current_hash, now)
                else:
                    # --- Non-code file path (Office, images, etc.) ---
                    # Wait for the writing application to release its lock
                    if _is_file_locked(path):
                        with self.buffer_lock:
                            if path in self.modification_buffer:
                                self.modification_buffer[path] = (last_hash, time.time())
                        continue

                    current_hash = file_hash(path)
                    with self.buffer_lock:
                        if path not in self.modification_buffer:
                            continue
                        if current_hash and current_hash == last_hash:
                            # Stable and unlocked → notify once
                            to_notify.append(path)
                            del self.modification_buffer[path]
                            self.hashes[path] = current_hash
                        elif current_hash:
                            # Hash changed while checking → keep waiting
                            self.modification_buffer[path] = (current_hash, time.time())

            for path in to_notify:
                with self.buffer_lock:
                    self.notify_buffer["file_modified"].add(path)

    # =========================
    # EVENT HANDLERS
    # =========================
    def on_created(self, event):
        if paused_state.get("paused"):
            return
        if event.is_directory:
            if is_noise(event.src_path):
                return
            if event.src_path in ignore_events:
                return
            if self._is_child_of_recent_move(event.src_path):
                return
            if self._is_child_of_recent_rename(event.src_path, target="new"):
                return
            self.known_folders.add(event.src_path)
            if is_windows_placeholder(event.src_path):
                self.dir_candidates[event.src_path] = time.time()
            else:
                name = os.path.basename(event.src_path)
                if name in self.recent_dir_deletes:
                    remaining = []
                    moved = False
                    for old_path, ts in self.recent_dir_deletes[name]:
                        if time.time() - ts <= SAME_FOLDER_RENAME_WINDOW and not moved:
                            with self.buffer_lock:
                                self.notify_buffer["folder_moved"].add((old_path, event.src_path))
                            moved = True
                        else:
                            remaining.append((old_path, ts))
                    if remaining:
                        self.recent_dir_deletes[name] = remaining
                    else:
                        self.recent_dir_deletes.pop(name, None)
                    if moved:
                        return
                self.reported_folders.add(event.src_path)
                with self.buffer_lock:
                    self.notify_buffer["folder_created"].add(event.src_path)
            return
        if is_noise(event.src_path):
            return
        if event.src_path in ignore_events:
            return
        if self._is_child_of_recent_move(event.src_path):
            return
        now = time.time()
        name = os.path.basename(event.src_path)
        if name in self.recent_deletes:
            remaining = []
            moved = False
            for old_path, ts in self.recent_deletes[name]:
                if now - ts <= SAME_FOLDER_RENAME_WINDOW and not moved:
                    with self.buffer_lock:
                        self.notify_buffer["file_moved"].add((old_path, event.src_path))
                    moved = True
                else:
                    remaining.append((old_path, ts))
            if remaining:
                self.recent_deletes[name] = remaining
            else:
                self.recent_deletes.pop(name, None)
            if moved:
                return
        self.candidates[event.src_path] = time.time()

    def on_modified(self, event):
        if paused_state.get("paused"):
            return
        if event.is_directory or is_noise(event.src_path):
            return
        path = event.src_path
        # Only process files that are already fully tracked with a known hash baseline.
        # This prevents spurious "File Modified" after a move (on_moved hasn't run yet,
        # so hashes[path] is absent → hash compare would be against None → false positive).
        if path not in self.finalized or path in ignore_events:
            return
        if path not in self.hashes:
            # No baseline yet — record hash silently so next real change is detectable.
            h = file_hash(path)
            if h:
                self.hashes[path] = h
            return
        h = file_hash(path)
        if not h or h == self.hashes.get(path):
            return
        # Add to debounce buffer instead of immediate notification
        with self.buffer_lock:
            self.modification_buffer[path] = (h, time.time())

    def on_deleted(self, event):
        if paused_state.get("paused"):
            return
        if is_noise(event.src_path):
            return
        path = event.src_path
        if path in ignore_events:
            return
        is_folder = event.is_directory or path in self.known_folders
        if is_folder:
            if self._is_child_of_recent_rename(path, target="old"):
                return
            name = os.path.basename(path)
            self.recent_dir_deletes.setdefault(name, []).append((path, time.time()))
            self.dir_candidates.pop(path, None)
            self.reported_folders.discard(path)
            self.known_folders.discard(path)
            return
        name = os.path.basename(path)
        self.recent_deletes.setdefault(name, []).append((path, time.time()))
        self.candidates.pop(path, None)
        self.finalized.discard(path)
        self.hashes.pop(path, None)
        self.recent_creates.pop(path, None)

    def on_moved(self, event):
        if paused_state.get("paused"):
            return
        old, new = event.src_path, event.dest_path
        if old in ignore_events or new in ignore_events:
            return
        if event.is_directory:
            if is_noise(old) or is_noise(new):
                return
            if self._is_rename_related_move(old, new):
                return
            self.known_folders.add(new)
            if old in self.known_folders:
                self.known_folders.discard(old)
            if old in self.dir_candidates or is_windows_placeholder(old):
                self.dir_candidates.pop(old, None)
                self.reported_folders.add(new)
                with self.buffer_lock:
                    self.notify_buffer["folder_created"].add(new)
                return
            # Folder rename (same parent) vs move
            self.reported_folders.add(new)
            if old in self.reported_folders:
                self.reported_folders.discard(old)
            with self.buffer_lock:
                if os.path.dirname(old) == os.path.dirname(new):
                    self.recent_folder_renames.append((old, new, time.time()))
                    self.notify_buffer["folder_renamed"].add((old, new))
                else:
                    self.recent_folder_moves.append((new, time.time()))
                    self.notify_buffer["folder_moved"].add((old, new))
            return
        if is_noise(old) or is_noise(new):
            return
        now = time.time()
        if old in self.candidates and is_windows_placeholder(old):
            self.candidates.pop(old)
            self.finalized.add(new)
            self.hashes[new] = file_hash(new)
            self.recent_creates[new] = now
            with self.buffer_lock:
                self.notify_buffer["file_created"].add(new)
            return
        
        # Check if this is a save-as-rename pattern (temp file renamed to original)
        # This happens with many editors and Office apps
        old_base = os.path.basename(old).lower()
        old_name_no_ext = os.path.splitext(old_base)[0]

        # Detect temp file patterns (Excel uses 8-char hex like DF3A3600, editors use ~ or .)
        is_temp_file = (
            old_base.startswith(("~", ".")) or 
            ".tmp" in old_base or 
            old_base.endswith(".tmp") or
            old_base.startswith("tmp") or
            (len(old_name_no_ext) == 8 and all(c in "0123456789abcdef" for c in old_name_no_ext))  # 8-char hex
        )
        
        # If in same folder, check for special save patterns
        if os.path.dirname(old) == os.path.dirname(new):
            # PRIORITY 1: Temp file renamed to tracked file = modification
            if is_temp_file and new in self.finalized:
                new_h = file_hash(new)
                if new_h:
                    old_hash = self.hashes.get(new)
                    if old_hash != new_h:
                        # File content changed, it's a modification via save
                        with self.buffer_lock:
                            self.modification_buffer[new] = (new_h, time.time())
                    # Cleanup temp file tracking
                    self.candidates.pop(old, None)
                    self.finalized.discard(old)
                    self.recent_creates.pop(old, None)
                return
            
            # PRIORITY 2: Temp file recently created, renamed to any file = suppress both events
            # (This handles new file creation via Office apps)
            creation_time = self.recent_creates.get(old)
            if is_temp_file and creation_time and now - creation_time <= SAME_FOLDER_RENAME_WINDOW:
                # Suppress the temp file creation event, treat final file as new creation
                self.finalized.add(new)
                self.hashes[new] = file_hash(new)
                self.recent_creates[new] = now
                self.recent_creates.pop(old, None)
                self.candidates.pop(old, None)
                self.finalized.discard(old)
                # Don't log anything - wait for finalization to log as created
                return
            
            # PRIORITY 3: Regular file recently created and renamed = treat as creation
            if creation_time and now - creation_time <= SAME_FOLDER_RENAME_WINDOW:
                self.finalized.add(new)
                self.hashes[new] = file_hash(new)
                self.recent_creates[new] = now
                self.recent_creates.pop(old, None)
                with self.buffer_lock:
                    self.notify_buffer["file_created"].add(new)
                return
            
            if old in self.finalized:
                self.finalized.remove(old)
                self.finalized.add(new)
                self.hashes[new] = self.hashes.pop(old, None)
            self.recent_creates.pop(old, None)
            with self.buffer_lock:
                self.notify_buffer["file_renamed"].add((old, new))
            return
        if old in self.finalized:
            self.finalized.remove(old)
            self.finalized.add(new)
            # Always ensure new path has a hash so on_modified baseline check works
            self.hashes[new] = self.hashes.pop(old, None) or file_hash(new)
            self.recent_creates[new] = self.recent_creates.pop(old, None)
            # Cancel any pending modification for either path so the move doesn't
            # also appear as a "File Modified" 2 seconds later
            with self.buffer_lock:
                self.modification_buffer.pop(old, None)
                self.modification_buffer.pop(new, None)
                self.notify_buffer["file_moved"].add((old, new))

# =========================
# MONITORING CONTROL
# =========================
observer = None
monitoring = False

def start_monitoring(path, widget, start_btn, stop_btn):
    global observer, monitoring
    if monitoring or not path:
        return
    # Backend safety validation (defence-in-depth)
    if not is_folder_safe(path):
        messagebox.showerror("Blocked", get_block_reason(path))
        return
    handler = Handler(widget, base_dir=path)
    observer = Observer()
    observer.schedule(handler, path, recursive=True)
    observer.start()
    monitoring = True
    start_btn.config(state="disabled")
    stop_btn.config(state="normal")
    if live_log_available():
        live_log_widget.config(state="normal")
        live_log_widget.insert(tk.END, f"\n--- Monitoring started: {path} ---\n")
        live_log_widget.config(state="disabled")

def stop_monitoring(widget, start_btn, stop_btn):
    global observer, monitoring
    if not monitoring or observer is None:
        return
    observer.stop()
    observer.join()
    observer = None
    monitoring = False
    start_btn.config(state="normal")
    stop_btn.config(state="disabled")
    if live_log_available():
        live_log_widget.config(state="normal")
        live_log_widget.insert(tk.END, "\n--- Monitoring stopped ---\n")
        live_log_widget.config(state="disabled")

# =========================
# ORGANIZE FOLDER FUNCTION WITH FINAL PROFESSIONAL NOTIFICATION
# =========================
def organize_folder(folder=None):
    if not folder:
        folder = filedialog.askdirectory(title="Select Folder to Organize")
    if not folder:
        return
    # Backend safety validation (defence-in-depth)
    if not is_folder_safe(folder):
        messagebox.showerror("Blocked", get_block_reason(folder))
        return

    moved_count = 0  # Count total files moved

    for item in os.listdir(folder):
        item_path = os.path.join(folder, item)
        if os.path.isfile(item_path):
            ext = os.path.splitext(item)[1].lower() or "no_extension"
            ext_folder = os.path.join(folder, ext.strip(".") if ext != "no_extension" else "no_extension")
            os.makedirs(ext_folder, exist_ok=True)
            dest_path = os.path.join(ext_folder, item)
            # If a file with the same name already exists at the destination,
            # generate a unique name (file_1.ext, file_2.ext, ...).
            if os.path.exists(dest_path):
                name_no_ext, ext_part = os.path.splitext(item)
                counter = 1
                while os.path.exists(dest_path):
                    dest_path = os.path.join(ext_folder, f"{name_no_ext}_{counter}{ext_part}")
                    counter += 1
            try:
                # Add both old and new path to ignore_events to remain silent
                ignore_events.add(item_path)
                ignore_events.add(dest_path)
                os.rename(item_path, dest_path)
                moved_count += 1
            except Exception as e:
                print(f"Could not move {item}: {e}")

    # Professional notification when done
    if moved_count > 0:
        notification.notify(
            title="📂 Folder Organized Successfully",
            message=f"{moved_count} file{'s' if moved_count>1 else ''} sorted into their respective folders.",
            app_name=APP_NAME,
            app_icon=_NOTIFY_ICON,
            timeout=5
        )
        if live_log_available():
            live_log_widget.config(state="normal")
            live_log_widget.insert(tk.END, f"\n--- ✅ Folder Organized: {moved_count} files sorted ---\n")
            live_log_widget.config(state="disabled")

# =========================
# DUPLICATE FILE FINDER
# =========================
duplicate_window = None

def find_duplicates(folder, progress_callback=None):
    """Scan folder recursively and find duplicate files based on content hash."""
    if not folder or not os.path.isdir(folder):
        return {}
    
    hash_map = {}  # hash -> list of file paths
    file_count = 0
    
    # First pass: count files
    total_files = sum(1 for root, _, files in os.walk(folder) 
                     for f in files if not is_noise(os.path.join(root, f)))
    
    # Second pass: hash files
    for root, _dirs, files in os.walk(folder):
        for filename in files:
            filepath = os.path.join(root, filename)
            
            # Skip noise files and very small files
            if is_noise(filepath):
                continue
            
            try:
                # Skip files smaller than 1KB (likely not worth deduplicating)
                if os.path.getsize(filepath) < 1024:
                    continue
                
                fhash = file_hash(filepath)
                if fhash:
                    if fhash not in hash_map:
                        hash_map[fhash] = []
                    hash_map[fhash].append(filepath)
                    
                file_count += 1
                if progress_callback and (file_count % DUP_SCAN_PROGRESS_STEP == 0):
                    progress_callback(file_count, total_files)
            except Exception:
                continue

    if progress_callback:
        progress_callback(file_count, total_files)
    
    # Filter to only keep duplicates (hash with 2+ files)
    duplicates = {h: paths for h, paths in hash_map.items() if len(paths) > 1}
    return duplicates

def format_size(bytes):
    """Format bytes to human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024.0:
            return f"{bytes:.1f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.1f} TB"

def open_duplicate_manager(duplicates, base_folder):
    """Open the duplicate file manager window."""
    global duplicate_window
    
    if duplicate_window and tk.Toplevel.winfo_exists(duplicate_window):
        duplicate_window.lift()
        return
    
    if not duplicates:
        notification.notify(
            title="🎉 No Duplicates Found",
            message="Your folder is clean! No duplicate files detected.",
            app_name=APP_NAME,
            app_icon=_NOTIFY_ICON,
            timeout=5
        )
        return
    
    duplicate_window = tk.Toplevel()
    duplicate_window.title("Duplicate File Manager")
    duplicate_window.geometry("920x680")
    duplicate_window.minsize(780, 520)
    
    # Colors matching the main theme
    dup_bg = "#0f1f35"
    dup_panel = "#142845"
    dup_card = "#132945"
    dup_text = "#e6f0ff"
    dup_muted = "#9fb4d1"
    dup_accent = "#3dd6d0"
    dup_danger = "#cc3d3d"
    
    duplicate_window.configure(bg=dup_bg)
    
    # Calculate statistics
    total_groups = len(duplicates)
    total_duplicates = sum(len(paths) - 1 for paths in duplicates.values())
    total_wasted = sum(os.path.getsize(paths[0]) * (len(paths) - 1) 
                       for paths in duplicates.values() 
                       if os.path.exists(paths[0]))
    
    # Header
    header = tk.Frame(duplicate_window, bg=dup_panel, highlightthickness=1, highlightbackground="#203d63")
    header.pack(fill="x", padx=16, pady=(14, 10))
    
    title = tk.Label(
        header,
        text="DUPLICATE FILE MANAGER",
        font=("Segoe UI", 13, "bold"),
        fg=dup_text,
        bg=dup_panel
    )
    title.pack(pady=(12, 4))
    
    stats_frame = tk.Frame(header, bg=dup_panel)
    stats_frame.pack(pady=(6, 12))
    
    stats = [
        (f"{total_groups} Groups", dup_accent),
        (f"{total_duplicates} Duplicates", "#ff9966"),
        (f"{format_size(total_wasted)} Wasted", "#ff6b6b")
    ]
    
    for stat_text, color in stats:
        stat_label = tk.Label(
            stats_frame,
            text=stat_text,
            font=("Segoe UI", 10, "bold"),
            fg=color,
            bg=dup_panel,
            padx=20
        )
        stat_label.pack(side="left", padx=8)
    
    # Body
    body = tk.Frame(duplicate_window, bg=dup_bg)
    body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
    
    # Controls
    controls = tk.Frame(body, bg=dup_bg)
    controls.pack(fill="x", pady=(0, 10))
    
    info_label = tk.Label(
        controls,
        text="Select duplicates to keep or delete. The first file in each group is highlighted.",
        font=("Segoe UI", 9),
        fg=dup_muted,
        bg=dup_bg
    )
    info_label.pack(side="left", anchor="w")
    
    # Scrollable duplicate list
    canvas_frame = tk.Frame(body, bg=dup_panel, highlightthickness=1, highlightbackground="#203d63")
    canvas_frame.pack(fill="both", expand=True)
    
    dup_canvas = tk.Canvas(canvas_frame, bg=dup_panel, highlightthickness=0)
    scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=dup_canvas.yview)
    scrollable_frame = tk.Frame(dup_canvas, bg=dup_panel)
    
    scrollable_frame.bind(
        "<Configure>",
        lambda e: dup_canvas.configure(scrollregion=dup_canvas.bbox("all"))
    )
    
    dup_canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    dup_canvas.configure(yscrollcommand=scrollbar.set)
    
    dup_canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    
    # Track selected files for deletion
    selected_files = set()
    # Track checkbox variables for each file
    checkbox_vars = {}
    # Track selection state for each group
    group_states = {}

    def refresh_status_label():
        count = len(selected_files)
        status_label.config(text=f"{count} file{'s' if count != 1 else ''} selected")
    
    def toggle_selection(filepath, checkbox_var):
        if checkbox_var.get():
            selected_files.add(filepath)
        else:
            selected_files.discard(filepath)
        refresh_status_label()
    
    # Display each duplicate group
    group_num = 0
    for file_hash, file_paths in sorted(duplicates.items(), 
                                        key=lambda x: os.path.getsize(x[1][0]) if os.path.exists(x[1][0]) else 0, 
                                        reverse=True):
        group_num += 1
        
        if not os.path.exists(file_paths[0]):
            continue
        
        file_size = os.path.getsize(file_paths[0])
        wasted_space = file_size * (len(file_paths) - 1)
        
        # Group card
        group_frame = tk.Frame(
            scrollable_frame,
            bg=dup_card,
            highlightthickness=1,
            highlightbackground="#203d63"
        )
        group_frame.pack(fill="x", padx=10, pady=8)
        
        # Group header
        group_header = tk.Frame(group_frame, bg="#1a3550")
        group_header.pack(fill="x")
        
        group_title = tk.Label(
            group_header,
            text=f"Group {group_num}: {os.path.basename(file_paths[0])}",
            font=("Segoe UI", 10, "bold"),
            fg=dup_accent,
            bg="#1a3550",
            anchor="w"
        )
        group_title.pack(side="left", padx=12, pady=8)
        
        group_info = tk.Label(
            group_header,
            text=f"{len(file_paths)} copies • {format_size(file_size)} each • {format_size(wasted_space)} wasted",
            font=("Segoe UI", 9),
            fg=dup_muted,
            bg="#1a3550"
        )
        group_info.pack(side="left", padx=8, pady=8)
        
        # Files in group
        for idx, filepath in enumerate(file_paths):
            if not os.path.exists(filepath):
                continue
            
            file_frame = tk.Frame(group_frame, bg=dup_card)
            file_frame.pack(fill="x", padx=8, pady=4)
            
            # Checkbox for selection
            is_first = idx == 0
            checkbox_var = tk.BooleanVar(value=False)
            # Store checkbox variable for this file
            if not is_first:
                checkbox_vars[filepath] = checkbox_var
            checkbox = tk.Checkbutton(
                file_frame,
                variable=checkbox_var,
                command=lambda fp=filepath, cv=checkbox_var: toggle_selection(fp, cv),
                bg=dup_card,
                fg=dup_text,
                selectcolor="#1f3555",
                activebackground=dup_card,
                activeforeground=dup_text,
                state="disabled" if is_first else "normal"
            )
            checkbox.pack(side="left", padx=4)
            
            # File indicator
            indicator = "🔒 Keep" if is_first else "  "
            indicator_label = tk.Label(
                file_frame,
                text=indicator,
                font=("Segoe UI", 9),
                fg=dup_accent if is_first else dup_muted,
                bg=dup_card,
                width=8,
                anchor="w"
            )
            indicator_label.pack(side="left", padx=4)
            
            # File path
            try:
                rel_path = os.path.relpath(filepath, base_folder)
            except Exception:
                rel_path = filepath
            
            path_label = tk.Label(
                file_frame,
                text=rel_path if len(rel_path) < 80 else "..." + rel_path[-77:],
                font=("Segoe UI", 9, "bold" if is_first else "normal"),
                fg=dup_text if is_first else dup_muted,
                bg=dup_card,
                anchor="w"
            )
            path_label.pack(side="left", fill="x", expand=True, padx=4)
        
        # Group actions
        action_frame = tk.Frame(group_frame, bg=dup_card)
        action_frame.pack(fill="x", padx=12, pady=8)
        
        # Initialize group state
        group_states[group_num] = False
        
        def toggle_group_selection(group_paths, group_id, btn):
            # Check if group is currently selected
            is_selected = group_states[group_id]
            
            # Toggle state
            group_states[group_id] = not is_selected
            
            if group_states[group_id]:
                # Select all duplicates (skip first file)
                for fp in group_paths[1:]:
                    if os.path.exists(fp) and fp in checkbox_vars:
                        checkbox_vars[fp].set(True)
                        selected_files.add(fp)
                btn.config(text="Deselect All Duplicates")
            else:
                # Deselect all duplicates
                for fp in group_paths[1:]:
                    if fp in checkbox_vars:
                        checkbox_vars[fp].set(False)
                        selected_files.discard(fp)
                btn.config(text="Select All Duplicates")
            refresh_status_label()
        
        select_btn = tk.Button(
            action_frame,
            text="Select All Duplicates",
            command=None,
            bg="#1c3558",
            fg=dup_text,
            activebackground="#24456f",
            activeforeground=dup_text,
            relief="flat",
            font=("Segoe UI", 9),
            padx=12,
            pady=4
        )
        select_btn.config(command=lambda gp=file_paths, gid=group_num, btn=select_btn: toggle_group_selection(gp, gid, btn))
        select_btn.pack(side="left", padx=4)
    
    # Action buttons at bottom
    action_bar = tk.Frame(duplicate_window, bg=dup_panel, highlightthickness=1, highlightbackground="#203d63")
    action_bar.pack(fill="x", padx=16, pady=(8, 16))
    
    status_label = tk.Label(
        action_bar,
        text="0 files selected",
        font=("Segoe UI", 9),
        fg=dup_muted,
        bg=dup_panel
    )
    status_label.pack(side="left", padx=12, pady=10)
    
    refresh_status_label()
    
    def delete_selected():
        if not selected_files:
            return
        
        count = len(selected_files)
        confirm = tk.messagebox.askyesno(
            "Confirm Deletion",
            f"Are you sure you want to permanently delete {count} file{'s' if count != 1 else ''}?\n\nThis action cannot be undone!",
            icon="warning"
        )
        
        if not confirm:
            return
        
        deleted = 0
        freed_space = 0
        
        for filepath in list(selected_files):
            try:
                if os.path.exists(filepath):
                    size = os.path.getsize(filepath)
                    ignore_events.add(filepath)
                    os.remove(filepath)
                    freed_space += size
                    deleted += 1
                selected_files.discard(filepath)
            except Exception as e:
                print(f"Could not delete {filepath}: {e}")

        refresh_status_label()
        
        if deleted > 0:
            notification.notify(
                title="🗑️ Duplicates Deleted",
                message=f"{deleted} file{'s' if deleted != 1 else ''} removed, {format_size(freed_space)} freed.",
                app_name=APP_NAME,
                app_icon=_NOTIFY_ICON,
                timeout=5
            )
            
            if live_log_available():
                live_log_widget.config(state="normal")
                live_log_widget.insert(tk.END, f"\n--- 🗑️ Deleted {deleted} duplicate files ({format_size(freed_space)} freed) ---\n")
                live_log_widget.config(state="disabled")
        
        duplicate_window.destroy()
    
    def move_to_folder():
        if not selected_files:
            return
        
        dest_folder = filedialog.askdirectory(title="Select Destination Folder")
        if not dest_folder:
            return
        # Block system folders as destination
        if not is_folder_safe(dest_folder):
            messagebox.showerror("Blocked", get_block_reason(dest_folder))
            return
        if not confirm_move_action(len(selected_files), dest_folder):
            return
        
        moved = 0
        for filepath in list(selected_files):
            try:
                if os.path.exists(filepath):
                    filename = os.path.basename(filepath)
                    dest_path = os.path.join(dest_folder, filename)
                    
                    # Handle name conflicts
                    counter = 1
                    while os.path.exists(dest_path):
                        name, ext = os.path.splitext(filename)
                        dest_path = os.path.join(dest_folder, f"{name}_{counter}{ext}")
                        counter += 1
                    
                    ignore_events.add(filepath)
                    ignore_events.add(dest_path)
                    os.rename(filepath, dest_path)
                    moved += 1
                selected_files.discard(filepath)
            except Exception as e:
                print(f"Could not move {filepath}: {e}")

        refresh_status_label()
        
        if moved > 0:
            notification.notify(
                title="📦 Duplicates Moved",
                message=f"{moved} file{'s' if moved != 1 else ''} moved to {os.path.basename(dest_folder)}.",
                app_name=APP_NAME,
                app_icon=_NOTIFY_ICON,
                timeout=5
            )
        
        duplicate_window.destroy()
    
    delete_btn = tk.Button(
        action_bar,
        text="Delete Selected",
        command=delete_selected,
        bg=dup_danger,
        fg=dup_text,
        activebackground="#e04a4a",
        activeforeground=dup_text,
        relief="flat",
        font=("Segoe UI", 10, "bold"),
        padx=20,
        pady=8
    )
    delete_btn.pack(side="right", padx=4, pady=8)
    
    move_btn = tk.Button(
        action_bar,
        text="Move Selected",
        command=move_to_folder,
        bg="#2d6cdf",
        fg=dup_text,
        activebackground="#3a7af2",
        activeforeground=dup_text,
        relief="flat",
        font=("Segoe UI", 10),
        padx=20,
        pady=8
    )
    move_btn.pack(side="right", padx=4, pady=8)
    
    close_btn = tk.Button(
        action_bar,
        text="Close",
        command=duplicate_window.destroy,
        bg="#1c3558",
        fg=dup_text,
        activebackground="#24456f",
        activeforeground=dup_text,
        relief="flat",
        font=("Segoe UI", 10),
        padx=20,
        pady=8
    )
    close_btn.pack(side="right", padx=4, pady=8)
    
    # Enable mouse wheel scrolling.
    # Guard against the canvas being destroyed (e.g. window closed while
    # the user is still scrolling), which would raise TclError.
    def on_mousewheel(event):
        try:
            if dup_canvas.winfo_exists():
                dup_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        except tk.TclError:
            pass

    dup_canvas.bind_all("<MouseWheel>", on_mousewheel)
    # Unbind the root-level handler when the duplicate window is closed
    # so scrolling elsewhere after closing doesn't raise TclError.
    duplicate_window.bind("<Destroy>", lambda _e: dup_canvas.unbind_all("<MouseWheel>"))

def scan_for_duplicates(folder=None):
    """Scan folder and open duplicate manager."""
    if not folder:
        folder = filedialog.askdirectory(title="Select Folder to Scan for Duplicates")
    if not folder:
        return
    # Backend safety validation
    if not is_folder_safe(folder):
        messagebox.showerror("Blocked", get_block_reason(folder))
        return
    
    # Create progress window
    progress_window = tk.Toplevel()
    progress_window.title("Scanning for Duplicates")
    progress_window.geometry("400x150")
    progress_window.resizable(False, False)
    progress_window.configure(bg="#12263f")
    
    label = tk.Label(
        progress_window,
        text="Scanning files...",
        font=("Segoe UI", 11),
        fg="#e6f0ff",
        bg="#12263f"
    )
    label.pack(pady=(20, 10))
    
    progress_label = tk.Label(
        progress_window,
        text="0 files scanned",
        font=("Segoe UI", 9),
        fg="#9fb4d1",
        bg="#12263f"
    )
    progress_label.pack(pady=10)

    progress_lock = threading.Lock()
    progress_state = {
        "current": 0,
        "total": 0,
        "done": False,
        "last_render": 0.0,
    }

    def update_progress(current, total):
        with progress_lock:
            progress_state["current"] = current
            progress_state["total"] = total

    def poll_progress():
        if not progress_window.winfo_exists():
            return
        now = time.time()
        with progress_lock:
            current = progress_state["current"]
            total = progress_state["total"]
            done = progress_state["done"]
            last_render = progress_state["last_render"]

        if done or (now - last_render) * 1000 >= DUP_SCAN_PROGRESS_UPDATE_MS:
            progress_label.config(text=f"{current} / {total} files scanned")
            with progress_lock:
                progress_state["last_render"] = now

        if done:
            progress_window.destroy()
            open_duplicate_manager(duplicates, folder)
            return

        progress_window.after(DUP_SCAN_PROGRESS_UPDATE_MS, poll_progress)
    
    # Run scan in thread to keep UI responsive
    duplicates = {}
    
    def scan_thread():
        nonlocal duplicates
        duplicates = find_duplicates(folder, update_progress)
        with progress_lock:
            progress_state["done"] = True
    
    thread = threading.Thread(target=scan_thread, daemon=True)
    thread.start()
    poll_progress()

# =========================
# CONFIRMATION DIALOGS
# =========================
def confirm_destructive_action(action_name: str, detail: str) -> bool:
    """Show a yes/no confirmation before any destructive operation.
    Returns True if user confirms.
    """
    return messagebox.askyesno(
        f"Confirm {action_name}",
        f"{detail}\n\nThis action cannot be undone. Continue?",
        icon="warning",
    )


def confirm_move_action(file_count: int, dest: str) -> bool:
    """Confirm before moving files."""
    return messagebox.askyesno(
        "Confirm Move",
        f"Move {file_count} file{'s' if file_count != 1 else ''} to:\n{dest}\n\nContinue?",
        icon="question",
    )


# =========================
# LOGIN / REGISTER WINDOW
# =========================
_login_overlay = None  # Track active overlay to prevent duplicates


def open_login_window(root, on_login_success):
    """Open a modal login/register dialog as an in-app overlay.
    A dark dim layer covers the entire window; the login card sits centred on top.
    Calls on_login_success() when a user successfully logs in.
    """
    global _login_overlay
    # Prevent opening multiple overlays
    if _login_overlay is not None:
        try:
            if _login_overlay.winfo_exists():
                return
        except Exception:
            pass
        _login_overlay = None

    # ---- Colours ----
    dim_bg = "#040c1a"
    bg = "#0a1628"
    panel = "#0f1f35"
    card = "#132945"
    txt = "#eaf2ff"
    muted = "#9fb4d1"
    accent = "#3dd6d0"
    btn_bg = "#2d6cdf"
    btn_hover = "#3a7af2"
    btn_press = "#255ec4"
    reg_bg = "#1c3558"
    reg_hover = "#24456f"
    reg_press = "#162b47"
    err_fg = "#ff6b6b"
    ok_fg = "#3dd6d0"
    entry_bg = "#101e34"

    # ---- Full-screen dark overlay (dim / blur simulation) ----
    overlay = tk.Canvas(root, bg=dim_bg, highlightthickness=0)
    overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
    _login_overlay = overlay

    # Draw subtle vignette / depth circles
    def _draw_dim(event=None):
        w = overlay.winfo_width()
        h = overlay.winfo_height()
        overlay.delete("dim")
        overlay.create_rectangle(0, 0, w, h, fill=dim_bg, outline="", tags="dim")
        overlay.create_oval(
            w // 2 - 400, h // 2 - 350,
            w // 2 + 400, h // 2 + 350,
            fill="#081428", outline="", tags="dim"
        )
        overlay.create_oval(
            w // 2 - 260, h // 2 - 280,
            w // 2 + 260, h // 2 + 280,
            fill="#0b1a30", outline="", tags="dim"
        )
    overlay.bind("<Configure>", _draw_dim)

    # Floating orbs on the overlay
    orbs = [
        {"id": overlay.create_oval(-60, 30, 140, 230, fill="#0e203e", outline=""), "dx": 0.35, "dy": 0.15},
        {"id": overlay.create_oval(500, -40, 700, 160, fill="#122848", outline=""), "dx": -0.25, "dy": 0.2},
        {"id": overlay.create_oval(200, 450, 460, 620, fill="#091830", outline=""), "dx": 0.2, "dy": -0.3},
    ]

    def _animate_orbs():
        if not overlay.winfo_exists():
            return
        w = overlay.winfo_width()
        h = overlay.winfo_height()
        if not LOW_ANIMATION_MODE.get("enabled"):
            for orb in orbs:
                overlay.move(orb["id"], orb["dx"], orb["dy"])
                coords = overlay.coords(orb["id"])
                if len(coords) < 4:
                    continue
                if coords[2] < -120 or coords[0] > w + 120:
                    orb["dx"] = -orb["dx"]
                if coords[3] < -120 or coords[1] > h + 120:
                    orb["dy"] = -orb["dy"]
        overlay.after(_anim_delay(30, 600), _animate_orbs)
    _animate_orbs()

    # Block all mouse interaction with background
    for seq in ("<Button-1>", "<Button-2>", "<Button-3>", "<MouseWheel>"):
        overlay.bind(seq, lambda e: "break")

    # ---- Login card (centred on overlay) ----
    login_card = tk.Frame(overlay, bg=bg, highlightthickness=2, highlightbackground="#1a3560")
    login_card.place(relx=0.5, rely=0.5, anchor="center", width=460, height=560)

    # ---- Close (X) button ----
    def _close_login():
        global _login_overlay
        try:
            overlay.destroy()
        except Exception:
            pass
        _login_overlay = None

    close_x = tk.Label(login_card, text="\u2715", font=("Segoe UI", 14, "bold"),
                       fg="#9fb4d1", bg=bg, cursor="hand2")
    close_x.place(relx=1.0, x=-32, y=8)
    close_x.bind("<Button-1>", lambda e: _close_login())
    close_x.bind("<Enter>", lambda e: close_x.config(fg="#ff6b6b"))
    close_x.bind("<Leave>", lambda e: close_x.config(fg="#9fb4d1"))

    # Escape key closes overlay
    root.bind("<Escape>", lambda e: _close_login())

    # ---- Content wrapper inside card ----
    content = tk.Frame(login_card, bg=bg)
    content.pack(fill="both", expand=True, padx=10, pady=10)

    # ---- Header with glow ----
    hdr = tk.Frame(content, bg=panel, highlightthickness=1, highlightbackground="#1a3560")
    hdr.pack(fill="x", pady=(4, 8))

    lock_lbl = tk.Label(hdr, text="\U0001F512", font=("Segoe UI", 22), fg=accent, bg=panel)
    lock_lbl.pack(pady=(14, 0))

    title_lbl = tk.Label(hdr, text="SMART FILE ORGANIZER", font=("Segoe UI", 15, "bold"),
                         fg=txt, bg=panel)
    title_lbl.pack(pady=(4, 2))
    sub_lbl = tk.Label(hdr, text="Log in or create an account", font=("Segoe UI", 9),
                       fg=muted, bg=panel)
    sub_lbl.pack(pady=(0, 12))

    # Animated title glow
    _glow_colors = ["#eaf2ff", "#cfe8ff", "#b0d4ff", "#cfe8ff"]
    def _title_glow(i=0):
        if not overlay.winfo_exists():
            return
        if not LOW_ANIMATION_MODE.get("enabled"):
            title_lbl.config(fg=_glow_colors[i % len(_glow_colors)])
            i += 1
        overlay.after(_anim_delay(220, 1100), lambda: _title_glow(i))
    _title_glow()

    # Animated accent line
    accent_cv = tk.Canvas(hdr, height=3, bg=panel, highlightthickness=0)
    accent_cv.pack(fill="x", padx=40, pady=(0, 10))
    accent_rect = accent_cv.create_rectangle(0, 0, 60, 3, fill=accent, outline="")
    def _animate_accent_line(dx=[1.5]):
        if not overlay.winfo_exists():
            return
        if not LOW_ANIMATION_MODE.get("enabled"):
            accent_cv.move(accent_rect, dx[0], 0)
            x1 = accent_cv.coords(accent_rect)[0]
            cw = accent_cv.winfo_width() or 360
            if x1 + 60 > cw or x1 < 0:
                dx[0] = -dx[0]
        overlay.after(_anim_delay(22, 700), _animate_accent_line)
    _animate_accent_line()

    # ---- Form card with pulsing border ----
    form = tk.Frame(content, bg=card, highlightthickness=1, highlightbackground="#203d63")
    form.pack(fill="x", pady=8)

    _card_borders = ["#203d63", "#254a76", "#2f5a8f", "#254a76"]
    def _pulse_card_border(i=0):
        if not overlay.winfo_exists():
            return
        if not LOW_ANIMATION_MODE.get("enabled"):
            form.config(highlightbackground=_card_borders[i % len(_card_borders)])
            i += 1
        overlay.after(_anim_delay(400, 1300), lambda: _pulse_card_border(i))
    _pulse_card_border()

    # ---- Username ----
    tk.Label(form, text="Username", font=("Segoe UI", 10, "bold"), fg=txt, bg=card).pack(anchor="w", padx=16, pady=(14, 2))
    user_var = tk.StringVar()
    user_entry = tk.Entry(form, textvariable=user_var, font=("Segoe UI", 11),
                          bg=entry_bg, fg=txt, insertbackground=txt,
                          relief="flat", highlightthickness=2,
                          highlightbackground="#203d63", highlightcolor=accent)
    user_entry.pack(fill="x", padx=16, pady=(0, 8))

    def _user_focus_in(e):
        user_entry.config(highlightbackground=accent)
    def _user_focus_out(e):
        user_entry.config(highlightbackground="#203d63")
    user_entry.bind("<FocusIn>", _user_focus_in)
    user_entry.bind("<FocusOut>", _user_focus_out)

    # ---- Password with eye toggle ----
    tk.Label(form, text="Password", font=("Segoe UI", 10, "bold"), fg=txt, bg=card).pack(anchor="w", padx=16, pady=(4, 2))
    pw_frame = tk.Frame(form, bg=entry_bg, highlightthickness=2, highlightbackground="#203d63")
    pw_frame.pack(fill="x", padx=16, pady=(0, 8))

    pw_var = tk.StringVar()
    pw_entry = tk.Entry(pw_frame, textvariable=pw_var, show="\u2022", font=("Segoe UI", 11),
                        bg=entry_bg, fg=txt, insertbackground=txt,
                        relief="flat", highlightthickness=0, bd=0)
    pw_entry.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=3)

    pw_visible = {"state": False}
    eye_btn = tk.Label(pw_frame, text="\U0001F441", font=("Segoe UI", 13), fg=muted,
                       bg=entry_bg, cursor="hand2", padx=8)
    eye_btn.pack(side="right", pady=2)

    def _toggle_pw_visibility(e=None):
        pw_visible["state"] = not pw_visible["state"]
        if pw_visible["state"]:
            pw_entry.config(show="")
            eye_btn.config(fg=accent)
        else:
            pw_entry.config(show="\u2022")
            eye_btn.config(fg=muted)
    eye_btn.bind("<Button-1>", _toggle_pw_visibility)
    eye_btn.bind("<Enter>", lambda e: eye_btn.config(fg=accent if not pw_visible["state"] else "#ff9966"))
    eye_btn.bind("<Leave>", lambda e: eye_btn.config(fg=accent if pw_visible["state"] else muted))

    def _pw_focus_in(e):
        pw_frame.config(highlightbackground=accent)
    def _pw_focus_out(e):
        pw_frame.config(highlightbackground="#203d63")
    pw_entry.bind("<FocusIn>", _pw_focus_in)
    pw_entry.bind("<FocusOut>", _pw_focus_out)

    # ---- Message label ----
    msg_label = tk.Label(form, text="", font=("Segoe UI", 9), fg=err_fg, bg=card, wraplength=380)
    msg_label.pack(anchor="w", padx=16, pady=(0, 6))

    # ---- Shake animation for errors ----
    def _shake_widget(widget, times=3, offset=8):
        def _step(n, dx):
            if not overlay.winfo_exists():
                return
            if n <= 0:
                return
            try:
                widget.pack_configure(padx=(16 + dx, 16 - dx))
            except Exception:
                pass
            overlay.after(50, lambda: _step(n - 1, -dx))
        _step(times * 2, offset)

    # ---- Login / Register logic ----
    def _close_and_succeed():
        global _login_overlay
        try:
            overlay.destroy()
        except Exception:
            pass
        _login_overlay = None
        on_login_success()

    def do_login():
        ok, msg = session_mgr.login(user_var.get(), pw_var.get())
        if ok:
            msg_label.config(text="\u2714  " + msg, fg=ok_fg)
            overlay.after(600, _close_and_succeed)
        else:
            msg_label.config(text="\u2718  " + msg, fg=err_fg)
            _shake_widget(msg_label)

    def do_register():
        ok, msg = session_mgr.register(user_var.get(), pw_var.get())
        if ok:
            msg_label.config(text="\u2714  " + msg, fg=ok_fg)
        else:
            msg_label.config(text="\u2718  " + msg, fg=err_fg)
            _shake_widget(msg_label)

    # ---- Buttons ----
    btn_frame = tk.Frame(form, bg=card)
    btn_frame.pack(fill="x", padx=16, pady=(6, 16))

    login_btn = tk.Button(btn_frame, text="\u2794  Log In", command=do_login,
                          bg=btn_bg, fg=txt, activebackground=btn_press,
                          activeforeground=txt, relief="flat",
                          font=("Segoe UI", 11, "bold"), padx=20, pady=8,
                          cursor="hand2")
    login_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

    reg_btn = tk.Button(btn_frame, text="\u270E  Register", command=do_register,
                        bg=reg_bg, fg=txt, activebackground=reg_press,
                        activeforeground=txt, relief="flat",
                        font=("Segoe UI", 11), padx=20, pady=8,
                        cursor="hand2")
    reg_btn.pack(side="left", expand=True, fill="x", padx=(6, 0))

    # Hover effects
    def _btn_hover_bind(btn, normal_bg, hover_bg, press_bg):
        def on_enter(e):
            btn.config(bg=hover_bg)
        def on_leave(e):
            btn.config(bg=normal_bg)
        def on_press(e):
            btn.config(bg=press_bg)
        def on_release(e):
            btn.config(bg=hover_bg)
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        btn.bind("<ButtonPress-1>", on_press)
        btn.bind("<ButtonRelease-1>", on_release)

    _btn_hover_bind(login_btn, btn_bg, btn_hover, btn_press)
    _btn_hover_bind(reg_btn, reg_bg, reg_hover, reg_press)

    # Pulsing border on login button
    _login_pulse = [btn_bg, "#3374e8", "#3a7af2", "#3374e8"]
    def _pulse_login_btn(i=0):
        if not overlay.winfo_exists():
            return
        if not LOW_ANIMATION_MODE.get("enabled"):
            login_btn.config(highlightbackground=_login_pulse[i % len(_login_pulse)])
            i += 1
        overlay.after(_anim_delay(350, 1200), lambda: _pulse_login_btn(i))
    _pulse_login_btn()

    # ---- Footer ----
    tk.Label(content, text="Login unlocks Organize & Duplicate features.\n"
             "Monitoring has 2 free tries without login.",
             font=("Segoe UI", 8), fg=muted, bg=bg, justify="center").pack(pady=(10, 8))

    # Key bindings
    pw_entry.bind("<Return>", lambda _: do_login())
    user_entry.bind("<Return>", lambda _: pw_entry.focus_set())
    user_entry.focus_set()

    # ---- Slide-in entrance animation ----
    login_card.place_configure(rely=0.6)  # start slightly below centre
    _target_rely = [0.5]
    def _slide_in(rely=0.6):
        if not overlay.winfo_exists():
            return
        if LOW_ANIMATION_MODE.get("enabled"):
            login_card.place_configure(rely=_target_rely[0])
            return
        if rely <= _target_rely[0]:
            login_card.place_configure(rely=_target_rely[0])
            return
        login_card.place_configure(rely=rely)
        overlay.after(_anim_delay(16, 100), lambda: _slide_in(rely - 0.008))
    _slide_in()


# =========================
# GUI
# =========================
def gui():
    root = tk.Tk()
    root.title("Intelligent File Organizer and Activity Tracker")

    # Set taskbar / window icon
    _base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    _icon_path = os.path.join(_base, "activity.ico")
    if os.path.isfile(_icon_path):
        root.iconbitmap(default=_icon_path)

    root.geometry("900x600")
    root.minsize(720, 420)
    root.state("zoomed")
    root.resizable(True, True)

    # --- Visual theme ---
    bg_color = "#0e1b2e"
    panel_color = "#12263f"
    accent = "#3dd6d0"
    accent_2 = "#7aa2ff"
    text_color = "#eaf2ff"
    root.configure(bg=bg_color)

    # --- Static background ---
    canvas = tk.Canvas(root, bg=bg_color, highlightthickness=0)
    canvas.place(relx=0, rely=0, relwidth=1, relheight=1)

    # --- Background image ---
    from PIL import Image, ImageTk
    _bg_image_path = os.path.join(
        getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))),
        "upscalemedia-transformed.jpeg"
    )
    _bg_photo = None
    _bg_image_id = None
    _bg_source_image = None
    _bg_resize_job = None
    _bg_settle_job = None
    _bg_last_render = {"size": (0, 0), "hq": False}

    if _bg_image_path and os.path.isfile(_bg_image_path):
        try:
            with Image.open(_bg_image_path) as _img:
                _bg_source_image = _img.convert("RGB")
        except Exception as e:
            print(f"[BG-IMG] Could not load background image: {e}")

    def _set_background_image(high_quality=True):
        nonlocal _bg_photo, _bg_image_id, _bg_resize_job, _bg_settle_job, _bg_last_render
        if high_quality:
            _bg_settle_job = None
        else:
            _bg_resize_job = None
        if _bg_source_image is None:
            return
        try:
            cw = canvas.winfo_width()
            ch = canvas.winfo_height()
            if cw < 2 or ch < 2:
                return
            size = (cw, ch)
            if size == _bg_last_render["size"] and (_bg_last_render["hq"] or not high_quality):
                return
            resample = Image.LANCZOS if high_quality else Image.BILINEAR
            img = _bg_source_image.resize(size, resample)
            _bg_photo = ImageTk.PhotoImage(img)
            if _bg_image_id:
                canvas.itemconfig(_bg_image_id, image=_bg_photo)
            else:
                _bg_image_id = canvas.create_image(0, 0, anchor="nw", image=_bg_photo)
            _bg_last_render["size"] = size
            _bg_last_render["hq"] = high_quality
            canvas.tag_lower(_bg_image_id)          # keep behind all other items
        except Exception as e:
            print(f"[BG-IMG] Could not load background image: {e}")

    def _on_canvas_resize(event):
        nonlocal _bg_resize_job, _bg_settle_job
        if _bg_resize_job is not None:
            canvas.after_cancel(_bg_resize_job)
        if _bg_settle_job is not None:
            canvas.after_cancel(_bg_settle_job)
        _bg_resize_job = canvas.after(BG_RESIZE_DEBOUNCE_MS, lambda: _set_background_image(False))
        _bg_settle_job = canvas.after(BG_RESIZE_DEBOUNCE_MS + BG_RESIZE_SETTLE_MS, lambda: _set_background_image(True))

    canvas.bind("<Configure>", _on_canvas_resize)
    root.after(100, _set_background_image)           # initial draw after window maps

    # --- Main panel ---
    panel = tk.Frame(root, bg=panel_color, bd=0, highlightthickness=0)
    panel_window = canvas.create_window(0, 0, window=panel)

    card_color = "#132945"
    card_border = "#203d63"
    card_border_pulse = "#2f5a8f"
    primary_btn = "#2d6cdf"
    primary_hover = "#3a7af2"
    primary_press = "#255ec4"
    danger_btn = "#cc3d3d"
    danger_hover = "#e04a4a"
    danger_press = "#b33535"
    ghost_btn = "#1c3558"
    ghost_hover = "#24456f"
    ghost_press = "#162b47"
    focus_ring = "#3a5f8f"

    # --- Theme definitions ---
    THEMES = {
        "dark": {
            "bg_color": "#0e1b2e", "panel_color": "#12263f",
            "card_color": "#132945", "card_border": "#203d63", "card_border_pulse": "#2f5a8f",
            "accent": "#3dd6d0", "accent_2": "#7aa2ff",
            "text_color": "#eaf2ff", "subtitle_fg": "#bcd0ee",
            "body_fg": "#d4e4ff", "value_fg": "#c6d7f5",
            "muted_fg": "#7a9abc", "warn_fg": "#f1b0b0",
            "logout_icon_fg": "#5a7a9e", "logout_label_fg": "#7a9abc",
            "primary_btn": "#2d6cdf", "primary_hover": "#3a7af2", "primary_press": "#255ec4",
            "danger_btn": "#cc3d3d", "danger_hover": "#e04a4a", "danger_press": "#b33535",
            "ghost_btn": "#1c3558", "ghost_hover": "#24456f", "ghost_press": "#162b47",
            "ghost_fg": "#eaf2ff",
            "focus_ring": "#3a5f8f", "separator_color": "#203d63",
            "dup_header_fg": "#9fb4d1", "clear_btn_fg": "#b0c4de",
            "disabled_bg": "#16263d", "disabled_fg": "#7a8ba8",
            "start_grad_top": "#1f53ad", "start_grad_bottom": "#10336f",
            "stop_grad_top": "#a11e1e", "stop_grad_bottom": "#641414",
            "log_grad_top": "#1f426e", "log_grad_bottom": "#182a45",
            "select_grad_top": "#214a75", "select_grad_bottom": "#182a45",
            "org_grad_top": "#2657a6", "org_grad_bottom": "#133b81",
            "dup_btn_bg": "#1c8a7a", "dup_grad_top": "#154757", "dup_grad_bottom": "#2D1562",
            "glow_colors": ["#eaf2ff", "#cfe8ff", "#b9d8ff", "#cfe8ff"],
            "session_glow_colors": ["#3dd6d0", "#5ae8e0", "#7ff0ea", "#5ae8e0"],
            "session_logged_out_colors": ["#7a9abc", "#8dacc8", "#7a9abc"],
            "border_cycle": ["#203d63", "#254a76", "#2f5a8f", "#254a76"],
            "pulse_cycle": ["#2d6cdf", "#3374e8", "#2d6cdf", "#2b63cc"],
            "pulse_cycle_org": ["#2d6cdf", "#3b79ec", "#2d6cdf", "#2a60c3"],
            "pulse_cycle_dup": ["#1c8a7a", "#1f9d8b", "#1c8a7a", "#197769"],
            "toggle_icon": "\u2600", "toggle_fg": "#f0c040",
            "idle_start": ["#4a6fa1", "#5b83b8", "#4a6fa1"],
            "idle_stop": ["#8b3d3d", "#a34b4b", "#8b3d3d"],
            "idle_log": ["#2d486e", "#3b5e89", "#2d486e"],
            "idle_select": ["#2b4c66", "#3c6a8c", "#2b4c66"],
            "idle_org": ["#3c63a6", "#4b78bf", "#3c63a6"],
            "idle_dup": ["#2a6d64", "#358a7f", "#2a6d64"],
        },
        "light": {
            "bg_color": "#e8eef5", "panel_color": "#ffffff",
            "card_color": "#f0f5fa", "card_border": "#c8d4e3", "card_border_pulse": "#a0b8d4",
            "accent": "#1a9e93", "accent_2": "#4a78c9",
            "text_color": "#1a2a3e", "subtitle_fg": "#4a5f7a",
            "body_fg": "#2d3e54", "value_fg": "#3a506e",
            "muted_fg": "#5a6f8a", "warn_fg": "#c0392b",
            "logout_icon_fg": "#7a8fa5", "logout_label_fg": "#6a7f9a",
            "primary_btn": "#2563cc", "primary_hover": "#3070dd", "primary_press": "#1d52b0",
            "danger_btn": "#c0392b", "danger_hover": "#d14a3c", "danger_press": "#a8301f",
            "ghost_btn": "#d0dae8", "ghost_hover": "#bcc8da", "ghost_press": "#a8b8cc",
            "ghost_fg": "#2d3e54",
            "focus_ring": "#8aa4c4", "separator_color": "#c8d4e3",
            "dup_header_fg": "#4a5f7a", "clear_btn_fg": "#6a7f99",
            "disabled_bg": "#dde4ee", "disabled_fg": "#8a98b0",
            "start_grad_top": "#2d6edb", "start_grad_bottom": "#1e50aa",
            "stop_grad_top": "#d14a3c", "stop_grad_bottom": "#a8301f",
            "log_grad_top": "#c0cee0", "log_grad_bottom": "#b0bdd0",
            "select_grad_top": "#c0cee0", "select_grad_bottom": "#b0bdd0",
            "org_grad_top": "#2e72e0", "org_grad_bottom": "#1c55b0",
            "dup_btn_bg": "#147a71", "dup_grad_top": "#2ca1b6", "dup_grad_bottom": "#017994",
            "glow_colors": ["#1a2a3e", "#2a3e5e", "#3a4e6e", "#2a3e5e"],
            "session_glow_colors": ["#1a9e93", "#25b8aa", "#30ccbe", "#25b8aa"],
            "session_logged_out_colors": ["#5a6f8a", "#6a7f9a", "#5a6f8a"],
            "border_cycle": ["#c8d4e3", "#b0c0d4", "#a0b8d4", "#b0c0d4"],
            "pulse_cycle": ["#2563cc", "#2d6edb", "#2563cc", "#2058bc"],
            "pulse_cycle_org": ["#2563cc", "#3070dd", "#2563cc", "#1d55b5"],
            "pulse_cycle_dup": ["#1a9e93", "#1fad9e", "#1a9e93", "#158876"],
            "toggle_icon": "\u263E", "toggle_fg": "#5a78a0",
            "idle_start": ["#7a9ec4", "#8bb0d5", "#7a9ec4"],
            "idle_stop": ["#a04040", "#b05050", "#a04040"],
            "idle_log": ["#a0b0c8", "#b0c0d8", "#a0b0c8"],
            "idle_select": ["#a0b4c8", "#b0c4d8", "#a0b4c8"],
            "idle_org": ["#6090c0", "#70a0d0", "#6090c0"],
            "idle_dup": ["#40907e", "#50a08e", "#40907e"],
        },
    }
    _theme_state = {"current": "dark"}

    title_base = 18
    subtitle_base = 10
    section_title_base = 11
    body_base = 9
    btn_font_base = 10

    header_frame = tk.Frame(panel, bg=panel_color)
    content_frame = tk.Frame(panel, bg=panel_color)

    class RoundedButton(tk.Canvas):
        def __init__(
            self,
            parent,
            text,
            command=None,
            radius=14,
            bg="#2d6cdf",
            grad_top=None,
            grad_bottom=None,
            fg="#fff7ea",
            ring="#3a5f8f",
            font=("Segoe UI", 10),
            height=38,
            glass=True,
            ring_width=2,
            transparent=False,
            **kwargs
        ):
            super().__init__(parent, highlightthickness=0, bd=0, relief="flat", cursor="hand2", **kwargs)
            self.is_rounded = True
            self.command = command
            self.radius = radius
            self.base_bg = bg
            self.current_bg = bg
            self.grad_top = grad_top
            self.grad_bottom = grad_bottom
            self.text_color = fg
            self.ring_color = ring
            self.font = font
            self.height = height
            self.text = text
            self.state = "normal"
            self.disabled_bg = "#16263d"
            self.disabled_fg = "#7a8ba8"
            self.transparent = transparent

            if transparent:
                # Transparent mode: match parent background, only show text
                parent_bg = parent.cget("bg")
                self.configure(height=self.height, bg=parent_bg, highlightthickness=0)
            else:
                self.ring_width = ring_width
                self.glass = glass
                self.configure(height=self.height, bg=parent.cget("bg"))
            self.bind("<Configure>", self._redraw)
            self.bind("<ButtonRelease-1>", self._on_click)
            self._redraw()

        def _rounded_rect(self, x1, y1, x2, y2, r, **kwargs):
            r = max(0, r)
            points = [
                x1 + r, y1,
                x2 - r, y1,
                x2, y1,
                x2, y1 + r,
                x2, y2 - r,
                x2, y2,
                x2 - r, y2,
                x1 + r, y2,
                x1, y2,
                x1, y2 - r,
                x1, y1 + r,
                x1, y1
            ]
            return self.create_polygon(points, smooth=True, **kwargs)

        def _blend(self, c1, c2, t):
            c1 = c1.lstrip("#")
            c2 = c2.lstrip("#")
            r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
            r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
            r = int(r1 + (r2 - r1) * t)
            g = int(g1 + (g2 - g1) * t)
            b = int(b1 + (b2 - b1) * t)
            return "#{:02x}{:02x}{:02x}".format(r, g, b)

        def _redraw(self, _event=None):
            self.delete("all")
            w = max(1, self.winfo_width())
            h = max(self.height, self.winfo_height())
            fg = self.text_color if self.state != "disabled" else self.disabled_fg
            
            if self.transparent:
                self.text_id = self.create_text(
                    w // 2,
                    h // 2,
                    text=self.text,
                    fill=fg,
                    font=self.font
                )
                return

            # Sync canvas bg with parent so corners are invisible
            try:
                parent_bg = self.master.cget("bg")
            except Exception:
                parent_bg = self.cget("bg")
            tk.Canvas.configure(self, bg=parent_bg)

            r = min(self.radius, int(min(w, h) / 2))
            ring = self.ring_color
            bg = self.current_bg if self.state != "disabled" else self.disabled_bg
            grad_top = self.grad_top or self._blend(bg, "#ffffff", 0.08)
            grad_bottom = self.grad_bottom or self._blend(bg, "#000000", 0.10)

            ring_width = max(0, int(self.ring_width))
            ring_outline = ring if ring_width > 0 else ""

            self._rounded_rect(
                1,
                1,
                w - 1,
                h - 1,
                r,
                outline=ring_outline,
                width=ring_width,
                fill=parent_bg
            )
            self._rounded_rect(
                1 + ring_width,
                1 + ring_width,
                w - 1 - ring_width,
                h - 1 - ring_width,
                r - ring_width,
                outline="",
                fill=grad_bottom
            )
            top_h = int(h * 0.50)
            top_r = min(r - ring_width, int(top_h / 2))
            self._rounded_rect(
                1 + ring_width,
                1 + ring_width,
                w - 1 - ring_width,
                top_h,
                top_r,
                outline="",
                fill=grad_top
            )
            if self.glass:
                hl = self._blend(grad_top, "#ffffff", 0.14)
                hl_h = int(h * 0.28)
                hl_r = min(r - ring_width - 2, int(hl_h / 2))
                self._rounded_rect(
                    4 + ring_width,
                    4 + ring_width,
                    w - 4 - ring_width,
                    hl_h,
                    hl_r,
                    outline="",
                    fill=hl
                )
            self.text_id = self.create_text(
                w // 2,
                h // 2,
                text=self.text,
                fill=fg,
                font=self.font
            )

        def _on_click(self, _event=None):
            if self.state == "disabled":
                return
            if self.command:
                self.command()

        def spawn_ripple(self, color):
            if self.state == "disabled":
                return
            if LOW_ANIMATION_MODE.get("enabled"):
                return
            w = max(1, self.winfo_width())
            h = max(1, self.winfo_height())
            max_r = int((w * w + h * h) ** 0.5)
            steps = 12

            def animate(i=0):
                self.delete("ripple")
                r = int((i / steps) * max_r)
                self.create_oval(
                    w // 2 - r,
                    h // 2 - r,
                    w // 2 + r,
                    h // 2 + r,
                    outline=_blend(color, self.cget("bg"), i / steps),
                    width=2,
                    tags="ripple"
                )
                self.tag_raise(self.text_id)
                if i < steps:
                    self.after(_anim_delay(16, 100), lambda: animate(i + 1))
                else:
                    self.delete("ripple")

            animate()

        def config(self, **kwargs):
            if "command" in kwargs:
                self.command = kwargs.pop("command")
            if "bg" in kwargs:
                self.current_bg = kwargs.pop("bg")
            if "fg" in kwargs:
                self.text_color = kwargs.pop("fg")
            if "highlightbackground" in kwargs:
                self.ring_color = kwargs.pop("highlightbackground")
            if "font" in kwargs:
                self.font = kwargs.pop("font")
            if "state" in kwargs:
                self.state = kwargs.pop("state")
            if "text" in kwargs:
                self.text = kwargs.pop("text")
            if "grad_top" in kwargs:
                self.grad_top = kwargs.pop("grad_top")
            if "grad_bottom" in kwargs:
                self.grad_bottom = kwargs.pop("grad_bottom")
            if "glass" in kwargs:
                self.glass = kwargs.pop("glass")
            if "ring_width" in kwargs:
                self.ring_width = kwargs.pop("ring_width")
            if "transparent" in kwargs:
                self.transparent = kwargs.pop("transparent")
            if "height" in kwargs:
                self.height = kwargs.pop("height")
                super().config(height=self.height)
            if "width" in kwargs:
                width = kwargs.pop("width")
                if width and width > 1:
                    super().config(width=width)
            super().config(**kwargs)
            self._redraw()

        configure = config

        def cget(self, key):
            if key == "state":
                return self.state
            if key == "bg":
                return self.current_bg
            if key == "fg":
                return self.text_color
            if key == "highlightbackground":
                return self.ring_color
            if key == "text":
                return self.text
            if key == "font":
                return self.font
            return super().cget(key)

    # Title at top of header
    title = tk.Label(
        header_frame,
        text="Intelligent File Organizer and Activity Tracker",
        font=("Segoe UI", title_base, "bold"),
        fg=text_color,
        bg=panel_color
    )
    title.pack(pady=(6, 2))

    # --- Subtitle row: subtitle centered, controls on right ---
    subtitle_row = tk.Frame(header_frame, bg=panel_color)
    subtitle_row.pack(fill="x", padx=16, pady=(0, 2))
    subtitle_row.grid_columnconfigure(0, weight=1)
    subtitle_row.grid_columnconfigure(1, weight=1)
    subtitle_row.grid_columnconfigure(2, weight=1)

    subtitle = tk.Label(
        subtitle_row,
        text="Monitor, organize, and find duplicate files in your folders",
        font=("Segoe UI", subtitle_base),
        fg="#bcd0ee",
        bg=panel_color,
        anchor="center",
        justify="center"
    )
    subtitle.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 2))

    # --- Top-right controls bar (overlaid on right of subtitle row) ---
    top_controls = tk.Frame(subtitle_row, bg=panel_color)
    top_controls.grid(row=0, column=2, sticky="e")

    # --- Session status bar (upgraded) ---
    session_bar = tk.Frame(header_frame, bg=panel_color)
    session_bar.pack(fill="x", padx=30, pady=(2, 4))

    # Left side: icon + status text
    session_left = tk.Frame(session_bar, bg=panel_color)
    session_left.pack(side="left", anchor="w")

    session_icon = tk.Label(
        session_left,
        text="\u26AA",  # grey circle for logged-out
        font=("Segoe UI", 10),
        fg="#5a7a9e",
        bg=panel_color
    )
    session_icon.pack(side="left", padx=(0, 6))

    session_label = tk.Label(
        session_left,
        text="Not logged in  \u2022  2 free monitoring tries",
        font=("Segoe UI", 9),
        fg="#7a9abc",
        bg=panel_color
    )
    session_label.pack(side="left")

    # Animated glow for session label when logged in
    _session_glow_active = {"on": False}
    _session_glow_colors = ["#3dd6d0", "#5ae8e0", "#7ff0ea", "#5ae8e0"]
    _session_logged_out_colors = ["#7a9abc", "#8dacc8", "#7a9abc"]

    def _animate_session_label(i=0):
        if not root.winfo_exists():
            return
        if _session_glow_active["on"] and not LOW_ANIMATION_MODE.get("enabled"):
            c = _session_glow_colors[i % len(_session_glow_colors)]
            session_label.config(fg=c)
            session_icon.config(fg=c)
            i += 1
        root.after(_anim_delay(300, 1100), lambda: _animate_session_label(i))
    _animate_session_label()

    def _refresh_session_ui():
        """Update the session status bar and enable/disable gated buttons."""
        _t = THEMES[_theme_state["current"]]
        if session_mgr.is_logged_in:
            _session_glow_active["on"] = True
            session_icon.config(text="\u2714", fg=_t["accent"], font=("Segoe UI", 10, "bold"))
            session_label.config(
                text=f"Logged in as  {session_mgr.username}",
                font=("Segoe UI", 9, "bold"),
                fg=_t["accent"]
            )
            logout_palette = _llb_logout_colors
            login_logout_btn.config(
                text="\u23FB  Logout",
                bg=logout_palette["bg"],
                grad_top=logout_palette["grad_top"],
                grad_bottom=logout_palette["grad_bottom"],
                fg="#ffffff",
                highlightbackground=logout_palette["ring"],
                font=("Segoe UI", 9, "bold"),
            )
            # Enable gated features
            try:
                org_btn.config(state="normal")
                dup_select_btn.config(state="normal")
            except Exception:
                pass
            # Auto-load saved folder paths
            settings = session_mgr.user_settings
            if settings.get("monitored_folder") and os.path.isdir(settings["monitored_folder"]):
                _saved_mon = settings["monitored_folder"]
                set_monitor_path(_saved_mon)
                # Auto-START monitoring so events & notifications work immediately
                if not monitoring and is_folder_safe(_saved_mon):
                    try:
                        session_mgr.audit("start_monitoring", f"Auto-started monitoring: {_saved_mon}")
                        start_monitoring(_saved_mon, root, start_btn, stop_btn)
                    except Exception:
                        pass
            if settings.get("organize_folder") and os.path.isdir(settings["organize_folder"]):
                set_organize_path(settings["organize_folder"])
            # Load user's saved theme preference
            saved_theme = settings.get("theme", "dark")
            if saved_theme in THEMES and saved_theme != _theme_state["current"]:
                root.after(50, lambda: apply_theme(saved_theme))
        else:
            _session_glow_active["on"] = False
            remaining = session_mgr.free_trial_remaining()
            session_icon.config(text="\u26AA", fg=_t["logout_icon_fg"], font=("Segoe UI", 10))
            session_label.config(
                text=f"Not logged in  \u2022  {remaining} free monitoring {'try' if remaining == 1 else 'tries'}",
                font=("Segoe UI", 9),
                fg=_t["logout_label_fg"]
            )
            login_palette = _llb_login_colors
            login_logout_btn.config(
                text="\u2794  Login",
                bg=login_palette["bg"],
                grad_top=login_palette["grad_top"],
                grad_bottom=login_palette["grad_bottom"],
                fg="#ffffff",
                font=("Hellbound", 14, "bold"),
                highlightbackground=login_palette["ring"],
            )
            # Disable gated features
            try:
                org_btn.config(state="disabled")
                dup_select_btn.config(state="disabled")
            except Exception:
                pass

    def _on_login_logout():
        if session_mgr.is_logged_in:
            session_mgr.logout()
            # --- FULL APPLICATION RESET ---
            # 1. Stop monitoring if running
            global monitoring, observer
            if monitoring:
                try:
                    stop_monitoring(root, start_btn, stop_btn)
                except Exception:
                    pass
            # 2. Close live log window
            global live_log_window, live_log_widget
            if live_log_window and tk.Toplevel.winfo_exists(live_log_window):
                try:
                    live_log_window.destroy()
                except Exception:
                    pass
                live_log_window = None
                live_log_widget = None
            # 3. Clear log history
            log_history.clear()
            # 4. Close duplicate manager window
            global duplicate_window
            if duplicate_window and tk.Toplevel.winfo_exists(duplicate_window):
                try:
                    duplicate_window.destroy()
                except Exception:
                    pass
                duplicate_window = None
            # 5. Clear monitor section
            try:
                clear_monitor_path()
            except Exception:
                pass
            # 6. Clear organize section
            try:
                clear_organize_path()
            except Exception:
                pass
            # 7. Reset duplicate selected path
            try:
                duplicate_selected["path"] = ""
            except Exception:
                pass
            # 8. Reset theme to dark
            if _theme_state["current"] != "dark":
                apply_theme("dark")
            # 9. Refresh UI to logged-out state
            _refresh_session_ui()
        else:
            open_login_window(root, _refresh_session_ui)

    login_logout_btn = RoundedButton(
        top_controls,
        text="\u2794  Login",
        command=_on_login_logout,
        bg="#2d6cdf",
        grad_top="#3877e3",
        grad_bottom="#2357b4",
        fg="#ffffff",
        ring="#3a5f8f",
        font=("Segoe UI", 9, "bold"),
        height=32,
        radius=16,
        ring_width=2,
        glass=True,
        width=130,
    )
    login_logout_btn.pack(side="right", pady=2, padx=(6, 0))

    settings_state = {
        "win": None,
        "card": None,
        "title": None,
        "subtitle": None,
        "theme_row": None,
        "theme_text": None,
        "theme_label": None,
        "theme_hint": None,
        "theme_value": None,
        "theme_switch": None,
        "motion_row": None,
        "motion_text": None,
        "motion_label": None,
        "motion_hint": None,
        "motion_value": None,
        "motion_switch": None,
    }

    def _create_setting_switch(parent, on_toggle):
        sw = tk.Canvas(
            parent,
            width=SETTINGS_SWITCH_WIDTH,
            height=SETTINGS_SWITCH_HEIGHT,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=1,
        )
        state = {
            "on": False,
            "progress": 0.0,
            "anim_job": None,
            "on_color": "#2d6cdf",
            "off_color": "#1c3558",
            "knob_color": "#ffffff",
            "border_color": "#203d63",
        }

        def _mix(c1, c2, ratio):
            c1 = c1.lstrip("#")
            c2 = c2.lstrip("#")
            r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
            r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            return "#{:02x}{:02x}{:02x}".format(r, g, b)

        def _draw():
            sw.delete("all")
            w, h = SETTINGS_SWITCH_WIDTH, SETTINGS_SWITCH_HEIGHT
            pad = 1
            radius = h // 2
            track_color = _mix(state["off_color"], state["on_color"], state["progress"])
            track_border = _mix(state["border_color"], track_color, 0.45)

            knob_d = h - 8
            knob_pad = (h - knob_d) // 2
            travel = w - (knob_d + (knob_pad * 2))
            knob_x = knob_pad + int(travel * state["progress"])

            sw.create_oval(pad, pad, h - pad, h - pad, fill=track_color, outline=track_border, width=1)
            sw.create_oval(w - h + pad, pad, w - pad, h - pad, fill=track_color, outline=track_border, width=1)
            sw.create_rectangle(radius, pad, w - radius, h - pad, fill=track_color, outline=track_border, width=1)

            sw.create_oval(
                knob_x,
                knob_pad,
                knob_x + knob_d,
                knob_pad + knob_d,
                fill=state["knob_color"],
                outline=_mix(state["knob_color"], state["border_color"], 0.25),
                width=1,
            )

        def _animate_to(target):
            if not sw.winfo_exists():
                return
            if LOW_ANIMATION_MODE.get("enabled") or abs(state["progress"] - target) <= 0.01:
                state["progress"] = target
                _draw()
                state["anim_job"] = None
                return
            state["progress"] += (target - state["progress"]) * 0.38
            _draw()
            state["anim_job"] = sw.after(_anim_delay(16, 100), lambda: _animate_to(target))

        def set_palette(on_color, off_color, knob_color="#ffffff", border_color="#203d63"):
            state["on_color"] = on_color
            state["off_color"] = off_color
            state["knob_color"] = knob_color
            state["border_color"] = border_color
            _draw()

        def set_state(is_on, animate=True):
            state["on"] = bool(is_on)
            target = 1.0 if state["on"] else 0.0
            if state["anim_job"] is not None:
                try:
                    sw.after_cancel(state["anim_job"])
                except Exception:
                    pass
                state["anim_job"] = None
            if animate:
                _animate_to(target)
            else:
                state["progress"] = target
                _draw()

        def _toggle(_event=None):
            set_state(not state["on"], animate=True)
            on_toggle(state["on"])

        sw.bind("<Button-1>", _toggle)
        sw.bind("<Return>", _toggle)
        sw.bind("<space>", _toggle)
        sw.set_palette = set_palette
        sw.set_state = set_state
        sw.get_state = lambda: state["on"]
        _draw()
        return sw

    def _refresh_settings_panel():
        t = THEMES[_theme_state["current"]]

        # Refresh gear icon styling
        settings_btn.config(
            bg=panel_color,
            fg=t["toggle_fg"],
            activebackground=panel_color,
            activeforeground=t["toggle_fg"],
        )

        win = settings_state.get("win")
        if not win or not win.winfo_exists():
            return

        card = settings_state.get("card")
        title_lbl = settings_state.get("title")
        subtitle_lbl = settings_state.get("subtitle")
        theme_row = settings_state.get("theme_row")
        theme_text = settings_state.get("theme_text")
        theme_lbl = settings_state.get("theme_label")
        theme_hint = settings_state.get("theme_hint")
        theme_value = settings_state.get("theme_value")
        theme_switch = settings_state.get("theme_switch")
        motion_row = settings_state.get("motion_row")
        motion_text = settings_state.get("motion_text")
        motion_lbl = settings_state.get("motion_label")
        motion_hint = settings_state.get("motion_hint")
        motion_value = settings_state.get("motion_value")
        motion_switch = settings_state.get("motion_switch")

        win.configure(bg=panel_color)
        if card:
            card.configure(bg=card_color, highlightbackground=card_border)
        if title_lbl:
            title_lbl.configure(bg=card_color, fg=accent_2)
        if subtitle_lbl:
            subtitle_lbl.configure(bg=card_color, fg=t["muted_fg"])
        if theme_row:
            theme_row.configure(bg=card_color, highlightbackground=card_border, highlightcolor=card_border)
        if theme_text:
            theme_text.configure(bg=card_color)
        if theme_lbl:
            theme_lbl.configure(bg=card_color, fg=t["body_fg"])
        if theme_hint:
            theme_hint.configure(bg=card_color, fg=t["muted_fg"])
        if theme_value:
            theme_value.configure(
                text="Dark" if _theme_state["current"] == "dark" else "Light",
                bg=card_color,
                fg=t["value_fg"],
            )
        if motion_row:
            motion_row.configure(bg=card_color, highlightbackground=card_border, highlightcolor=card_border)
        if motion_text:
            motion_text.configure(bg=card_color)
        if motion_lbl:
            motion_lbl.configure(bg=card_color, fg=t["body_fg"])
        if motion_hint:
            motion_hint.configure(bg=card_color, fg=t["muted_fg"])
        if motion_value:
            motion_value.configure(
                text="On" if LOW_ANIMATION_MODE.get("enabled") else "Off",
                bg=card_color,
                fg=t["value_fg"],
            )

        if theme_switch:
            theme_switch.configure(bg=card_color)
            theme_switch.set_palette(
                on_color=t["primary_btn"],
                off_color=t["ghost_btn"],
                knob_color="#ffffff",
                border_color=card_border,
            )
            theme_switch.set_state(_theme_state["current"] == "dark", animate=False)

        if motion_switch:
            motion_switch.configure(bg=card_color)
            motion_switch.set_palette(
                on_color=t["dup_btn_bg"],
                off_color=t["ghost_btn"],
                knob_color="#ffffff",
                border_color=card_border,
            )
            motion_switch.set_state(LOW_ANIMATION_MODE.get("enabled"), animate=False)

    def _refresh_low_anim_btn():
        _refresh_settings_panel()

    def _toggle_low_animation():
        LOW_ANIMATION_MODE["enabled"] = not LOW_ANIMATION_MODE.get("enabled")
        _refresh_settings_panel()

    def _close_settings_panel():
        win = settings_state.get("win")
        if win and win.winfo_exists():
            win.destroy()
        settings_state.update({
            "win": None,
            "card": None,
            "title": None,
            "subtitle": None,
            "theme_row": None,
            "theme_text": None,
            "theme_label": None,
            "theme_hint": None,
            "theme_value": None,
            "theme_switch": None,
            "motion_row": None,
            "motion_text": None,
            "motion_label": None,
            "motion_hint": None,
            "motion_value": None,
            "motion_switch": None,
        })

    def _open_settings_panel():
        if settings_state.get("win") and settings_state["win"].winfo_exists():
            settings_state["win"].lift()
            settings_state["win"].focus_force()
            return

        win = tk.Toplevel(root)
        win.title("Settings")
        win.resizable(False, False)
        win.transient(root)

        panel_w, panel_h = SETTINGS_PANEL_WIDTH, SETTINGS_PANEL_HEIGHT
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        x = settings_btn.winfo_rootx() - panel_w + settings_btn.winfo_width() + 12
        y = settings_btn.winfo_rooty() + settings_btn.winfo_height() + 10
        x = max(8, min(x, screen_w - panel_w - 8))
        y = max(8, min(y, screen_h - panel_h - 8))
        win.geometry(f"{panel_w}x{panel_h}+{x}+{y}")

        card = tk.Frame(win, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=10, pady=10)

        title_lbl = tk.Label(card, text="Settings", font=("Segoe UI", 11, "bold"))
        title_lbl.pack(anchor="w", padx=12, pady=(10, 0))
        subtitle_lbl = tk.Label(card, text="Quick appearance and motion controls", font=("Segoe UI", 8))
        subtitle_lbl.pack(anchor="w", padx=12, pady=(2, 8))

        def _on_theme_switch(is_dark):
            desired = "dark" if is_dark else "light"
            if desired != _theme_state["current"]:
                _toggle_theme()
            _refresh_settings_panel()

        def _on_motion_switch(is_on):
            if LOW_ANIMATION_MODE.get("enabled") != is_on:
                LOW_ANIMATION_MODE["enabled"] = is_on
            _refresh_settings_panel()

        theme_row = tk.Frame(card, highlightthickness=1)
        theme_row.pack(fill="x", padx=12, pady=(0, 8))
        theme_text = tk.Frame(theme_row, bg=card_color)
        theme_text.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=8)
        theme_lbl = tk.Label(theme_text, text="Theme", bg=card_color, font=("Segoe UI", 10, "bold"))
        theme_lbl.pack(anchor="w")
        theme_hint = tk.Label(theme_text, text="Switch dark or light mode", bg=card_color, font=("Segoe UI", 8))
        theme_hint.pack(anchor="w")
        theme_value = tk.Label(theme_row, text="", width=5, anchor="e", bg=card_color, font=("Segoe UI", 9, "bold"))
        theme_value.pack(side="left", padx=(0, 10))
        theme_switch = _create_setting_switch(theme_row, _on_theme_switch)
        theme_switch.pack(side="right", padx=(0, 12), pady=8)

        motion_row = tk.Frame(card, highlightthickness=1)
        motion_row.pack(fill="x", padx=12, pady=(0, 10))
        motion_text = tk.Frame(motion_row, bg=card_color)
        motion_text.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=8)
        motion_lbl = tk.Label(motion_text, text="Low Motion", bg=card_color, font=("Segoe UI", 10, "bold"))
        motion_lbl.pack(anchor="w")
        motion_hint = tk.Label(motion_text, text="Reduce effects for max speed", bg=card_color, font=("Segoe UI", 8))
        motion_hint.pack(anchor="w")
        motion_value = tk.Label(motion_row, text="", width=5, anchor="e", bg=card_color, font=("Segoe UI", 9, "bold"))
        motion_value.pack(side="left", padx=(0, 10))
        motion_switch = _create_setting_switch(motion_row, _on_motion_switch)
        motion_switch.pack(side="right", padx=(0, 12), pady=8)

        win.bind("<Escape>", lambda _e: _close_settings_panel())
        win.protocol("WM_DELETE_WINDOW", _close_settings_panel)

        settings_state.update({
            "win": win,
            "card": card,
            "title": title_lbl,
            "subtitle": subtitle_lbl,
            "theme_row": theme_row,
            "theme_text": theme_text,
            "theme_label": theme_lbl,
            "theme_hint": theme_hint,
            "theme_value": theme_value,
            "theme_switch": theme_switch,
            "motion_row": motion_row,
            "motion_text": motion_text,
            "motion_label": motion_lbl,
            "motion_hint": motion_hint,
            "motion_value": motion_value,
            "motion_switch": motion_switch,
        })

        _refresh_settings_panel()

    def _toggle_settings_panel():
        win = settings_state.get("win")
        if win and win.winfo_exists():
            _close_settings_panel()
        else:
            _open_settings_panel()

    settings_btn = tk.Button(
        top_controls,
        text="\ue713",
        command=_toggle_settings_panel,
        bg=panel_color,
        fg=THEMES[_theme_state["current"]]["toggle_fg"],
        activebackground=panel_color,
        activeforeground=THEMES[_theme_state["current"]]["toggle_fg"],
        relief="flat",
        font=("Segoe MDL2 Assets", 16),
        padx=4,
        pady=0,
        cursor="hand2",
        bd=0,
        highlightthickness=0,
    )
    settings_btn.pack(side="right", padx=(0, 8), pady=2)

    def _settings_enter(_e):
        settings_btn.config(fg="#ffe066")

    def _settings_leave(_e):
        _refresh_settings_panel()

    settings_btn.bind("<Enter>", _settings_enter)
    settings_btn.bind("<Leave>", _settings_leave)

    _refresh_settings_panel()

    # --- Hover, press & pulse animations for login/logout RoundedButton ---
    _llb_state = {"logged_in": False}

    _llb_login_colors = {
        "bg": "#1D50AF", "grad_top": "#163f86", "grad_bottom": "#0d2a5b",
        "hover_bg": "#0b2556", "hover_top": "#184690", "hover_bot": "#0a1d41",
        "press_bg": "#255ec4", "press_top": "#2d6ad4", "press_bot": "#1c4ea8",
        "ring": "#3a5f8f", "glow_ring": "#6fb0ff",
        "pulse": ["#2d6cdf", "#3374e8", "#3a7af2", "#3374e8"],
        "idle_ring": ["#15437f", "#11488b", "#1c579a", "#14427b"],
    }
    _llb_logout_colors = {
        "bg": "#8C2323", "grad_top": "#921a1a", "grad_bottom": "#540A0A",
        "hover_bg": "#931515", "hover_top": "#931a1a", "hover_bot": "#570E0E",
        "press_bg": "#6e2424", "press_top": "#802c2c", "press_bot": "#5a1e1e",
        "ring": "#5a3030", "glow_ring": "#ff8f8f",
        "pulse": ["#8b2e2e", "#9d3636", "#a63a3a", "#9d3636"],
        "idle_ring": ["#581515", "#601919", "#6E2525", "#881f1f"],
    }

    def _sync_login_logout_palettes():
        t = THEMES[_theme_state["current"]]

        def _mix(c1, c2, ratio):
            c1 = c1.lstrip("#")
            c2 = c2.lstrip("#")
            r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
            r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            return "#{:02x}{:02x}{:02x}".format(r, g, b)

        _llb_login_colors.update({
            "bg": t["primary_btn"],
            "grad_top": t["start_grad_top"],
            "grad_bottom": t["start_grad_bottom"],
            "hover_bg": t["primary_hover"],
            "hover_top": _mix(t["start_grad_top"], "#ffffff", 0.10),
            "hover_bot": t["start_grad_bottom"],
            "press_bg": t["primary_press"],
            "press_top": _mix(t["start_grad_top"], "#000000", 0.08),
            "press_bot": _mix(t["start_grad_bottom"], "#000000", 0.10),
            "ring": t["focus_ring"],
            "glow_ring": _mix(t["primary_btn"], "#ffffff", 0.35),
            "pulse": t["pulse_cycle"],
            "idle_ring": t["idle_start"],
        })

        _llb_logout_colors.update({
            "bg": t["danger_btn"],
            "grad_top": t["stop_grad_top"],
            "grad_bottom": t["stop_grad_bottom"],
            "hover_bg": t["danger_hover"],
            "hover_top": _mix(t["stop_grad_top"], "#ffffff", 0.08),
            "hover_bot": t["stop_grad_bottom"],
            "press_bg": t["danger_press"],
            "press_top": _mix(t["stop_grad_top"], "#000000", 0.08),
            "press_bot": _mix(t["stop_grad_bottom"], "#000000", 0.10),
            "ring": _mix(t["danger_btn"], "#000000", 0.20),
            "glow_ring": _mix(t["danger_btn"], "#ffffff", 0.35),
            "pulse": [
                t["danger_btn"],
                t["danger_hover"],
                t["danger_btn"],
                _mix(t["danger_btn"], "#000000", 0.10),
            ],
            "idle_ring": t["idle_stop"],
        })

    def _llb_get_palette():
        return _llb_logout_colors if _llb_state["logged_in"] else _llb_login_colors

    _sync_login_logout_palettes()

    def _llb_enter(e):
        p = _llb_get_palette()
        login_logout_btn.config(bg=p["hover_bg"], grad_top=p["hover_top"], grad_bottom=p["hover_bot"],
                                highlightbackground=p["glow_ring"])

    def _llb_leave(e):
        p = _llb_get_palette()
        login_logout_btn.config(bg=p["bg"], grad_top=p["grad_top"], grad_bottom=p["grad_bottom"],
                                highlightbackground=p["ring"])

    def _llb_press(e):
        p = _llb_get_palette()
        login_logout_btn.config(bg=p["press_bg"], grad_top=p["press_top"], grad_bottom=p["press_bot"],
                                highlightbackground=p["glow_ring"])
        login_logout_btn.spawn_ripple(p["glow_ring"])

    def _llb_release(e):
        p = _llb_get_palette()
        login_logout_btn.config(bg=p["hover_bg"], grad_top=p["hover_top"], grad_bottom=p["hover_bot"])

    login_logout_btn.bind("<Enter>", _llb_enter, add="+")
    login_logout_btn.bind("<Leave>", _llb_leave, add="+")
    login_logout_btn.bind("<ButtonPress-1>", _llb_press, add="+")
    login_logout_btn.bind("<ButtonRelease-1>", _llb_release, add="+")

    # Idle pulse on login/logout button ring
    def _llb_idle_pulse(i=0):
        if not root.winfo_exists():
            return
        if not LOW_ANIMATION_MODE.get("enabled"):
            p = _llb_get_palette()
            ring_c = p["idle_ring"][i % len(p["idle_ring"])]
            login_logout_btn.config(highlightbackground=ring_c)
            i += 1
        root.after(_anim_delay(280, 1200), lambda: _llb_idle_pulse(i))
    _llb_idle_pulse()

    # Subtle bg pulse animation
    def _llb_bg_pulse(i=0):
        if not root.winfo_exists():
            return
        if not LOW_ANIMATION_MODE.get("enabled"):
            p = _llb_get_palette()
            c = p["pulse"][i % len(p["pulse"])]
            login_logout_btn.config(bg=c)
            i += 1
        root.after(_anim_delay(260, 1200), lambda: _llb_bg_pulse(i))
    _llb_bg_pulse()

    # Keep _llb_state in sync with session
    _orig_refresh = _refresh_session_ui
    def _refresh_session_ui_wrapper():
        _orig_refresh()
        _llb_state["logged_in"] = session_mgr.is_logged_in
    _refresh_session_ui = _refresh_session_ui_wrapper

    header_accent = tk.Canvas(header_frame, height=6, bg=panel_color, highlightthickness=0)
    header_accent.pack(fill="x", padx=120, pady=(2, 6))
    accent_line = header_accent.create_rectangle(0, 0, 120, 6, fill=accent, outline="")

    monitor_section = tk.Frame(
        content_frame,
        bg=card_color,
        highlightthickness=1,
        highlightbackground=card_border
    )
    organize_section = tk.Frame(
        content_frame,
        bg=card_color,
        highlightthickness=1,
        highlightbackground=card_border
    )
    gap_frame = tk.Frame(content_frame, bg=panel_color)

    monitor_title = tk.Label(
        monitor_section,
        text="MONITORING",
        font=("Segoe UI", section_title_base, "bold"),
        fg=accent_2,
        bg=card_color
    )
    monitor_title.pack(anchor="w", padx=16, pady=(14, 6))

    monitor_body = tk.Label(
        monitor_section,
        text=(
            "- Start and stop folder monitoring\n"
            "- Get instant notifications on changes\n"
            "- Review activity in the live log"
        ),
        justify="left",
        font=("Segoe UI", body_base),
        fg="#d4e4ff",
        bg=card_color
    )
    monitor_body.pack(anchor="w", padx=16, pady=(0, 10))

    monitor_selected = {"path": ""}
    monitor_value = tk.Label(
        monitor_section,
        text="No folder selected",
        justify="left",
        font=("Segoe UI", body_base),
        fg="#c6d7f5",
        bg=card_color
    )
    monitor_value.pack(anchor="w", padx=16, pady=(2, 2))

    monitor_warn = tk.Label(
        monitor_section,
        text="",
        justify="left",
        font=("Segoe UI", 8, "italic"),
        fg="#f1b0b0",
        bg=card_color
    )
    monitor_warn.pack(anchor="w", padx=16, pady=(0, 6))

    organize_title = tk.Label(
        organize_section,
        text="ORGANIZE & CLEANUP",
        font=("Segoe UI", section_title_base, "bold"),
        fg=accent_2,
        bg=card_color
    )
    organize_title.pack(anchor="w", padx=16, pady=(14, 6))

    organize_body = tk.Label(
        organize_section,
        text=(
            "• Organize files by extension\n"
            "• Find and remove duplicate files\n"
            "• Free up disk space"
        ),
        justify="left",
        font=("Segoe UI", body_base),
        fg="#d4e4ff",
        bg=card_color
    )
    organize_body.pack(anchor="w", padx=16, pady=(0, 10))

    # Subtle title glow animation
    glow_colors = ["#eaf2ff", "#cfe8ff", "#b9d8ff", "#cfe8ff"]
    def animate_title_glow(i=0):
        if not LOW_ANIMATION_MODE.get("enabled"):
            title.config(fg=glow_colors[i % len(glow_colors)])
            i += 1
        root.after(_anim_delay(180, 1000), lambda: animate_title_glow(i))

    def animate_header_accent():
        w = max(1, header_accent.winfo_width())
        if not hasattr(animate_header_accent, "x"):
            animate_header_accent.x = 0
            animate_header_accent.dx = 2.4
        if not LOW_ANIMATION_MODE.get("enabled"):
            animate_header_accent.x += animate_header_accent.dx
            if animate_header_accent.x > w or animate_header_accent.x < -120:
                animate_header_accent.dx *= -1
            header_accent.coords(accent_line, animate_header_accent.x, 0, animate_header_accent.x + 120, 6)
        root.after(_anim_delay(30, 700), animate_header_accent)

    monitor_btn_row = tk.Frame(monitor_section, bg=card_color)
    monitor_btn_row.pack(fill="x", padx=16, pady=(4, 8))
    monitor_btn_row.grid_columnconfigure(0, weight=1)
    monitor_btn_row.grid_columnconfigure(1, weight=1)

    def draw_group_outline(canvas_widget, radius=16):
        canvas_widget.delete("all")
        w = max(1, canvas_widget.winfo_width())
        h = max(1, canvas_widget.winfo_height())
        r = min(radius, int(min(w, h) / 2))
        points = [
            r, 0,
            w - r, 0,
            w, 0,
            w, r,
            w, h - r,
            w, h,
            w - r, h,
            r, h,
            0, h,
            0, h - r,
            0, r,
            0, 0
        ]
        canvas_widget.create_polygon(
            points,
            smooth=True,
            outline=card_border,
            width=2,
            fill=card_color
        )

    def attach_group_outline(container):
        outline = tk.Canvas(container, bg=card_color, highlightthickness=0)
        outline.place(x=0, y=0, relwidth=1, relheight=1)
        outline.tk.call("lower", outline._w)

        def _redraw(_event=None):
            draw_group_outline(outline)

        container.bind("<Configure>", _redraw)
        return outline

    start_wrap = tk.Frame(monitor_btn_row, bg=card_color)
    start_wrap.grid(row=0, column=0, padx=(0, 6), pady=4, sticky="ew")
    start_wrap.grid_columnconfigure(0, weight=1)
    start_wrap.grid_columnconfigure(1, weight=0)
    attach_group_outline(start_wrap)

    def _guarded_start_monitoring():
        """Start monitoring with free-trial / login gating + folder safety."""
        # ---- Gate check FIRST (before folder dialog) ----
        if not session_mgr.is_logged_in:
            remaining = session_mgr.free_trial_remaining()
            if remaining <= 0:
                messagebox.showwarning(
                    "Free Trial Exhausted",
                    "You have used all free monitoring attempts.\n"
                    "Please log in or create an account to continue."
                )
                open_login_window(root, _refresh_session_ui)
                return
        # ---- Now allow folder selection ----
        if not monitor_selected["path"]:
            choose_monitor_folder()
        path = monitor_selected["path"]
        if not path:
            return
        # Folder safety check
        if not is_folder_safe(path):
            messagebox.showerror("Blocked", get_block_reason(path))
            return
        # Consume free trial for non-logged-in user
        if not session_mgr.is_logged_in:
            allowed, msg = session_mgr.try_free_monitoring()
            if not allowed:
                messagebox.showwarning("Free Trial Exhausted", msg)
                open_login_window(root, _refresh_session_ui)
                return
            messagebox.showinfo("Free Trial", msg)
            _refresh_session_ui()
        else:
            # Save monitored folder for logged-in user
            session_mgr.save_setting("monitored_folder", path)
        session_mgr.audit("start_monitoring", f"Monitoring started: {path}")
        start_monitoring(path, root, start_btn, stop_btn)

    start_btn = RoundedButton(
        start_wrap,
        text="Start Monitoring",
        bg=primary_btn,
        grad_top="#3067c6",
        grad_bottom="#174188",
        fg=text_color,
        highlightbackground=focus_ring,
        height=40,
        radius=20,
        command=_guarded_start_monitoring
    )
    start_btn.grid(row=0, column=0, columnspan=2, padx=(2, 2), pady=2, sticky="ew")

    stop_btn = RoundedButton(
        monitor_btn_row,
        text="Stop Monitoring",
        bg=danger_btn,
        grad_top="#8F1111",
        grad_bottom="#690707",
        fg=text_color,
        highlightbackground=focus_ring,
        state="disabled",
        height=40,
        radius=20,
        command=lambda: stop_monitoring(root, start_btn, stop_btn)
    )
    stop_btn.grid(row=0, column=1, padx=(6, 0), pady=4, sticky="ew")

    monitor_clear_btn = tk.Label(
        start_wrap,
        text="✕",
        bg=card_color,
        fg="#b0c4de",
        font=("Segoe UI", 12, "bold"),
        cursor="hand2",
        width=2,
        height=1
    )
    monitor_clear_btn.place(relx=1.0, x=-12, rely=0.5, anchor="e")
    monitor_clear_btn.place_forget()
    
    # Add hover effect
    def on_monitor_x_enter(e):
        monitor_clear_btn.config(fg="#ff6b6b")
    def on_monitor_x_leave(e):
        monitor_clear_btn.config(fg=THEMES[_theme_state["current"]]["clear_btn_fg"])
    monitor_clear_btn.bind("<Enter>", on_monitor_x_enter)
    monitor_clear_btn.bind("<Leave>", on_monitor_x_leave)
    
    start_btn.tk.call("raise", start_btn._w)
    monitor_clear_btn.tk.call("raise", monitor_clear_btn._w)

    def _guarded_open_live_log():
        """Open Live Activity Log only if logged in or free trial remaining."""
        if not session_mgr.is_logged_in:
            remaining = session_mgr.free_trial_remaining()
            if remaining <= 0:
                messagebox.showwarning(
                    "Login Required",
                    "Free trial exhausted. Please log in to access the Live Activity Log."
                )
                open_login_window(root, _refresh_session_ui)
                return
        open_live_log()

    log_btn = RoundedButton(
        monitor_section,
        text="Open Live Activity Log",
        bg=ghost_btn,
        grad_top="#1f426e",
        grad_bottom="#182a45",
        fg=text_color,
        highlightbackground=focus_ring,
        height=40,
        radius=20,
        command=_guarded_open_live_log
    )
    log_btn.pack(fill="x", padx=16, pady=(2, 12))

    selected_folder = {"path": ""}
    folder_value = tk.Label(
        organize_section,
        text="No folder selected",
        justify="left",
        font=("Segoe UI", 7),
        fg="#c6d7f5",
        bg=card_color
    )
    folder_value.pack(anchor="w", padx=16, pady=(2, 2))

    folder_warn = tk.Label(
        organize_section,
        text="",
        justify="left",
        font=("Segoe UI", 8, "italic"),
        fg="#f1b0b0",
        bg=card_color
    )
    folder_warn.pack(anchor="w", padx=16, pady=(0, 6))

    select_row = tk.Frame(organize_section, bg=card_color)
    select_row.pack(fill="x", padx=16, pady=6)
    select_row.grid_columnconfigure(0, weight=1)
    select_row.grid_columnconfigure(1, weight=0)
    attach_group_outline(select_row)

    org_row = tk.Frame(organize_section, bg=card_color)
    org_row.pack(fill="x", padx=16, pady=(2, 12))
    org_row.grid_columnconfigure(0, weight=1)

    folder_clear_btn = tk.Label(
        select_row,
        text="✕",
        bg=card_color,
        fg="#b0c4de",
        font=("Segoe UI", 12, "bold"),
        cursor="hand2",
        width=2,
        height=1
    )
    folder_clear_btn.place(relx=1.0, x=-12, rely=0.5, anchor="e")
    folder_clear_btn.place_forget()
    
    # Add hover effect
    def on_folder_x_enter(e):
        folder_clear_btn.config(fg="#ff6b6b")
    def on_folder_x_leave(e):
        folder_clear_btn.config(fg=THEMES[_theme_state["current"]]["clear_btn_fg"])
    folder_clear_btn.bind("<Enter>", on_folder_x_enter)
    folder_clear_btn.bind("<Leave>", on_folder_x_leave)

    warn_timers = {"monitor": None, "organize": None}

    def show_warning(label, text, key):
        if warn_timers[key] is not None:
            try:
                label.after_cancel(warn_timers[key])
            except Exception:
                pass
        label.config(text=text)
        warn_timers[key] = label.after(2800, lambda: label.config(text=""))

    def set_monitor_path(path):
        if path and os.path.isdir(path):
            monitor_selected["path"] = path
            monitor_value.config(text=path)
            monitor_warn.config(text="")
            monitor_clear_btn.place(relx=1.0, x=-12, rely=0.5, anchor="e")
            monitor_clear_btn.tk.call("raise", monitor_clear_btn._w)
        else:
            monitor_selected["path"] = ""
            monitor_value.config(text="No folder selected")
            show_warning(monitor_warn, "Invalid folder selection", "monitor")
            monitor_clear_btn.config(state="disabled")
            monitor_clear_btn.place_forget()

    def clear_monitor_path():
        monitor_selected["path"] = ""
        monitor_value.config(text="No folder selected")
        monitor_warn.config(text="")
        monitor_clear_btn.place_forget()

    def reset_monitor_section():
        if monitoring:
            stop_monitoring(root, start_btn, stop_btn)
        if live_log_window and tk.Toplevel.winfo_exists(live_log_window):
            live_log_window.destroy()
        clear_monitor_path()

    monitor_clear_btn.bind("<Button-1>", lambda e: reset_monitor_section())

    def choose_monitor_folder():
        # Block folder selection when trial exhausted and not logged in
        if not session_mgr.is_logged_in and session_mgr.free_trial_remaining() <= 0:
            messagebox.showwarning(
                "Login Required",
                "Free trial exhausted. Please log in to select a monitoring folder."
            )
            open_login_window(root, _refresh_session_ui)
            return
        path = filedialog.askdirectory(title="Select Folder to Monitor")
        if path:
            if not is_folder_safe(path):
                messagebox.showerror("Blocked", get_block_reason(path))
                return
            set_monitor_path(path)

    def set_organize_path(path):
        if path and os.path.isdir(path):
            selected_folder["path"] = path
            folder_value.config(text=path)
            folder_warn.config(text="")
            folder_clear_btn.place(relx=1.0, x=-12, rely=0.5, anchor="e")
            folder_clear_btn.tk.call("raise", folder_clear_btn._w)
            # Only enable if logged in
            if session_mgr.is_logged_in:
                org_btn.config(state="normal")
                dup_select_btn.config(state="normal")
            # Persist for logged-in user
            if session_mgr.is_logged_in:
                session_mgr.save_setting("organize_folder", path)
        else:
            selected_folder["path"] = ""
            folder_value.config(text="No folder selected")
            show_warning(folder_warn, "Invalid folder selection", "organize")
            folder_clear_btn.config(state="disabled")
            folder_clear_btn.place_forget()
            org_btn.config(state="disabled")
            dup_select_btn.config(state="disabled")

    def clear_organize_path():
        selected_folder["path"] = ""
        folder_value.config(text="No folder selected")
        folder_warn.config(text="")
        folder_clear_btn.place_forget()
        org_btn.config(state="disabled")
        dup_select_btn.config(state="disabled")

    def reset_organize_section():
        clear_organize_path()

    folder_clear_btn.bind("<Button-1>", lambda e: reset_organize_section())

    def choose_organize_folder():
        if not session_mgr.is_logged_in:
            messagebox.showwarning("Login Required", "Please log in to use the Organize feature.")
            open_login_window(root, _refresh_session_ui)
            return
        path = filedialog.askdirectory(title="Select Folder to Organize")
        if path:
            if not is_folder_safe(path):
                messagebox.showerror("Blocked", get_block_reason(path))
                return
            set_organize_path(path)

    def run_organize_folder():
        if not session_mgr.is_logged_in:
            messagebox.showwarning("Login Required", "Please log in to organize folders.")
            open_login_window(root, _refresh_session_ui)
            return
        if not selected_folder["path"]:
            return
        path = selected_folder["path"]
        if not is_folder_safe(path):
            messagebox.showerror("Blocked", get_block_reason(path))
            return
        if not confirm_destructive_action("Organize Folder", f"Files in:\n{path}\nwill be sorted into sub-folders by extension."):
            return
        session_mgr.audit("organize", f"Organized folder: {path}")
        organize_folder(path)
        clear_organize_path()

    select_btn = RoundedButton(
        select_row,
        text="Select Folder",
        bg=ghost_btn,
        grad_top="#214a75",
        grad_bottom="#182a45",
        fg=text_color,
        highlightbackground=focus_ring,
        radius=20,
        command=choose_organize_folder
    )
    select_btn.grid(row=0, column=0, columnspan=2, padx=(2, 2), pady=2, sticky="ew")
    select_btn.tk.call("raise", select_btn._w)
    folder_clear_btn.tk.call("raise", folder_clear_btn._w)

    org_btn = RoundedButton(
        org_row,
        text="Organize Folder",
        bg=primary_btn,
        grad_top="#2657a6",
        grad_bottom="#133b81",
        fg=text_color,
        highlightbackground=focus_ring,
        state="disabled",
        height=40,
        radius=20,
        command=run_organize_folder
    )
    org_btn.grid(row=0, column=0, sticky="ew")

    # Separator line
    separator = tk.Frame(organize_section, bg="#203d63", height=1)
    separator.pack(fill="x", padx=16, pady=(8, 12))

    # Duplicate Finder Section (within organize_section)
    dup_header = tk.Label(
        organize_section,
        text="Find Duplicates",
        font=("Segoe UI", 10, "bold"),
        fg="#9fb4d1",
        bg=card_color
    )
    dup_header.pack(anchor="w", padx=16, pady=(4, 6))

    duplicate_selected = {"path": ""}

    dup_select_row = tk.Frame(organize_section, bg=card_color)
    dup_select_row.pack(fill="x", padx=16, pady=6)
    dup_select_row.grid_columnconfigure(0, weight=1)
    dup_select_row.grid_columnconfigure(1, weight=0)
    attach_group_outline(dup_select_row)

    dup_scan_row = tk.Frame(organize_section, bg=card_color)
    dup_scan_row.pack(fill="x", padx=16, pady=(2, 12))
    dup_scan_row.grid_columnconfigure(0, weight=1)

    def run_duplicate_scan():
        if not session_mgr.is_logged_in:
            messagebox.showwarning("Login Required", "Please log in to scan for duplicates.")
            open_login_window(root, _refresh_session_ui)
            return
        path = selected_folder["path"]
        if not path:
            show_warning(folder_warn, "Please select a folder first", "organize")
            return
        if not is_folder_safe(path):
            messagebox.showerror("Blocked", get_block_reason(path))
            return
        session_mgr.audit("duplicate_scan", f"Scanning for duplicates: {path}")
        scan_for_duplicates(path)

    dup_select_btn = RoundedButton(
        dup_scan_row,
        text="Scan for Duplicates",
        bg="#1c608a",
        grad_top="#154757",
        grad_bottom="#2D1562",
        fg=text_color,
        highlightbackground=focus_ring,
        state="disabled",
        height=40,
        radius=20,
        command=run_duplicate_scan
    )
    dup_select_btn.grid(row=0, column=0, sticky="ew")

    # Button visual + animation helpers
    def _hex_to_rgb(value):
        value = value.lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    def _rgb_to_hex(rgb):
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    def _blend(c1, c2, t):
        r1, g1, b1 = _hex_to_rgb(c1)
        r2, g2, b2 = _hex_to_rgb(c2)
        return _rgb_to_hex((
            int(r1 + (r2 - r1) * t),
            int(g1 + (g2 - g1) * t),
            int(b1 + (b2 - b1) * t),
        ))

    def spawn_ripple(btn, color):
        if hasattr(btn, "spawn_ripple"):
            btn.spawn_ripple(color)
            return
        if btn["state"] == "disabled":
            return
        if LOW_ANIMATION_MODE.get("enabled"):
            return
        btn.update_idletasks()
        parent = btn.master
        try:
            parent_bg = parent.cget("bg")
        except Exception:
            parent_bg = bg_color
        w = max(1, btn.winfo_width())
        h = max(1, btn.winfo_height())
        x = btn.winfo_x()
        y = btn.winfo_y()
        ripple = tk.Canvas(parent, width=w, height=h, bg=parent_bg, highlightthickness=0)
        ripple.place(x=x, y=y)
        btn.tkraise()

        max_r = int((w * w + h * h) ** 0.5)
        steps = 12

        def animate(i=0):
            ripple.delete("ring")
            r = int((i / steps) * max_r)
            ripple.create_oval(
                w // 2 - r,
                h // 2 - r,
                w // 2 + r,
                h // 2 + r,
                outline=_blend(color, parent_bg, i / steps),
                width=2,
                tags="ring"
            )
            if i < steps:
                ripple.after(_anim_delay(16, 100), lambda: animate(i + 1))
            else:
                ripple.destroy()

        animate()

    def bind_button_fx(btn, base, hover, press, glow, idle_colors, idle_speed=320):
        palette = {
            "base": base,
            "hover": hover,
            "press": press,
            "glow": glow,
            "idle_colors": list(idle_colors),
        }
        state = {"step": 0, "target": 0, "running": False, "hover": False, "pressed": False}

        def animate():
            if LOW_ANIMATION_MODE.get("enabled"):
                t = 1.0 if state["target"] > state["step"] else 0.0
                btn.config(
                    bg=_blend(palette["base"], palette["hover"], t),
                    highlightbackground=_blend(focus_ring, palette["glow"], t)
                )
                state["step"] = state["target"]
                state["running"] = False
                return
            if state["step"] == state["target"]:
                state["running"] = False
                return
            state["step"] += 1 if state["target"] > state["step"] else -1
            t = state["step"] / 12.0
            btn.config(
                bg=_blend(palette["base"], palette["hover"], t),
                highlightbackground=_blend(focus_ring, palette["glow"], t)
            )
            btn.after(_anim_delay(16, 100), animate)

        def set_target(target):
            state["target"] = target
            if not state["running"]:
                state["running"] = True
                animate()

        def on_enter(_):
            if btn["state"] == "disabled":
                return
            state["hover"] = True
            set_target(12)

        def on_leave(_):
            state["hover"] = False
            btn.config(bg=palette["base"], highlightbackground=focus_ring)
            state["step"] = 0
            state["target"] = 0

        def on_press(_):
            if btn["state"] == "disabled":
                return
            state["pressed"] = True
            btn.config(bg=palette["press"], highlightbackground=palette["glow"])
            spawn_ripple(btn, palette["glow"])

        def on_release(_):
            state["pressed"] = False
            if btn.winfo_containing(btn.winfo_pointerx(), btn.winfo_pointery()) == btn:
                set_target(12)
            else:
                on_leave(None)

        btn.bind("<Enter>", on_enter, add="+")
        btn.bind("<Leave>", on_leave, add="+")
        btn.bind("<ButtonPress-1>", on_press, add="+")
        btn.bind("<ButtonRelease-1>", on_release, add="+")

        def idle_glow(i=0):
            if not LOW_ANIMATION_MODE.get("enabled") and not state["hover"] and not state["pressed"] and btn["state"] != "disabled":
                colors = palette["idle_colors"]
                if colors:
                    btn.config(highlightbackground=colors[i % len(colors)])
            btn.after(_anim_delay(idle_speed, 1200), lambda: idle_glow(i + 1))

        idle_glow()

        def update_palette(base=None, hover=None, press=None, glow=None, idle_colors=None):
            if base is not None:
                palette["base"] = base
            if hover is not None:
                palette["hover"] = hover
            if press is not None:
                palette["press"] = press
            if glow is not None:
                palette["glow"] = glow
            if idle_colors:
                palette["idle_colors"] = list(idle_colors)

            if not state["hover"] and not state["pressed"] and btn["state"] != "disabled":
                btn.config(bg=palette["base"], highlightbackground=focus_ring)

        return update_palette

    start_btn_fx = bind_button_fx(
        start_btn,
        primary_btn,
        primary_hover,
        primary_press,
        "#6fb0ff",
        ["#4a6fa1", "#5b83b8", "#4a6fa1"],
        260
    )
    stop_btn_fx = bind_button_fx(
        stop_btn,
        danger_btn,
        danger_hover,
        danger_press,
        "#ff8f8f",
        ["#8b3d3d", "#a34b4b", "#8b3d3d"],
        280
    )
    log_btn_fx = bind_button_fx(
        log_btn,
        ghost_btn,
        ghost_hover,
        ghost_press,
        "#4a6fa1",
        ["#2d486e", "#3b5e89", "#2d486e"],
        300
    )
    select_btn_fx = bind_button_fx(
        select_btn,
        ghost_btn,
        ghost_hover,
        ghost_press,
        "#4a6fa1",
        ["#2b4c66", "#3c6a8c", "#2b4c66"],
        320
    )
    org_btn_fx = bind_button_fx(
        org_btn,
        primary_btn,
        primary_hover,
        primary_press,
        "#6fb0ff",
        ["#3c63a6", "#4b78bf", "#3c63a6"],
        260
    )
    dup_select_btn_fx = bind_button_fx(
        dup_select_btn,
        "#1c8a7a",
        "#22a08e",
        "#157265",
        "#4dd6c4",
        ["#2a6d64", "#358a7f", "#2a6d64"],
        300
    )

    content_frame.grid_rowconfigure(0, weight=1)
    content_frame.grid_columnconfigure(0, weight=4)
    content_frame.grid_columnconfigure(1, weight=0)
    content_frame.grid_columnconfigure(2, weight=0)
    monitor_section.grid(row=0, column=0, sticky="nsew")
    gap_frame.grid(row=0, column=1, sticky="ns")
    organize_section.grid(row=0, column=2, sticky="nsew")

    # ==========================================================
    # Theme System — apply_theme() & _toggle_theme()
    # ==========================================================
    def apply_theme(theme_name):
        """Switch the entire UI between 'dark' and 'light' themes."""
        nonlocal bg_color, panel_color, card_color, card_border, card_border_pulse
        nonlocal accent, accent_2, text_color
        nonlocal primary_btn, primary_hover, primary_press
        nonlocal danger_btn, danger_hover, danger_press
        nonlocal ghost_btn, ghost_hover, ghost_press, focus_ring

        t = THEMES[theme_name]
        _theme_state["current"] = theme_name

        # Update closure-captured colour variables
        bg_color = t["bg_color"]
        panel_color = t["panel_color"]
        card_color = t["card_color"]
        card_border = t["card_border"]
        card_border_pulse = t["card_border_pulse"]
        accent = t["accent"]
        accent_2 = t["accent_2"]
        text_color = t["text_color"]
        primary_btn = t["primary_btn"]
        primary_hover = t["primary_hover"]
        primary_press = t["primary_press"]
        danger_btn = t["danger_btn"]
        danger_hover = t["danger_hover"]
        danger_press = t["danger_press"]
        ghost_btn = t["ghost_btn"]
        ghost_hover = t["ghost_hover"]
        ghost_press = t["ghost_press"]
        focus_ring = t["focus_ring"]

        # ---- Root + canvas ----
        root.configure(bg=bg_color)
        canvas.configure(bg=bg_color)

        # ---- Panel + major frames ----
        panel.configure(bg=panel_color)
        header_frame.configure(bg=panel_color)
        content_frame.configure(bg=panel_color)
        gap_frame.configure(bg=panel_color)
        top_controls.configure(bg=panel_color)
        subtitle_row.configure(bg=panel_color)

        # ---- Session bar ----
        session_bar.configure(bg=panel_color)
        session_left.configure(bg=panel_color)
        session_icon.configure(bg=panel_color)
        session_label.configure(bg=panel_color)

        # ---- Title + subtitle ----
        title.configure(fg=t["text_color"], bg=panel_color)
        subtitle.configure(fg=t["subtitle_fg"], bg=panel_color)

        # ---- Header accent canvas ----
        header_accent.configure(bg=panel_color)

        # ---- Card sections ----
        monitor_section.configure(bg=card_color, highlightbackground=card_border)
        organize_section.configure(bg=card_color, highlightbackground=card_border)

        # ---- Monitor section widgets ----
        monitor_title.configure(fg=accent_2, bg=card_color)
        monitor_body.configure(fg=t["body_fg"], bg=card_color)
        monitor_value.configure(fg=t["value_fg"], bg=card_color)
        monitor_warn.configure(fg=t["warn_fg"], bg=card_color)
        monitor_btn_row.configure(bg=card_color)
        start_wrap.configure(bg=card_color)
        monitor_clear_btn.configure(bg=card_color, fg=t["clear_btn_fg"])

        # ---- Organize section widgets ----
        organize_title.configure(fg=accent_2, bg=card_color)
        organize_body.configure(fg=t["body_fg"], bg=card_color)
        folder_value.configure(fg=t["value_fg"], bg=card_color)
        folder_warn.configure(fg=t["warn_fg"], bg=card_color)
        select_row.configure(bg=card_color)
        org_row.configure(bg=card_color)
        separator.configure(bg=t["separator_color"])
        dup_header.configure(fg=t["dup_header_fg"], bg=card_color)
        dup_select_row.configure(bg=card_color)
        dup_scan_row.configure(bg=card_color)
        folder_clear_btn.configure(bg=card_color, fg=t["clear_btn_fg"])

        # ---- RoundedButtons — update colours (canvas bg synced automatically in _redraw) ----
        for btn in (start_btn, stop_btn, log_btn, select_btn, org_btn, dup_select_btn):
            btn.disabled_bg = t["disabled_bg"]
            btn.disabled_fg = t["disabled_fg"]

        # Button text: white on colored buttons, dark on ghost buttons in light mode
        _btn_text = "#ffffff"  # always white on primary/danger/teal buttons
        start_btn.config(
            bg=t["primary_btn"], grad_top=t["start_grad_top"],
            grad_bottom=t["start_grad_bottom"], fg=_btn_text,
            highlightbackground=t["focus_ring"],
        )
        stop_btn.config(
            bg=t["danger_btn"], grad_top=t["stop_grad_top"],
            grad_bottom=t["stop_grad_bottom"], fg=_btn_text,
            highlightbackground=t["focus_ring"],
        )
        log_btn.config(
            bg=t["ghost_btn"], grad_top=t["log_grad_top"],
            grad_bottom=t["log_grad_bottom"], fg=t["ghost_fg"],
            highlightbackground=t["focus_ring"],
        )
        select_btn.config(
            bg=t["ghost_btn"], grad_top=t["select_grad_top"],
            grad_bottom=t["select_grad_bottom"], fg=t["ghost_fg"],
            highlightbackground=t["focus_ring"],
        )
        org_btn.config(
            bg=t["primary_btn"], grad_top=t["org_grad_top"],
            grad_bottom=t["org_grad_bottom"], fg=_btn_text,
            highlightbackground=t["focus_ring"],
        )
        dup_select_btn.config(
            bg=t["dup_btn_bg"], grad_top=t["dup_grad_top"],
            grad_bottom=t["dup_grad_bottom"], fg=_btn_text,
            highlightbackground=t["focus_ring"],
        )

        ghost_glow = _blend(t["focus_ring"], "#ffffff", 0.25)
        start_btn_fx(
            base=t["primary_btn"],
            hover=t["primary_hover"],
            press=t["primary_press"],
            glow=_blend(t["primary_btn"], "#ffffff", 0.35),
            idle_colors=t["idle_start"],
        )
        stop_btn_fx(
            base=t["danger_btn"],
            hover=t["danger_hover"],
            press=t["danger_press"],
            glow=_blend(t["danger_btn"], "#ffffff", 0.35),
            idle_colors=t["idle_stop"],
        )
        log_btn_fx(
            base=t["ghost_btn"],
            hover=t["ghost_hover"],
            press=t["ghost_press"],
            glow=ghost_glow,
            idle_colors=t["idle_log"],
        )
        select_btn_fx(
            base=t["ghost_btn"],
            hover=t["ghost_hover"],
            press=t["ghost_press"],
            glow=ghost_glow,
            idle_colors=t["idle_select"],
        )
        org_btn_fx(
            base=t["primary_btn"],
            hover=t["primary_hover"],
            press=t["primary_press"],
            glow=_blend(t["primary_btn"], "#ffffff", 0.35),
            idle_colors=t["idle_org"],
        )
        dup_select_btn_fx(
            base=t["dup_btn_bg"],
            hover=_blend(t["dup_btn_bg"], "#ffffff", 0.12 if theme_name == "dark" else 0.08),
            press=_blend(t["dup_btn_bg"], "#000000", 0.16),
            glow=_blend(t["dup_btn_bg"], "#ffffff", 0.34),
            idle_colors=t["idle_dup"],
        )

        _sync_login_logout_palettes()

        # Force redraw all RoundedButtons so canvas bg syncs with parent
        for btn in (start_btn, stop_btn, log_btn, select_btn, org_btn, dup_select_btn):
            btn._redraw()

        # ---- Login / logout button ----
        login_logout_btn.configure(fg="#ffffff")
        login_logout_btn._redraw()

        # ---- Settings controls ----
        _refresh_settings_panel()

        # ---- Update animation colour lists in-place ----
        glow_colors[:] = t["glow_colors"]
        _session_glow_colors[:] = t["session_glow_colors"]
        _session_logged_out_colors[:] = t["session_logged_out_colors"]
        border_cycle[:] = t["border_cycle"]
        pulse_cycle[:] = t["pulse_cycle"]
        pulse_cycle_org[:] = t["pulse_cycle_org"]
        pulse_cycle_dup[:] = t["pulse_cycle_dup"]

        # ---- Trigger group outline redraws ----
        for w in (start_wrap, select_row, dup_select_row):
            try:
                w.event_generate("<Configure>")
            except Exception:
                pass

        # ---- Refresh session UI ----
        _refresh_session_ui()

    def _toggle_theme():
        new_theme = "light" if _theme_state["current"] == "dark" else "dark"
        if session_mgr.is_logged_in:
            session_mgr.save_setting("theme", new_theme)
        apply_theme(new_theme)

    # Panel entrance animation
    panel_state = {"y": -200, "target": 0}
    _layout_resize_job = None
    _last_layout_size = (0, 0)

    def layout_panel():
        w = max(1, root.winfo_width())
        h = max(1, root.winfo_height())
        panel_w = min(980, int(w * 0.82))
        panel_h = min(680, int(h * 0.74))
        target_y = max(40, int(h * 0.5))
        canvas.itemconfig(panel_window, width=panel_w, height=panel_h)
        panel_state["target"] = target_y
        canvas.coords(panel_window, int(w * 0.5), int(panel_state["y"]))
        canvas.lift(panel_window)

        # Scale content based on window size
        scale = min(w / 900.0, h / 600.0)
        scale = max(0.9, min(1.6, scale))
        title.config(font=("Segoe UI", int(title_base * scale), "bold"))
        subtitle.config(font=("Segoe UI", max(8, int(subtitle_base * scale))))
        monitor_title.config(font=("Segoe UI", max(9, int(section_title_base * scale)), "bold"))
        organize_title.config(font=("Segoe UI", max(9, int(section_title_base * scale)), "bold"))
        monitor_body.config(font=("Segoe UI", max(8, int(body_base * scale))))
        organize_body.config(font=("Segoe UI", max(8, int(body_base * scale))))
        folder_value.config(font=("Segoe UI", max(6, int(7 * scale))))
        dup_header.config(font=("Segoe UI", max(9, int(10 * scale)), "bold"))

        header_pad = int(12 * scale)
        header_h = int(95 * scale)
        header_frame.place(x=0, y=header_pad, width=panel_w, height=header_h)

        content_pad = int(18 * scale)
        content_y = header_pad + header_h
        content_h = max(380, panel_h - content_y - content_pad)
        content_w = max(420, panel_w - (content_pad * 2))
        content_frame.place(x=content_pad, y=content_y, width=content_w, height=content_h)
        content_frame.grid_propagate(False)

        gap = max(18, int(18 * scale))
        content_frame.grid_columnconfigure(1, minsize=gap)
        gap_frame.config(width=gap)

        left_w = int((content_w - gap) * 0.45)
        right_w = content_w - gap - left_w
        monitor_body.config(wraplength=max(180, int(left_w * 0.9)))
        organize_body.config(wraplength=max(160, int(right_w * 0.9)))
        folder_value.config(wraplength=max(160, int(right_w * 0.9)))
        content_frame.update_idletasks()

    def animate_panel_in():
        if panel_state["y"] < panel_state["target"]:
            panel_state["y"] += max(6, int((panel_state["target"] - panel_state["y"]) * 0.15))
            layout_panel()
            root.after(16, animate_panel_in)
        else:
            panel_state["y"] = panel_state["target"]
            layout_panel()

    border_cycle = [card_border, "#254a76", card_border_pulse, "#254a76"]
    def animate_card_border(i=0):
        if not LOW_ANIMATION_MODE.get("enabled"):
            organize_section.config(highlightbackground=border_cycle[i % len(border_cycle)])
            monitor_section.config(highlightbackground=border_cycle[(i + 2) % len(border_cycle)])
            i += 1
        root.after(_anim_delay(180, 1200), lambda: animate_card_border(i))

    pulse_cycle = [primary_btn, "#3374e8", primary_btn, "#2b63cc"]
    def animate_start_pulse(i=0):
        if not LOW_ANIMATION_MODE.get("enabled") and start_btn["state"] != "disabled":
            start_btn.config(bg=pulse_cycle[i % len(pulse_cycle)])
            i += 1
        root.after(_anim_delay(240, 1100), lambda: animate_start_pulse(i))

    pulse_cycle_org = [primary_btn, "#3b79ec", primary_btn, "#2a60c3"]
    def animate_org_pulse(i=0):
        if not LOW_ANIMATION_MODE.get("enabled") and org_btn["state"] != "disabled":
            org_btn.config(bg=pulse_cycle_org[i % len(pulse_cycle_org)])
            i += 1
        root.after(_anim_delay(260, 1100), lambda: animate_org_pulse(i))

    pulse_cycle_dup = ["#1c8a7a", "#1f9d8b", "#1c8a7a", "#197769"]
    def animate_dup_pulse(i=0):
        if not LOW_ANIMATION_MODE.get("enabled") and dup_select_btn["state"] != "disabled":
            dup_select_btn.config(bg=pulse_cycle_dup[i % len(pulse_cycle_dup)])
            i += 1
        root.after(_anim_delay(280, 1100), lambda: animate_dup_pulse(i))

    for b in (start_btn, stop_btn, log_btn, select_btn, org_btn, dup_select_btn):
        b.config(font=("Gabriola", 20, "bold"))

    def _run_debounced_layout():
        nonlocal _layout_resize_job, _last_layout_size
        _layout_resize_job = None
        size = (root.winfo_width(), root.winfo_height())
        if size == _last_layout_size:
            return
        _last_layout_size = size
        layout_panel()

    def on_resize(_event):
        nonlocal _layout_resize_job
        if _layout_resize_job is not None:
            root.after_cancel(_layout_resize_job)
        _layout_resize_job = root.after(LAYOUT_RESIZE_DEBOUNCE_MS, _run_debounced_layout)

    root.bind("<Configure>", on_resize)

    animate_title_glow()
    animate_header_accent()
    layout_panel()
    animate_panel_in()
    animate_card_border()
    animate_start_pulse()
    animate_org_pulse()
    animate_dup_pulse()

    # Initial session UI state (disables gated features until login)
    _refresh_session_ui()

    root.mainloop()

if __name__ == "__main__":
    gui()