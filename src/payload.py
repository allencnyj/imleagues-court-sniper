"""Building the RegisterSession request body.

The shape is dictated by /spa/reservation/registerevent.js, which
assembles a joinEventVO from the registerFitnessEventVM that Initialize
returns. This mirrors that assembly exactly, including the API's
misspelled "addtionInfo" field.

Anything that amounts to agreeing to something -- a waiver, a signature,
waiver questions -- is deliberately NOT invented here. It comes from a
Consent the user filled in once, having seen the actual text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Consent:
    """What the account holder agreed to during setup.

    Stored in settings.json so the app never has to guess at, or
    silently accept, terms on the user's behalf.
    """

    accept_terms: bool = False
    digital_signature: str = ""
    #: Answers to waiver questions, positional, matching waiverQuestions.
    waiver_answers: list[Any] = field(default_factory=list)
    #: Only used when the network asks for a student ID at booking time.
    sid: str = ""
    phone: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "accept_terms": self.accept_terms,
            "digital_signature": self.digital_signature,
            "waiver_answers": list(self.waiver_answers),
            "sid": self.sid,
            "phone": self.phone,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "Consent":
        data = data or {}
        return cls(
            accept_terms=bool(data.get("accept_terms")),
            digital_signature=str(data.get("digital_signature") or ""),
            waiver_answers=list(data.get("waiver_answers") or []),
            sid=str(data.get("sid") or ""),
            phone=str(data.get("phone") or ""),
        )


@dataclass
class Requirements:
    """What a given event's registration form actually demands."""

    waiver_shown: bool = False
    waiver_text: str = ""
    waiver_questions: list[dict[str, Any]] = field(default_factory=list)
    needs_signature: bool = False
    needs_sid: bool = False
    sid_label: str = ""
    needs_phone: bool = False
    needs_payment: bool = False
    recaptcha: bool = False
    skip_input: bool = False

    @property
    def blockers(self) -> list[str]:
        """Requirements that stop a fully unattended booking."""
        out = []
        if self.recaptcha:
            out.append("this network requires a reCAPTCHA on booking")
        if self.needs_payment:
            out.append("this session requires payment")
        return out

    @property
    def consent_needed(self) -> list[str]:
        """Things the user must supply once, in the setup dialog."""
        out = []
        if self.waiver_shown:
            out.append("accept the waiver")
        if self.needs_signature:
            out.append("provide a digital signature")
        if self.waiver_questions:
            out.append(f"answer {len(self.waiver_questions)} waiver question(s)")
        if self.needs_sid:
            out.append(f"provide {self.sid_label or 'student ID'}")
        if self.needs_phone:
            out.append("provide a phone number")
        return out


def _sub(vm: dict[str, Any], key: str) -> dict[str, Any]:
    value = vm.get(key)
    return value if isinstance(value, dict) else {}


def read_requirements(vm: dict[str, Any]) -> Requirements:
    """Inspect a registerFitnessEventVM and report what it asks for."""
    waiver = _sub(vm, "waiver")
    sid = _sub(vm, "sid")
    phone = _sub(vm, "phone")
    questions = vm.get("waiverQuestions") or []
    return Requirements(
        waiver_shown=bool(waiver.get("show")),
        waiver_text=str(waiver.get("text") or waiver.get("content") or ""),
        waiver_questions=list(questions),
        needs_signature=bool(vm.get("showDigitalSignature")),
        needs_sid=bool(sid.get("show")),
        sid_label=str(sid.get("label") or ""),
        needs_phone=bool(phone.get("show")),
        needs_payment=bool(vm.get("showPaymentSelect")),
        recaptcha=bool(vm.get("enableFitnessRecaptcha")),
        skip_input=bool(vm.get("skipInput")),
    )


def build_payload(vm: dict[str, Any], event_id: int, consent: Consent,
                  *, recaptcha_token: str = "") -> dict[str, Any]:
    """Assemble the joinEventVO exactly as the site's own page does."""
    waiver = _sub(vm, "waiver")
    sid = _sub(vm, "sid")
    phone = _sub(vm, "phone")
    reminder = _sub(vm, "sessionReminder")
    questions = vm.get("waiverQuestions") or []

    # The page starts acceptTerms True when no waiver is shown, and makes
    # the user tick the box otherwise. We use the stored consent for the
    # tick rather than assuming it.
    accept = True if not waiver.get("show") else bool(consent.accept_terms)

    answers: list[Any] = [None] * len(questions)
    for i, value in enumerate(consent.waiver_answers[:len(questions)]):
        answers[i] = value

    payload: dict[str, Any] = {
        "eventId": int(event_id),
        "acceptTerms": accept,
        "waiverQuestionAnswerList": answers,
        "digitalSignature": consent.digital_signature if vm.get("showDigitalSignature") else "",
        "sid": consent.sid or str(sid.get("text") or ""),
        "addtionInfo": str(_sub(vm, "additionalInfo").get("text") or ""),
        "carrierId": phone.get("carrierId"),
        "allowSMSMessage": bool(phone.get("allowSmsMessage")),
        "allowMobileAppSMS": True,
        "allowSessionReminder": bool(reminder.get("allowReminder")),
        "remindDays": reminder.get("memberRemindDays"),
    }

    if phone.get("show"):
        payload["phone"] = consent.phone or str(phone.get("value") or "")
    if vm.get("allowDependent") and (vm.get("dependents") or []):
        # Booking for ourselves, not a dependent.
        payload["onBehalfOfMemberId"] = ""
    if vm.get("showPaymentSelect"):
        payload["paymentChoice"] = "C"
    if vm.get("enableFitnessRecaptcha") and recaptcha_token:
        payload["reCaptchaResponse"] = recaptcha_token

    return payload


def retarget(payload: dict[str, Any], event_id: int) -> dict[str, Any]:
    """Same body, different event -- used for court fallback.

    The form fields are account-level, so one template serves every court
    at a given time. Only the event changes.
    """
    clone = dict(payload)
    clone["eventId"] = int(event_id)
    return clone
