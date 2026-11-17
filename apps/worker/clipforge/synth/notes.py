"""Reading a person's notes out of the file they uploaded: PDF, Word, PowerPoint or text.

The owner's lecture notes arrive as files ("I normally get them in pdf, word or
PowerPoint", 2026-11-18). The phone uploads one to the bucket under
``notes/{uid}/``; the LYRICS stage fetches it, reads the words out of it here,
and deletes the bucket copy once the song is written.

Only the words: no layout, no pictures. A PowerPoint's speaker notes count,
because that is often where the explanation is. A PDF made of scanned pages has
no words to read, and says so rather than producing a song about nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

__all__ = ["SUPPORTED", "NotesError", "chunks", "extract_text", "word_count"]

SUPPORTED = frozenset({".pdf", ".docx", ".pptx", ".txt", ".md"})

# The old binary formats need Office to read; saving as the new one takes a second.
_OLD = {".doc": ".docx", ".ppt": ".pptx"}


class NotesError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.retryable = False
        self.code = "NOTES"


def extract_text(path: Path) -> str:
    """The words in the file, sections separated by blank lines. Raises NotesError."""
    suffix = path.suffix.lower()
    if suffix in _OLD:
        raise NotesError(
            f"{path.name} is in the old {suffix} format. Open it and save it as {_OLD[suffix]}, then upload that."
        )
    if suffix not in SUPPORTED:
        raise NotesError(
            f"{path.name} is not a file the notes can be read from (PDF, Word, PowerPoint or text)."
        )
    try:
        if suffix == ".pdf":
            text = _pdf(path)
        elif suffix == ".docx":
            text = _docx(path)
        elif suffix == ".pptx":
            text = _pptx(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except NotesError:
        raise
    except Exception as exc:  # a damaged or protected file, in any of three libraries
        raise NotesError(
            f"{path.name} could not be read: {type(exc).__name__}: {exc}"[:400]
        ) from exc
    text = _tidy(text)
    if word_count(text) < 15:
        hint = " It may be scanned pages, which have no text to read." if suffix == ".pdf" else ""
        raise NotesError(f"{path.name} has almost no words in it.{hint}")
    return text


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise NotesError(f"{path.name} is password-protected.") from exc
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(dict.fromkeys(cells)))
    return "\n".join(parts)


def _pptx(path: Path) -> str:
    from pptx import Presentation

    slides = []
    for number, slide in enumerate(Presentation(str(path)).slides, start=1):
        lines = [f"Slide {number}"]
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                lines += [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"Speaker notes: {notes}")
        slides.append("\n".join(lines))
    return "\n\n".join(slides)


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(text.split())


def chunks(text: str, size: int) -> Iterator[str]:
    """Pieces of about `size` characters, broken at paragraph ends where possible."""
    current: list[str] = []
    length = 0
    for paragraph in text.split("\n\n"):
        while len(paragraph) > size:  # one enormous paragraph: cut it at a line or a space
            cut = paragraph.rfind("\n", 0, size)
            cut = cut if cut > size // 2 else paragraph.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            if current:
                yield "\n\n".join(current)
                current, length = [], 0
            yield paragraph[:cut]
            paragraph = paragraph[cut:].strip()
        if length + len(paragraph) > size and current:
            yield "\n\n".join(current)
            current, length = [], 0
        if paragraph:
            current.append(paragraph)
            length += len(paragraph) + 2
    if current:
        yield "\n\n".join(current)
