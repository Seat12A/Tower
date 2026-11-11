from __future__ import annotations

import pytest

from tower.core.domain.language import detect_language, normalize_hint

SENTENCES = {
    "fr": "Le gouvernement a annoncé hier que la réforme des retraites sera présentée "
    "au Parlement avant la fin de l'année, malgré les critiques de l'opposition.",
    "en": "The government announced yesterday that the pension reform will be presented "
    "to Parliament before the end of the year, despite criticism from the opposition.",
    "es": "El gobierno anunció ayer que la reforma de las pensiones será presentada "
    "al Parlamento antes de que termine el año, a pesar de las críticas de la oposición.",
    "de": "Die Regierung hat gestern angekündigt, dass die Rentenreform noch vor dem "
    "Ende des Jahres dem Parlament vorgelegt wird, trotz der Kritik der Opposition.",
    "it": "Il governo ha annunciato ieri che la riforma delle pensioni sarà presentata "
    "al Parlamento prima della fine dell'anno, nonostante le critiche dell'opposizione.",
    "pt": "O governo anunciou ontem que a reforma das pensões será apresentada ao "
    "Parlamento antes do fim do ano, apesar das críticas da oposição e dos sindicatos.",
    "nl": "De regering heeft gisteren aangekondigd dat de pensioenhervorming nog voor het "
    "einde van het jaar aan het parlement wordt voorgelegd, ondanks de kritiek van de oppositie.",
}


@pytest.mark.parametrize(("code", "sentence"), SENTENCES.items())
def test_latin_languages(code: str, sentence: str) -> None:
    detected, confidence = detect_language(sentence)
    assert detected == code
    assert 0.5 <= confidence <= 0.95


@pytest.mark.parametrize(
    ("code", "text"),
    [
        ("ar", "الحكومة أعلنت أمس أن إصلاح نظام التقاعد سيعرض على البرلمان قبل نهاية السنة"),
        (
            "ru",
            "Правительство вчера объявило, что пенсионная реформа будет представлена парламенту",
        ),
        ("he", "הממשלה הודיעה אתמול שרפורמת הפנסיה תוצג בפני הפרלמנט לפני סוף השנה"),
        ("el", "Η κυβέρνηση ανακοίνωσε χθες ότι η μεταρρύθμιση του συνταξιοδοτικού θα κατατεθεί"),
        (
            "ja",
            "政府は昨日、年金改革を年末までに議会に提出すると発表しました。ありがとうございます",
        ),
        ("ko", "정부는 어제 연금 개혁안을 올해 말까지 의회에 제출할 것이라고 발표했습니다"),
        ("hi", "सरकार ने कल घोषणा की कि पेंशन सुधार को साल के अंत से पहले संसद में पेश किया जाएगा"),
        ("th", "รัฐบาลประกาศเมื่อวานนี้ว่าจะเสนอการปฏิรูปบำนาญต่อรัฐสภาก่อนสิ้นปีนี้"),
        ("zh", "政府昨天宣布养老金改革将在今年年底之前提交给议会审议"),
    ],
)
def test_non_latin_scripts(code: str, text: str) -> None:
    detected, confidence = detect_language(text)
    assert detected == code
    assert 0.5 <= confidence <= 0.95


def test_valid_hint_wins() -> None:
    assert detect_language(SENTENCES["fr"], "pt-BR") == ("pt", 1.0)
    assert detect_language("", "en") == ("en", 1.0)


@pytest.mark.parametrize(
    "hint", ["qme", "und", "zxx", "QME", "x", "!!", "en US", "", "-en", "a1b", None]
)
def test_invalid_or_non_linguistic_hints_are_ignored(hint: str | None) -> None:
    assert normalize_hint(hint) is None
    assert detect_language(SENTENCES["es"], hint)[0] == "es"


def test_normalize_hint_values() -> None:
    assert normalize_hint("pt-BR") == "pt"
    assert normalize_hint("zh_Hant_TW") == "zh"
    assert normalize_hint("FR") == "fr"
    assert normalize_hint("fil") == "fil"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ok",
        "merci beaucoup",
        "😀🔥👍",
        "1234 5678 90",
        "https://example.com/the/and/of",
        "@alice @bob",
    ],
)
def test_short_or_non_linguistic_text_is_undetermined(text: str) -> None:
    assert detect_language(text) == ("und", 0.0)


def test_mixed_text_does_not_crash() -> None:
    code, confidence = detect_language(
        "Le gouvernement a annoncé que the reform will be presented au Parlement before the end"
    )
    assert code in {"fr", "en", "es", "de", "it", "pt", "nl", "und"}
    assert 0.0 <= confidence <= 1.0
