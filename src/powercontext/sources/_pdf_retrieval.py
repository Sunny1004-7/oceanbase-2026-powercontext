"""Request-local lexical retrieval of complete, current Memory PDF pages."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime.application import ScopedMemoryApplication
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, MemoryEntryRecord, SearchMemoryRequest

_STOP = frozenset([
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
    "quantity",
    "together",
    "according",
    "retrieve",
    "locate",
    "provide",
    "find",
    "versus",
    "vs",
    "than",
    "rather",
    "following",
    "based",
    "company",
    "annual",
    "financial",
    "statements",
    "statement",
    "consolidated",
    "period",
    "amount",
    "amounts",
    "total",
    "input",
    "inputs",
    "page",
    "pages",
    "10k",
    "10",
    "k",
    "form",
    "please",
    "expressed",
    "express",
    "billion",
    "billions",
    "percentage",
    "percent",
    "ratio",
    "value",
    "values",
    "us",
    "inc",
    "plc",
    "corporation",
])
_ALIASES = (
    ("revenue", ("revenue", "net sales", "turnover", "sales revenue")),
    ("operating", ("operating income", "operating profit", "income from operations", "profit from operations", "ebit")),
    ("earnings", ("net income", "net earnings", "net profit", "earnings per share", "income for the period")),
    ("depreciation", ("depreciation", "amortization", "amortisation", "d a")),
    (
        "capex",
        (
            "capex",
            "capital expenditure",
            "capital spending",
            "purchases of property",
            "payments for property",
            "additions to property",
        ),
    ),
    ("cashflow", ("cash flow", "operating cash", "cash generated", "cash provided by", "cash from operations")),
    ("litigation", ("litigation", "legal battle", "legal proceedings", "lawsuit", "ongoing suit")),
    (
        "geography",
        (
            "geographic",
            "geographical",
            "geographies",
            "countries",
            "regions",
            "primarily operate",
            "principally operate",
        ),
    ),
    ("restructuring", ("restructuring", "reorganization", "reorganisation", "severance")),
    ("debt", ("debt", "borrowing", "credit facility", "notes payable")),
    ("inventory", ("inventory", "inventories", "stock on hand")),
    ("dividends", ("dividend", "cash distribution")),
    ("repurchases", ("repurchase", "buyback", "treasury stock")),
    ("tax", ("income tax", "tax expense", "effective tax rate", "taxation")),
    ("pension", ("pension", "retirement benefits", "postretirement")),
    ("lease", ("lease", "right of use", "rental")),
    ("acquisition", ("acquisition", "business combination")),
    ("assets", ("current assets", "total assets")),
    ("liabilities", ("current liabilities", "total liabilities")),
)
_STATEMENTS = {
    "income": r"(?:consolidated\s+)?(?:statements?\s+of\s+(?:income|operations|earnings)|income\s+statements?)",
    "cashflow": r"(?:consolidated\s+)?(?:statements?\s+of\s+cash\s+flows?|cash\s+flows?\s+statements?)",
    "balance": r"(?:consolidated\s+)?(?:balance\s+sheets?|statements?\s+of\s+financial\s+position)",
}


def _words(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("\u00ad", "")
    normalized = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", normalized)
    normalized = re.sub(r"\bfy\s*(?=\d{4})", " ", normalized)
    return tuple(re.findall(r"[^\W_]+", normalized))


def _stem(word: str) -> str:
    word = word.replace("isation", "ization")
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_stem(word) for word in _words(text))


def _contains(tokens: Sequence[str], phrase: Sequence[str]) -> bool:
    return bool(phrase) and (" " + " ".join(phrase) + " ") in (" " + " ".join(tokens) + " ")


@dataclass(frozen=True)
class _Page:
    record: MemoryEntryRecord
    source_id: str
    document: str
    document_id: str | None
    number: int
    body: str
    tokens: tuple[str, ...]
    headings: frozenset[str]


@dataclass(frozen=True)
class _Question:
    terms: tuple[str, ...]
    concepts: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]
    phrases: tuple[tuple[str, ...], ...]
    years: frozenset[str]
    statements: frozenset[str]
    queries: tuple[str, ...]


@dataclass(frozen=True)
class _RankedPage:
    page: _Page
    score: float
    lexical: float
    local: float
    coverage: float
    concepts: frozenset[str]
    statement: frozenset[str]


def _document_body(text: str, document_hash: str) -> tuple[str | None, str]:
    # SourceRef carries no projection metadata. Verify the complete ID prefix,
    # including embedded newlines, against the Source's stable document digest.
    digest = hashlib.sha256()
    previous = 0
    for match in re.finditer("\n", text):
        end = match.start()
        digest.update(text[previous:end].encode("utf-8"))
        if digest.hexdigest() == document_hash:
            return text[:end], text[end + 1 :]
        digest.update(b"\n")
        previous = end + 1
    # Canonical NFC/outer whitespace can prevent recovering the original ID.
    # Keep the complete text and group by Source identity rather than inventing it.
    return None, text


def _headings(text: str) -> frozenset[str]:
    lines = [" ".join(_words(line)).strip() for line in text[:700].splitlines()]
    lines = [line for line in lines if line and not line.isdecimal() and line not in {"table of contents", "contents"}][
        :3
    ]
    titles = [" ".join(lines[index : index + width]) for index in range(len(lines)) for width in (1, 2)]
    return frozenset(
        name
        for name, pattern in _STATEMENTS.items()
        if any(re.fullmatch(pattern + r"(?: continued)?(?: in millions)?", title) for title in titles)
    )


def _pages(records: Sequence[MemoryEntryRecord]) -> tuple[_Page, ...]:
    pages = []
    for record in records:
        refs = record.entry.sources
        if record.state != "active" or len(refs) != 1 or refs[0].source_type != "pdf-page":
            continue
        match = re.fullmatch(r"pdf:([0-9a-f]{64}):([0-9a-f]{64}):([1-9][0-9]*)", refs[0].source_id)
        if match is None:
            continue
        document_id, body = _document_body(record.entry.text, match[1])
        pages.append(
            _Page(
                record,
                refs[0].source_id,
                match[1] + ":" + match[2],
                document_id,
                int(match[3]),
                body,
                _tokens(body),
                _headings(body),
            )
        )
    unique = {}
    for page in sorted(pages, key=lambda item: (-len(item.body), item.record.entry.entry_id)):
        unique.setdefault(page.source_id, page)
    return tuple(unique.values())


def _question(question: str) -> _Question:
    tokens = _tokens(question)
    terms = tuple(dict.fromkeys(word for word in tokens if word not in _STOP))
    concepts = [
        (name, tuple(_tokens(phrase) for phrase in phrases))
        for name, phrases in _ALIASES
        if any(_contains(tokens, _tokens(phrase)) for phrase in phrases)
    ]
    derived = set()
    if "ebitda" in tokens:
        derived.update(("operating", "depreciation"))
        if "margin" in tokens:
            derived.add("revenue")
    if _contains(tokens, ("free", "cash", "flow")):
        derived.update(("cashflow", "capex"))
    concepts.extend(
        (name, tuple(_tokens(phrase) for phrase in phrases))
        for name, phrases in _ALIASES
        if name in derived and name not in {item[0] for item in concepts}
    )
    statements = {name for name, pattern in _STATEMENTS.items() if re.search(pattern, " ".join(_words(question)))}
    names = {name for name, _ in concepts}
    if names & {"operating", "revenue", "earnings"}:
        statements.add("income")
    if names & {"capex", "cashflow", "depreciation"}:
        statements.add("cashflow")
    if names & {"assets", "liabilities", "inventory"}:
        statements.add("balance")
    phrases = tuple(tuple(word for word in pair if word not in _STOP) for pair in pairwise(tokens))
    phrases = tuple(pair for pair in phrases if len(pair) == 2)
    queries = [" ".join(terms[:32])]
    queries.extend(" ".join(phrase) for _name, variants in concepts for phrase in variants[:1])
    queries.extend(" ".join(terms[index : index + 3]) for index in range(0, min(9, len(terms)), 3))
    return _Question(
        terms,
        tuple(concepts),
        phrases,
        frozenset(word for word in tokens if re.fullmatch(r"(?:19|20)\d{2}", word)),
        frozenset(statements),
        tuple(dict.fromkeys(query for query in queries if query))[:8],
    )


def _route(pages: tuple[_Page, ...], question: _Question) -> tuple[_Page, ...]:
    names: dict[str, set[str]] = defaultdict(set)
    for page in pages:
        if page.document_id is not None:
            names[page.document].update(_tokens(page.document_id))
    terms = set(question.terms)
    explicit = {doc for doc, words in names.items() if (words - _STOP - question.years) & terms - question.years}
    if not explicit:
        return pages
    # Multiple named companies and years are a comparison, not a unique route.
    dated = {doc for doc in explicit if names[doc] & question.years}
    selected = dated or explicit
    # A missing year for one named company must not drop that company's evidence.
    if any(
        not any((names[doc] - _STOP - question.years) & (names[other] - _STOP - question.years) for other in dated)
        for doc in explicit
    ):
        selected = explicit
    return tuple(page for page in pages if page.document in selected)


def _bm25(counts: Counter[str], length: int, average: float, weights: dict[str, float]) -> float:
    norm = 1.2 * (0.35 + 0.65 * length / max(average, 1))
    return sum(weight * counts[word] * 2.2 / (counts[word] + norm) for word, weight in weights.items() if counts[word])


def _score(page: _Page, question: _Question, weights: dict[str, float], average: float) -> _RankedPage | None:
    counts = Counter(page.tokens)
    lexical = _bm25(counts, len(page.tokens), average, weights)
    matched = frozenset(
        name for name, variants in question.concepts if any(_contains(page.tokens, phrase) for phrase in variants)
    )
    if not lexical and not matched:
        return None
    coverage = sum(weight for term, weight in weights.items() if counts[term]) / max(sum(weights.values()), 1)
    windows = [page.tokens[index : index + 120] for index in range(0, len(page.tokens), 60)]
    local = max((_bm25(Counter(window), len(window), 120, weights) for window in windows), default=0)
    phrase = sum(
        sum(weights.get(word, 0) for word in pair) for pair in question.phrases if _contains(page.tokens, pair)
    )
    # Concepts expand narrow accounting aliases, never the captured text. Local
    # coverage distinguishes a coherent explanation from scattered note mentions.
    local_concepts = max(
        (
            sum(any(_contains(window, phrase) for phrase in variants) for _name, variants in question.concepts)
            for window in windows
        ),
        default=0,
    )
    statement = page.headings & question.statements
    score = (
        0.65 * lexical
        + 0.55 * local
        + 0.3 * phrase
        + 2 * coverage
        + 1.8 * len(matched)
        + local_concepts
        + 5 * len(statement)
    )
    return _RankedPage(page, score, lexical, local, coverage, matched, statement)


def _rank_corpus(pages: tuple[_Page, ...], question: _Question) -> tuple[_RankedPage, ...]:
    routed = _route(pages, question)
    if not routed:
        return ()
    identities = {word for page in routed if page.document_id for word in _tokens(page.document_id)}
    terms = [term for term in question.terms if term not in identities and term not in question.years]
    frequency = Counter(term for page in routed for term in set(page.tokens))
    weights = {term: math.log(1 + (len(routed) - frequency[term] + 0.5) / (frequency[term] + 0.5)) for term in terms}
    average = sum(len(page.tokens) for page in routed) / len(routed)
    ranked = [score for page in routed if (score := _score(page, question, weights, average)) is not None]
    return tuple(sorted(ranked, key=lambda row: (-row.score, row.page.document, row.page.number, row.page.source_id)))


def _select(ranked: tuple[_RankedPage, ...], limit: int) -> tuple[_Page, ...]:
    selected: list[_Page] = []
    remaining = list(ranked)
    anchors: set[tuple[str, str]] = set()
    for row in ranked:
        keys = {(row.page.document, name) for name in row.statement}
        if keys - anchors and row.score >= ranked[0].score * 0.35:
            selected.append(row.page)
            anchors.update(keys)
            remaining.remove(row)
        if len(selected) >= min(limit, 4):
            break
    covered: set[str] = set()
    while remaining and len(selected) < limit:
        row = max(
            remaining,
            key=lambda item: item.score + len(item.concepts - covered) - _duplicate_penalty(item.page, selected),
        )
        selected.append(row.page)
        covered.update(row.concepts)
        remaining.remove(row)
    return tuple(selected)


def _duplicate_penalty(page: _Page, selected: list[_Page]) -> float:
    words = set(page.tokens)
    for previous in selected:
        other = set(previous.tokens)
        if words and len(words & other) / max(len(words | other), 1) > 0.9:
            return 8.0
    return 0.0


async def _snapshot_search(
    memory: ScopedMemoryApplication, question: _Question, limit: int
) -> tuple[MemoryCitation, ...] | None:
    current = await memory.list()
    pages = _pages(current.entries)
    if not pages:
        return ()
    # Keep the native FTS candidate path. The complete public current-page view
    # supplements channel truncation; it is the authoritative corpus for scoring.
    admitted: set[tuple[str, str]] = set()
    for query in question.queries:
        result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=100))
        if result.memory_ref != current.memory_ref:
            return None
        admitted.update((hit.entry_id, hit.entry_version_id) for hit in result.hits)
    ranked = _rank_corpus(pages, question)
    # Channel membership breaks equal lexical scores without excluding full-view
    # candidates. All candidates retain their exact stored entry identities.
    ranked = tuple(
        sorted(
            ranked,
            key=lambda row: (
                -row.score,
                (row.page.record.entry.entry_id, row.page.record.entry.entry_version_id) not in admitted,
                row.page.document,
                row.page.number,
            ),
        )
    )
    chosen = _select(ranked, limit)
    for page in chosen:
        exact = await memory.get(GetMemoryEntryRequest(citation=page.record.citation))
        if exact.state != "active" or exact.entry != page.record.entry or exact.memory_ref != current.memory_ref:
            raise RuntimeError("PDF citation did not resolve to the selected active page")  # noqa: TRY003
    if (await memory.list()).memory_ref != current.memory_ref:
        return None
    return tuple(page.record.citation for page in chosen)


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Rank complete current pages, preserving exact citations and snapshot state.

    Only scoped public list/search/get APIs are used. Statistics, document routing,
    BM25 and passage co-occurrence are rebuilt per request from actual Memory
    pages. No external corpus, persistent index, model or answer labels are read.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("page limit must be between 1 and 100")  # noqa: TRY003
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must contain text")  # noqa: TRY003
    plan = _question(question)
    if not plan.queries:
        return ()
    for _ in range(3):
        result = await _snapshot_search(memory, plan, limit)
        if result is not None:
            return result
    raise RuntimeError("Memory head changed repeatedly during PDF search; retry the request")  # noqa: TRY003
