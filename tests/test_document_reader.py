import base64
import io
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from PIL import Image

from app.prompts import TRANSCRIPTION_PROMPT, load_prompt
from app.services.document_reader import (
    MAX_IMAGE_SIDE,
    DocumentReader,
    DocumentReadError,
)

LOREM = "Invoice NF-123 issued by ACME Ltda, CNPJ 12.345.678/0001-90. Total due: R$ 1.000,00."


def make_text_pdf(pages: list[str]) -> bytes:
    """Build a minimal digital PDF with one line of Helvetica text per page."""
    count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(count))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        stream = f"BT /F1 10 Tf 40 700 Td ({text}) Tj ET"
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_scanned_pdf(page_count: int) -> bytes:
    """Build an image-only PDF, like the output of a scanner."""
    pages = [Image.new("RGB", (600, 800), "white") for _ in range(page_count)]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


def make_image(size: tuple[int, int], fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format=fmt)
    return buffer.getvalue()


def _mock_llm(reply: str = "transcribed text") -> MagicMock:
    llm = MagicMock(spec=BaseChatModel)
    llm.invoke.return_value = AIMessage(content=reply)
    return llm


def _reader(llm: MagicMock, *, pdf_max_pages: int = 50, vision_max_pages: int = 10) -> Any:
    return DocumentReader(llm, "v1", pdf_max_pages=pdf_max_pages, vision_max_pages=vision_max_pages)


def _sent_images(llm: MagicMock) -> list[dict[str, Any]]:
    (messages,), _ = llm.invoke.call_args
    system, human = messages
    assert isinstance(system, SystemMessage)
    assert system.content == load_prompt(TRANSCRIPTION_PROMPT, "v1")
    assert isinstance(human, HumanMessage)
    return [block for block in human.content if block["type"] == "image"]  # type: ignore[index,union-attr]


# Digital PDFs


def test_digital_pdf_is_read_locally_without_llm() -> None:
    llm = _mock_llm()

    document = _reader(llm).read(make_text_pdf([LOREM, LOREM]), "invoice.pdf")

    assert document.source == "pdf_text"
    assert document.page_count == 2
    assert document.text.count("NF-123") == 2
    llm.invoke.assert_not_called()


def test_pdf_over_page_limit_is_rejected() -> None:
    with pytest.raises(DocumentReadError, match="3 pages; the limit is 2"):
        _reader(_mock_llm(), pdf_max_pages=2).read(make_text_pdf([LOREM] * 3), "big.pdf")


@pytest.mark.parametrize("data", [b"not a pdf", b"%PDF-1.4\ngarbage"])
def test_corrupted_pdf_is_rejected(data: bytes) -> None:
    with pytest.raises(DocumentReadError, match="Could not open PDF"):
        _reader(_mock_llm()).read(data, "broken.pdf")


# Scanned PDFs


def test_scanned_pdf_falls_back_to_vision_with_one_image_per_page() -> None:
    llm = _mock_llm("--- page 1 ---\nTOTAL R$ 10,00")

    document = _reader(llm).read(make_scanned_pdf(3), "scan.pdf")

    assert document.source == "vision"
    assert document.page_count == 3
    assert document.text == "--- page 1 ---\nTOTAL R$ 10,00"
    images = _sent_images(llm)
    assert len(images) == 3
    assert all(image["mime_type"] == "image/png" for image in images)


def test_pdf_with_too_little_text_is_treated_as_scanned() -> None:
    llm = _mock_llm()

    document = _reader(llm).read(make_text_pdf(["p. 1"]), "mostly-scanned.pdf")

    assert document.source == "vision"


def test_scanned_pdf_over_vision_limit_is_rejected_without_llm() -> None:
    llm = _mock_llm()

    with pytest.raises(DocumentReadError, match="limit for scanned documents is 2"):
        _reader(llm, vision_max_pages=2).read(make_scanned_pdf(3), "scan.pdf")

    llm.invoke.assert_not_called()


def test_empty_transcription_is_rejected() -> None:
    with pytest.raises(DocumentReadError, match="no text"):
        _reader(_mock_llm("   ")).read(make_scanned_pdf(1), "blank.pdf")


# Images


@pytest.mark.parametrize(
    ("filename", "fmt"), [("a.png", "PNG"), ("b.JPG", "JPEG"), ("c.webp", "WEBP")]
)
def test_images_are_sent_to_vision_as_png(filename: str, fmt: str) -> None:
    llm = _mock_llm()

    document = _reader(llm).read(make_image((800, 600), fmt), filename)

    assert document.source == "vision"
    assert document.page_count == 1
    (image,) = _sent_images(llm)
    decoded = Image.open(io.BytesIO(base64.b64decode(image["base64"])))
    assert decoded.format == "PNG"
    assert decoded.size == (800, 600)


def test_large_images_are_downscaled() -> None:
    llm = _mock_llm()

    _reader(llm).read(make_image((4000, 1000)), "wide.png")

    (image,) = _sent_images(llm)
    decoded = Image.open(io.BytesIO(base64.b64decode(image["base64"])))
    assert decoded.size == (MAX_IMAGE_SIDE, 500)


def test_corrupted_image_is_rejected() -> None:
    with pytest.raises(DocumentReadError, match="Could not decode image"):
        _reader(_mock_llm()).read(b"not an image", "photo.png")


def test_unsupported_extension_is_rejected() -> None:
    with pytest.raises(DocumentReadError, match="Unsupported"):
        _reader(_mock_llm()).read(b"hello", "notes.txt")


def test_pdf_without_pages_is_rejected() -> None:
    # pdfium already refuses to open a zero-page PDF; either way it must not reach the LLM.
    with pytest.raises(DocumentReadError):
        _reader(_mock_llm()).read(make_text_pdf([]), "empty.pdf")


def test_images_with_alpha_are_flattened_to_rgb() -> None:
    llm = _mock_llm()
    buffer = io.BytesIO()
    Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(buffer, format="PNG")

    _reader(llm).read(buffer.getvalue(), "transparent.png")

    (image,) = _sent_images(llm)
    assert Image.open(io.BytesIO(base64.b64decode(image["base64"]))).mode == "RGB"
