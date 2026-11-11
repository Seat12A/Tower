"""Lightweight language detection, without any model. Standard library only."""

from __future__ import annotations

import re
from collections import Counter

_HINT_RE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*")
_URL_OR_MENTION = re.compile(r"https?://\S+|www\.\S+|(?<![\w@])@\w+")
_WORD = re.compile(r"[^\W\d_]+")
_MAX_CHARS = 4000
_NON_LATIN_THRESHOLD = 0.6
_MIN_HITS = 2


def _words(raw: str) -> frozenset[str]:
    return frozenset(raw.split())


_STOPWORDS: dict[str, frozenset[str]] = {
    "fr": _words(
        """le les des une est dans pour pas sur avec cette ces sont aux nous vous ils elle
        elles où qu ce cet leur leurs être avoir fait été était très aussi bien tout tous
        sans sous donc car chez depuis cela ça faire peut fois même après encore notre
        votre pourquoi parce lui ont mon ma mes ton ses suis êtes sommes"""
    ),
    "en": _words(
        """the and of to that this with for are be by at from have not but they his her
        you she we which will would there their what if about who been has had were can
        all more when one said out up my your its than them then these those because just
        also into over some only how new did does"""
    ),
    "es": _words(
        """el los las un unos unas su sus más pero sin estos estas ese esa eso muy también
        ya fue han hay está están tiene cuando donde hasta otro otra yo ellos nosotros sido
        siempre solo años año nuestro cada mismo así aquí qué cómo"""
    ),
    "de": _words(
        """der und ist nicht ein eine einen dem den mit von zu auf für im zum zur auch
        sich aber wie wenn oder nach bei aus dass wird werden wurde hat haben sind sein
        noch nur über kann schon mehr als ich sie ihr keine diese dieser hier jetzt sehr
        gegen unter zwischen weil denn dann immer ihre seine dieses einem einer wurden
        worden"""
    ),
    "it": _words(
        """di che è della delle dello degli nel nella nei sul sulla non sono più anche
        questo questa quello quella essere stato stata hanno ho abbiamo molto tutto tutti
        dal dalla alla alle allo agli cui perché dei tra fra suo loro dell nell sull dall
        quest però già ancora sempre così dove cosa"""
    ),
    "pt": _words(
        """os uma uns umas não dos das apesar em pelo pela pelos pelas com muito também já são
        foi tem têm seu seus suas isso isto esse essa ele ela eles elas você nós mas ao
        aos à às então ainda depois até onde foram tinha pode fazer anos hoje nosso
        meu minha há"""
    ),
    "nl": _words(
        """het een dat op zijn voor niet aan maar om ook bij dan nog uit naar wordt
        heeft hebben deze door ze geen wel ik hij zij wij jullie hun haar zou zouden moet
        tegen onder tussen veel zeer omdat want toen nooit altijd gaat ging wat hoe
        waar dit dus jaar twee"""
    ),
}

# Scripts: (first code point, last code point, script name)
_SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0041, 0x024F, "latin"),
    (0x1E00, 0x1EFF, "latin"),
    (0x0370, 0x03FF, "greek"),
    (0x1F00, 0x1FFF, "greek"),
    (0x0400, 0x052F, "cyrillic"),
    (0x0530, 0x058F, "armenian"),
    (0x0590, 0x05FF, "hebrew"),
    (0x0600, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"),
    (0xFB50, 0xFDFF, "arabic"),
    (0xFE70, 0xFEFF, "arabic"),
    (0x0900, 0x097F, "devanagari"),
    (0x0E00, 0x0E7F, "thai"),
    (0x10A0, 0x10FF, "georgian"),
    (0x1100, 0x11FF, "hangul"),
    (0x3130, 0x318F, "hangul"),
    (0xAC00, 0xD7AF, "hangul"),
    (0x3040, 0x30FF, "kana"),
    (0x31F0, 0x31FF, "kana"),
    (0x3400, 0x4DBF, "han"),
    (0x4E00, 0x9FFF, "han"),
    (0x20000, 0x2A6DF, "han"),
)
_SCRIPT_LANGUAGE: dict[str, tuple[str, float]] = {
    "hebrew": ("he", 0.9),
    "greek": ("el", 0.9),
    "thai": ("th", 0.9),
    "hangul": ("ko", 0.9),
    "kana": ("ja", 0.9),
    "georgian": ("ka", 0.9),
    "armenian": ("hy", 0.9),
    "arabic": ("ar", 0.6),
    "cyrillic": ("ru", 0.6),
    "devanagari": ("hi", 0.6),
    "han": ("zh", 0.6),
}

UNDETERMINED = "und"
NON_LINGUISTIC_HINTS: frozenset[str] = frozenset({"und", "zxx", "qme", "qht", "qam", "qct", "qst"})
"""Platform hints that carry no language (X uses ``qme`` for media only, etc.)."""


def _script_of(ch: str) -> str | None:
    cp = ord(ch)
    for lo, hi, name in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return name
    return None


def normalize_hint(hint: str | None) -> str | None:
    """Primary subtag, lowercase, if ``hint`` looks like BCP 47
    (``^[A-Za-z]{2,3}([-_][A-Za-z0-9]{1,8})*$``) and is not in ``NON_LINGUISTIC_HINTS``;
    else None. ``"pt-BR"`` → ``"pt"``."""
    if not hint or not _HINT_RE.fullmatch(hint):
        return None
    primary = re.split(r"[-_]", hint, maxsplit=1)[0].lower()
    return None if primary in NON_LINGUISTIC_HINTS else primary


def detect_language(text: str, hint: str | None = None) -> tuple[str, float]:
    """Return ``(code, confidence)`` with ``code`` a BCP 47 primary subtag or ``"und"``.

    1. A valid platform hint wins: ``(normalize_hint(hint), 1.0)``.
    2. Script: if ≥ 60 % of letters belong to one non-Latin script, return its language
       when unambiguous (Hebrew ``he``, Greek ``el``, Thai ``th``, Hangul ``ko``, kana
       ``ja``, Georgian ``ka``, Armenian ``hy``) or the dominant language of the script
       with a lower confidence (Arabic ``ar``, Cyrillic ``ru``, Devanagari ``hi``, Han
       only ``zh``) — confidence 0.9 for unambiguous scripts, 0.6 otherwise.
    3. Latin script: score function words (stopword lists) for fr, en, es, de, it, pt,
       nl; best language wins if it has ≥ 2 hits and leads the second by a clear margin;
       confidence in [0.5, 0.95] growing with hits and margin.
    4. Otherwise (too short, mixed, no letters) → ``("und", 0.0)``.
    """
    primary = normalize_hint(hint)
    if primary is not None:
        return primary, 1.0
    text = _URL_OR_MENTION.sub(" ", text[:_MAX_CHARS]).lower()
    scripts: Counter[str] = Counter()
    total = 0
    for ch in text:
        if ch.isalpha():
            total += 1
            script = _script_of(ch)
            if script is not None:
                scripts[script] += 1
    if total == 0:
        return UNDETERMINED, 0.0
    if scripts["kana"]:
        scripts["kana"] += scripts.pop("han", 0)
    if scripts:
        script, count = scripts.most_common(1)[0]
        if script != "latin" and count / total >= _NON_LATIN_THRESHOLD:
            return _SCRIPT_LANGUAGE[script]
    if scripts["latin"] / total < _NON_LATIN_THRESHOLD:
        return UNDETERMINED, 0.0
    hits = {lang: 0 for lang in _STOPWORDS}
    for word in _WORD.findall(text):
        for lang, words in _STOPWORDS.items():
            if word in words:
                hits[lang] += 1
    ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
    (best, best_hits), (_, second_hits) = ranked[0], ranked[1]
    margin = best_hits - second_hits
    if best_hits < _MIN_HITS or margin < max(1.0, 0.3 * best_hits):
        return UNDETERMINED, 0.0
    confidence = 0.5 + 0.25 * min(1.0, best_hits / 8) + 0.2 * (margin / best_hits)
    return best, round(min(confidence, 0.95), 3)
