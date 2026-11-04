"""Terminal-output redaction for demos (`apollo_enroll.py --redact`).

Masks contact names and email addresses in everything written to stdout, while leaving company
names readable. Applied as a stdout wrapper rather than at each print site, so lines printed from
deep inside providers.py (e.g. `_provider_error`, which echoes the email being looked up) and skip
reasons that name a *different* contact (the same-person guard) are covered without threading a
flag through every call.

    mask_name("Priya Lal")       -> "P**** L**"
    mask_email("priya@acme.io")  -> "p***@acme.io"
    linkedin.com/in/priya-lal    -> linkedin.com/in/***

Only terminal output is affected. Files under outputs/ and anything sent to Apollo or Fibery are
untouched.
"""
import re
from typing import Iterable, List, Optional

EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# A LinkedIn profile slug is usually the person's name ("/in/priya-lal-4b2"), so it is masked too.
LINKEDIN_RE = re.compile(r"(linkedin\.com/in/)([^/\s?#\"')]+)", re.IGNORECASE)

# Tokens this short are too likely to collide with ordinary words to mask on their own.
_MIN_TOKEN_LEN = 3


def mask_word(word: str) -> str:
    return word[:1] + "*" * (len(word) - 1) if word else word


def mask_name(name: str) -> str:
    return " ".join(mask_word(w) for w in (name or "").split())


def mask_email(email: str) -> str:
    m = EMAIL_RE.fullmatch((email or "").strip())
    if not m:
        return email
    return "{}***@{}".format(m.group(1)[:1], m.group(2))


class Redactor:
    """Masks every email address it sees, plus the specific names it has been told about.

    Names are matched whole-word, case-sensitively, longest first, so a full name is masked as a
    unit ("P**** L**") before its first name is masked on its own (the "Hey Priya," greeting in
    generated copy). A name token that is also a word in a protected company name is never masked
    on its own, so company names stay visible; the full name is still masked.
    """

    def __init__(self, names: Optional[Iterable[str]] = None,
                 protected: Optional[Iterable[str]] = None):
        self._protected = set()
        self._names: set = set()
        self._pattern: Optional[re.Pattern] = None
        self.protect(protected or [])
        self.add_names(names or [])

    def protect(self, phrases: Iterable[str]) -> None:
        for phrase in phrases:
            for tok in re.findall(r"[A-Za-z][A-Za-z'-]*", phrase or ""):
                self._protected.add(tok.lower())
        self._rebuild()

    def add_names(self, names: Iterable[str]) -> None:
        for name in names:
            name = " ".join((name or "").split())
            if not name:
                continue
            self._names.add(name)
            first = name.split()[0]
            self._names.add(first)
        self._rebuild()

    def _rebuild(self) -> None:
        terms: List[str] = []
        for n in self._names:
            if " " in n:
                terms.append(n)
            elif len(n) >= _MIN_TOKEN_LEN and n.lower() not in self._protected:
                terms.append(n)
        if not terms:
            self._pattern = None
            return
        terms.sort(key=len, reverse=True)
        self._pattern = re.compile(
            r"(?<![\w])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w])")

    def redact(self, text: str) -> str:
        if not text:
            return text
        text = EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
        text = LINKEDIN_RE.sub(lambda m: m.group(1) + "***", text)
        if self._pattern is not None:
            text = self._pattern.sub(lambda m: mask_name(m.group(1)), text)
        return text


class RedactingStream:
    """A file-like wrapper that redacts each write before passing it to the real stream."""

    def __init__(self, stream, redactor: Redactor):
        self._stream = stream
        self.redactor = redactor

    def write(self, text: str) -> int:
        return self._stream.write(self.redactor.redact(text))

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, attr):
        return getattr(self._stream, attr)
