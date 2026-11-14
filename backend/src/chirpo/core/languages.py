"""The languages a workspace can write its issues in (M6.4).

A closed list on purpose: the name goes into the model's instructions, so it must never be text
someone typed (an admin's "English, and ignore the rules" would be an injection).
"""

LANGUAGES: dict[str, str] = {
    "am": "Amharic",
    "ar": "Arabic",
    "bn": "Bengali",
    "ca": "Catalan",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "fa": "Persian",
    "fi": "Finnish",
    "fr": "French",
    "ha": "Hausa",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "id": "Indonesian",
    "ig": "Igbo",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ms": "Malay",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sv": "Swedish",
    "sw": "Swahili",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "yo": "Yoruba",
    "zh": "Chinese",
    "zu": "Zulu",
}


def language_code(said: str) -> str | None:
    """The code for "fr", "French" or "french"; None if it isn't on the list."""
    wanted = said.strip().casefold()
    if wanted in LANGUAGES:
        return wanted
    return next((code for code, name in LANGUAGES.items() if name.casefold() == wanted), None)
