"""Shared target-language registry; Chinese remains the compatible default."""

DEFAULT_LANGUAGE = "zh-Hans"
LANGUAGES = {
    "zh-Hans": ("简体中文", "Simplified Chinese"),
    "zh-Hant": ("繁體中文", "Traditional Chinese"),
    "en": ("English", "English"),
    "ja": ("日本語", "Japanese"),
    "ko": ("한국어", "Korean"),
    "de": ("Deutsch", "German"),
    "fr": ("Français", "French"),
    "es": ("Español", "Spanish"),
    "pt-BR": ("Português (Brasil)", "Brazilian Portuguese"),
    "ru": ("Русский", "Russian"),
    "uk": ("Українська", "Ukrainian"),
    "id": ("Bahasa Indonesia", "Indonesian"),
}
ALIASES = {code.lower(): code for code in LANGUAGES}
ALIASES.update({"zh": "zh-Hans", "zh-cn": "zh-Hans", "zh-sg": "zh-Hans",
                "zh-tw": "zh-Hant", "zh-hk": "zh-Hant", "zh-mo": "zh-Hant"})
for base, regions in {"en": ("US", "GB"), "ja": ("JP",), "ko": ("KR",),
                      "de": ("DE",), "fr": ("FR",), "es": ("ES",),
                      "ru": ("RU",), "uk": ("UA",), "id": ("ID",)}.items():
    for region in regions:
        ALIASES[base + "-" + region.lower()] = base


def normalize_language(value):
    if not isinstance(value, str):
        raise ValueError("Unsupported language")
    result = ALIASES.get(value.strip().replace("_", "-").lower())
    if not result:
        raise ValueError("Unsupported language")
    return result


def language_options():
    return [dict(code=code, name=names[0], english_name=names[1]) for code, names in LANGUAGES.items()]
