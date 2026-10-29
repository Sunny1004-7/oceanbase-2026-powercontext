"""Complete offline PDF capture with immutable physical-page identities."""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.memory.models import MemoryEntryVersion
from powercontext.builtin.runtime.application import ScopedMemoryApplication
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _nonblank_document_id(value: str) -> str:
    if not value.strip():
        raise ValueError("document ID must not be blank")  # noqa: TRY003
    return value


class PDFDocumentInput(BaseModel):
    document_id: str = Field(strict=True)
    content: bytes = Field(strict=True)

    _document_id = field_validator("document_id")(_nonblank_document_id)


class PDFPageInput(BaseModel):
    document_id: str = Field(strict=True)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    page_number: int = Field(strict=True, gt=0)
    text: str = Field(strict=True)

    _document_id = field_validator("document_id")(_nonblank_document_id)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page before publishing any result."""
    try:
        if not value.content or not value.content.startswith(b"%PDF-"):
            raise ValueError("invalid PDF header")  # noqa: TRY003, TRY301
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("a nonempty PDF password is required")  # noqa: TRY003, TRY301
        digest = hashlib.sha256(value.content).hexdigest()
        return tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=number,
                text=page.extract_text() or "",
            )
            for number, page in enumerate(reader.pages, 1)
        )
    except Exception as error:
        raise ValueError("PDF could not be completely parsed") from error  # noqa: TRY003


class PDFPageSource(Source):
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _source_id(value: PDFPageInput) -> str:
    document = hashlib.sha256(value.document_id.encode("utf-8")).hexdigest()
    identity = f"pdf:{document}:{value.document_sha256}:{value.page_number}"
    if len(identity) > 256:
        raise ValueError("page identity exceeds the Source limit")  # noqa: TRY003
    return identity


class _PDFAdapter:
    input_class = PDFPageInput
    source_class = PDFPageSource
    name = "pdf-page"

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=_source_id(value), materialization=SourceMaterialization.CAPTURED, **value.model_dump()
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=source.document_id + "\n" + source.text,
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFAdapter(), projections=(_PDFTextProjection(),)
)


_FUNCTION_WORDS = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "its",
    "it",
    "has",
    "have",
    "had",
    "any",
    "much",
    "many",
    "give",
    "answer",
    "respond",
    "response",
    "question",
    "assuming",
    "perspective",
    "analyst",
    "investment",
    "only",
    "use",
    "using",
    "details",
    "shown",
    "within",
    "relying",
    "stated",
    "explicitly",
    "directly",
    "outlined",
    "then",
    "state",
    "not",
    "if",
    "report",
    "reported",
    "fiscal",
    "fy",
    "year",
    "end",
    "million",
    "millions",
    "usd",
    "dollar",
    "dollars",
    "calculate",
    "define",
    "unadjusted",
    "quantity",
    "together",
])

# Ordinary accounting vocabulary, shared by every document and query. These expand
# retrieval only; captured page text and Source identities are never normalized.
_FINANCIAL_CONCEPTS = (
    (
        "operations",
        (
            "geographies",
            "geographic",
            "geographical",
            "countries",
            "regions",
            "primarily operate",
            "principally operate",
            "principal countries",
            "operated",
            "operates",
            "segments",
            "international",
        ),
    ),
    (
        "litigation",
        (
            "litigation",
            "legal battle",
            "legal battles",
            "legal proceedings",
            "lawsuit",
            "contingencies",
            "infringement",
            "ongoing suit",
            "material legal",
        ),
    ),
    ("restructuring", ("restructuring", "reorganization", "severance", "impairment charges")),
    (
        "operating",
        ("operating income", "operating profit", "income from operations", "earnings from operations", "ebit"),
    ),
    (
        "income_statement",
        (
            "income statement",
            "income statements",
            "statement of income",
            "statements of income",
            "statement of operations",
            "statements of operations",
            "p l statement",
            "profit and loss statement",
        ),
    ),
    ("revenue", ("revenue", "revenues", "net sales", "sales revenue", "turnover")),
    ("depreciation", ("depreciation", "amortization", "d a")),
    (
        "capex",
        (
            "capex",
            "capital expenditures",
            "capital expenditure",
            "capital spending",
            "purchases of property",
            "property plant and equipment",
        ),
    ),
    ("cashflow", ("cash flow", "cash flows", "operating cash", "cash provided by", "cash used in")),
    (
        "debt",
        ("debt", "borrowing", "borrowings", "credit facility", "credit facilities", "notes payable", "obligations"),
    ),
    ("assets", ("current assets", "total assets")),
    ("liabilities", ("current liabilities", "total liabilities")),
    ("inventory", ("inventory", "inventories", "stock on hand")),
    ("dividends", ("dividends", "dividend", "cash distribution", "distributions")),
    ("repurchases", ("repurchases", "repurchase", "stock buybacks", "share buyback", "treasury stock")),
    ("earnings", ("net income", "net earnings", "net profit", "earnings per share")),
    ("tax", ("income tax", "tax expense", "effective tax rate", "taxation")),
    ("interest", ("interest expense", "interest income", "interest paid")),
    ("equity", ("equity", "stockholders", "shareholders")),
    ("pension", ("pension", "retirement benefits", "postretirement")),
    ("lease", ("leases", "lease", "right of use", "rental")),
    ("acquisition", ("acquisition", "acquisitions", "business combination", "goodwill")),
    ("balance_sheet", ("balance sheet", "balance sheets", "statement of financial position")),
)


def _words(text: str) -> tuple[str, ...]:
    # Join line-end hyphenation for retrieval without rewriting the stored page.
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text.casefold())
    text = re.sub(r"\bfy\s*(?=\d{4})", " ", text)
    return tuple(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def _contains(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def _query_plan(question: str) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...], tuple[str, ...]]:
    normalized = " ".join(_words(question))
    concepts = [
        (name, phrases)
        for name, phrases in _FINANCIAL_CONCEPTS
        if any(_contains(normalized, phrase) for phrase in phrases)
    ]
    if "ebitda" in normalized.split():
        derived = {"operating", "depreciation"}
        if "margin" in normalized.split():
            derived.add("revenue")
        concepts.extend(
            (name, phrases)
            for name, phrases in _FINANCIAL_CONCEPTS
            if name in derived and name not in {item[0] for item in concepts}
        )
    terms = tuple(dict.fromkeys(term for term in _words(question) if term not in _FUNCTION_WORDS))
    variants = [" ".join(terms[:32])]
    for _name, phrases in concepts:
        # Two-term variants also admit pages containing just one synonym. Long
        # natural-language questions otherwise lose evidence at the FTS admission gate.
        tokens = list(
            dict.fromkeys(word for phrase in phrases for word in phrase.split() if word not in _FUNCTION_WORDS)
        )
        variants.extend(" ".join(tokens[index : index + 2]) for index in range(0, min(len(tokens), 6), 2))
    variants.extend(" ".join(terms[index : index + 2]) for index in range(0, min(len(terms), 12), 2))
    queries = tuple(dict.fromkeys(variant for variant in variants if variant))[:16]
    return queries, tuple(concepts), terms


@dataclass(frozen=True)
class _PageCandidate:
    citation: MemoryCitation
    source_id: str
    document_id: str
    text: str
    score: float
    concepts: frozenset[str]
    statements: frozenset[str]


def _primary_headings(body: str) -> tuple[str, ...]:
    lines = [re.sub(r"\s+", " ", line.casefold()).strip(" .:") for line in body[:500].splitlines()]
    lines = [line for line in lines if line and line not in {"table of contents", "contents"}][:3]
    # Titles can wrap across two lines. Later table column labels such as
    # "Income Statement" are not primary headings.
    return tuple(" ".join(lines[index : index + width]) for index in range(len(lines)) for width in (1, 2))


def _rank_page(
    citation: MemoryCitation,
    entry: MemoryEntryVersion,
    terms: tuple[str, ...],
    concepts: tuple[tuple[str, tuple[str, ...]], ...],
    frequency: Counter[str],
    count: int,
) -> _PageCandidate | None:
    refs = [ref for ref in entry.sources if ref.source_type == "pdf-page"]
    if len(refs) != 1 or not re.fullmatch(r"pdf:[0-9a-f]{64}:[0-9a-f]{64}:[1-9][0-9]*", refs[0].source_id):
        return None
    document_id, _, body = entry.text.partition("\n")
    normalized = " ".join(_words(body))
    words = set(normalized.split())
    years = {term for term in terms if re.fullmatch(r"(?:19|20)\d{2}", term)}
    weights = {term: math.log1p((count + 1) / (frequency[term] + 1)) for term in terms}
    matches = {name: tuple(phrase for phrase in phrases if _contains(normalized, phrase)) for name, phrases in concepts}
    matched = frozenset(name for name, phrases in matches.items() if phrases)
    if not matched and not words.intersection(set(terms) - years):
        return None
    lexical = sum(weight for term, weight in weights.items() if term in words)
    # Rare aliases carry more evidence than ubiquitous heading/index words.
    specificity = sum(
        max(
            (
                sum(math.log1p((count + 1) / (frequency[word] + 1)) for word in phrase.split()) / len(phrase.split())
                for phrase in phrases
            ),
            default=0,
        )
        for phrases in matches.values()
    )
    doc_words = set(_words(document_id))
    entity = sum(weights[term] for term in terms if term in doc_words and term not in years)
    year = 0.6 if years & doc_words else (-0.4 if years else 0.0)
    heading = " ".join(_words(body[:400]))
    title = sum(any(_contains(heading, phrase) for phrase in phrases) for _name, phrases in concepts)
    statements = (
        (
            r"(?:consolidated\s+)?(?:statements?\s+of\s+(?:income|operations|earnings)|income\s+statements?)",
            {"operating", "revenue", "earnings", "income_statement"},
        ),
        (
            r"(?:consolidated\s+)?(?:statements?\s+of\s+cash\s+flows?|cash\s+flow\s+statements?)",
            {"cashflow", "depreciation", "capex"},
        ),
        (
            r"(?:consolidated\s+)?(?:balance\s+sheets?|statements?\s+of\s+financial\s+position)",
            {"assets", "liabilities", "debt", "inventory", "equity", "balance_sheet"},
        ),
    )
    # A primary statement heading is stronger evidence than a footnote merely
    # mentioning many inputs of a calculation. Match headings, not references.
    requested = {name for name, _phrases in concepts}
    headings = _primary_headings(body)
    primary = frozenset(
        pattern
        for pattern, names in statements
        if requested & names and any(re.fullmatch(pattern + r"(?:\s+\([^)]*\))?", heading) for heading in headings)
    )
    statement_bonus = 12 * len(primary)
    score = lexical + 6 * len(matched) + 2 * specificity + min(entity, 4) + year + 0.7 * title + statement_bonus
    return _PageCandidate(citation, refs[0].source_id, document_id, body, score, matched, primary)


def _select_pages(candidates: dict[str, _PageCandidate], limit: int) -> tuple[MemoryCitation, ...]:
    selected: list[MemoryCitation] = []
    covered: set[str] = set()
    # Reserve one actual primary statement per requested family. A calculation's
    # income and cash-flow pages must not lose every slot to explanatory notes.
    for statement in sorted({statement for page in candidates.values() for statement in page.statements}):
        anchors = [page for page in candidates.values() if statement in page.statements]
        if not anchors or len(selected) == limit:
            continue
        best = min(anchors, key=lambda page: (-page.score, page.document_id, page.source_id))
        selected.append(best.citation)
        covered.update(best.concepts)
        del candidates[best.source_id]
    while candidates and len(selected) < limit:
        best = min(
            candidates.values(),
            key=lambda page: (
                -(page.score + 2 * len(page.concepts - covered)),
                page.document_id,
                page.source_id,
                page.citation.entry_id,
                page.citation.entry_version_id,
            ),
        )
        selected.append(best.citation)
        covered.update(best.concepts)
        del candidates[best.source_id]
    return tuple(selected)


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Retrieve bounded lexical pools, then rank exact pages in the current scope.

    Query variants isolate financial concepts from entities and answer instructions.
    Request-local document frequencies downweight repeated company/year headings;
    phrase coverage and a small diversity bonus preserve multi-page requirements.
    No external text, model, private database API, or cross-request cache is used.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("page limit must be between 1 and 100")  # noqa: TRY003
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must contain text")  # noqa: TRY003
    queries, concepts, terms = _query_plan(question)
    if not queries:
        return ()
    current = await memory.list()
    records = [
        record for record in current.entries if any(ref.source_type == "pdf-page" for ref in record.entry.sources)
    ]
    if not records:
        return ()
    frequency: Counter[str] = Counter(term for record in records for term in set(_words(record.entry.text)))
    count = len(records)
    candidates: dict[str, _PageCandidate] = {}
    checked: set[tuple[str, str]] = set()
    for query in queries:
        result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=100))
        # If a writer advances the head, use only results from the snapshot whose
        # active entries supplied our statistics. A later call takes a fresh head.
        if result.memory_ref != current.memory_ref:
            continue
        for hit in result.hits:
            key = (hit.entry_id, hit.entry_version_id)
            if key in checked:
                continue
            checked.add(key)
            citation = MemoryCitation(
                memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
            )
            entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
            page = _rank_page(citation, entry, terms, concepts, frequency, count)
            if page is not None:
                candidates[page.source_id] = page
    return _select_pages(candidates, limit)
