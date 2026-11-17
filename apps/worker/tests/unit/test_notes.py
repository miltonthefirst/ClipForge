"""Notes read out of the file the owner uploaded: PDF, Word, PowerPoint or text."""

from __future__ import annotations

from pathlib import Path

import pytest
from clipforge.synth.notes import NotesError, chunks, extract_text, word_count

pytestmark = pytest.mark.unit

LAWS = (
    "The first law: an object stays at rest or keeps moving at constant velocity "
    "unless a net force acts on it. The second law: force equals mass times acceleration."
)


def test_a_word_document_is_read_with_its_tables(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading("Newton's laws", level=1)
    document.add_paragraph(LAWS)
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Third law"
    table.rows[0].cells[1].text = "equal and opposite reaction"
    path = tmp_path / "lecture.docx"
    document.save(str(path))

    text = extract_text(path)
    assert "Newton's laws" in text and "net force acts on it" in text
    assert "Third law | equal and opposite reaction" in text


def test_a_powerpoint_is_read_with_its_speaker_notes(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Newton's laws"
    box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(2))
    box.text_frame.text = LAWS
    slide.notes_slide.notes_text_frame.text = "Stress that velocity includes direction."
    path = tmp_path / "lecture.pptx"
    deck.save(str(path))

    text = extract_text(path)
    assert text.startswith("Slide 1") and "net force acts on it" in text
    assert "Speaker notes: Stress that velocity includes direction." in text


def _pdf_with(text: str) -> bytes:
    """A one-page PDF with `text` on it, written by hand: pypdf reads but does not typeset."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n".encode()
    )
    return bytes(out)


def test_a_pdf_is_read(tmp_path: Path) -> None:
    path = tmp_path / "lecture.pdf"
    path.write_bytes(_pdf_with(LAWS))
    assert "net force acts on it" in extract_text(path)


def test_a_pdf_with_no_words_says_it_may_be_scanned(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    path.write_bytes(_pdf_with(""))
    with pytest.raises(NotesError, match="scanned"):
        extract_text(path)


def test_old_office_formats_say_how_to_fix_it(tmp_path: Path) -> None:
    path = tmp_path / "lecture.ppt"
    path.write_bytes(b"old")
    with pytest.raises(NotesError, match=r"save it as \.pptx"):
        extract_text(path)


def test_a_damaged_file_is_a_clear_refusal(tmp_path: Path) -> None:
    path = tmp_path / "lecture.docx"
    path.write_bytes(b"not a zip")
    with pytest.raises(NotesError, match="could not be read"):
        extract_text(path)


def test_long_text_is_read_in_pieces_and_nothing_is_lost() -> None:
    paragraphs = [f"Paragraph {n}. " + "word " * 60 for n in range(40)]
    text = "\n\n".join(paragraphs)
    pieces = list(chunks(text, 2000))
    assert len(pieces) > 5 and all(len(piece) <= 2000 for piece in pieces)
    assert word_count(" ".join(pieces)) == word_count(text)

    one_block = "x " * 5000
    assert word_count(" ".join(chunks(one_block, 1000))) == 5000
