"""The phone helpline of the Raichur Grievance Tracker: a citizen talks, the assistant answers out loud.

It does not repeat the WhatsApp bot's work. A call is turned into the same messages the WhatsApp bot already
understands, and the bot's fixed texts are turned back into something that sounds right when spoken:

    caller speaks -> speech to text (the browser, or the phone provider) -> VoiceHelpline.turn()
        -> WhatsAppBot.process() (the same steps: account, type, place, check, register, track)
        -> speakable() -> text to speech (the browser, or the phone provider)

So the facts (complaint type, time limit, office, complaint ID) come from the same code as on the website and in
WhatsApp. A model only helps to understand a free-text problem, exactly as in WhatsApp (wa_ai.py), and falls back to
keywords without a key. A call cannot carry photos, so the photo step is skipped for the caller.

Two ways in, both in portal/server.py:
  * the call page in the browser (/call): the browser listens and speaks. A logged-in citizen only, so the caller is
    known, and no phone number or account with a provider is needed.
  * /api/voice/twilio: a phone call from Twilio (speech to text and text to speech are done by Twilio). The caller's
    number comes from the phone network. The request is checked with Twilio's signature.

Limits, stated plainly: English is the language that works best. Kannada (kn-IN) is used for speech in and out when the
caller's chat is in Kannada, but recognition of Kannada speech is less reliable than English, and the Kannada lines are
a first draft that a native speaker should review.
"""

import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from complaint_portal.messages import when_text
from complaint_portal.whatsapp import Inbound
from complaint_portal.workflow import utcnow

MAX_HEARD_CHARS = 600
MAX_TURNS = 60                 # a call is ended after this many things the caller said
MAX_SILENCES = 3               # and after this many turns in a row with nothing heard
CALL_FORGET_SECONDS = 3600
MAX_CALLS_KEPT = 500

GREETING = {
    "en": ("Welcome to the Raichur Grievance Tracker helpline. I am an AI assistant, not a government officer. "
           "Tell me your problem in a sentence, and I will register a complaint. "
           "Or say track, to check a complaint. Say goodbye to end the call."),
    "kn": ("ರಾಯಚೂರು ದೂರು ಟ್ರ್ಯಾಕರ್ ಹೆಲ್ಪ್‌ಲೈನ್‌ಗೆ ಸ್ವಾಗತ. ನಾನು ಎಐ ಸಹಾಯಕ, ಸರ್ಕಾರಿ ಅಧಿಕಾರಿ ಅಲ್ಲ. "
           "ನಿಮ್ಮ ಸಮಸ್ಯೆಯನ್ನು ಒಂದು ವಾಕ್ಯದಲ್ಲಿ ಹೇಳಿ, ನಾನು ದೂರು ನೋಂದಾಯಿಸುತ್ತೇನೆ. "
           "ಅಥವಾ ದೂರಿನ ಸ್ಥಿತಿ ತಿಳಿಯಲು ಟ್ರ್ಯಾಕ್ ಎಂದು ಹೇಳಿ. ಕರೆ ಮುಗಿಸಲು ಬೈ ಎಂದು ಹೇಳಿ."),
}
MENU = {
    "en": ("You can tell me the problem in your own words, and I will register a complaint. "
           "Or say track, to check a complaint. Or say my complaints. Say goodbye to end the call."),
    "kn": ("ನಿಮ್ಮ ಸಮಸ್ಯೆಯನ್ನು ನಿಮ್ಮ ಮಾತಿನಲ್ಲಿ ಹೇಳಿ, ನಾನು ದೂರು ನೋಂದಾಯಿಸುತ್ತೇನೆ. "
           "ಅಥವಾ ದೂರಿನ ಸ್ಥಿತಿ ತಿಳಿಯಲು ಟ್ರ್ಯಾಕ್ ಎಂದು ಹೇಳಿ. ಅಥವಾ ನನ್ನ ದೂರುಗಳು ಎಂದು ಹೇಳಿ. ಕರೆ ಮುಗಿಸಲು ಬೈ ಎಂದು ಹೇಳಿ."),
}
MORE = {"en": "Is there anything else? Say goodbye to end the call.",
        "kn": "ಇನ್ನೇನಾದರೂ ಬೇಕೇ? ಕರೆ ಮುಗಿಸಲು ಬೈ ಎಂದು ಹೇಳಿ."}
BYE = {"en": "Thank you for calling. Goodbye.", "kn": "ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ಬೈ."}
NOT_HEARD = {"en": "I did not hear anything. Please say that again.", "kn": "ನನಗೆ ಏನೂ ಕೇಳಿಸಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಹೇಳಿ."}
# Said after a complaint is registered: how long it should take. {days} is a whole number, {date} is like "13 Nov 2026".
DAYS_LINE = {
    "en": {"many": "It should be resolved within {days} days, by {date}.",
           "one": "It should be resolved within 1 day, by {date}."},
    "kn": {"many": "ಈ ದೂರನ್ನು {days} ದಿನಗಳ ಒಳಗೆ, {date}ರ ಒಳಗೆ ಪರಿಹರಿಸಬೇಕು.",
           "one": "ಈ ದೂರನ್ನು 1 ದಿನದ ಒಳಗೆ, {date}ರ ಒಳಗೆ ಪರಿಹರಿಸಬೇಕು."},
}
NO_ANSWER_BYE = {"en": "I still cannot hear you. Please call again. Goodbye.",
                 "kn": "ಇನ್ನೂ ನನಗೆ ನಿಮ್ಮ ಧ್ವನಿ ಕೇಳಿಸುತ್ತಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಕರೆ ಮಾಡಿ. ಬೈ."}
TOO_LONG_BYE = {"en": "This call has gone on for long. Please call again if you need more help. Goodbye.",
                "kn": "ಈ ಕರೆ ತುಂಬಾ ಹೊತ್ತು ನಡೆಯಿತು. ಇನ್ನೂ ಸಹಾಯ ಬೇಕಾದರೆ ಮತ್ತೆ ಕರೆ ಮಾಡಿ. ಬೈ."}

END_WORDS = {"goodbye", "good bye", "bye", "bye bye", "hang up", "end call", "end the call", "that is all", "thats all",
             "that's all", "ಬೈ", "ಬಾಯ್", "ಮುಗಿಯಿತು", "ಕರೆ ಮುಗಿಸಿ"}
YES_WORDS = {"yes", "yeah", "yep", "ya", "yup", "ok", "okay", "sure", "submit", "correct", "right", "haan", "ಹೌದು", "ಸರಿ", "ಹೂಂ"}
NO_WORDS = {"no", "nope", "nah", "cancel", "dont", "don't", "nahi", "ಇಲ್ಲ", "ಬೇಡ"}

UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "ate": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
         "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
UNITS.update({"ಸೊನ್ನೆ": 0, "ಒಂದು": 1, "ಎರಡು": 2, "ಮೂರು": 3, "ನಾಲ್ಕು": 4, "ಐದು": 5, "ಆರು": 6, "ಏಳು": 7, "ಎಂಟು": 8,
              "ಒಂಬತ್ತು": 9, "ಹತ್ತು": 10, "ಹನ್ನೊಂದು": 11, "ಹನ್ನೆರಡು": 12, "ಹದಿಮೂರು": 13, "ಹದಿನಾಲ್ಕು": 14, "ಹದಿನೈದು": 15,
              "ಹದಿನಾರು": 16, "ಹದಿನೇಳು": 17, "ಹದಿನೆಂಟು": 18, "ಹತ್ತೊಂಬತ್ತು": 19})   # Kannada number words
TENS = {"ಇಪ್ಪತ್ತು": 20, "ಇಪ್ಪತ್ತ": 20, "ಮೂವತ್ತು": 30, "ಮೂವತ್ತ": 30, "ನಲವತ್ತು": 40, "ನಲವತ್ತ": 40, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
FILLERS = {"option", "number", "choice", "ward", "no", "is", "the", "please", "select", "say", "press"}
NUMBER_WORDS = set(UNITS) | set(TENS) | {"hundred"}


@dataclass
class Turn:
    """What to say next. lines are plain sentences for text to speech. end means hang up after saying them."""
    lines: List[str]
    end: bool = False
    lang: str = "en"
    complaint: Optional[Dict[str, Any]] = None   # the complaint just registered or looked up, for the call page's side panel

    def as_dict(self) -> Dict[str, Any]:
        return {"say": self.lines, "end": self.end, "lang": self.lang, "complaint": self.complaint}


KANNADA_MONTHS = {"Jan": "ಜನವರಿ", "Feb": "ಫೆಬ್ರವರಿ", "Mar": "ಮಾರ್ಚ್", "Apr": "ಏಪ್ರಿಲ್", "May": "ಮೇ", "Jun": "ಜೂನ್",
                  "Jul": "ಜುಲೈ", "Aug": "ಆಗಸ್ಟ್", "Sep": "ಸೆಪ್ಟೆಂಬರ್", "Oct": "ಅಕ್ಟೋಬರ್", "Nov": "ನವೆಂಬರ್", "Dec": "ಡಿಸೆಂಬರ್"}


def spoken_date(date: str, lang: str) -> str:
    """'23 Nov 2026' stays as it is in English and becomes '23 ನವೆಂಬರ್ 2026' for a Kannada caller."""
    if lang != "kn":
        return date
    return " ".join(KANNADA_MONTHS.get(part, part) for part in date.split())


def days_left(deadline: Optional[str], now: datetime) -> Optional[int]:
    """Whole days from now to the time limit, counting a part of a day as a day. None if there is no time limit."""
    if not deadline:
        return None
    try:
        due = datetime.fromisoformat(deadline)
    except ValueError:
        return None
    return max(0, math.ceil((due - now).total_seconds() / 86400))


class QuietChannel:
    """The bot needs a channel object. A call has no messages to push, so this one sends nothing."""
    name = "voice"
    free_text_hours = 24

    def send_text(self, mobile: str, text: str):
        return False, "A call has no text messages."

    def fetch_media(self, media: Any):
        raise RuntimeError("A call has no files.")


# ---------------------------------------------------------------------------------------------- what was heard


def _clean(text: str) -> str:
    text = re.sub(r"[.,!?;:\"“”]", " ", text or "").replace("’", "'")
    return " ".join(text.split())


def spoken_number(text: str) -> Optional[str]:
    """'five' -> '5', 'twenty three' -> '23', 'two zero two six' -> '2026', '12' -> '12'. None if it is not a number."""
    words = [word for word in _clean(text).lower().split() if word not in FILLERS or word.isdigit()]
    if not words:
        return None
    if all(word.isdigit() for word in words):
        return "".join(words)
    if not all(word in NUMBER_WORDS or word.isdigit() for word in words):
        return None
    out: List[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word.isdigit():
            out.append(word)
        elif word in TENS:
            value = TENS[word]
            if index + 1 < len(words) and words[index + 1] in UNITS and 1 <= UNITS[words[index + 1]] <= 9:
                value += UNITS[words[index + 1]]
                index += 1
            out.append(str(value))
        elif word in UNITS:
            out.append(str(UNITS[word]))
        else:
            return None
        index += 1
    return "".join(out)


def complaint_id_from_speech(text: str) -> Optional[str]:
    """'C M P 2 0 2 6 1 1 1 0 1 2 3 4' (or digits only) -> 'CMP-20261110-1234'. None if it does not have 12 digits."""
    digits = ""
    for word in _clean(text).lower().split():
        if word in ("c", "m", "p", "cmp", "complaint", "id", "number", "is", "dash", "hyphen", "minus"):
            continue
        if word.isdigit():
            digits += word
        elif word in UNITS and UNITS[word] <= 9:
            digits += str(UNITS[word])
        else:
            return None
    return f"CMP-{digits[:8]}-{digits[8:]}" if len(digits) == 12 else None


def understood(heard: str, step: Optional[str]) -> str:
    """The text the bot should see for what the caller said, given where the conversation is."""
    text = _clean(heard)[:MAX_HEARD_CHARS]
    low = text.lower()
    if step == "track_id":
        found = complaint_id_from_speech(text)
        return found or text
    number = spoken_number(text)
    if number is not None and step in (None, "reg_kind", "reg_category", "signup_taluk", "signup_ward", "pick_complaint",
                                       "fix_pick", "fix_code"):
        return number
    first = low.split(" ")[0] if low else ""
    if step in ("reg_check", "fix_answer"):
        if first in YES_WORDS:
            return "1"
        if first in NO_WORDS:
            return "2"
    if step == "reg_place" and low.startswith(("same", "my home", "home address", "my house", "ಅದೇ", "ಮನೆ")):
        return "same"
    if low in ("menu", "main menu", "start over", "go back", "back"):
        return "0"
    if low in ("my complaints", "my complaint", "list", "list my complaints", "show my complaints") and step is None:
        return "3"
    return text


# ---------------------------------------------------------------------------------------------- what is said


def speakable(text: str, lang: str = "en") -> str:
    """Turn one of the bot's WhatsApp texts into sentences that sound right when read aloud."""
    text = re.sub(r"[*_`]", "", text or "")
    text = re.sub(r"\s*\([^()]*CMP-\d{8}-\d+[^()]*\)", "", text)    # "(for example CMP-...)": not worth reading out
    text = re.sub(r"\s*\((?:at least|[^()]*\bcharacters)[^()]*\)", "", text)
    if lang == "kn":
        text = re.sub(r"(?m)^(\d+) - (.*)$", lambda m: f"{m.group(2).rstrip('.')}, {m.group(1)} ಎಂದು ಹೇಳಿ.", text)
        text = re.sub(r"(ಕಳುಹಿಸಿ|ಉತ್ತರಿಸಿ|ಬರೆಯಿರಿ)(?=[\s.:]|$)", "ಹೇಳಿ", text)
        text = re.sub(r"(?m)^\s*ಕ್ರಮ ನಿರೀಕ್ಷಿತ ಸಮಯ:.*$\n?", "", text)   # said as "within N days" instead (VoiceHelpline)
        text = re.sub(r"ಪ್ರತಿ ಹಂತದಲ್ಲೂ ನಾನು ಇಲ್ಲೇ ಸಂದೇಶ ಕಳುಹಿಸುತ್ತೇನೆ\.[^\n]*",
                      "ಪ್ರತಿ ಹಂತವನ್ನು ವೆಬ್‌ಸೈಟ್‌ನಲ್ಲಿ ನೋಡಬಹುದು, ಅಥವಾ ಮತ್ತೆ ಕರೆ ಮಾಡಿ ಟ್ರ್ಯಾಕ್ ಎಂದು ಹೇಳಿ.", text)
    else:
        text = re.sub(r"(?m)^(\d+) - (.*)$", lambda m: f"For {m.group(2).rstrip('.')}, say {m.group(1)}.", text)
        text = re.sub(r"\b(Send|Reply with|Reply|Write)\b", "Say", text)
        text = re.sub(r"\b(send|reply with|write)\b", "say", text)
        text = text.replace("Say 0 any time to come back to this menu.", "")
        text = re.sub(r"(?m)^\s*Action expected by:.*$\n?", "", text)   # said as "within N days" instead (VoiceHelpline)
    # The WhatsApp texts say "I will message you here"; on a call the updates are on the website.
    text = re.sub(r"I will message you here at each step\.\s*Say the ID any time to see the status\.",
                  "You can follow each step on the website, or call again and say track.", text)
    text = re.sub(r"CMP-(\d{8})-(\d{3,6})",
                  lambda m: "C M P. " + " ".join(m.group(1)) + ". " + " ".join(m.group(2)) + ".", text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    out = []
    for line in lines:
        out.append(line if line[-1] in ".?!:।" else line + ".")
    return " ".join(out)


def _is_menu(text: str) -> bool:
    return text.lstrip().startswith(("*Raichur Grievance Tracker*", "*ರಾಯಚೂರು ದೂರು ಟ್ರ್ಯಾಕರ್*"))


class VoiceHelpline:
    """One instance serves every call. A call is a call id plus the caller's 10-digit mobile number."""

    def __init__(self, portal: Any, understanding: Any = None, clock=utcnow, bot: Any = None) -> None:
        if bot is None:
            from complaint_portal.wa_bot import WhatsAppBot   # imported here: wa_bot is large and not needed by tests of this file
            bot = WhatsAppBot(portal, QuietChannel(), understanding, clock=clock, background=False)
        self.bot = bot
        self.portal = portal
        self.clock = clock
        self._calls: Dict[str, Dict[str, Any]] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ public

    def start(self, mobile: str, call_id: str, language: Optional[str] = None) -> Turn:
        """A new call: forget any half-finished chat for this number and greet. language ("en" or "kn") is what the
        caller chose on the call page; without it the language of their last chat is kept."""
        self._remember(call_id, {"turns": 0, "silences": 0, "mobile": mobile})
        self._ask(mobile, call_id, "0")   # back to the menu; its text is not used
        contact = self._contact(mobile)
        if language in ("en", "kn") and contact and contact.get("language") != language:
            self.portal.store.wa_save(mobile, language, contact.get("state") or "{}")
        return Turn([GREETING[self._lang(mobile)]], lang=self._lang(mobile))

    def turn(self, mobile: str, call_id: str, heard: Optional[str]) -> Turn:
        """The caller said something (or nothing). Returns what to say back."""
        call = self._calls.get(call_id)
        if call is None or call.get("mobile") != mobile:
            return self.start(mobile, call_id)
        lang = self._lang(mobile)
        text = _clean(heard or "")
        if not text:
            call["silences"] += 1
            if call["silences"] >= MAX_SILENCES:
                return Turn([NO_ANSWER_BYE[lang]], end=True, lang=lang)
            return Turn([NOT_HEARD[lang]], lang=lang)
        call["silences"] = 0
        call["turns"] += 1
        if call["turns"] > MAX_TURNS:
            return Turn([TOO_LONG_BYE[lang]], end=True, lang=lang)
        if text.lower() in END_WORDS:
            return Turn([BYE[lang]], end=True, lang=lang)
        before = {row["id"] for row in self.portal.store.list_complaints("citizen_contact = ?", (mobile,))}
        was_tracking = self._step(mobile) == "track_id"
        heard_as = understood(text, self._step(mobile))
        replies = self._ask(mobile, call_id, heard_as)
        # A call cannot take photos: where the bot asks for them, answer "skip" for the caller.
        guard = 0
        while self._step(mobile) == "reg_photos" and guard < 3:
            guard += 1
            replies = replies[:-1] + self._ask(mobile, call_id, "skip")
        lang = self._lang(mobile)
        lines = []
        for reply in replies:
            lines.append(MENU[lang] if _is_menu(reply) else speakable(reply, lang))
        complaint = self._complaint_for_panel(mobile, before, heard_as if was_tracking else None, lang)
        if complaint and complaint.get("answer"):
            lines.append(complaint["answer"])
        if self._step(mobile) is None and replies and not any(_is_menu(reply) for reply in replies):
            lines.append(MORE[lang])
        if not lines:
            lines.append(NOT_HEARD[lang])
        return Turn(lines, lang=lang, complaint=complaint)

    # ------------------------------------------------------------------ helpers

    def _complaint_for_panel(self, mobile: str, before: set, tracked: Optional[str], lang: str) -> Optional[Dict[str, Any]]:
        """The complaint this turn registered (or the one the caller asked about), with the time it should take."""
        rows = self.portal.store.list_complaints("citizen_contact = ?", (mobile,))
        row = next((r for r in rows if r["id"] not in before), None)
        kind = "new"
        if row is None and tracked:
            found = re.search(r"CMP-\d{8}-\d+", tracked)
            row = next((r for r in rows if found and r["id"] == found.group(0)), None)
            kind = "track"
        if row is None:
            return None
        now = self.clock()
        summary = self.portal._summary(row, now)      # pylint: disable=protected-access
        days = days_left(row["stage_deadline"], now) if row["status"] == "open" else None
        date = when_text(row["stage_deadline"]) if row["stage_deadline"] and row["status"] == "open" else None
        answer = None
        if kind == "new" and days is not None and date:
            words = DAYS_LINE[lang]
            answer = (words["one"] if days <= 1 else words["many"]).format(days=days, date=spoken_date(date.split(",")[0], lang))
        return {
            "kind": kind,
            "id": row["id"],
            "problem": (row["complaint_text"] or "")[:400],
            "type": summary["complaint_label"],
            "category": summary.get("category_label") or "",
            "department": summary["field"],
            "office": summary["current_office"],
            "status": row["status"],
            "status_text": summary["status_text"],
            "days": days,
            "due": date,
            "overdue": bool(summary["overdue"]),
            "answer": answer,
        }

    def _ask(self, mobile: str, call_id: str, text: str) -> List[str]:
        call = self._calls[call_id]
        call["seq"] = call.get("seq", 0) + 1
        return self.bot.process(Inbound(f"call:{call_id}:{call['seq']}", mobile, text))

    def _contact(self, mobile: str) -> Dict[str, Any]:
        return self.portal.store.wa_get(mobile) or {}

    def _lang(self, mobile: str) -> str:
        language = self._contact(mobile).get("language") or "en"
        return language if language in ("en", "kn") else "en"

    def _step(self, mobile: str) -> Optional[str]:
        import json
        try:
            return json.loads(self._contact(mobile).get("state") or "{}").get("step")
        except ValueError:
            return None

    def _remember(self, call_id: str, data: Dict[str, Any]) -> None:
        with self._guard:
            now = time.time()
            for key in [key for key, value in self._calls.items() if now - value.get("at", now) > CALL_FORGET_SECONDS]:
                self._calls.pop(key, None)
            if len(self._calls) >= MAX_CALLS_KEPT:
                self._calls.pop(next(iter(self._calls)), None)
            data["at"] = now
            self._calls[call_id] = data


# ---------------------------------------------------------------------------------------------- Twilio


def twiml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def twiml_for(turn: Turn, action: str, speech_language: str = "en-IN") -> bytes:
    """The answer to a Twilio call: say the lines, then listen again (or say them and hang up)."""
    voice_language = "kn-IN" if turn.lang == "kn" else speech_language
    says = "".join(f'<Say language="{voice_language}">{twiml_escape(line)}</Say>' for line in turn.lines)
    if turn.end:
        body = says + "<Hangup/>"
    else:
        body = (f'<Gather input="speech dtmf" language="{"kn-IN" if turn.lang == "kn" else speech_language}" '
                f'speechTimeout="auto" timeout="6" numDigits="12" action="{twiml_escape(action)}" method="POST">'
                f"{says}</Gather>"
                f'<Redirect method="POST">{twiml_escape(action)}</Redirect>')
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>' + body + "</Response>").encode("utf-8")
