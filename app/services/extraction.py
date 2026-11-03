from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from app.core.config import Settings
from app.models import DocumentType
from app.prompts import load_prompt
from app.schemas.extraction import EXTRACTION_SCHEMAS, ExtractionResult
from app.services.document_reader import DocumentReader, ProgressCallback
from app.services.llm import build_chat_model


class ExtractionError(Exception):
    """The document could not be turned into a valid structured result.

    Raised for problems that retrying will not fix (empty input, output that does not match
    the schema). Provider/network errors are left to propagate so callers can retry them.
    """


class ExtractionService:
    """Turns documents into validated Pydantic objects using LLM structured output."""

    def __init__(self, llm: BaseChatModel, prompt_version: str, reader: DocumentReader) -> None:
        self._llm = llm
        self._prompt_version = prompt_version
        self._reader = reader

    def extract_document(
        self,
        data: bytes,
        filename: str,
        document_type: DocumentType,
        on_progress: ProgressCallback | None = None,
    ) -> ExtractionResult:
        """Read a PDF or image and extract its fields, reporting each step to `on_progress`.

        Raises DocumentReadError or ExtractionError for permanent failures.
        """
        document = self._reader.read(data, filename, on_progress)
        if on_progress is not None:
            on_progress("extracting")
        return self.extract(document.text, document_type)

    def extract(self, text: str, document_type: DocumentType) -> ExtractionResult:
        if not text.strip():
            raise ExtractionError("Document has no text to extract from.")

        schema = EXTRACTION_SCHEMAS[document_type]
        # json_schema uses the provider's native structured output (no forced tool calling).
        structured_llm = self._llm.with_structured_output(schema, method="json_schema")

        try:
            result = structured_llm.invoke(self._build_messages(text, document_type))
        except (OutputParserException, ValidationError) as exc:
            raise ExtractionError(
                f"Model output did not match the {schema.__name__} schema."
            ) from exc

        if not isinstance(result, schema):
            raise ExtractionError(f"Expected {schema.__name__}, got {type(result).__name__}.")
        return result

    def _build_messages(self, text: str, document_type: DocumentType) -> list[BaseMessage]:
        return [
            SystemMessage(content=load_prompt(document_type, self._prompt_version)),
            HumanMessage(content=f"<document>\n{text}\n</document>"),
        ]


def build_extraction_service(settings: Settings) -> ExtractionService:
    """Wire the configured chat model into a reader and an extraction service."""
    llm = build_chat_model(settings)
    reader = DocumentReader(
        llm,
        settings.prompt_version,
        pdf_max_pages=settings.pdf_max_pages,
        vision_max_pages=settings.vision_max_pages,
    )
    return ExtractionService(llm, settings.prompt_version, reader)
