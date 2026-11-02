"""Structured output schemas returned by the LLM extraction step.

Every field is required but nullable: the model must explicitly answer `null` when a value is
absent from the document instead of omitting it. This keeps the JSON Schema compatible with
strict structured-output modes across providers.
"""

from collections.abc import Mapping
from datetime import date

from pydantic import BaseModel, Field

from app.models import DocumentType

# Invoice


class InvoiceLineItem(BaseModel):
    description: str = Field(description="Product or service description as printed.")
    quantity: float | None = Field(description="Quantity billed.")
    unit_price: float | None = Field(description="Price per unit.")
    total: float | None = Field(description="Line total.")


class Invoice(BaseModel):
    """Fields extracted from an invoice."""

    invoice_number: str | None = Field(description="Invoice number or identifier.")
    issuer_name: str | None = Field(description="Legal or trade name of the issuer.")
    issuer_tax_id: str | None = Field(
        description="Issuer tax identifier exactly as printed (e.g. CNPJ, CPF, VAT, EIN)."
    )
    issue_date: date | None = Field(description="Date the invoice was issued.")
    currency: str | None = Field(description="ISO 4217 currency code, e.g. BRL or USD.")
    line_items: list[InvoiceLineItem] = Field(description="Billed items, in document order.")
    total_amount: float | None = Field(description="Grand total payable.")


# Contract


class ContractParty(BaseModel):
    name: str = Field(description="Person or organization name.")
    role: str | None = Field(description="Role in the contract, e.g. contractor, client, lessor.")
    tax_id: str | None = Field(description="Tax identifier as printed, if any.")


class ContractClause(BaseModel):
    title: str = Field(description="Clause heading or a short label for it.")
    summary: str = Field(description="One or two sentence summary of the clause.")


class Contract(BaseModel):
    """Fields extracted from a contract."""

    parties: list[ContractParty] = Field(description="All parties bound by the contract.")
    subject: str | None = Field(description="What the contract is about (its object).")
    start_date: date | None = Field(description="Date the contract takes effect.")
    end_date: date | None = Field(description="Date the contract ends, if fixed.")
    term_description: str | None = Field(
        description="Duration and renewal terms as stated, e.g. '12 months, auto-renewing'."
    )
    value: float | None = Field(description="Total contract value.")
    currency: str | None = Field(description="ISO 4217 currency code of the value.")
    key_clauses: list[ContractClause] = Field(
        description="Most important clauses: payment, termination, penalties, confidentiality."
    )


# Resume


class ResumeContact(BaseModel):
    email: str | None
    phone: str | None
    location: str | None = Field(description="City, region and/or country.")
    links: list[str] = Field(description="Profile or portfolio URLs (LinkedIn, GitHub, site).")


class WorkExperience(BaseModel):
    company: str
    title: str | None = Field(description="Job title.")
    start_date: str | None = Field(description="Start as YYYY-MM, or YYYY if month is unknown.")
    end_date: str | None = Field(description="End as YYYY-MM or YYYY; null if current.")
    description: str | None = Field(description="Responsibilities and achievements.")


class Education(BaseModel):
    institution: str
    degree: str | None = Field(description="Degree or certificate, e.g. BSc, MBA.")
    field_of_study: str | None
    start_date: str | None = Field(description="Start as YYYY-MM, or YYYY if month is unknown.")
    end_date: str | None = Field(description="End as YYYY-MM or YYYY; null if ongoing.")


class Resume(BaseModel):
    """Fields extracted from a resume / CV."""

    full_name: str | None
    contact: ResumeContact
    experience: list[WorkExperience] = Field(description="Work history, most recent first.")
    education: list[Education]
    skills: list[str] = Field(description="Technical and professional skills.")


ExtractionResult = Invoice | Contract | Resume

EXTRACTION_SCHEMAS: Mapping[DocumentType, type[ExtractionResult]] = {
    DocumentType.INVOICE: Invoice,
    DocumentType.CONTRACT: Contract,
    DocumentType.RESUME: Resume,
}
