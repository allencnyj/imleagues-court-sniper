"""The desktop window.

Three bands: session status up top, the target queue in the middle,
timing settings and the log at the bottom. Closing the window hides it
to the tray so an armed queue keeps running.
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import date as _date, datetime, time as _time, timedelta
from typing import Any

import customtkinter as ctk

import config as config_module
import constants as C
import theme as T
from account_session import AccountSession
from config import APP_ICON, AppConfig
from models import Slot, Target, TargetState
from payload import Requirements, read_requirements
from planner import Plan, Request, confine_manual_fallbacks, describe, plan_requests
from scheduler import Sniper, held_by_day, slots_for
from tray import TrayIcon, make_app_icon
from timing import format_countdown

log = logging.getLogger(__name__)

# Committed dark theme; the palette lives in theme.py.
ctk.set_appearance_mode("dark")

COURT_CHOICES = [f"Court {n}" for n in C.COURTS]
STATE_COLOURS = {
    TargetState.WON: T.OK,
    TargetState.LOST: T.ERROR,
    TargetState.ERROR: T.ERROR,
    TargetState.SKIPPED: T.WARN,
    TargetState.SIMULATED: T.TEXT_MUTED,
    TargetState.WAITLISTED: T.WARN,
    TargetState.WAITING: T.WARN,
    TargetState.FIRING: T.TEXT,
    TargetState.ARMED: T.TEXT_MUTED,
    TargetState.IDLE: T.TEXT_FAINT,
}


def card(parent, **kw) -> ctk.CTkFrame:
    """A raised surface: the basic building block of the layout."""
    opts = dict(fg_color=T.SURFACE, corner_radius=T.R_LG,
                border_width=1, border_color=T.BORDER)
    opts.update(kw)
    return ctk.CTkFrame(parent, **opts)


def eyebrow_label(parent, text: str) -> ctk.CTkLabel:
    """Small, spaced, uppercase section header."""
    return ctk.CTkLabel(parent, text=T.eyebrow(text), font=T.EYEBROW,
                        text_color=T.TEXT_FAINT, anchor="w")


class StatTile(ctk.CTkFrame):
    """One of the numbers across the top: big value, quiet caption."""

    def __init__(self, parent, caption: str, *, wide: bool = False) -> None:
        super().__init__(parent, fg_color=T.SURFACE, corner_radius=T.R_LG,
                         border_width=1, border_color=T.BORDER)
        self.grid_columnconfigure(0, weight=1)
        self.value = ctk.CTkLabel(self, text="--", font=T.DISPLAY if wide else T.STAT,
                                  text_color=T.TEXT)
        self.value.grid(row=0, column=0, pady=(T.MD, 2), padx=T.MD)
        self.caption = ctk.CTkLabel(self, text=T.eyebrow(caption), font=T.EYEBROW,
                                    text_color=T.TEXT_FAINT)
        self.caption.grid(row=1, column=0, pady=(0, T.SM), padx=T.MD)
        self.sub = ctk.CTkLabel(self, text="", font=T.CAPTION, text_color=T.TEXT_MUTED)
        self.sub.grid(row=2, column=0, pady=(0, T.SM), padx=T.MD)

    def set(self, value: str, *, sub: str = "", colour: str | None = None) -> None:
        self.value.configure(text=value, text_color=colour or T.TEXT)
        self.sub.configure(text=sub)


class TargetRow:
    """One row of the queue: date, time, court, fallback, state."""

    def __init__(self, parent: ctk.CTkFrame, index: int, app: "SniperApp") -> None:
        self.app = app
        self.frame = ctk.CTkFrame(parent, fg_color=T.SURFACE_HI, corner_radius=T.R_MD)
        self.frame.grid(row=index + 1, column=0, sticky="ew", pady=(0, T.SM))
        for col, weight in enumerate((0, 3, 3, 2, 2, 4)):
            self.frame.grid_columnconfigure(col, weight=weight)

        menu_kw = dict(fg_color=T.SURFACE_ACTIVE, button_color=T.SURFACE_ACTIVE,
                       button_hover_color=T.BORDER_HI, text_color=T.TEXT,
                       dropdown_fg_color=T.SURFACE_ACTIVE, dropdown_text_color=T.TEXT,
                       dropdown_hover_color=T.BORDER_HI, corner_radius=T.R_SM,
                       font=T.BODY, dropdown_font=T.BODY, height=32)
        check_kw = dict(fg_color=T.PRIMARY, hover_color=T.PRIMARY_HOVER,
                        checkmark_color=T.PRIMARY_TEXT, border_color=T.BORDER_HI,
                        text_color=T.TEXT_MUTED, font=T.LABEL,
                        checkbox_width=18, checkbox_height=18, corner_radius=T.R_SM)

        self.enabled = ctk.BooleanVar(value=index == 0)
        ctk.CTkCheckBox(self.frame, text="", variable=self.enabled, width=24,
                        command=app.refresh_plan, **check_kw).grid(
            row=0, column=0, padx=(T.MD, T.SM), pady=T.MD)

        self.date_var = ctk.StringVar()
        self.date_menu = ctk.CTkOptionMenu(self.frame, variable=self.date_var, values=[""],
                                           command=lambda _v: app.on_date_changed(self),
                                           **menu_kw)
        self.date_menu.grid(row=0, column=1, sticky="ew", padx=T.XS)

        self.time_var = ctk.StringVar()
        self.time_menu = ctk.CTkOptionMenu(self.frame, variable=self.time_var, values=[""],
                                           command=lambda _v: app.refresh_plan(), **menu_kw)
        self.time_menu.grid(row=0, column=2, sticky="ew", padx=T.XS)

        self.court_var = ctk.StringVar(value=COURT_CHOICES[index % len(COURT_CHOICES)])
        ctk.CTkOptionMenu(self.frame, variable=self.court_var, values=COURT_CHOICES,
                          command=lambda _v: app.refresh_plan(), **menu_kw).grid(
            row=0, column=3, sticky="ew", padx=T.XS)

        self.fallback = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.frame, text="fallback", variable=self.fallback,
                        command=app.refresh_plan, **check_kw).grid(
            row=0, column=4, padx=T.SM)

        self.status = ctk.CTkLabel(self.frame, text="Idle", anchor="w",
                                   font=T.LABEL, text_color=T.TEXT_FAINT)
        self.status.grid(row=0, column=5, sticky="ew", padx=(T.MD, T.MD))

    # --- state --------------------------------------------------------

    @property
    def court(self) -> int:
        try:
            return int(self.court_var.get().split()[-1])
        except (ValueError, IndexError):
            return 1

    def selected_date(self) -> _date | None:
        raw = self.date_var.get()
        try:
            return _date.fromisoformat(raw.split(" ")[0])
        except (ValueError, IndexError):
            return None

    def selected_time(self) -> _time | None:
        raw = self.time_var.get().strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%I:%M %p").time()
        except ValueError:
            return None

    def to_target(self) -> Target | None:
        day, when = self.selected_date(), self.selected_time()
        if not self.enabled.get() or day is None or when is None:
            return None
        # wait_for_free: a court held by this same account is watched
        # until it frees rather than given up on.
        return Target(date=day, start_time=when, preferred_court=self.court,
                      allow_fallback=self.fallback.get(), wait_for_free=True)

    def apply(self, target: Target) -> None:
        self.enabled.set(True)
        self.date_var.set(target.date.isoformat())
        self.time_var.set(target.start_time.strftime("%I:%M %p").lstrip("0"))
        self.court_var.set(f"Court {target.preferred_court}")
        self.fallback.set(target.allow_fallback)

    def clear(self) -> None:
        self.enabled.set(False)

    def set_status(self, text: str, state: TargetState | None = None) -> None:
        colour = STATE_COLOURS.get(state) if state else None
        self.status.configure(text=text, text_color=colour or T.TEXT_FAINT)

    def set_dates(self, dates: list[str]) -> None:
        """Offer these dates, snapping to a valid one if ours has gone."""
        self.date_menu.configure(values=dates or [""])
        if self.date_var.get() not in dates:
            self.date_var.set(dates[0] if dates else "")

    def set_times(self, times: list[str]) -> None:
        self.time_menu.configure(values=times or [""])
        if self.time_var.get() not in times:
            self.time_var.set(times[0] if times else "")

    def set_locked(self, locked: bool) -> None:
        state = "disabled" if locked else "normal"
        for child in self.frame.winfo_children():
            try:
                child.configure(state=state)
            except (AttributeError, ValueError):
                pass
        self.status.configure(state="normal")


COURT_COUNT_CHOICES = [f"{n} court{'s' if n > 1 else ''}" for n in range(1, len(C.COURTS) + 1)]
#: How many time slots the auto planner will take: four accounts can hold
#: twelve court-slots, so twelve distinct times is the most that could
#: ever be booked. The list scrolls past REQUEST_ROWS_VISIBLE rows.
MAX_REQUEST_ROWS = 12
REQUEST_ROWS_VISIBLE = 4
#: One request row's height including its spacing (measured).
REQUEST_ROW_H = 64
#: Date, start, how-many picker widths; the header uses the same.
REQUEST_COL_WIDTHS = (160, 130, 130)


class RequestRow:
    """One line of the auto planner: a date, a start time, how many courts.

    No court number and no account -- that is the planner's job."""

    def __init__(self, parent: ctk.CTkFrame, index: int, app: "SniperApp") -> None:
        self.app = app
        self.frame = ctk.CTkFrame(parent, fg_color=T.SURFACE_HI, corner_radius=T.R_MD)
        self.frame.grid(row=index + 1, column=0, sticky="ew", pady=(0, T.SM))
        self.frame.grid_columnconfigure(3, weight=1)

        menu_kw = dict(fg_color=T.SURFACE_ACTIVE, button_color=T.SURFACE_ACTIVE,
                       button_hover_color=T.BORDER_HI, text_color=T.TEXT,
                       dropdown_fg_color=T.SURFACE_ACTIVE, dropdown_text_color=T.TEXT,
                       dropdown_hover_color=T.BORDER_HI, corner_radius=T.R_SM,
                       font=T.BODY, dropdown_font=T.BODY, height=32)

        # Fixed-width pickers and a stretching status column, so every
        # row's columns line up regardless of how long its status is.
        self.date_var = ctk.StringVar()
        self.date_menu = ctk.CTkOptionMenu(self.frame, variable=self.date_var, values=[""],
                                           command=lambda _v: app.on_request_date_changed(self),
                                           width=REQUEST_COL_WIDTHS[0], **menu_kw)
        self.date_menu.grid(row=0, column=0, sticky="w", padx=(T.MD, T.XS), pady=T.MD)

        self.time_var = ctk.StringVar()
        self.time_menu = ctk.CTkOptionMenu(self.frame, variable=self.time_var, values=[""],
                                           command=lambda _v: app.on_request_edited(),
                                           width=REQUEST_COL_WIDTHS[1], **menu_kw)
        self.time_menu.grid(row=0, column=1, sticky="w", padx=T.XS)

        self.count_var = ctk.StringVar(value=COURT_COUNT_CHOICES[0])
        ctk.CTkOptionMenu(self.frame, variable=self.count_var, values=COURT_COUNT_CHOICES,
                          command=lambda _v: app.on_request_edited(),
                          width=REQUEST_COL_WIDTHS[2], **menu_kw).grid(
            row=0, column=2, sticky="w", padx=T.XS)

        self.status = ctk.CTkLabel(self.frame, text="", anchor="w",
                                   font=T.LABEL, text_color=T.TEXT_FAINT)
        self.status.grid(row=0, column=3, sticky="ew", padx=(T.MD, T.SM))

        self.remove_btn = ctk.CTkButton(
            self.frame, text="×", width=30, height=30, font=(T.FAMILY, 15),
            fg_color="transparent", hover_color=T.SURFACE_ACTIVE,
            text_color=T.TEXT_FAINT, corner_radius=T.R_SM,
            command=lambda: app.on_remove_request(self))
        self.remove_btn.grid(row=0, column=4, padx=(0, T.SM))

    @property
    def courts(self) -> int:
        try:
            return int(self.count_var.get().split()[0])
        except (ValueError, IndexError):
            return 1

    def selected_date(self) -> _date | None:
        try:
            return _date.fromisoformat(self.date_var.get().split(" ")[0])
        except (ValueError, IndexError):
            return None

    def selected_time(self) -> _time | None:
        raw = self.time_var.get().strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%I:%M %p").time()
        except ValueError:
            return None

    def to_request(self) -> Request | None:
        day, when = self.selected_date(), self.selected_time()
        if day is None or when is None:
            return None
        return Request(date=day, start_time=when, courts=self.courts)

    def apply(self, r: Request) -> None:
        self.date_var.set(r.date.isoformat())
        self.time_var.set(r.start_time.strftime("%I:%M %p").lstrip("0"))
        self.count_var.set(COURT_COUNT_CHOICES[max(0, min(len(COURT_COUNT_CHOICES), r.courts) - 1)])

    def set_status(self, text: str, colour: str | None = None) -> None:
        self.status.configure(text=text, text_color=colour or T.TEXT_FAINT)

    def set_dates(self, dates: list[str]) -> None:
        self.date_menu.configure(values=dates or [""])
        if self.date_var.get() not in dates:
            self.date_var.set(dates[0] if dates else "")

    def set_times(self, times: list[str]) -> None:
        self.time_menu.configure(values=times or [""])
        if self.time_var.get() not in times:
            self.time_var.set(times[0] if times else "")

    def set_locked(self, locked: bool) -> None:
        state = "disabled" if locked else "normal"
        for child in self.frame.winfo_children():
            try:
                child.configure(state=state)
            except (AttributeError, ValueError):
                pass
        self.status.configure(state="normal")

    def destroy(self) -> None:
        self.frame.destroy()


class WaiverDialog(ctk.CTkToplevel):
    """Shows the real waiver and records what the user agrees to.

    The app will not book while a waiver is required and unaccepted --
    agreeing to terms is the account holder's to do, not the tool's, so
    the actual text is put in front of them here once.
    """

    def __init__(self, parent: "SniperApp", requirements, consent, on_save) -> None:
        super().__init__(parent)
        self.title("Booking requirements")
        self.geometry("760x640")
        self.transient(parent)
        self.requirements = requirements
        self.consent = consent
        self._on_save = on_save
        self.question_entries: list[ctk.CTkEntry] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="IMLeagues asks for the following before it will book a court.",
                     anchor="w", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=16, pady=(16, 8))

        body = ctk.CTkScrollableFrame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)
        body.grid_columnconfigure(0, weight=1)
        row = 0

        if requirements.waiver_shown:
            ctk.CTkLabel(body, text="Waiver / Agreement", anchor="w",
                         font=ctk.CTkFont(size=13, weight="bold")).grid(
                row=row, column=0, sticky="ew", pady=(4, 4))
            row += 1
            text = ctk.CTkTextbox(body, height=260, wrap="word")
            text.grid(row=row, column=0, sticky="ew", pady=(0, 8))
            text.insert("1.0", requirements.waiver_text or
                        "(The site did not return the waiver text. Read it on the "
                        "IMLeagues registration page before accepting here.)")
            text.configure(state="disabled")
            row += 1

            self.accept = ctk.BooleanVar(value=consent.accept_terms)
            ctk.CTkCheckBox(
                body, variable=self.accept, text=(
                    "I have read, understand, acknowledge and accept the above "
                    "Waiver/Agreement."),
            ).grid(row=row, column=0, sticky="w", pady=(0, 12))
            row += 1
        else:
            self.accept = ctk.BooleanVar(value=True)
            ctk.CTkLabel(body, text="No waiver is required for this network.",
                         anchor="w").grid(row=row, column=0, sticky="ew", pady=8)
            row += 1

        if requirements.needs_signature:
            ctk.CTkLabel(body, text="Digital signature (type your full name)", anchor="w",
                         font=ctk.CTkFont(size=13, weight="bold")).grid(
                row=row, column=0, sticky="ew", pady=(8, 4))
            row += 1
            self.signature = ctk.StringVar(value=consent.digital_signature)
            ctk.CTkEntry(body, textvariable=self.signature).grid(
                row=row, column=0, sticky="ew", pady=(0, 12))
            row += 1
        else:
            self.signature = ctk.StringVar(value=consent.digital_signature)

        for i, question in enumerate(requirements.waiver_questions):
            label = str(question.get("question") or question.get("text") or f"Question {i + 1}")
            ctk.CTkLabel(body, text=label, anchor="w", wraplength=660, justify="left").grid(
                row=row, column=0, sticky="ew", pady=(6, 2))
            row += 1
            entry = ctk.CTkEntry(body)
            if i < len(consent.waiver_answers) and consent.waiver_answers[i] is not None:
                entry.insert(0, str(consent.waiver_answers[i]))
            entry.grid(row=row, column=0, sticky="ew", pady=(0, 6))
            self.question_entries.append(entry)
            row += 1

        if requirements.needs_sid:
            ctk.CTkLabel(body, text=requirements.sid_label or "Student ID", anchor="w",
                         font=ctk.CTkFont(size=13, weight="bold")).grid(
                row=row, column=0, sticky="ew", pady=(8, 2))
            row += 1
            self.sid = ctk.StringVar(value=consent.sid)
            ctk.CTkEntry(body, textvariable=self.sid).grid(row=row, column=0, sticky="ew")
            row += 1
        else:
            self.sid = ctk.StringVar(value=consent.sid)

        if requirements.needs_phone:
            ctk.CTkLabel(body, text="Mobile phone", anchor="w",
                         font=ctk.CTkFont(size=13, weight="bold")).grid(
                row=row, column=0, sticky="ew", pady=(8, 2))
            row += 1
            self.phone = ctk.StringVar(value=consent.phone)
            ctk.CTkEntry(body, textvariable=self.phone).grid(row=row, column=0, sticky="ew")
            row += 1
        else:
            self.phone = ctk.StringVar(value=consent.phone)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", padx=16, pady=12)
        buttons.grid_columnconfigure(0, weight=1)
        self.error = ctk.CTkLabel(buttons, text="", anchor="w", text_color="#b42318")
        self.error.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(buttons, text="Cancel", width=100, fg_color="gray45",
                      hover_color="gray35", command=self.destroy).grid(row=0, column=1, padx=6)
        ctk.CTkButton(buttons, text="Save", width=120, command=self._save).grid(row=0, column=2)

        self.after(120, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:  # noqa: BLE001 - focus is cosmetic
            pass

    def _save(self) -> None:
        if self.requirements.waiver_shown and not self.accept.get():
            self.error.configure(text="Tick the acceptance box, or Cancel.")
            return
        if self.requirements.needs_signature and not self.signature.get().strip():
            self.error.configure(text="A digital signature is required.")
            return
        self.consent.accept_terms = bool(self.accept.get())
        self.consent.digital_signature = self.signature.get().strip()
        self.consent.sid = self.sid.get().strip()
        self.consent.phone = self.phone.get().strip()
        self.consent.waiver_answers = [e.get().strip() for e in self.question_entries]
        self._on_save()
        self.destroy()


class ReservationsDialog(ctk.CTkToplevel):
    """Lists one account's current reservations, each with Unreserve.

    Confirmed spots and waitlist spots are both shown and labelled; the
    Unreserve button cancels either (leaving a waitlist counts as
    unreserving too).
    """

    def __init__(self, parent: "SniperApp", account_display: str,
                 reservations: list[dict], on_unreserve) -> None:
        super().__init__(parent)
        self.title("Reservations")
        self.geometry("560x460")
        self.configure(fg_color=T.BG)
        self.transient(parent)
        self._on_unreserve = on_unreserve
        self._rows: dict[int, ctk.CTkFrame] = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self, text=f"{account_display} — current reservations",
                     font=T.TITLE, text_color=T.TEXT, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=T.LG, pady=(T.LG, T.SM))

        self.body = ctk.CTkScrollableFrame(self, fg_color=T.SURFACE,
                                           corner_radius=T.R_MD)
        self.body.grid(row=1, column=0, sticky="nsew", padx=T.MD, pady=T.SM)
        self.body.grid_columnconfigure(0, weight=1)

        self.hint = ctk.CTkLabel(self, text="", font=T.CAPTION, anchor="w",
                                 text_color=T.TEXT_MUTED)
        self.hint.grid(row=2, column=0, sticky="ew", padx=T.LG, pady=(0, T.SM))
        ctk.CTkButton(self, text="Close", width=100, command=self.destroy,
                      fg_color=T.GHOST, hover_color=T.GHOST_HOVER,
                      text_color=T.TEXT_MUTED, border_width=1,
                      border_color=T.BORDER).grid(row=3, column=0, sticky="e",
                                                  padx=T.LG, pady=(0, T.MD))
        self.render(reservations)
        self.after(120, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def render(self, reservations: list[dict]) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self._rows.clear()
        if not reservations:
            ctk.CTkLabel(self.body, text="No reservations for this account.",
                         font=T.BODY, text_color=T.TEXT_MUTED).grid(
                row=0, column=0, sticky="w", padx=T.MD, pady=T.MD)
            return
        for i, r in enumerate(reservations):
            frame = ctk.CTkFrame(self.body, fg_color=T.SURFACE_HI,
                                 corner_radius=T.R_SM)
            frame.grid(row=i, column=0, sticky="ew", pady=(0, T.SM), padx=T.XS)
            frame.grid_columnconfigure(0, weight=1)
            tag = "  (waitlist)" if r.get("waitlisted") else ""
            colour = T.WARN if r.get("waitlisted") else T.TEXT
            ctk.CTkLabel(frame, text=r["label"] + tag, font=T.BODY,
                         text_color=colour, anchor="w").grid(
                row=0, column=0, sticky="w", padx=T.MD, pady=T.SM)
            btn = ctk.CTkButton(
                frame, text="Unreserve", width=100,
                fg_color=T.DANGER, hover_color=T.DANGER_HOVER, text_color=T.TEXT,
                corner_radius=T.R_SM, font=T.LABEL,
                command=lambda rr=r: self._click(rr))
            btn.grid(row=0, column=1, padx=T.SM, pady=T.SM)
            self._rows[r["event_member_id"]] = frame

    def _click(self, reservation: dict) -> None:
        mid = reservation["event_member_id"]
        frame = self._rows.get(mid)
        if frame:
            for child in frame.winfo_children():
                try:
                    child.configure(state="disabled")
                except (AttributeError, ValueError):
                    pass
        self.hint.configure(text=f"Unreserving {reservation['label']}...",
                            text_color=T.TEXT_MUTED)
        self._on_unreserve(reservation, self)

    def mark_result(self, reservation: dict, ok: bool, message: str) -> None:
        if ok:
            frame = self._rows.pop(reservation["event_member_id"], None)
            if frame:
                frame.destroy()
            self.hint.configure(text=f"Unreserved {reservation['label']}.",
                                text_color=T.OK)
        else:
            frame = self._rows.get(reservation["event_member_id"])
            if frame:
                for child in frame.winfo_children():
                    try:
                        child.configure(state="normal")
                    except (AttributeError, ValueError):
                        pass
            self.hint.configure(text=f"Could not unreserve: {message}",
                                text_color=T.ERROR)


class SniperApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self._set_app_icon()
        self.title("IMLeagues Badminton Sniper - UT Dallas")
        self.geometry("1120x940")
        self.minsize(1020, 820)

        self.config_data = AppConfig.load()
        self.sessions: list[AccountSession] = [
            self._make_session(a) for a in self.config_data.accounts]
        self.active_idx = 0
        self.snipers: list[Sniper] = []
        self._row_targets: dict[int, Target] = {}
        # Auto-plan mode state.
        self.request_rows: list[RequestRow] = []
        #: Courts each account already holds, by account id then by day
        #: (the rule is per day); refreshed from the calendar so the
        #: planner never hands out a spent court.
        self._held_by_account: dict[str, dict[_date, set[int]]] = {}
        self._plan: Plan | None = None
        #: request index -> the Targets serving it, once armed.
        self._request_targets: dict[int, list[Target]] = {}
        #: True once the user has chosen an account, so the async boot
        #: check does not clobber their selection when it completes late.
        self._user_selected_account = False
        self.slots: list[Slot] = []
        self._events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._callbacks: queue.Queue[tuple[Any, Any]] = queue.Queue()
        self._log_tags: set[str] = set()
        self._armed = False
        self._arming = False
        self._busy = False
        self._quitting = False
        self._tick_n = 0
        # Any command left over from a previous run is stale, not an order.
        self._clear_commands()
        self._hidden_hint_shown = False
        self.tray = TrayIcon(
            on_open=lambda: self._callbacks.put((lambda _r: self._show_window(), None)),
            on_quit=lambda: self._callbacks.put((lambda _r: self._really_quit(), None)),
            on_disarm=lambda: self._callbacks.put((lambda _r: self.on_arm(), None)),
            is_armed=lambda: self._armed,
        )

        self._build_ui()
        self._refresh_account_ui()
        self._load_active_targets()
        self._load_requests()
        self._apply_mode()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.tray.start()
        self._pump_after: str | None = None
        self.after(200, self._pump)
        self.after(300, self._boot)

    # --- accounts -----------------------------------------------------

    def _make_session(self, account) -> AccountSession:
        return AccountSession(
            account,
            notify=self.emit,
            on_signed_in=lambda _s: self._callbacks.put(
                (lambda _r: self._refresh_account_ui(), None)),
            fire_imminent=self._fire_imminent,
            on_form_saved=self._save_quiet,
        )

    @property
    def active(self) -> AccountSession:
        return self.sessions[self.active_idx]

    @property
    def signed_in(self) -> bool:
        return self.active.signed_in

    @property
    def any_signed_in(self) -> bool:
        return any(s.signed_in for s in self.sessions)

    def _save_quiet(self) -> None:
        try:
            self.config_data.save()
        except OSError as exc:
            log.warning("could not save settings: %s", exc)

    def _set_app_icon(self) -> None:
        """Shuttlecock in the title bar, taskbar and Alt-Tab.

        Windows groups taskbar buttons by Application User Model ID, and
        without an explicit one this would inherit pythonw's identity and
        show the generic Python icon. Setting it must happen before the
        window is mapped.
        """
        # Deliberately NOT setting an explicit AppUserModelID. Doing so
        # makes Windows look for a registered shortcut carrying that ID
        # to source the taskbar icon from, and finding none it falls back
        # to the executable's icon -- i.e. pythonw's. Leaving it unset
        # makes the taskbar use the window icon we set below, which is
        # the one we actually want.

        if not APP_ICON.exists() and not make_app_icon(APP_ICON):
            return
        try:
            # default=True so dialogs inherit it too. Note this sets the
            # window *class* icon only.
            self.iconbitmap(default=str(APP_ICON))
        except Exception as exc:  # noqa: BLE001
            log.debug("could not apply window icon: %s", exc)
        # Tk leaves the per-window icon empty (WM_GETICON returns 0) and
        # the taskbar reads that first, so set it explicitly.
        #
        # Timing matters: Windows snapshots the icon when it creates the
        # taskbar button, so this has to happen BEFORE the window is
        # first mapped. Doing it on a timer afterwards left the button
        # showing Tk's Python-logo icon even though the title bar was
        # correct. Re-applied on <Map> as well, for the case where the
        # window is later restored from the tray.
        self._set_win32_icon()
        self.bind("<Map>", lambda _e: self._set_win32_icon(), add="+")

    def _set_win32_icon(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                            ctypes.c_uint, wintypes.HANDLE]
            hwnd = int(self.winfo_id())
            # The Tk window is a child; the taskbar cares about its
            # top-level ancestor.
            root = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT

            IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
            WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
            path = str(APP_ICON)
            for which, cx, cy in ((ICON_SMALL, 16, 16), (ICON_BIG, 32, 32)):
                h = user32.LoadImageW(None, path, IMAGE_ICON, cx, cy,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    user32.SendMessageW(root, WM_SETICON, which, h)
            log.debug("win32 window icon applied to hwnd %s", root)
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.debug("could not set win32 window icon: %s", exc)

    # --- layout -------------------------------------------------------

    def _build_ui(self) -> None:
        self.configure(fg_color=T.BG)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        self._build_header()
        self._build_stats()
        self._build_targets()
        self._build_controls()
        self._build_log()

    def _build_header(self) -> None:
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=T.XL, pady=(T.LG, T.MD))
        head.grid_columnconfigure(1, weight=1)

        title = ctk.CTkFrame(head, fg_color="transparent")
        title.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title, text="Badminton Sniper", font=(T.FAMILY, 20, "bold"),
                     text_color=T.TEXT).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title, text="Rec Center West \u00b7 UT Dallas", font=T.CAPTION,
                     text_color=T.TEXT_FAINT).grid(row=1, column=0, sticky="w")

        status = ctk.CTkFrame(head, fg_color=T.SURFACE, corner_radius=999,
                              border_width=1, border_color=T.BORDER)
        status.grid(row=0, column=1, rowspan=2, sticky="e", padx=T.MD)
        self.session_dot = ctk.CTkLabel(status, text="\u25cf", font=(T.FAMILY, 13),
                                        text_color=T.ERROR)
        self.session_dot.grid(row=0, column=0, padx=(T.MD, T.XS), pady=T.SM)
        self.account_var = ctk.StringVar()
        self.account_menu = ctk.CTkOptionMenu(
            status, variable=self.account_var, values=[""], width=196,
            command=self._on_account_selected,
            fg_color=T.SURFACE_ACTIVE, button_color=T.SURFACE_ACTIVE,
            button_hover_color=T.BORDER_HI, text_color=T.TEXT,
            dropdown_fg_color=T.SURFACE_ACTIVE, dropdown_text_color=T.TEXT,
            dropdown_hover_color=T.BORDER_HI, corner_radius=999, font=T.LABEL)
        self.account_menu.grid(row=0, column=1, padx=(0, T.SM), pady=2)

        ghost = dict(fg_color=T.GHOST, hover_color=T.GHOST_HOVER, text_color=T.TEXT_MUTED,
                     border_width=1, border_color=T.BORDER, corner_radius=T.R_SM,
                     font=T.LABEL, height=34)
        buttons = ctk.CTkFrame(head, fg_color="transparent")
        buttons.grid(row=0, column=2, rowspan=2, sticky="e")
        self.sign_in_btn = ctk.CTkButton(buttons, text="Sign in", width=84,
                                         command=self.on_sign_in, **ghost)
        self.sign_in_btn.grid(row=0, column=0, padx=T.XS)
        ctk.CTkButton(buttons, text="+ Account", width=86, command=self.on_add_account,
                      **ghost).grid(row=0, column=1, padx=T.XS)
        ctk.CTkButton(buttons, text="Refresh", width=72, command=self.on_refresh_slots,
                      **ghost).grid(row=0, column=2, padx=T.XS)
        ctk.CTkButton(buttons, text="Waiver", width=70, command=self.on_waiver,
                      **ghost).grid(row=0, column=3, padx=T.XS)
        ctk.CTkButton(buttons, text="Reservations", width=100,
                      command=self.on_reservations, **ghost).grid(
            row=0, column=4, padx=T.XS)

    def _build_stats(self) -> None:
        stats = ctk.CTkFrame(self, fg_color="transparent")
        stats.grid(row=1, column=0, sticky="ew", padx=T.XL)
        for col in range(4):
            stats.grid_columnconfigure(col, weight=1, uniform="stat")

        self.tile_next = StatTile(stats, "next fire", wide=True)
        self.tile_next.grid(row=0, column=0, sticky="nsew", padx=(0, T.SM))
        self.tile_courts = StatTile(stats, "courts held")
        self.tile_courts.grid(row=0, column=1, sticky="nsew", padx=T.SM)
        self.tile_slots = StatTile(stats, "slots loaded")
        self.tile_slots.grid(row=0, column=2, sticky="nsew", padx=T.SM)
        self.tile_clock = StatTile(stats, "server clock")
        self.tile_clock.grid(row=0, column=3, sticky="nsew", padx=(T.SM, 0))

        self.tile_next.set("--:--", sub="nothing armed")
        self.tile_courts.set("?", sub="sign in to see")
        self.tile_courts.caption.configure(text=T.eyebrow("reservations"))
        self.tile_slots.set("0", sub="not loaded")
        self.tile_clock.set("--", sub="not synced")

        self.consent_label = ctk.CTkLabel(self, text="", anchor="w", font=T.LABEL)
        self.consent_label.grid(row=2, column=0, sticky="w", padx=T.XL, pady=(T.MD, 0))

    def _build_targets(self) -> None:
        wrap = card(self)
        wrap.grid(row=3, column=0, sticky="ew", padx=T.XL, pady=(T.MD, 0))
        wrap.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(wrap, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=T.LG, pady=(T.LG, T.SM))
        bar.grid_columnconfigure(1, weight=1)
        eyebrow_label(bar, "targets").grid(row=0, column=0, sticky="w")
        # Auto: say what you want, the planner picks accounts and courts.
        # Manual: each account's own three rows, as before.
        self.mode_var = ctk.StringVar(value="Auto plan")
        self.mode_switch = ctk.CTkSegmentedButton(
            bar, values=["Auto plan", "Manual"], variable=self.mode_var,
            command=self._on_mode_switched, font=T.LABEL, height=28,
            # One text colour serves both states, so the selected pill
            # must contrast with white text: a light grey, not PRIMARY.
            fg_color=T.SURFACE_ACTIVE, selected_color=T.BORDER_HI,
            selected_hover_color=T.BORDER_HI, unselected_color=T.SURFACE_ACTIVE,
            unselected_hover_color=T.BORDER, text_color=T.TEXT,
            corner_radius=T.R_SM)
        self.mode_switch.grid(row=0, column=1, sticky="w", padx=(T.MD, 0))
        # Names which account these rows belong to; the header selector
        # switches it.
        self.targets_hint = ctk.CTkLabel(bar, text="", font=T.CAPTION,
                                         text_color=T.TEXT_FAINT)
        self.targets_hint.grid(row=0, column=2, sticky="e")

        # --- manual: the per-account rows ---
        self.manual_holder = holder = ctk.CTkFrame(wrap, fg_color="transparent")
        holder.grid(row=1, column=0, sticky="ew", padx=T.LG)
        holder.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(holder, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, T.XS))
        for col, (text, weight) in enumerate([
                ("", 0), ("Date", 3), ("Start", 3), ("Court", 2),
                ("Fallback", 2), ("Status", 4)]):
            header.grid_columnconfigure(col, weight=weight)
            if text:
                ctk.CTkLabel(header, text=text, anchor="w", font=T.CAPTION,
                             text_color=T.TEXT_FAINT).grid(row=0, column=col,
                                                           sticky="w", padx=T.SM)
        # Match the row checkbox gutter so the columns actually line up.
        header.grid_columnconfigure(0, minsize=48)

        # Fixed three rows: each account has its own three, and only the
        # active account's are shown.
        self.rows = [TargetRow(holder, i, self) for i in range(C.MAX_RESERVATIONS)]

        # --- auto: the wish list ---
        self.auto_holder = auto = ctk.CTkFrame(wrap, fg_color="transparent")
        auto.grid(row=1, column=0, sticky="ew", padx=T.LG)
        auto.grid_columnconfigure(0, weight=1)
        aheader = ctk.CTkFrame(auto, fg_color="transparent")
        aheader.grid(row=0, column=0, sticky="ew", pady=(0, T.XS))
        # Same fixed widths as the row pickers (plus their padding), so
        # the captions sit over their columns.
        pads = (T.MD + T.XS, 2 * T.XS, 2 * T.XS)
        for col, text in enumerate(("Date", "Start", "How many")):
            aheader.grid_columnconfigure(col, minsize=REQUEST_COL_WIDTHS[col] + pads[col])
            ctk.CTkLabel(aheader, text=text, anchor="w", font=T.CAPTION,
                         text_color=T.TEXT_FAINT).grid(row=0, column=col, sticky="w",
                                                       padx=(T.MD if col == 0 else T.XS, 0))
        aheader.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(aheader, text="Plan / status", anchor="w", font=T.CAPTION,
                     text_color=T.TEXT_FAINT).grid(row=0, column=3, sticky="w", padx=T.MD)
        # Scrolls once there are more rows than fit; sized to its contents
        # below that so a short list does not leave a hole.
        self.request_holder = ctk.CTkScrollableFrame(
            auto, fg_color="transparent", corner_radius=0,
            height=REQUEST_ROW_H, scrollbar_button_color=T.BORDER_HI,
            scrollbar_button_hover_color=T.TEXT_FAINT)
        self.request_holder.grid(row=1, column=0, sticky="ew")
        self.request_holder.grid_columnconfigure(0, weight=1)
        self.add_request_btn = ctk.CTkButton(
            auto, text="+ Add a time", width=110, height=30, command=self.on_add_request,
            fg_color=T.GHOST, hover_color=T.GHOST_HOVER, text_color=T.TEXT_MUTED,
            border_width=1, border_color=T.BORDER, corner_radius=T.R_SM, font=T.LABEL)
        self.add_request_btn.grid(row=2, column=0, sticky="w", pady=(T.XS, 0))

        # wraplength: a WAITING line with its reason runs long; wrap rather
        # than clip it at the card's edge.
        self.plan_label = ctk.CTkLabel(wrap, text="", anchor="w", justify="left",
                                       font=T.CAPTION, text_color=T.TEXT_MUTED,
                                       wraplength=1000)
        self.plan_label.grid(row=2, column=0, sticky="ew", padx=T.LG, pady=(T.SM, T.LG))

    # --- auto plan: the request list ---------------------------------

    @property
    def auto_mode(self) -> bool:
        return self.config_data.mode == "auto"

    def _apply_mode(self) -> None:
        """Show whichever editor the mode calls for."""
        if self.auto_mode:
            self.mode_var.set("Auto plan")
            self.manual_holder.grid_remove()
            self.auto_holder.grid()
        else:
            self.mode_var.set("Manual")
            self.auto_holder.grid_remove()
            self.manual_holder.grid()
            self._load_active_targets()
        self._update_targets_hint()
        self.refresh_plan()

    def _on_mode_switched(self, chosen: str) -> None:
        if self._armed:
            self.emit("warn", "Disarm before switching modes.")
            self.mode_var.set("Auto plan" if self.auto_mode else "Manual")
            return
        self._capture_active_targets()
        self._capture_requests()
        self.config_data.mode = "auto" if chosen == "Auto plan" else "manual"
        self._save_quiet()
        self._apply_mode()

    def _load_requests(self) -> None:
        for row in self.request_rows:
            row.destroy()
        self.request_rows = []
        for r in self.config_data.requests[:MAX_REQUEST_ROWS]:
            self._add_request_row().apply(r)
        if not self.request_rows:
            self._add_request_row()

    def _capture_requests(self) -> None:
        self.config_data.requests = [
            r for r in (row.to_request() for row in self.request_rows) if r]

    def _requests(self) -> list[Request]:
        return [r for r in (row.to_request() for row in self.request_rows) if r]

    def _add_request_row(self) -> RequestRow:
        row = RequestRow(self.request_holder, len(self.request_rows), self)
        self.request_rows.append(row)
        dates = sorted({s.slot_date for s in self.slots if s.slot_date})
        row.set_dates([d.isoformat() for d in dates])
        row.set_times(self._times_for(row.selected_date()))
        self._fit_request_list()
        return row

    def _fit_request_list(self) -> None:
        """Grow the list up to REQUEST_ROWS_VISIBLE rows, then scroll."""
        n = max(1, min(len(self.request_rows), REQUEST_ROWS_VISIBLE))
        self.request_holder.configure(height=n * REQUEST_ROW_H)
        self.add_request_btn.configure(
            state="normal" if len(self.request_rows) < MAX_REQUEST_ROWS else "disabled")

    def on_add_request(self) -> None:
        if self._armed or len(self.request_rows) >= MAX_REQUEST_ROWS:
            return
        row = self._add_request_row()
        # Start from the previous row's date so adding several times on
        # one day is two clicks, not four.
        if len(self.request_rows) > 1:
            prev = self.request_rows[-2]
            row.date_var.set(prev.date_var.get())
            row.set_times(self._times_for(row.selected_date()))
        self.on_request_edited()
        # Bring the new row into view when the list is already scrolling.
        self.after(50, lambda: self.request_holder._parent_canvas.yview_moveto(1.0))

    def on_remove_request(self, row: RequestRow) -> None:
        if self._armed:
            return
        row.destroy()
        self.request_rows.remove(row)
        for i, r in enumerate(self.request_rows):
            r.frame.grid_configure(row=i + 1)
        if not self.request_rows:
            self._add_request_row()
        self._fit_request_list()
        self.on_request_edited()

    def on_request_date_changed(self, row: RequestRow) -> None:
        row.set_times(self._times_for(row.selected_date()))
        self.on_request_edited()

    def on_request_edited(self) -> None:
        """Any edit to the wish list drops the previous run's results."""
        self._request_targets = {}
        self.refresh_plan()

    def _usable_sessions(self) -> list[AccountSession]:
        """Signed-in accounts the planner may book with."""
        return [s for s in self.sessions if s.signed_in]

    def _refresh_all_held(self, *, then=None) -> None:
        """Re-read every signed-in account's held courts, in parallel."""
        sessions = self._usable_sessions()
        if not sessions:
            self._held_by_account = {}
            self.refresh_plan()
            if then:
                then()
            return

        def work():
            out: dict[str, dict[_date, set[int]]] = {}
            errors: list[str] = []

            def one(s: AccountSession):
                try:
                    out[s.account.id] = held_by_day(self._fetch_slots(s))
                except Exception as exc:  # noqa: BLE001 - reported below
                    errors.append(f"[{s.account.display}] {exc}")

            threads = [threading.Thread(target=one, args=(s,), daemon=True) for s in sessions]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            return out, errors

        def done(result):
            if result is None:
                return
            held, errors = result
            for msg in errors:
                self.emit("warn", f"could not read held courts: {msg}")
            self._held_by_account.update(held)
            self.refresh_plan()
            if then:
                then()

        self._run_bg(work, then=done, force=True)

    @staticmethod
    def _fetch_slots(session: AccountSession) -> list[Slot]:
        today = datetime.now(C.SCHOOL_TZ).date()
        return session.client.search_slots(today, today + timedelta(days=8))

    def _build_plan(self) -> Plan:
        held = {s.account.id: self._held_by_account.get(s.account.id, {})
                for s in self._usable_sessions()}
        return plan_requests(self._requests(), held)

    def _names(self) -> dict[str, str]:
        return {s.account.id: s.display for s in self.sessions}

    def _refresh_auto_plan(self) -> None:
        reqs = self._requests()
        if not reqs:
            self._plan = None
            self.plan_label.configure(text="Add a time and how many courts you want.",
                                      text_color=T.TEXT_FAINT)
            return
        plan = self._build_plan()
        self._plan = plan
        usable = self._usable_sessions()
        names = self._names()
        # Per-row: who is going for what. Rows and requests line up in
        # order, skipping rows that are incomplete. A row that still
        # shows a finished run's result is left alone until it is edited.
        idx = 0
        for i, row in enumerate(self.request_rows):
            if row.to_request() is None:
                row.set_status("")
                continue
            placed = plan.for_request(idx)
            short = idx in plan.shortfalls
            idx += 1
            if i in self._request_targets:
                continue
            if not placed:
                row.set_status("nobody can take this", T.ERROR)
            else:
                text = ", ".join(
                    f"{names.get(p.account_id, p.account_id)}→Ct{p.court}"
                    + ("(wait)" if p.waiting else "") for p in placed)
                row.set_status(text + ("  (short)" if short else ""),
                               T.WARN if short else T.TEXT_MUTED)
        if not usable:
            head = "No account is signed in - sign in at least one to plan."
            colour = T.ERROR
        elif plan.complete:
            head = (f"All {plan.wanted} court(s) can be booked across "
                    f"{len(plan.accounts_used())} account(s).")
            colour = T.OK
        else:
            head = (f"{plan.planned} of {plan.wanted} court(s) can be booked at the open; "
                    f"{plan.waiting} will WAIT for a court this account already holds to "
                    "free up (unreserve it, or let it lapse) and book then.")
            colour = T.WARN
        lines = describe(plan, names)
        self.plan_label.configure(text=head + "\n" + "\n".join("    " + ln for ln in lines),
                                  text_color=colour)

    # --- per-account target editing -----------------------------------

    def _load_active_targets(self) -> None:
        """Show the active account's saved targets in the rows."""
        targets = self.active.account.targets
        for i, row in enumerate(self.rows):
            if i < len(targets):
                row.apply(targets[i])
            else:
                row.clear()
        self._update_targets_hint()

    def _update_targets_hint(self) -> None:
        if self.auto_mode:
            self.targets_hint.configure(
                text="any signed-in account may be used · the planner picks "
                     "who books which court")
        else:
            self.targets_hint.configure(
                text=f"editing {self.active.account.display} · "
                     f"max {C.MAX_RESERVATIONS}, each a different court")

    def _capture_active_targets(self) -> None:
        """Write the visible rows back onto the active account."""
        self.active.account.targets = [t for t in (r.to_target() for r in self.rows) if t]

    def _build_controls(self) -> None:
        wrap = card(self)
        wrap.grid(row=4, column=0, sticky="ew", padx=T.XL, pady=T.MD)
        wrap.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(wrap, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=T.LG, pady=T.MD)
        eyebrow_label(left, "timing").grid(row=0, column=0, columnspan=3,
                                           sticky="w", pady=(0, T.SM))

        entry_kw = dict(fg_color=T.SURFACE_HI, border_color=T.BORDER, border_width=1,
                        text_color=T.TEXT, corner_radius=T.R_SM, font=T.BODY,
                        width=76, height=32, justify="center")
        fire = self.config_data.fire
        self.offset_var = ctk.StringVar(value=f"{fire.offset_s:.2f}")
        self.retry_var = ctk.StringVar(value=f"{fire.retry_interval_s:.2f}")
        self.window_var = ctk.StringVar(value=f"{fire.retry_window_s:.0f}")
        for col, (label, var) in enumerate([
                ("offset", self.offset_var), ("retry every", self.retry_var),
                ("keep trying", self.window_var)]):
            ctk.CTkLabel(left, text=label, font=T.CAPTION,
                         text_color=T.TEXT_FAINT).grid(row=1, column=col,
                                                       sticky="w", padx=(0, T.MD))
            ctk.CTkEntry(left, textvariable=var, **entry_kw).grid(
                row=2, column=col, sticky="w", padx=(0, T.MD))
            ctk.CTkLabel(left, text="seconds", font=T.CAPTION,
                         text_color=T.TEXT_FAINT).grid(row=3, column=col,
                                                       sticky="w", padx=(0, T.MD))

        hint = ("Offset is measured from the moment registration opens.\n"
                "    -1.00    start trying 1s early   (recommended)\n"
                "     0.00    fire exactly on the open\n"
                "    +2.00    deliberately book 2s late\n"
                "Early is insurance: server clocks disagree by up to a second, and an\n"
                "early shot is simply rejected and retried, while a late one can lose\n"
                "the court to someone faster.")
        ctk.CTkLabel(wrap, text=hint, justify="left", anchor="w", font=T.CAPTION,
                     text_color=T.TEXT_FAINT).grid(row=0, column=1, sticky="w", padx=T.LG)

        actions = ctk.CTkFrame(wrap, fg_color="transparent")
        actions.grid(row=0, column=2, sticky="e", padx=T.LG, pady=T.MD)
        self.dry_btn = ctk.CTkButton(actions, text="Dry run", width=110, height=38,
                                     command=self.on_dry_run, font=T.LABEL,
                                     fg_color=T.GHOST, hover_color=T.GHOST_HOVER,
                                     text_color=T.TEXT_MUTED, border_width=1,
                                     border_color=T.BORDER, corner_radius=T.R_SM)
        self.dry_btn.grid(row=0, column=0, padx=(0, T.SM))
        self.arm_btn = ctk.CTkButton(actions, text="Arm", width=150, height=38,
                                     command=self.on_arm, font=(T.FAMILY, 14, "bold"),
                                     fg_color=T.PRIMARY, hover_color=T.PRIMARY_HOVER,
                                     text_color=T.PRIMARY_TEXT, corner_radius=T.R_SM)
        self.arm_btn.grid(row=0, column=1)

    def _build_log(self) -> None:
        wrap = card(self)
        wrap.grid(row=5, column=0, sticky="nsew", padx=T.XL, pady=(0, T.LG))
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(1, weight=1, minsize=150)
        eyebrow_label(wrap, "activity").grid(row=0, column=0, sticky="w",
                                             padx=T.LG, pady=(T.MD, T.SM))
        self.log_box = ctk.CTkTextbox(wrap, wrap="word", activate_scrollbars=True,
                                      fg_color=T.BG, text_color=T.TEXT_MUTED,
                                      font=T.LOG, corner_radius=T.R_MD,
                                      border_width=1, border_color=T.BORDER)
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=T.LG, pady=(0, T.MD))
        self.log_box.configure(state="disabled")

    # --- config -------------------------------------------------------

    def _collect_settings(self) -> None:
        def num(var, fallback, lo, hi):
            try:
                return min(hi, max(lo, float(var.get())))
            except (TypeError, ValueError):
                return fallback

        fire = self.config_data.fire
        fire.offset_s = num(self.offset_var, fire.offset_s, -30.0, 30.0)
        fire.retry_interval_s = num(self.retry_var, fire.retry_interval_s, 0.05, 5.0)
        fire.retry_window_s = num(self.window_var, fire.retry_window_s, 1.0, 300.0)
        self.offset_var.set(f"{fire.offset_s:.2f}")
        self.retry_var.set(f"{fire.retry_interval_s:.2f}")
        self.window_var.set(f"{fire.retry_window_s:.0f}")

    def _save(self) -> None:
        self._collect_settings()
        self._capture_active_targets()
        self._capture_requests()
        try:
            self.config_data.save()
        except OSError as exc:
            self.emit("warn", f"Could not save settings: {exc}")

    # --- background helpers -------------------------------------------

    #: emit() levels mapped onto logging levels for the file log.
    _LOG_LEVELS = {"error": logging.ERROR, "warn": logging.WARNING}

    def emit(self, level: str, message: str) -> None:
        """Thread-safe log line, to the UI and to the file.

        The file copy matters because the app normally runs under
        pythonw with no console: without it, the activity trail would
        live only in the window and be lost the moment it closes.
        """
        log.log(self._LOG_LEVELS.get(level, logging.INFO), message)
        self._events.put((level, message))

    def _run_bg(self, fn, *, then=None, force: bool = False) -> None:
        """Run a blocking call off the UI thread.

        The completion callback is handed back through a queue rather
        than tkinter's after(), which may only be called from the thread
        running the main loop.

        The `_busy` guard prevents double-submitting the SAME kind of
        action (e.g. two sign-in clicks). It used to drop *any* second
        action while one was in flight, which silently swallowed things
        like Unreserve if a slot refresh happened to be running. Pass
        force=True for independent, safe actions so they always run.
        """
        if self._busy and not force:
            self.emit("warn", "Still working on the previous action.")
            return
        if not force:
            self._busy = True

        def worker():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - surface everything
                log.exception("background task failed")
                self.emit("error", str(exc))
                result = None
            finally:
                if not force:
                    self._busy = False
            if then is not None:
                self._callbacks.put((then, result))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self) -> None:
        """Drain queued log lines and completion callbacks, on the UI thread."""
        drained = 0
        while drained < 50:
            try:
                level, message = self._events.get_nowait()
            except queue.Empty:
                break
            drained += 1
            stamp = datetime.now(C.SCHOOL_TZ).strftime("%H:%M:%S")
            self.log_box.configure(state="normal")
            tag = f"lvl_{level}"
            if tag not in self._log_tags:
                self.log_box.tag_config(tag, foreground=T.LEVEL_COLOUR.get(level, T.TEXT_MUTED))
                self._log_tags.add(tag)
            self.log_box.insert("end", f"{stamp}  {message}\n", tag)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        while True:
            try:
                callback, result = self._callbacks.get_nowait()
            except queue.Empty:
                break
            try:
                callback(result)
            except Exception:  # noqa: BLE001 - a bad callback must not kill the pump
                log.exception("UI callback failed")

        if self._quitting:
            return
        self._tick()
        self._tick_n += 1
        if self._tick_n % 5 == 0:          # about once a second
            self._poll_commands()
            self._write_status()
        self._pump_after = self.after(200, self._pump)

    # --- remote control: config/commands/ and config/status.json -------
    #
    # The window can be driven without touching it: drop an empty file
    # named arm, dry-run or disarm into config/commands/ and it is acted
    # on within a second (and deleted). status.json mirrors what the
    # window shows, so a remote helper can confirm what happened.

    COMMANDS = ("arm", "dry-run", "disarm")

    @property
    def _commands_dir(self):
        return config_module.CONFIG_DIR / "commands"

    def _clear_commands(self) -> None:
        try:
            for name in self.COMMANDS:
                (self._commands_dir / name).unlink(missing_ok=True)
        except OSError:
            pass

    def _poll_commands(self) -> None:
        d = self._commands_dir
        if not d.is_dir():
            return
        for name in self.COMMANDS:
            path = d / name
            if not path.exists():
                continue
            try:
                path.unlink()
            except OSError:
                continue
            self.emit("info", f"remote command: {name}")
            if name == "arm" and not self._armed:
                self.on_arm()
            elif name == "dry-run" and not self._armed:
                self.on_dry_run()
            elif name == "disarm" and self._armed:
                self.on_arm()
            else:
                self.emit("warn", f"remote command '{name}' ignored: "
                                  + ("already armed" if self._armed else "not armed"))

    def _write_status(self) -> None:
        import json
        targets = []
        for sniper in self.snipers:
            for t in sniper.targets:
                targets.append({
                    "account": self._names().get(t.account_id or "", t.account_id),
                    "label": t.label, "state": t.state.value, "message": t.message,
                    "won_court": t.won_court,
                    "fire_at": t.fire_at.isoformat() if t.fire_at else None,
                })
        status = {
            "at": datetime.now(C.SCHOOL_TZ).isoformat(timespec="seconds"),
            "armed": self._armed,
            "dry_run": any(s.dry_run for s in self.snipers),
            "mode": self.config_data.mode,
            "accounts": [{"id": s.account.id, "display": s.display,
                          "signed_in": s.signed_in} for s in self.sessions],
            "targets": targets,
        }
        path = config_module.CONFIG_DIR / "status.json"
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def _tick(self) -> None:
        """Drive the countdown, the clock tile, and per-row status."""
        if self._armed and self.snipers and not any(s.running for s in self.snipers):
            self._on_run_finished()
            return
        if not self.snipers or not any(s.running for s in self.snipers):
            self.tile_next.set("--:--", sub="nothing armed")
            return

        clock = next((s.clock for s in self.snipers if s.clock is not None), None)
        if clock is not None:
            self.tile_clock.set(f"{clock.uncertainty * 1000:.0f}ms",
                                sub=f"rtt {clock.rtt_ms:.0f}ms")

        # Soonest fire across every account.
        best = None
        for sniper in self.snipers:
            if sniper.clock is None:
                continue
            for t in sniper.targets:
                if t.fire_at and t.state in (TargetState.ARMED, TargetState.FIRING):
                    remaining = sniper.clock.seconds_until(t.fire_at)
                    if best is None or remaining < best[0]:
                        best = (remaining, t)
        if best is not None:
            remaining, nxt = best
            colour = T.TEXT if remaining > 60 else T.WARN
            self.tile_next.set(format_countdown(remaining), sub=nxt.label, colour=colour)
        else:
            self.tile_next.set("--:--", sub="nothing pending")

        # Per-row status from the armed Target each row owns.
        for row in self.rows:
            target = self._row_targets.get(id(row))
            if target is None:
                continue
            label = target.state.value
            if target.message:
                label = f"{target.state.value} - {target.message}"
            row.set_status(label, target.state)
        # Auto mode: each request row summarises the targets serving it.
        names = self._names()
        for i, row in enumerate(self.request_rows):
            targets = self._request_targets.get(i)
            if targets:
                row.set_status(*self._request_status(targets, names))

    @staticmethod
    def _request_status(targets: list[Target], names: dict[str, str]) -> tuple[str, str]:
        """One line + colour summarising a request's targets."""
        parts, colour = [], T.TEXT_MUTED
        for t in targets:
            who = names.get(t.account_id or "", t.account_id or "?")
            if t.state is TargetState.WON:
                parts.append(f"{who}: booked Ct{t.won_court}")
            elif t.state is TargetState.WAITLISTED:
                parts.append(f"{who}: WAITLIST Ct{t.preferred_court}")
            elif t.state is TargetState.WAITING:
                parts.append(f"{who}: waiting for Ct{t.preferred_court} to free")
            elif t.state in (TargetState.LOST, TargetState.ERROR, TargetState.SKIPPED):
                parts.append(f"{who}: {t.state.value.lower()}")
            elif t.state is TargetState.SIMULATED:
                parts.append(f"{who}: dry run Ct{t.preferred_court}")
            else:
                parts.append(f"{who}: {t.state.value.lower()} Ct{t.preferred_court}")
        states = {t.state for t in targets}
        if states <= {TargetState.WON}:
            colour = T.OK
        elif states & {TargetState.WON, TargetState.WAITLISTED, TargetState.WAITING}:
            colour = T.WARN
        elif states & {TargetState.LOST, TargetState.ERROR}:
            colour = T.ERROR
        elif TargetState.FIRING in states:
            colour = T.TEXT
        return ", ".join(parts), colour

    # --- boot ---------------------------------------------------------

    def _boot(self) -> None:
        self.emit("info", "Checking for saved sessions...")

        def work():
            for s in self.sessions:
                # Fast path: cached cookies still valid. Otherwise, if
                # this account was ever signed in, try a silent SSO
                # round trip through its browser profile before giving
                # up -- that is what keeps "sign in once" true across
                # restarts, with no window and no password.
                if s.try_cached_session():
                    s.start_keeper()
                elif s.has_prior_session:
                    self.emit("info", f"[{s.account.display}] saved login expired; "
                                      "trying to reconnect silently...")
                    if s.silent_reauth():
                        self.emit("success", f"[{s.account.display}] reconnected "
                                             "automatically.")
                        s.start_keeper()
            return None

        self._run_bg(work, then=lambda _r: self._after_boot())

    def _after_boot(self) -> None:
        signed = [s for s in self.sessions if s.signed_in]
        if signed:
            # Land on a signed-in account so the tiles have real data --
            # but never override an account the user already picked while
            # this async check was still running.
            if not self._user_selected_account:
                self.active_idx = self.sessions.index(signed[0])
            for s in signed:
                self.emit("success", f"[{s.display}] signed in from the saved session.")
        else:
            self.emit("warn", "No saved session. Pick an account and click Sign in - "
                              "you log in yourself, and it is saved for next time.")
        self._refresh_account_ui()
        self._load_active_targets()
        self.on_refresh_slots()
        self._refresh_all_held()

    def _account_label(self, s: AccountSession) -> str:
        dot = "● " if s.signed_in else "○ "
        who = s.display if s.signed_in else (s.account.display + " (signed out)")
        return dot + who

    def _refresh_account_ui(self) -> None:
        """Sync the account selector and status dot to current state."""
        labels = [self._account_label(s) for s in self.sessions]
        self.account_menu.configure(values=labels)
        self.account_var.set(labels[self.active_idx])
        self.session_dot.configure(text_color=T.OK if self.active.signed_in else T.ERROR)
        self.sign_in_btn.configure(
            text="Re-sign in" if self.active.signed_in else "Sign in")
        self._update_consent_label()

    def _on_account_selected(self, chosen: str) -> None:
        if self._armed:
            self.emit("warn", "Disarm before switching accounts.")
            self._refresh_account_ui()   # snap the menu back
            return
        self._user_selected_account = True
        labels = [self._account_label(s) for s in self.sessions]
        if chosen not in labels or labels.index(chosen) == self.active_idx:
            return
        # Save the account we are leaving, then show the one we picked.
        self._capture_active_targets()
        self.active_idx = labels.index(chosen)
        self._refresh_account_ui()
        self._load_active_targets()
        self._populate_choices()
        self.refresh_plan()
        self.on_refresh_slots()

    def on_add_account(self) -> None:
        if self._armed:
            self.emit("warn", "Disarm before adding an account.")
            return
        self._user_selected_account = True
        self._capture_active_targets()
        acc = self.config_data.add_account()
        self.sessions.append(self._make_session(acc))
        self.active_idx = len(self.sessions) - 1
        self._save_quiet()
        self._refresh_account_ui()
        self._load_active_targets()
        self._populate_choices()
        self.emit("info", "Added an account. It is now selected - click Sign in to log "
                          "into it with that person's NetID, then set its targets.")

    # --- actions ------------------------------------------------------

    def on_sign_in(self) -> None:
        session = self.active
        self.emit("info", f"[{session.account.display}] opening a browser to sign in...")

        def work():
            return session.sign_in_interactive(
                progress=lambda m: self.emit("info", session._tag(m)))

        def done(result):
            if result is None:
                return
            self.emit("success" if result.ok else "error", session._tag(result.detail))
            self._refresh_account_ui()
            if result.ok:
                session.keeper.resume()
                session.start_keeper()
                self.on_refresh_slots()
                self._refresh_all_held()

        self._run_bg(work, then=done)

    def _fire_imminent(self, within_s: float = 120.0) -> bool:
        """True when any armed shot is close enough not to disturb."""
        soonest = None
        for sniper in self.snipers:
            if not sniper.running or sniper.clock is None:
                continue
            pending = [t for t in sniper.targets
                       if t.fire_at and t.state in (TargetState.ARMED, TargetState.FIRING)]
            for t in pending:
                s = sniper.clock.seconds_until(t.fire_at)
                soonest = s if soonest is None else min(soonest, s)
        return soonest is not None and soonest < within_s

    def _waiver_required(self) -> bool:
        """Did the active account's last form read show a waiver?"""
        return bool(self.active.account.requirements_seen.get("waiver_shown"))

    def _update_consent_label(self) -> None:
        acc = self.active.account
        seen = acc.requirements_seen
        if not seen:
            self.consent_label.configure(
                text=f"[{acc.display}] requirements not checked \u2014 open Waiver.",
                text_color=T.TEXT_FAINT)
            return
        if self._waiver_required() and not acc.consent.accept_terms:
            self.consent_label.configure(
                text=f"[{acc.display}] waiver required and not accepted \u2014 "
                     "open Waiver before arming.",
                text_color=T.ERROR)
        elif self._waiver_required():
            self.consent_label.configure(text=f"[{acc.display}] waiver accepted.",
                                         text_color=T.OK)
        else:
            self.consent_label.configure(text=f"[{acc.display}] no waiver required.",
                                         text_color=T.OK)

    def on_waiver(self) -> None:
        session = self.active
        if not session.signed_in:
            self.emit("error", f"[{session.account.display}] sign in first - the booking "
                               "form can only be read with an authenticated session.")
            return

        def work() -> Requirements:
            # Not just the first open slot: IMLeagues refuses to open the
            # form for a court the account already holds (code 205), and
            # for a full one. Picking blindly meant the waiver dialog
            # died on a court that happened to be unavailable, even
            # though other courts were perfectly readable.
            by_day = held_by_day(self.slots)
            held = {c for cs in by_day.values() for c in cs}
            candidates = [s for s in self.slots
                          if s.status_code == C.STATUS_OPEN and not s.is_full
                          and s.court not in by_day.get(s.slot_date, set())]

            last = ""
            for slot in candidates[:4]:
                self.emit("info", f"[{session.account.display}] reading the booking form "
                                  f"for event {slot.event_id}...")
                try:
                    vm = session.client.init_register(slot.event_id)
                    # Keep it: at fire time the target slot has not opened
                    # yet and Initialize refuses it (code 147), so a saved
                    # model is the only way to build the booking.
                    session.remember_form(vm)
                    return read_requirements(vm)
                except RuntimeError as exc:
                    last = str(exc)
                    log.info("Initialize refused event %s (%s); trying another",
                             slot.event_id, last)

            # Nothing could be read live -- every court is held, or none
            # is open right now. The waiver/booking form is account-level,
            # so a form saved from any earlier read still describes it
            # exactly. This is what lets you accept the waiver even while
            # you already hold all three courts (e.g. to free one and
            # rebook a different day).
            if session.account.form_model:
                self.emit("info", f"[{session.account.display}] no court is open to read "
                                  "a fresh form; showing the one saved earlier.")
                return read_requirements(session.account.form_model)

            raise RuntimeError(
                "The booking form can't be read yet, and none is saved. "
                + (f"This account already holds court(s) {sorted(held)}, so IMLeagues "
                   "opens no form for it. Unreserve one from Reservations, then click "
                   "Waiver. " if held else
                   "Click Waiver once while any court is open; it is saved after that. ")
                + (f"({last})" if last else ""))

        def done(reqs) -> None:
            if reqs is None:
                return
            session.account.requirements_seen = {
                "waiver_shown": reqs.waiver_shown,
                "needs_signature": reqs.needs_signature,
                "needs_sid": reqs.needs_sid,
                "needs_phone": reqs.needs_phone,
                "needs_payment": reqs.needs_payment,
                "recaptcha": reqs.recaptcha,
                "question_count": len(reqs.waiver_questions),
            }
            if reqs.recaptcha:
                self.emit("error", "This network now requires a reCAPTCHA on booking. "
                                   "Unattended booking is not possible.")
            WaiverDialog(self, reqs, session.account.consent, self._on_consent_saved)

        self._run_bg(work, then=done)

    def _on_consent_saved(self) -> None:
        self._update_consent_label()
        self._save_quiet()
        self.emit("success", f"[{self.active.account.display}] booking requirements saved.")

    def on_refresh_slots(self) -> None:
        session = self.active

        def work():
            today = datetime.now(C.SCHOOL_TZ).date()
            return session.client.search_slots(today, today + timedelta(days=8))

        def done(slots):
            if slots is None:
                return
            self.slots = slots
            self._populate_choices()
            by_day = held_by_day(slots)
            days = len({s.slot_date for s in slots if s.slot_date})
            self.tile_slots.set(str(len(slots)), sub=f"over {days} days")
            if session.signed_in:
                self._held_by_account[session.account.id] = by_day
                total = sum(len(cs) for cs in by_day.values())
                # Per day, because that is how the limit works.
                which = " · ".join(
                    f"{d:%a} Ct{','.join(str(c) for c in sorted(cs))}"
                    for d, cs in sorted(by_day.items())) or "nothing held"
                full_day = any(len(cs) >= C.MAX_RESERVATIONS for cs in by_day.values())
                self.tile_courts.set(
                    str(total), sub=f"{session.account.display}: {which}",
                    colour=T.WARN if full_day else T.TEXT)
            else:
                self.tile_courts.set("?", sub="sign in to see")
            self.emit("info", f"Calendar refreshed: {len(slots)} slots over the next 8 days.")
            self.refresh_plan()

        self._run_bg(work, then=done)

    def _populate_choices(self) -> None:
        dates = sorted({s.slot_date for s in self.slots if s.slot_date})
        date_strs = [d.isoformat() for d in dates]
        for row in self.rows + self.request_rows:
            # Dates first: a saved target whose day has rolled off the
            # calendar gets snapped to a real one, and only then can we
            # know which times to offer. Computing times against the old
            # date left a valid date paired with an empty time.
            row.set_dates(date_strs)
            row.set_times(self._times_for(row.selected_date()))

    def _times_for(self, day: _date | None) -> list[str]:
        if day is None:
            return []
        seen: dict[_time, str] = {}
        for slot in self.slots:
            if slot.slot_date == day and slot.slot_time is not None:
                seen[slot.slot_time] = slot.time_label
        return [seen[k] for k in sorted(seen)]

    def on_date_changed(self, row: TargetRow) -> None:
        times = self._times_for(row.selected_date())
        row.time_menu.configure(values=times or [""])
        if row.time_var.get() not in times:
            row.time_var.set(times[0] if times else "")
        self.refresh_plan()

    def refresh_plan(self) -> None:
        """Show what each signed-in account will try, before arming."""
        if self.auto_mode:
            self._refresh_auto_plan()
            return
        # The visible rows belong to the active account; make sure they
        # are captured so the plan reflects the latest edits.
        self._capture_active_targets()

        lines = []
        any_target = False
        for session in self.sessions:
            acc = session.account
            if not acc.targets:
                continue
            any_target = True
            head = session.display if session.signed_in \
                else f"{session.display} (not signed in)"
            lines.append(head + ":")
            projected: set[int] = set()
            for t in acc.targets:
                ordered = [t.preferred_court] + [c for c in C.COURTS
                                                 if c != t.preferred_court]
                order = [c for c in ordered if c not in projected]
                if not t.allow_fallback:
                    order = [t.preferred_court] if t.preferred_court not in projected else []
                at_time = slots_for(self.slots, t.date, t.start_time)
                opens = next((s.opens for s in at_time.values() if s.opens), None)
                when = opens.strftime("%a %b %d, %I:%M %p") if opens else "opens unknown"
                if not order:
                    lines.append(f"    {t.label}  ->  SKIP (no court left)")
                else:
                    lines.append(f"    {t.label}  ->  try {order}, {when}")
                    projected.add(order[0])

        if not any_target:
            self.plan_label.configure(text="No targets on any account yet.",
                                      text_color=T.TEXT_FAINT)
        else:
            self.plan_label.configure(text="\n".join(lines), text_color=T.TEXT_MUTED)

    def _accounts_to_fire(self):
        """(session, targets) for each signed-in account that has targets,
        or None if something blocks arming."""
        self._capture_active_targets()
        ready = []
        for session in self.sessions:
            acc = session.account
            if not acc.targets:
                continue
            if not session.signed_in:
                self.emit("error", f"[{acc.display}] has targets but is not signed in. "
                                   "Select it and sign in, or clear its targets.")
                return None
            if acc.requirements_seen.get("waiver_shown") and not acc.consent.accept_terms:
                self.emit("error", f"[{acc.display}] has not accepted its waiver. "
                                   "Select it and click Waiver first.")
                return None
            ready.append((session, list(acc.targets)))
        if not ready:
            self.emit("warn", "Nothing to do - add targets and sign in.")
            return None
        return ready

    # --- reservations / unreserve ------------------------------------

    def _fetch_reservations(self, session: AccountSession) -> list[dict]:
        """This account's held reservations, with member id + waitlist."""
        today = datetime.now(C.SCHOOL_TZ).date()
        slots = session.client.search_slots(today, today + timedelta(days=8))
        out = []
        for s in slots:
            if not s.is_registered:
                continue
            info = session.client.reservation_info(s.event_id)
            if not info.get("event_member_id"):
                continue
            out.append({
                "label": f"{s.slot_date:%a %b %d} {s.time_label} · Court {s.court}",
                "event_id": s.event_id,
                "event_member_id": info["event_member_id"],
                "waitlisted": info["waitlisted"],
            })
        return out

    def on_reservations(self) -> None:
        session = self.active
        if not session.signed_in:
            self.emit("error", f"[{session.account.display}] sign in first.")
            return
        self.emit("info", f"[{session.account.display}] loading reservations...")

        def done(reservations):
            if reservations is None:
                return
            dialog = ReservationsDialog(
                self, session.account.display, reservations,
                on_unreserve=lambda r, dlg: self._unreserve(session, r, dlg))
            self._reservations_dialog = dialog

        self._run_bg(lambda: self._fetch_reservations(session), then=done, force=True)

    def _unreserve(self, session: AccountSession, reservation: dict, dialog) -> None:
        def work():
            return session.client.unregister(reservation["event_member_id"])

        def done(res):
            ok, msg = res if res else (False, "no response")
            try:
                dialog.mark_result(reservation, ok, msg)
            except Exception:  # noqa: BLE001 - dialog may be closed
                pass
            if ok:
                self.emit("success", f"[{session.account.display}] unreserved "
                                     f"{reservation['label']}.")
                self.on_refresh_slots()
            else:
                self.emit("error", f"[{session.account.display}] could not unreserve "
                                   f"{reservation['label']}: {msg}")

        # force: never let a concurrent slot refresh swallow an Unreserve.
        self._run_bg(work, then=done, force=True)

    def on_dry_run(self) -> None:
        self._start(dry_run=True)

    def on_arm(self) -> None:
        if self._armed:
            for sniper in self.snipers:
                sniper.disarm()
            self.snipers = []
            self._set_armed(False)
            self.emit("warn", "Disarmed.")
            return
        self._start(dry_run=False)

    def _on_run_finished(self) -> None:
        """Clean up once every sniper has drained, reporting outcomes."""
        snipers, self.snipers = self.snipers, []
        self._set_armed(False)

        all_targets = [t for s in snipers for t in s.targets]
        won = [t for t in all_targets if t.state is TargetState.WON]
        waitlisted = [t for t in all_targets if t.state is TargetState.WAITLISTED]
        failed = [t for t in all_targets
                  if t.state in (TargetState.LOST, TargetState.ERROR)]
        dry = any(s.dry_run for s in snipers)

        if dry:
            self.emit("info", "Dry run finished. Nothing was submitted.")
        elif won or waitlisted:
            parts = []
            if won:
                parts.append(f"booked {len(won)}: " + ", ".join(f"Ct{t.won_court}" for t in won))
            if waitlisted:
                # Per the user's request: notify, do not act on it.
                parts.append(f"WAITLISTED {len(waitlisted)}")
            summary = "; ".join(parts)
            self.emit("success" if won else "warn", f"Done - {summary}. Disarmed.")
            self.tray.notify(summary + ("  (waitlist not confirmed)" if waitlisted else ""))
        else:
            self.emit("warn", "Finished without booking anything. Disarmed.")
            self.tray.notify("Finished without booking anything.")

        if waitlisted:
            self.emit("warn", f"{len(waitlisted)} target(s) landed on the WAITLIST, "
                              "not a confirmed court. Open Reservations to unreserve "
                              "any you don't want.")

        if not dry:
            for row in self.rows:
                t = self._row_targets.get(id(row))
                if t is not None and t.state is TargetState.WON:
                    row.enabled.set(False)
                    row.set_status("Booked \u00b7 court %d" % t.won_court, TargetState.WON)
            if won:
                self.on_refresh_slots()
            if won or waitlisted:
                # The planner must see the new holdings before the next arm.
                self._refresh_all_held()
        if failed:
            self.emit("info", f"{len(failed)} target(s) left for retry.")

    def _start(self, *, dry_run: bool) -> None:
        if self.auto_mode:
            self._start_auto(dry_run=dry_run)
            return
        ready = self._accounts_to_fire()
        if ready is None:
            return
        self._save()

        # The visible rows belong to the active account; map them to that
        # account's Targets so _tick shows live status for what's on screen.
        active_targets = self.active.account.targets
        self._row_targets = {}
        for row, t in zip(self.rows, active_targets):
            self._row_targets[id(row)] = t
        self._request_targets = {}
        # No planner in manual mode, so do its one essential job here:
        # never let two of our own accounts fight for one court.
        confine_manual_fallbacks({s.account.id: ts for s, ts in ready})
        self._launch(ready, dry_run=dry_run)

    def _plannable_sessions(self) -> list[AccountSession] | None:
        """Signed-in accounts cleared to book, or None if one is blocked.

        An account that has not accepted its waiver is not quietly left
        out: the plan the user was shown counted on it, so arming with a
        different set of accounts than the preview would be a surprise.
        """
        usable = self._usable_sessions()
        if not usable:
            self.emit("error", "No account is signed in. Sign in at least one first.")
            return None
        for s in usable:
            acc = s.account
            if not acc.requirements_seen:
                self.emit("error", f"[{acc.display}] has never had its booking form "
                                   "checked, so the planner cannot count on it. Select "
                                   "it and click Waiver once.")
                return None
            if acc.requirements_seen.get("waiver_shown") and not acc.consent.accept_terms:
                self.emit("error", f"[{acc.display}] has not accepted its waiver. Select "
                                   "it and click Waiver first.")
                return None
        return usable

    def _start_auto(self, *, dry_run: bool) -> None:
        reqs = self._requests()
        if not reqs:
            self.emit("warn", "Nothing to do - add a time and how many courts you want.")
            return
        if self._plannable_sessions() is None:
            return
        if self._arming:
            self.emit("warn", "Already arming - one moment.")
            return
        self._arming = True
        self._save()
        self.emit("info", "Checking what each account already holds...")
        self._row_targets = {}
        self.arm_btn.configure(state="disabled")

        def go():
            try:
                plan = self._build_plan()
                if not plan.placements:
                    self.emit("error", "Nothing can be booked: " + "; ".join(
                        f"{r.label}: {plan.shortfalls.get(i, '?')}"
                        for i, r in enumerate(plan.requests)))
                    return
                if not plan.complete:
                    self.emit("warn", f"{plan.planned} of {plan.wanted} court(s) can be "
                                      f"booked at the open; {plan.waiting} will wait for a "
                                      "held court to free up and book the moment it does.")
                for line in describe(plan, self._names()):
                    self.emit("info", "plan: " + line)

                by_id = {s.account.id: s for s in self.sessions}
                ready = []
                # Rows and requests line up in order over the complete rows,
                # so map request index -> row index for live status.
                row_of = [i for i, row in enumerate(self.request_rows)
                          if row.to_request() is not None]
                self._request_targets = {}
                for acc_id in plan.accounts_used():
                    targets = plan.targets_for(acc_id)
                    ready.append((by_id[acc_id], targets))
                    for t in targets:
                        self._request_targets.setdefault(
                            row_of[t.request_index], []).append(t)
                self._launch(ready, dry_run=dry_run)
            finally:
                self._arming = False
                self.arm_btn.configure(state="normal")

        # Fresh held-courts first: a court booked (or unreserved) since
        # the last look changes who can take what.
        self._refresh_all_held(then=go)

    def _launch(self, ready, *, dry_run: bool) -> None:
        """One Sniper per account, all firing in parallel at the open instant."""
        self.snipers = []
        total = 0
        for session, targets in ready:
            total += len(targets)
            sniper = Sniper(
                session.client, self.config_data.fire, session.account.consent,
                notify=(lambda lvl, msg, d=session.account.display:
                        self.emit(lvl, f"[{d}] {msg}")),
                reauth=session.reauth,
                form_model=session.account.form_model,
                on_form_model=session.remember_form)
            sniper.arm(targets, dry_run=dry_run)
            self.snipers.append(sniper)

        self._set_armed(True, dry_run=dry_run)
        mode = "DRY RUN" if dry_run else "ARMED"
        self.emit("info", f"{mode}: {total} target(s) across {len(ready)} account(s). "
                          f"Offset {self.config_data.fire.offset_s:+.2f}s, "
                          f"retry every {self.config_data.fire.retry_interval_s:.2f}s "
                          f"for {self.config_data.fire.retry_window_s:.0f}s.")

    def _set_armed(self, armed: bool, *, dry_run: bool = False) -> None:
        self._armed = armed
        self.tray.set_armed(armed and not dry_run)
        if armed:
            self.arm_btn.configure(text="Disarm",
                                   fg_color=T.DANGER if not dry_run else T.SURFACE_ACTIVE,
                                   hover_color=T.DANGER_HOVER if not dry_run else T.BORDER_HI,
                                   text_color=T.TEXT)
        else:
            self.arm_btn.configure(text="Arm", fg_color=T.PRIMARY,
                                   hover_color=T.PRIMARY_HOVER, text_color=T.PRIMARY_TEXT)
        self.dry_btn.configure(state="disabled" if armed else "normal")
        for row in self.rows:
            row.set_locked(armed)
        for rrow in self.request_rows:
            rrow.set_locked(armed)
        self.mode_switch.configure(state="disabled" if armed else "normal")
        if armed:
            self.add_request_btn.configure(state="disabled")
        else:
            self._fit_request_list()

    # --- shutdown -----------------------------------------------------

    def _on_close(self) -> None:
        """Hide to the tray rather than quitting.

        A queued booking can be hours away, and closing the window is an
        easy misclick. Quitting for real is done deliberately, from the
        tray menu.
        """
        self._save()
        if not self.tray.available:
            # No notification area to hide in; behave conventionally.
            self._really_quit()
            return

        self.withdraw()
        if self._armed:
            self.tray.notify("Still armed and running in the background.")
        elif not self._hidden_hint_shown:
            self._hidden_hint_shown = True
            self.tray.notify("Still running. Right-click the tray icon to quit.")

    def report_callback_exception(self, exc, val, tb) -> None:
        """Route Tk callback errors to the log, not a console we don't have.

        During teardown, widgets scheduled by CustomTkinter fire against
        a destroyed interpreter; that noise is expected and dropped.
        Anything else is a real bug and gets logged.
        """
        if self._quitting:
            return
        log.error("Tk callback failed", exc_info=(exc, val, tb))

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _really_quit(self) -> None:
        """Actually exit: stop the worker threads, then tear down."""
        if self._quitting:
            return
        self._quitting = True
        if self._pump_after is not None:
            try:
                self.after_cancel(self._pump_after)
            except Exception:  # noqa: BLE001 - already gone
                pass
            self._pump_after = None
        self._save()
        for sniper in self.snipers:
            if sniper.running:
                sniper.disarm()
        for s in self.sessions:
            s.stop_keeper()
        self.tray.stop()
        self.destroy()
