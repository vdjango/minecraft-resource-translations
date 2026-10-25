"""Keep formatting outside the model and protect executable/technical literals."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser


class HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "object", "svg"):
            self.skip += 1
        if not self.skip and tag in (
            "p",
            "div",
            "br",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "pre",
            "tr",
        ):
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "object", "svg"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def display_text(text, format):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Structure and attributes stay outside model control. Unsafe embedded
    # documents are dropped; display clients must still sanitize all markup.
    text = re.sub(
        r"<(script|style|iframe|object|svg)\b[^>]*>.*?</\1\s*>",
        "",
        text,
        flags=re.S | re.I,
    )
    return text


PROTECTED = re.compile(
    r"```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~|`[^`\n]+`|"
    r"<pre\b[^>]*>.*?</pre\s*>|<code\b[^>]*>.*?</code\s*>|<[^>]+>|"
    r"!?\[[^\]\n]*\]\([^\n)]*\)|https?://[^\s<>]+|"
    r"\b\d+(?:\.\d+)*(?:[%x×])?\b",
    re.S | re.I,
)
TOKEN = re.compile(r"⟦MC_[0-9a-f]{16}⟧")


@dataclass
class Segment:
    id: str
    text: str
    context: str


def prepare(text, format, title):
    text = display_text(text, format)
    placeholders = {}
    # Reject reserved markers from upstream rather than letting them masquerade
    # as our own placeholders.
    text = TOKEN.sub(lambda match: match.group().replace("MC_", "SRC_"), text)

    def protect(match):
        value = match.group()
        key = "⟦MC_" + hashlib.sha256(value.encode()).hexdigest()[:16] + "⟧"
        placeholders[key] = value
        return key

    protected = PROTECTED.sub(protect, text)
    blocks = re.split(r"(\n\s*\n)", protected)
    # Split overlong paragraphs on sentence boundaries, never within tokens.
    pieces = []
    for block in blocks:
        if len(block) > 2400:
            chunks, part = [], ""
            for word in re.split(r"(\s+)", block):
                if len(part) + len(word) > 2400 and part:
                    chunks.append(part)
                    part = ""
                part += word
            pieces.extend(chunks + ([part] if part else []))
        else:
            pieces.append(block)
    segments, template = [], []
    for index, part in enumerate(pieces):
        if any(unicodedata.category(char).startswith("L") for char in TOKEN.sub("", part)):
            context = (
                title
                + "\n"
                + (pieces[index - 1] if index else "")
                + "\n"
                + (pieces[index + 1] if index + 1 < len(pieces) else "")
            )
            identity = hashlib.sha256((part + "\0" + context).encode()).hexdigest()
            segments.append(Segment(identity, part, context))
            template.append((True, identity))
        else:
            template.append((False, part))
    return segments, template, placeholders


def no_translation_reason(field, language, title="", segments=None):
    """Skip only verified or deterministic cases; never guess from script."""
    from .languages import normalize_language

    if field.get("field") == "title":
        return "project_name"
    try:
        if normalize_language(field.get("source_language")) == language:
            return "already_target_language"
    except ValueError:
        pass
    if "text" not in field:
        return None
    text = field["text"]
    if not text.strip():
        return "empty_content"
    if title and text.strip() == title.strip():
        return "project_name"
    if segments is None:
        segments, _, _ = prepare(text, field["format"], title)
    return None if segments else "technical_content"


def validate_translation(original, translated):
    if (
        not isinstance(translated, str)
        or not translated.strip()
        or len(translated) > max(2000, len(original) * 8)
    ):
        raise ValueError("Invalid translated text")
    if TOKEN.findall(original) != TOKEN.findall(translated):
        raise ValueError("Translation changed protected content")
    if re.search(r"<(?:script|iframe|style|object)\b", translated, re.I):
        raise ValueError("Unsafe translated markup")
    return translated


def assemble(template, results, placeholders):
    text = "".join(results[value] if dynamic else value for dynamic, value in template)
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text
