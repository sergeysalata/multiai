"""Turning an uploaded file into something the agents can read.

Models take text. So every upload is reduced to text here, once, at upload
time — not on every turn — and that text is what goes into the prompt.

What we can read, and what we do when we can't, is deliberately visible to the
user: a file that could not be parsed still appears in the room with a note
saying why, rather than silently contributing nothing.
"""

import io
import os
import re
import unicodedata

# Extensions we can read as plain text. Anything textual not on this list is
# still attempted if the bytes decode cleanly.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".ini", ".cfg", ".conf", ".toml",
    ".log", ".sql", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".ps1",
    ".r", ".m", ".tex", ".env", ".gitignore", ".dockerfile", ".makefile",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff"}


def safe_name(filename):
    """A filename safe to put on disk, keeping something recognisable."""
    name = unicodedata.normalize("NFKD", filename or "file")
    name = name.encode("ascii", "ignore").decode("ascii")
    name = os.path.basename(name).strip().replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    if "." in name:
        # A name written entirely in a non-Latin script reduces to nothing
        # here, leaving a dotfile like ".csv". Keep the extension, name the stem.
        stem, _, extension = name.rpartition(".")
        name = f"{stem or 'file'}.{extension}" if extension else (stem or "file")
    return (name or "file")[:120]


def _decode(data):
    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _read_pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "PDF support needs the pypdf package: pip install pypdf"

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return None, "The PDF is password protected."
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"[page {index}]\n{text}")
        if not pages:
            return None, (
                "No text in this PDF — it is probably a scan. Run OCR on it "
                "first, or paste the relevant part into the chat."
            )
        return "\n\n".join(pages), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the PDF: {exc}"


def _read_docx(data):
    try:
        import docx  # python-docx
    except ImportError:
        return None, "Word support needs the python-docx package: pip install python-docx"

    try:
        document = docx.Document(io.BytesIO(data))
        blocks = [p.text.strip() for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    blocks.append(" | ".join(cells))
        if not blocks:
            return None, "That Word file has no readable text in it."
        return "\n".join(blocks), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the Word file: {exc}"


def extract(filename, data, limit_chars):
    """Return (text, truncated, note).

    text  — what the agents will read, "" if nothing could be read
    note  — why, in plain language, when text is empty
    """
    extension = os.path.splitext(filename or "")[1].lower()

    if extension == ".pdf":
        text, note = _read_pdf(data)
    elif extension in (".docx", ".docm"):
        text, note = _read_docx(data)
    elif extension == ".doc":
        text, note = None, (
            "Old .doc files can't be read. Save it as .docx or PDF and upload again."
        )
    elif extension in IMAGE_EXTENSIONS:
        text, note = None, (
            "Images can't be read by the agents in this room — they only "
            "receive text. Describe what matters, or upload a text version."
        )
    else:
        decoded = _decode(data)
        if decoded is None:
            text, note = None, (
                "This looks like a binary file. Upload a text, PDF or Word "
                "version if you want the agents to read it."
            )
        elif extension and extension not in TEXT_EXTENSIONS and "\x00" in decoded:
            text, note = None, "This file is not readable as text."
        else:
            text, note = decoded, ""

    if not text:
        return "", False, note

    text = text.replace("\x00", "").strip()
    truncated = len(text) > limit_chars
    if truncated:
        # Keep the head: the beginning of a document is almost always the part
        # that says what it is.
        text = text[:limit_chars].rstrip() + "\n\n[…file truncated…]"
    return text, truncated, ""


# --- avatars ---------------------------------------------------------------
# SVG is deliberately excluded: it is a document format that can carry script,
# and these images are served from our own origin.
AVATAR_TYPES = {
    b"\x89PNG\r\n\x1a\n": ("image/png", ".png"),
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
    b"GIF87a": ("image/gif", ".gif"),
    b"GIF89a": ("image/gif", ".gif"),
}

MAX_AVATAR_BYTES = 2 * 1024 * 1024


def sniff_image(data):
    """Identify an image by its bytes, not by what the upload claims.

    Returns (mime, extension) or (None, reason).
    """
    if not data:
        return None, "That file is empty."
    if len(data) > MAX_AVATAR_BYTES:
        return None, "Avatars must be under 2 MB."

    for signature, (mime, extension) in AVATAR_TYPES.items():
        if data.startswith(signature):
            return (mime, extension), ""

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image/webp", ".webp"), ""

    return None, "Use a PNG, JPEG, GIF or WebP image."
