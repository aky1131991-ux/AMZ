"""Conservative Amazon shift monitor.

The application deliberately leaves authentication, CAPTCHA, 2FA, and page
structure changes to the user. It only acts on visible, labelled controls and
requires confirmation immediately before final submission.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover
    tk = None


APP_DIR = Path.home() / ".amazon_shift_finder"
DB_PATH = APP_DIR / "shifts.sqlite3"
CONFIG_PATH = APP_DIR / "config.json"
PROFILE_DIR = APP_DIR / "browser-profile"
TELEGRAM_PROFILE_DIR = APP_DIR / "telegram-browser-profile"
AMAZON_URL = "https://www.jobsatamazon.co.uk/app#/myApplications"
TELEGRAM_URL = "https://web.telegram.org/a/#-1003679177308"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Settings:
    locations: list[str]
    minimum_hours: int
    polling_seconds: int
    desktop_notifications: bool
    telegram_notifications: bool
    email_notifications: bool
    require_confirmation: bool
    telegram_channel: str = "-1003679177308"

    @classmethod
    def load(cls) -> "Settings":
        APP_DIR.mkdir(parents=True, exist_ok=True)
        values = asdict(cls.default())
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                values.update(data)
            except (OSError, TypeError, ValueError):
                pass
        return cls(**values)

    @classmethod
    def default(cls) -> "Settings":
        return cls(["Dartford", "Tilbury"], 30, 60, True, False, False, True)

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))


class ShiftStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shifts (
                    fingerprint TEXT PRIMARY KEY, detected_at TEXT NOT NULL,
                    telegram_message_id TEXT, location TEXT NOT NULL,
                    hours INTEGER, shift_type TEXT, start_time TEXT,
                    end_time TEXT, date TEXT, reference TEXT,
                    amazon_id TEXT, status TEXT NOT NULL, raw_text TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    level TEXT NOT NULL, message TEXT NOT NULL
                );
                """
            )
            self.connection.commit()

    def record_event(self, message: str, level: str = "INFO") -> None:
        with self.lock:
            self.connection.execute("INSERT INTO events VALUES (NULL, ?, ?, ?)", (now_iso(), level, message))
            self.connection.commit()

    def seen(self, fingerprint: str) -> bool:
        with self.lock:
            return self.connection.execute("SELECT 1 FROM shifts WHERE fingerprint = ?", (fingerprint,)).fetchone() is not None

    def add(self, shift: dict[str, Any], status: str = "Detected") -> bool:
        fingerprint = shift["fingerprint"]
        with self.lock:
            try:
                self.connection.execute(
                    "INSERT INTO shifts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (fingerprint, now_iso(), shift.get("telegram_message_id"), shift.get("location", ""),
                     shift.get("hours"), shift.get("shift_type", ""), shift.get("start_time", ""),
                     shift.get("end_time", ""), shift.get("date", ""), shift.get("reference", ""),
                     shift.get("amazon_id", ""), status, shift.get("raw_text", "")),
                )
                self.connection.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def update_status(self, fingerprint: str, status: str) -> None:
        with self.lock:
            self.connection.execute("UPDATE shifts SET status = ? WHERE fingerprint = ?", (status, fingerprint))
            self.connection.commit()

    def count_status(self, status: str) -> int:
        with self.lock:
            return int(self.connection.execute("SELECT COUNT(*) FROM shifts WHERE status = ?", (status,)).fetchone()[0])


def parse_shift(text: str, message_id: str | None = None) -> dict[str, Any] | None:
    """Extract only the fields needed for a conservative alert match."""
    location_match = re.search(r"\b(Dartford|Tilbury)\b", text, re.I)
    hours_match = re.search(r"\b(\d{1,2})\s*(?:h|hours?)\b", text, re.I)
    if not location_match or not hours_match:
        return None
    location = location_match.group(1).title()
    hours = int(hours_match.group(1))
    shift_type_match = re.search(r"\b(day|night|morning|evening|flex(?:ible)?)\s*(?:shift)?\b", text, re.I)
    times = re.findall(r"\b(?:[01]?\d|2[0-3]):?[0-5]\d\b", text)
    date_match = re.search(r"\b(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b", text)
    reference_match = re.search(r"\b(?:job|shift|ref(?:erence)?)\s*[:#-]?\s*([A-Z0-9-]{4,})\b", text, re.I)
    data = {
        "telegram_message_id": message_id,
        "location": location,
        "hours": hours,
        "shift_type": shift_type_match.group(1).title() if shift_type_match else "",
        "start_time": times[0] if times else "",
        "end_time": times[1] if len(times) > 1 else "",
        "date": date_match.group(1) if date_match else "",
        "reference": reference_match.group(1) if reference_match else "",
        "raw_text": text,
    }
    data["fingerprint"] = "|".join(str(data.get(key, "")).lower() for key in ("location", "hours", "shift_type", "date", "start_time", "end_time", "reference"))
    return data


def is_match(shift: dict[str, Any], settings: Settings) -> bool:
    return shift.get("location", "").casefold() in {value.casefold() for value in settings.locations} and int(shift.get("hours") or 0) >= settings.minimum_hours


class AmazonAdapter:
    def __init__(self, store: ShiftStore, notify: Callable[[str, str], None]) -> None:
        self.store = store
        self.notify = notify
        self.requests: queue.Queue[tuple[Callable[[], Any], Future[Any]]] = queue.Queue()
        self.worker = threading.Thread(target=self._worker_loop, name="amazon-playwright", daemon=True)
        self.worker.start()
        self.playwright = None
        self.browser = None
        self.page = None

    def _worker_loop(self) -> None:
        while True:
            operation, result = self.requests.get()
            try:
                result.set_result(operation())
            except Exception as exc:
                if self.page:
                    try:
                        self.page.screenshot(path=str(APP_DIR / f"amazon-error-{int(time.time())}.png"), full_page=True)
                    except Exception:
                        pass
                result.set_exception(exc)

    def _call(self, operation: Callable[[], Any]) -> Any:
        result: Future[Any] = Future()
        self.requests.put((operation, result))
        return result.result()

    def _require_playwright(self) -> None:
        if self.playwright is None:
            try:
                from playwright.sync_api import sync_playwright
                self.playwright = sync_playwright().start()
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                self.browser = self.playwright.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
                self.page = self.browser.pages[0] if self.browser.pages else self.browser.new_page()
            except Exception as exc:
                raise RuntimeError("Playwright is unavailable. Install dependencies and browsers first.") from exc

    def _open(self) -> None:
        self._require_playwright()
        self.page.goto(AMAZON_URL, wait_until="domcontentloaded", timeout=30_000)

    def _verification_required(self) -> bool:
        body = self.page.locator("body").inner_text(timeout=5_000).lower()
        return any(term in body for term in ("captcha", "two-factor", "2fa", "verification code", "security check"))

    def _test_login(self) -> bool:
        self._open()
        if self._verification_required():
            self.notify("Amazon requires manual CAPTCHA/2FA verification.", "verification")
            return False
        return "my applications" in self.page.locator("body").inner_text(timeout=5_000).lower()

    def _visible_shifts(self) -> list[dict[str, Any]]:
        self._open()
        if self._verification_required():
            self.notify("Amazon requires manual CAPTCHA/2FA verification.", "verification")
            return []
        buttons = self.page.get_by_text(re.compile(r"select shift", re.I))
        results = []
        for index in range(buttons.count()):
            button = buttons.nth(index)
            card_text = button.locator("xpath=ancestor::*[self::li or @role='article' or contains(@class, 'card')][1]").inner_text(timeout=3_000)
            parsed = parse_shift(card_text)
            if parsed:
                parsed["amazon_id"] = str(index)
                parsed["fingerprint"] = "amazon|" + parsed["fingerprint"]
                results.append(parsed)
        return results

    def _select_and_open_confirmation(self, shift: dict[str, Any]) -> bool:
        if self._verification_required():
            self.notify("Amazon requires manual CAPTCHA/2FA verification.", "verification")
            return False
        buttons = self.page.get_by_text(re.compile(r"select shift", re.I))
        target = buttons.nth(int(shift.get("amazon_id", 0)))
        target.click()
        self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
        return not self._verification_required()

    def _submit_after_confirmation(self) -> None:
        if self._verification_required():
            raise RuntimeError("Amazon verification is required; submission stopped.")
        submit = self.page.get_by_role("button", name=re.compile(r"submit|apply", re.I)).last
        if submit.count() != 1:
            raise RuntimeError("Could not identify exactly one final submit button; submission stopped.")
        submit.click()

    def test_login(self) -> bool:
        return self._call(self._test_login)

    def visible_shifts(self) -> list[dict[str, Any]]:
        return self._call(self._visible_shifts)

    def select_and_open_confirmation(self, shift: dict[str, Any]) -> bool:
        return self._call(lambda: self._select_and_open_confirmation(shift))

    def submit_after_confirmation(self) -> None:
        self._call(self._submit_after_confirmation)


class TelegramAdapter:
    def __init__(self, settings: Settings, on_message: Callable[[str, str], None], notify: Callable[[str, str], None]) -> None:
        self.settings, self.on_message, self.notify = settings, on_message, notify
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.context = None
        self.playwright = None
        self.seen_messages: set[str] = set()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
            TELEGRAM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(str(TELEGRAM_PROFILE_DIR), headless=False)
            page = self.context.pages[0] if self.context.pages else self.context.new_page()
            page.goto(TELEGRAM_URL, wait_until="domcontentloaded", timeout=30_000)
            while not self.stop_event.is_set():
                try:
                    body = page.locator("body").inner_text(timeout=10_000)
                except Exception as exc:
                    self.notify(f"Telegram Web is not ready: {exc}", "error")
                    self.stop_event.wait(self.settings.polling_seconds)
                    continue
                lowered = body.lower()
                if any(term in lowered for term in ("log in", "qr code", "verification code")):
                    self.notify("Telegram Web needs manual login or verification.", "verification")
                for message in self._visible_messages(page):
                    if message[0] not in self.seen_messages:
                        self.seen_messages.add(message[0])
                        self.on_message(message[0], message[1])
                self.stop_event.wait(self.settings.polling_seconds)
        except Exception as exc:
            self.notify(f"Telegram Web stopped: {exc}", "error")
        finally:
            if self.context:
                self.context.close()
            if self.playwright:
                self.playwright.stop()

    @staticmethod
    def _visible_messages(page: Any) -> list[tuple[str, str]]:
        messages = page.locator("[data-message-id], .message, [class*='message']")
        found: list[tuple[str, str]] = []
        for index in range(min(messages.count(), 100)):
            item = messages.nth(index)
            try:
                text = item.inner_text(timeout=1_000).strip()
            except Exception:
                continue
            if not text:
                continue
            message_id = item.get_attribute("data-message-id") or f"web-{hash(text)}"
            found.append((message_id, text))
        return found

    def stop(self) -> None:
        self.stop_event.set()


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root, self.settings, self.store = root, Settings.load(), ShiftStore()
        self.running = False
        self.amazon = AmazonAdapter(self.store, self.notify)
        self.telegram = TelegramAdapter(self.settings, self.handle_telegram, self.notify)
        self.status_var = tk.StringVar(value="Stopped")
        self.amazon_var = tk.StringVar(value="Disconnected")
        self.telegram_var = tk.StringVar(value="Disconnected")
        self.last_amazon_var = tk.StringVar(value="Never")
        self.last_telegram_var = tk.StringVar(value="Never")
        self.matches_var = tk.StringVar(value="0")
        self.selected_var = tk.StringVar(value="0")
        self.submitted_var = tk.StringVar(value="0")
        self.errors_var = tk.StringVar(value=str(self.store.count_status("Failed")))
        self.build_ui()

    def build_ui(self) -> None:
        self.root.title("Amazon Shift Finder")
        self.root.geometry("900x620")
        self.root.minsize(760, 520)
        self.root.configure(bg="#f4f1ea")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f1ea")
        style.configure("TLabel", background="#f4f1ea", foreground="#24313a", font=("DejaVu Sans", 10))
        style.configure("Title.TLabel", font=("DejaVu Sans", 26, "bold"), foreground="#15252c")
        style.configure("Muted.TLabel", foreground="#65737a")
        style.configure("TButton", padding=(12, 8), font=("DejaVu Sans", 10, "bold"))
        outer = ttk.Frame(self.root, padding=28); outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="SHIFT FINDER", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Watch alerts. Verify availability. Keep the final decision yours.", style="Muted.TLabel").pack(anchor="w", pady=(2, 22))
        top = ttk.Frame(outer); top.pack(fill="x")
        status = ttk.Label(top, textvariable=self.status_var, font=("DejaVu Sans", 12, "bold")); status.pack(side="left")
        for label, command in (("START MONITORING", self.start), ("STOP", self.stop), ("CHECK NOW", self.check_now), ("TEST AMAZON LOGIN", self.test_amazon), ("TEST TELEGRAM", self.test_telegram), ("SETTINGS", self.settings_dialog)):
            ttk.Button(top, text=label, command=command).pack(side="right", padx=(8, 0))
        ttk.Separator(outer).pack(fill="x", pady=22)
        grid = ttk.Frame(outer); grid.pack(fill="x")
        cards = [("Amazon connection", self.amazon_var), ("Telegram connection", self.telegram_var), ("Last Amazon check", self.last_amazon_var), ("Last Telegram alert", self.last_telegram_var), ("Matching shifts found", self.matches_var), ("Shifts selected", self.selected_var), ("Applications submitted", self.submitted_var), ("Errors", self.errors_var)]
        for index, (label, variable) in enumerate(cards):
            box = ttk.Frame(grid, padding=14); box.grid(row=index // 4, column=index % 4, sticky="nsew", padx=4, pady=4)
            ttk.Label(box, text=label.upper(), style="Muted.TLabel", font=("DejaVu Sans", 8, "bold")).pack(anchor="w")
            ttk.Label(box, textvariable=variable, font=("DejaVu Sans", 12, "bold")).pack(anchor="w", pady=(8, 0))
        for col in range(4): grid.columnconfigure(col, weight=1)
        ttk.Label(outer, text="ACTIVITY", font=("DejaVu Sans", 10, "bold")).pack(anchor="w", pady=(24, 8))
        self.log = tk.Text(outer, height=12, state="disabled", bg="#18272b", fg="#d6e2dc", relief="flat", padx=14, pady=12, font=("DejaVu Sans Mono", 9))
        self.log.pack(fill="both", expand=True)
        self.write_log("Ready. Authentication uses your existing browser profile.")

    def write_log(self, message: str) -> None:
        self.store.record_event(message)
        self.log.configure(state="normal"); self.log.insert("end", f"{datetime.now().strftime('%H:%M:%S')}  {message}\n"); self.log.see("end"); self.log.configure(state="disabled")

    def notify(self, message: str, kind: str = "info") -> None:
        self.store.record_event(message, "ERROR" if kind == "error" else "INFO")
        self.root.after(0, lambda: self.write_log(message))
        if self.settings.desktop_notifications:
            try:
                subprocess.Popen(["notify-send", "Amazon Shift Finder", message])
            except OSError:
                self.root.bell()
        if kind == "verification":
            self.root.after(0, lambda: self.status_var.set("Waiting for verification"))

    def start(self) -> None:
        if self.running: return
        self.running = True; self.status_var.set("Monitoring"); self.write_log("Monitoring started.")
        try:
            self.telegram.start(); self.telegram_var.set("Connecting")
        except RuntimeError as exc:
            self.telegram_var.set("Disconnected"); self.notify(str(exc), "error")
        self.root.after(100, self.check_now)

    def stop(self) -> None:
        self.running = False; self.status_var.set("Stopped"); self.telegram.stop(); self.telegram_var.set("Disconnected"); self.write_log("Monitoring stopped.")

    def check_now(self) -> None:
        threading.Thread(target=self._check_amazon, daemon=True).start()

    def test_amazon(self) -> None:
        def run() -> None:
            try:
                connected = self.amazon.test_login()
                self.root.after(0, lambda: self.amazon_var.set("Connected" if connected else "Disconnected"))
                self.notify("Amazon login test passed." if connected else "Amazon login was not confirmed.", "info" if connected else "error")
            except Exception as exc:
                self.notify(f"Amazon login test failed: {exc}", "error")
        threading.Thread(target=run, daemon=True).start()

    def test_telegram(self) -> None:
        self.telegram.start()
        self.telegram_var.set("Connecting")
        self.write_log("Telegram Web test started; complete login manually in the browser window if needed.")

    def _check_amazon(self) -> None:
        try:
            shifts = self.amazon.visible_shifts()
            self.root.after(0, lambda: self.amazon_var.set("Connected"))
            self.root.after(0, lambda: self.last_amazon_var.set(datetime.now().strftime("%H:%M:%S")))
            for shift in shifts:
                if not is_match(shift, self.settings) or self.store.seen(shift["fingerprint"]): continue
                self.store.add(shift, "Matching"); self.root.after(0, lambda: self.matches_var.set(str(self.store.count_status("Matching"))))
                self.notify(f"MATCH FOUND: {shift['location']} / {shift['hours']}h / {shift.get('shift_type') or 'shift'}", "match")
                self.root.after(0, lambda item=shift: self.offer_shift(item))
        except Exception as exc:
            self.root.after(0, lambda: self.amazon_var.set("Disconnected")); self.notify(f"Amazon check stopped: {exc}", "error")
        if self.running: self.root.after(self.settings.polling_seconds * 1000, self.check_now)

    def handle_telegram(self, message_id: str, text: str) -> None:
        shift = parse_shift(text, message_id)
        self.root.after(0, lambda: self.last_telegram_var.set(datetime.now().strftime("%H:%M:%S")))
        if not shift or not is_match(shift, self.settings): return
        if self.store.add(shift, "Matching"):
            self.root.after(0, lambda: self.telegram_var.set("Connected")); self.notify(f"Telegram match: {shift['location']} / {shift['hours']}h", "match")
            self.root.after(0, self.check_now)

    def offer_shift(self, shift: dict[str, Any]) -> None:
        summary = f"MATCH FOUND\n\nLocation: {shift['location']}\nHours: {shift['hours']}\nShift: {shift.get('shift_type') or 'Not stated'}\nDate: {shift.get('date') or 'Not stated'}\nTime: {shift.get('start_time') or '?'} - {shift.get('end_time') or '?'}"
        if not messagebox.askyesno("Confirmation required", summary + "\n\nOpen this shift and continue to final confirmation?"):
            self.store.update_status(shift["fingerprint"], "Rejected"); return
        try:
            if self.amazon.select_and_open_confirmation(shift):
                self.store.update_status(shift["fingerprint"], "Selected"); self.selected_var.set(str(self.store.count_status("Selected")))
                self.notify("Shift selected; review Amazon's confirmation page.", "selected")
                if self.settings.require_confirmation and messagebox.askyesno("Confirm & Submit", summary + "\n\nSubmit this application now?"):
                    self.amazon.submit_after_confirmation(); self.store.update_status(shift["fingerprint"], "Submitted"); self.submitted_var.set(str(self.store.count_status("Submitted")))
        except Exception as exc:
            self.store.update_status(shift["fingerprint"], "Failed"); self.errors_var.set(str(self.store.count_status("Failed"))); self.notify(f"Selection stopped: {exc}", "error")

    def settings_dialog(self) -> None:
        dialog = tk.Toplevel(self.root); dialog.title("Settings"); dialog.transient(self.root); dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20); frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Locations").grid(row=0, column=0, sticky="w")
        dartford = tk.BooleanVar(value="Dartford" in self.settings.locations); tilbury = tk.BooleanVar(value="Tilbury" in self.settings.locations)
        ttk.Checkbutton(frame, text="Dartford", variable=dartford).grid(row=1, column=0, sticky="w"); ttk.Checkbutton(frame, text="Tilbury", variable=tilbury).grid(row=2, column=0, sticky="w")
        ttk.Label(frame, text="Minimum weekly hours").grid(row=3, column=0, sticky="w", pady=(12, 0)); hours = tk.IntVar(value=self.settings.minimum_hours); ttk.Spinbox(frame, from_=1, to=168, textvariable=hours, width=8).grid(row=4, column=0, sticky="w")
        ttk.Label(frame, text="Polling interval").grid(row=5, column=0, sticky="w", pady=(12, 0)); interval = tk.IntVar(value=self.settings.polling_seconds); ttk.Combobox(frame, textvariable=interval, values=(30, 60, 90, 120), state="readonly", width=8).grid(row=6, column=0, sticky="w")
        desktop = tk.BooleanVar(value=self.settings.desktop_notifications); ttk.Checkbutton(frame, text="Desktop notifications", variable=desktop).grid(row=7, column=0, sticky="w", pady=(12, 0))
        def save() -> None:
            locations = (["Dartford"] if dartford.get() else []) + (["Tilbury"] if tilbury.get() else [])
            if not locations: messagebox.showerror("Settings", "Select at least one location.", parent=dialog); return
            self.settings.locations, self.settings.minimum_hours, self.settings.polling_seconds, self.settings.desktop_notifications = locations, hours.get(), int(interval.get()), desktop.get(); self.settings.save(); dialog.destroy(); self.write_log("Settings saved.")
        ttk.Button(frame, text="SAVE", command=save).grid(row=8, column=0, pady=(20, 0), sticky="e")


def main() -> None:
    if tk is None: raise SystemExit("Tkinter is required to run the desktop application.")
    root = tk.Tk(); App(root); root.mainloop()


if __name__ == "__main__":
    main()