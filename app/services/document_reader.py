"""Turns uploaded files (PDFs and images) into plain text for the extraction step.

Digital PDFs are read locally with pdfium. Scanned PDFs (little or no embedded text) and
images are sent to the vision-capable chat model for transcription.
"""

import base64
import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Literal

import pypdfium2 as pdfium
import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image, ImageOps, UnidentifiedImageError

from app.prompts import TRANSCRIPTION_PROMPT, load_prompt
from app.schemas.events import ProgressStage

logger = structlog.get_logger(__name__)

IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})
# A PDF averaging fewer non-whitespace characters per page than this is treated as scanned.
MIN_TEXT_CHARS_PER_PAGE = 50
# Render scale for scanned pages (1.0 = 72 DPI), and the longest side sent to the model.
PDF_RENDER_SCALE = 2.0
MAX_IMAGE_SIDE = 2000


ProgressCallback = Callable[[ProgressStage], None]


def _no_progress(_: ProgressStage) -> None:
    pass


class DocumentReadError(Exception):
    """The file cannot be read (corrupted, unsupported, too long, or no text found)."""


@dataclass(frozen=True)
class DocumentText:
    text: str
    source: Literal["pdf_text", "vision"]
    page_count: int


class DocumentReader:
    def __init__(
        self,
        llm: BaseChatModel,
        prompt_version: str,
        *,
        pdf_max_pages: int,
        vision_max_pages: int,
    ) -> None:
        self._llm = llm
        self._prompt_version = prompt_version
        self._pdf_max_pages = pdf_max_pages
        self._vision_max_pages = vision_max_pages

    def read(
        self, data: bytes, filename: str, on_progress: ProgressCallback | None = None
    ) -> DocumentText:
        progress = on_progress or _no_progress
        progress("reading")
        extension = PurePath(filename).suffix.lower()
        if extension == ".pdf":
            document = self._read_pdf(data, progress)
        elif extension in IMAGE_EXTENSIONS:
            progress("transcribing")
            document = DocumentText(
                text=self._transcribe([_encode_image(_open_image(data))]),
                source="vision",
                page_count=1,
            )
        else:
            raise DocumentReadError(f"Unsupported file extension {extension or '(none)'!r}.")

        logger.info(
            "document_read",
            filename=filename,
            source=document.source,
            page_count=document.page_count,
            chars=len(document.text),
        )
        return document

    def _read_pdf(self, data: bytes, progress: ProgressCallback) -> DocumentText:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise DocumentReadError(
                "Could not open PDF (corrupted or password-protected)."
            ) from exc

        try:
            page_count = len(pdf)
            if page_count == 0:
                raise DocumentReadError("PDF has no pages.")
            if page_count > self._pdf_max_pages:
                raise DocumentReadError(
                    f"PDF has {page_count} pages; the limit is {self._pdf_max_pages}."
                )

            text = "\n\n".join(_page_text(pdf[i]) for i in range(page_count)).strip()
            if _has_enough_text(text, page_count):
                return DocumentText(text=text, source="pdf_text", page_count=page_count)

            if page_count > self._vision_max_pages:
                raise DocumentReadError(
                    f"Scanned PDF has {page_count} pages; the limit for scanned documents "
                    f"is {self._vision_max_pages}."
                )
            images = [_render_page(pdf[i]) for i in range(page_count)]
        finally:
            pdf.close()

        progress("transcribing")
        return DocumentText(text=self._transcribe(images), source="vision", page_count=page_count)

    def _transcribe(self, images_b64: list[str]) -> str:
        content: list[str | dict[str, Any]] = []
        for number, image in enumerate(images_b64, start=1):
            content.append({"type": "text", "text": f"Page {number}:"})
            content.append({"type": "image", "base64": image, "mime_type": "image/png"})

        response = self._llm.invoke(
            [
                SystemMessage(content=load_prompt(TRANSCRIPTION_PROMPT, self._prompt_version)),
                HumanMessage(content=content),
            ]
        )
        text = response.text.strip()
        if not text:
            raise DocumentReadError("Vision model returned no text for the document.")
        return text


def _page_text(page: pdfium.PdfPage) -> str:
    text_page = page.get_textpage()
    try:
        return text_page.get_text_range().strip()
    finally:
        text_page.close()
        page.close()


def _has_enough_text(text: str, page_count: int) -> bool:
    visible_chars = sum(1 for char in text if not char.isspace())
    return visible_chars >= MIN_TEXT_CHARS_PER_PAGE * page_count


def _render_page(page: pdfium.PdfPage) -> str:
    try:
        bitmap = page.render(scale=PDF_RENDER_SCALE)
        return _encode_image(bitmap.to_pil())
    finally:
        page.close()


def _open_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise DocumentReadError("Could not decode image (corrupted or unsupported).") from exc
    return image


def _encode_image(image: Image.Image) -> str:
    """Normalize orientation, color mode and size, and return a base64-encoded PNG."""
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
