from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from app.models import DocumentType
from app.prompts import load_prompt
from app.schemas.extraction import EXTRACTION_SCHEMAS, ExtractionResult


class ExtractionError(Exception):
    """The document could not be turned into a valid structured result.

    Raised for problems that retrying will not fix (empty input, output that does not match
    the schema). Provider/network errors are left to propagate so callers can retry them.
    """


class ExtractionService:
    """Turns document text into a validated Pydantic object using LLM structured output."""

    def __init__(self, llm: BaseChatModel, prompt_version: str) -> None:
        self._llm = llm
        self._prompt_version = prompt_version

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
