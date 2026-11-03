"""Fusion reranking of PDF page evidence: several query views, one offline-fitted page scorer.

Every view is retrieved through the public Memory API; pages are then ranked from the texts those
searches returned — BM25 with prefix-tolerant matching, one pseudo-relevance expansion round,
section-heading overlap, document affinity and consensus votes across the views. The weight vector
is a constant produced by an offline fit on public financial-report benchmarks; no answer labels,
evidence page lists or pre-parsed bodies are read, and no external corpus or persistent index is
used.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import Counter

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

_WORD = re.compile(r"[A-Za-z0-9]+")
_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_NUMBER = re.compile(r"\d[\d,.]*")
_APOSTROPHE = re.compile(r"['’‘`´]")
_STOPWORDS = frozenset(
    """
    a an the and or of to in on at by for from with as is are was were be been being
    what which who how why when where do does did can could should would we you it its
    our your this that these those please any reported report has have had not no
    as of for from during between under over per than then there their they
    """.split()
)
_EXPANSION_GROUPS = (
    (
        ("geograph", "region", "country", "countries", "territor"),
        ("reportable segments geographic areas", "geographic regions countries", "net revenue by segment"),
    ),
    (
        ("legal", "litig", "lawsuit", "battle", "claim", "proceeding"),
        ("legal proceedings", "litigation and other legal matters", "commitments and contingencies"),
    ),
    (
        ("restructur", "severance", "impairment", "asset write"),
        ("restructuring charges", "restructuring and asset impairment charges", "severance and related charges"),
    ),
    (
        ("ebitda", "operating income", "operating profit", "income from operations"),
        ("operating income", "income from operations", "consolidated statement of income"),
    ),
    (
        ("capex", "capital expenditure", "purchases of property"),
        ("capital expenditures", "purchases of property plant and equipment", "consolidated statement of cash flows"),
    ),
    (
        ("margin", "profitability", "gross profit", "cost of sales", "cost of revenue"),
        ("gross profit", "cost of sales", "operating profit", "consolidated statement of income"),
    ),
    (
        ("liquidity", "quick ratio", "current ratio", "working capital", "cash equivalent"),
        ("current assets", "cash and cash equivalents", "liquidity and capital resources", "consolidated balance sheet"),
    ),
    (
        ("concentration", "customer", "customers", "creditor"),
        ("customer concentration", "significant customers", "concentration of credit risk"),
    ),
    (("tax", "taxes", "levy"), ("income taxes", "effective tax rate", "provision for income taxes")),
    (
        ("debt", "borrowings", "bonds", "notes payable"),
        ("long-term debt", "senior notes", "debt and credit facility"),
    ),
    (
        ("segment", "segments", "division", "reporting"),
        ("reportable segments", "segment information", "net sales by segment"),
    ),
    (
        ("payable", "receivable", "days payable", "days sales", "inventory days"),
        ("accounts payable", "trade accounts payable and accruals", "consolidated balance sheet"),
    ),
    (
        ("acquisition", "merger", "divestiture", "m&a"),
        ("acquisitions and divestitures", "business acquisitions", "goodwill and other intangible assets"),
    ),
    (
        ("capital intensive", "property plant", "fixed asset", "depreciation"),
        ("property plant and equipment", "capital expenditures", "depreciation and amortization"),
    ),
    (
        ("growth", "grew", "decline", "dragged", "increase", "decrease", "change"),
        ("results of operations", "management s discussion and analysis", "year over year"),
    ),
)
_STATEMENT_MARKERS = (
    "consolidated statement of income",
    "consolidated statement of cash flows",
    "consolidated balance sheet",
    "management s discussion",
    "notes to consolidated financial statements",
    "results of operations",
)
_SECTION_MARKERS = (
    "legal proceedings",
    "litigation",
    "contingencies",
    "reportable segments",
    "restructuring",
    "capital expenditures",
    "income taxes",
    "liquidity",
    "customer concentration",
    "property plant and equipment",
    "effective tax rate",
    "accounts payable",
)
_NUMERIC_HINTS = frozenset(
    "ebitda margin cost costs revenue ratio rate amount total quantity percentage percent capex depreciation".split()
)
_DOCUMENT_STOP_TOKENS = frozenset("10k 10q 8k 10ka 11k annual report fy q1 q2 q3 q4 usd the and for".split())
_MIN_PREFIX = 6
_MAX_PREFIX_VARIANTS = 6
_PROXIMITY_WINDOW = 16
_BM25_K1 = 1.2
_BM25_B = 0.72
_SHARPNESS = 5.0
_GATE = 60
_PRF_PAGES = 40
_PRF_TERMS = 20
_HEADING = re.compile(r"^(item|note|part)\b", re.I)
_HEADING_PREFIX = re.compile(r"^(?:item|note|part)?\s*[\d\-.–]*\s*", re.I)


def _content_terms(question: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for term in _WORD.findall(question.casefold()):
        if len(term) < 2 or term in _STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _salient_terms(question: str, terms: list[str]) -> list[str]:
    def weight(term: str) -> tuple[int, int]:
        score = 0
        if any(character.isdigit() for character in term):
            score += 4
        if len(term) >= 8:
            score += 3
        if len(term) >= 5:
            score += 1
        if term.istitle() or term.isupper():
            score += 2
        return (-score, terms.index(term))

    return sorted(terms, key=weight)


def _expansion_queries(question: str, terms: list[str]) -> list[str]:
    folded = question.casefold()
    expansions: list[str] = []
    for prefixes, phrases in _EXPANSION_GROUPS:
        if any(term.startswith(prefix) or prefix in folded for term in terms for prefix in prefixes):
            expansions.extend(phrases)
    return expansions


_LENS = (8, 5, 3)


def _query_variants(question: str) -> tuple[str, ...]:
    """Query views for the engine: the whole question, term bags, rare-term pairs, triples, singles
    and financial phrase expansions. Each view is a separate search because the admission floor
    keeps a page only when it covers enough of that view's terms."""

    terms = _content_terms(question)
    if not terms:
        return (question.strip(),) if question.strip() else ()
    singles, pairs, triples = _LENS
    salient = _salient_terms(question, terms)
    variants: list[str] = [question.strip(), " ".join(terms)]
    variants.append(" ".join(salient[:4]))
    head = salient[:pairs]
    for index, left in enumerate(head):
        for right in head[index + 1 :]:
            variants.append(f"{left} {right}")
    trio = salient[:triples]
    for index, left in enumerate(trio):
        for middle_index, middle in enumerate(trio[index + 1 :], start=index + 1):
            for right in trio[middle_index + 1 :]:
                variants.append(f"{left} {middle} {right}")
    variants.extend(salient[:singles])
    variants.extend(_expansion_queries(question, terms))
    unique: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        normalized = " ".join(variant.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return tuple(unique)


def _question_grams(question: str) -> tuple[str, ...]:
    """Consecutive content-word runs of the question as 2..5 grams, longest first."""

    runs: list[list[str]] = []
    current: list[str] = []
    for token in _WORD.findall(question.casefold()):
        if token in _STOPWORDS or len(token) < 3:
            if current:
                runs.append(current)
            current = []
        else:
            current.append(token)
    if current:
        runs.append(current)
    grams = {
        " ".join(run[start : start + size])
        for run in runs
        for size in (5, 4, 3, 2)
        for start in range(len(run) - size + 1)
    }
    return tuple(sorted(grams, key=lambda gram: (-len(gram), gram)))


_LEXICAL_NAMES = (
    "bm25",
    "coverage",
    "salient",
    "prior_max",
    "view",
    "ngram",
    "window",
    "window_prf",
    "bm25_prf",
    "coverage_prf",
    "ngram_prf",
    "heading",
    "heading_prf",
)
_SECONDARY_NAMES = ("density", "statement", "section", "numcov", "concise", "affinity", "prior")
_SIGNAL_NAMES = (
    "prior",
    "prior_max",
    "view",
    "ngram",
    "bm25",
    "coverage",
    "salient",
    "window",
    "bm25_prf",
    "coverage_prf",
    "window_prf",
    "ngram_prf",
    "numcov",
    "density",
    "statement",
    "section",
    "heading",
    "heading_prf",
    "affinity",
    "concise",
)
# Tuned offline against FinanceBench gold (~/ob2026/pc-eval); see 07_赛题PowerContext/T8_REPORT.md.
_WEIGHTS: dict[str, float] = {
    "bm25": 0.4,
    "concise": 0.2,
    "coverage_prf~": 0.6,
    "density": 0.2,
    "density gated": 1.0,
    "density~": 0.4,
    "prior": 0.2,
    "prior_max~": 0.4,
    "salient~": 0.2,
    "section": 0.2,
    "window_prf~": 0.2,
}


_MIN_PAGE_TOKENS = 25


def _document_name_tokens(document_id: str) -> tuple[str, ...]:
    tokens = [token for token in _SPLIT.split(document_id.casefold()) if token]
    return tuple(
        dict.fromkeys(
            token
            for token in tokens
            if token not in _DOCUMENT_STOP_TOKENS and not token.isdigit() and not _YEAR.fullmatch(token)
        )
    )


class _Page:
    """One pooled page and the token statistics every page signal reads."""

    __slots__ = ("body", "citation", "counts", "document", "headings", "joined", "length", "tokens")

    def __init__(self, citation: MemoryCitation, text: str) -> None:
        self.citation = citation
        self.tokens = tuple(_WORD.findall(text.casefold()))
        self.length = len(self.tokens) or 1
        self.counts: Counter = Counter(self.tokens)
        self.joined = " ".join(self.tokens)
        document, _, body = text.partition("\n")
        self.document = document.strip()
        normalized = _APOSTROPHE.sub("", body).casefold()
        self.headings = " ".join(_heading_lines(body))
        self.body = normalized

    @property
    def digit_share(self) -> float:
        return sum(count for token, count in self.counts.items() if token[:1].isdigit()) / self.length


def _heading_lines(text: str) -> tuple[str, ...]:
    """Section titles on a page: ITEM/NOTE/PART headings and short uppercase lines."""

    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) > 90:
            continue
        if _HEADING.match(stripped) or (stripped.isupper() and len(stripped) > 5):
            lines.append(_HEADING_PREFIX.sub("", stripped).casefold())
    return tuple(lines)


class _Pool:
    """Candidate pages of one question plus the corpus statistics the reranker reads."""

    def __init__(self, pages: list[_Page]) -> None:
        self.pages = pages
        self.size = len(pages)
        self.vocab = sorted({token for page in pages for token in page.counts})
        pages_per_term: Counter = Counter()
        for page in pages:
            pages_per_term.update(page.counts.keys())
        self.pages_per_term = pages_per_term
        self.average_length = sum(page.length for page in pages) / self.size
        self.documents = {page.document for page in pages}

    def surfaces(self, term: str) -> tuple[str, ...]:
        """Exact term plus vocabulary sharing its prefix, so inflected forms still match."""

        if len(term) < _MIN_PREFIX or term.isdigit():
            return (term,)
        start, stop = bisect_left(self.vocab, term), bisect_right(self.vocab, f"{term}\uffff")
        variants = [token for token in self.vocab[start:stop] if token != term and len(token) - len(term) <= 6]
        return tuple(dict.fromkeys([term, *sorted(variants, key=len)[:_MAX_PREFIX_VARIANTS]]))

    def frequency(self, forms: tuple[str, ...]) -> list[int]:
        if len(forms) == 1:
            return [page.counts.get(forms[0], 0) for page in self.pages]
        return [sum(page.counts.get(form, 0) for form in forms) for page in self.pages]

    def idf(self, present: int) -> float:
        return math.log(1.0 + (self.size - present + 0.5) / (present + 0.5))


def _term_index(pool: _Pool, terms: tuple[str, ...]) -> tuple[dict[str, tuple[str, ...]], dict[str, list[int]], dict[str, float]]:
    surfaces = {term: pool.surfaces(term) for term in terms}
    counts = {term: pool.frequency(surfaces[term]) for term in terms}
    idf = {term: pool.idf(sum(1 for value in counts[term] if value)) for term in terms}
    return surfaces, counts, idf


def _bm25(pool: _Pool, idf: dict[str, float], counts: dict[str, list[int]]) -> list[float]:
    ceiling = sum(idf.values()) * (_BM25_K1 + 1.0) or 1.0
    scales = [_BM25_K1 * (1.0 - _BM25_B + _BM25_B * page.length / pool.average_length) for page in pool.pages]
    scores = [0.0] * pool.size
    for term, weight in idf.items():
        for index, frequency in enumerate(counts[term]):
            if frequency:
                scores[index] += weight * frequency * (_BM25_K1 + 1.0) / (frequency + scales[index])
    return [score / ceiling for score in scores]


def _coverage(idf: dict[str, float], counts: dict[str, list[int]]) -> list[float]:
    mass = sum(idf.values()) or 1.0
    terms = tuple(idf)
    return [sum(idf[term] for term in terms if counts[term][index]) / mass for index in range(len(counts[terms[0]]))]


def _window(pool: _Pool, surfaces: dict[str, tuple[str, ...]], terms: tuple[str, ...]) -> list[float]:
    """Share of the given rare terms that one proximity window of the page covers together."""

    group = {term: surfaces[term] for term in terms if term in surfaces}
    scores = [0.0] * pool.size
    if len(group) < 2:
        return scores
    form_to_term: dict[str, str] = {}
    for term, forms in group.items():
        for form in forms:
            form_to_term.setdefault(form, term)
    for index, page in enumerate(pool.pages):
        events = [
            (position, form_to_term[token]) for position, token in enumerate(page.tokens) if token in form_to_term
        ]
        if len(events) < 2:
            continue
        seen: Counter = Counter()
        best = 1
        left = 0
        for position, term in events:
            seen[term] += 1
            while position - events[left][0] > _PROXIMITY_WINDOW:
                seen[events[left][1]] -= 1
                if not seen[events[left][1]]:
                    del seen[events[left][1]]
                left += 1
            best = max(best, len(seen))
            if best == len(group):
                break
        scores[index] = best / len(group)
    return scores


def _grams(pool: _Pool, grams: tuple[str, ...]) -> list[float]:
    """Longest question n-gram the page reproduces, as the evidence phrase actually quoted."""

    scores = [0.0] * pool.size
    for gram in grams:
        length = len(gram.split())
        pattern = f" {gram} "
        for index, page in enumerate(pool.pages):
            if scores[index] < length and pattern in f" {page.joined} ":
                scores[index] = float(length)
    return [score / 4.0 for score in scores]


def _headings(pool: _Pool, surfaces: dict[str, tuple[str, ...]], idf: dict[str, float]) -> list[float]:
    mass = sum(idf.values()) or 1.0
    cutoff = sorted(idf.values())[len(idf) // 2]
    rare = tuple(term for term in idf if idf[term] >= cutoff)
    return [
        sum(
            idf[term]
            for term in rare
            if any(f" {form} " in f" {page.headings} " for form in surfaces.get(term, (term,)))
        )
        / mass
        for page in pool.pages
    ]


def _ranks(scores: list[float]) -> list[int]:
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    ranks = [0] * len(scores)
    for rank, index in enumerate(order, start=1):
        ranks[index] = rank
    return ranks


def _percentile(ranks: list[int], size: int) -> list[float]:
    return [1.0 - (value - 1) / max(size - 1, 1) for value in ranks]


def _decay(ranks: list[int]) -> list[float]:
    return [(_SHARPNESS + 1.0) / (_SHARPNESS + value) for value in ranks]


def _view_rows(pool: _Pool, views: list[tuple[str, list[_Page]]], terms: tuple[str, ...], idf: dict[str, float]):
    """Per-page fusion of the ranks that the individual query views gave it."""

    mass = sum(idf.values()) or 1.0
    wanted = set(terms)
    position = {id(page): index for index, page in enumerate(pool.pages)}
    prior = [0.0] * pool.size
    prior_max = [0.0] * pool.size
    view = [0.0] * pool.size
    for query, ordered in views:
        query_terms = frozenset(_WORD.findall(query.casefold())) & wanted
        specificity = sum(idf.get(term, 0.0) for term in query_terms) / mass
        weight = 3.0 if len(_WORD.findall(query.casefold())) >= 4 else 1.0
        for rank, page in enumerate(ordered, start=1):
            index = position[id(page)]
            prior[index] += weight / (40.0 + rank)
            prior_max[index] = max(prior_max[index], 1.0 / (40.0 + rank))
            view[index] = max(view[index], specificity / (10.0 + rank))
    return prior, prior_max, view


def _signal_rows(
    pool: _Pool, question: str, views: list[tuple[str, list[_Page]]]
) -> dict[str, list[float]]:
    """Every page signal for one question, including the pseudo-relevance expansion round."""

    terms = tuple(_content_terms(question))
    expansion = tuple(
        dict.fromkeys(
            term
            for phrase in _expansion_queries(question, list(terms))
            for term in _WORD.findall(phrase.casefold())
        )
    )
    all_terms = tuple(dict.fromkeys([*terms, *expansion]))
    surfaces, counts, idf = _term_index(pool, all_terms)
    prior, prior_max, view = _view_rows(pool, views, all_terms, idf)
    rare = tuple(sorted(idf, key=lambda term: -idf[term])[: max(2, len(idf) // 3)])
    salient = frozenset(_salient_terms(question, list(terms))[:4])
    salient_mass = sum(idf.get(term, 0.0) for term in salient) or 1.0
    numbers = tuple(dict.fromkeys(_NUMBER.findall(question)))
    numeric = any(character.isdigit() for term in terms for character in term) or any(
        term in _NUMERIC_HINTS for term in terms
    )
    folded = frozenset(_WORD.findall(question.casefold()))
    named = frozenset(
        document
        for document in pool.documents
        if len(pool.documents) > 1 and any(token in folded for token in _document_name_tokens(document))
    )
    grams = _question_grams(question)
    rows: dict[str, list[float]] = {
        "prior": prior,
        "prior_max": prior_max,
        "view": view,
        "ngram": _grams(pool, grams),
        "bm25": _bm25(pool, idf, counts),
        "coverage": _coverage(idf, counts),
        "salient": [
            sum(idf.get(term, 0.0) for term in salient if counts[term][index]) / salient_mass
            for index in range(pool.size)
        ],
        "window": _window(pool, surfaces, rare[:6]),
        "numcov": [
            (sum(1 for number in numbers if number in page.body) / len(numbers) if numbers else 0.0)
            for page in pool.pages
        ],
        "density": [page.digit_share for page in pool.pages],
        "statement": [
            1.0 if numeric and any(marker in page.body for marker in _STATEMENT_MARKERS) else 0.0
            for page in pool.pages
        ],
        "section": [
            min(1.0, sum(1 for marker in _SECTION_MARKERS if marker in page.body) / 2.0) for page in pool.pages
        ],
        "heading": _headings(pool, surfaces, idf),
        "affinity": [1.0 if named and page.document in named else 0.0 for page in pool.pages],
        "concise": [1.0 / math.sqrt(page.length / pool.average_length) for page in pool.pages],
    }

    base = [
        rows["bm25"][index]
        + 0.8 * rows["coverage"][index]
        + 0.6 * rows["prior_max"][index]
        + 0.5 * rows["ngram"][index]
        + 0.4 * rows["affinity"][index]
        for index in range(pool.size)
    ]
    feedback = sorted(range(pool.size), key=lambda index: (-base[index], index))[:_PRF_PAGES]
    query_set = set(all_terms)
    mined: Counter = Counter()
    for index in feedback:
        mined.update(term for term in pool.pages[index].counts if term not in query_set)
    informative = [
        (pool.idf(pool.pages_per_term.get(term, 0)) * (seen / max(len(feedback), 1)), term)
        for term, seen in mined.items()
        if len(term) >= 5 and not term.isdigit()
    ]
    extra = tuple(term for _score, term in sorted(informative, reverse=True)[:_PRF_TERMS])
    extended = tuple(dict.fromkeys([*all_terms, *extra]))
    surfaces2, counts2, idf2 = _term_index(pool, extended)
    rare2 = tuple(sorted(idf2, key=lambda term: -idf2[term])[: max(2, len(idf2) // 3)])
    rows["bm25_prf"] = _bm25(pool, idf2, counts2)
    rows["coverage_prf"] = _coverage(idf2, counts2)
    rows["window_prf"] = _window(pool, surfaces2, rare2[:8])
    rows["ngram_prf"] = _grams(pool, _question_grams(f"{question} {' '.join(extra)}"))
    rows["heading_prf"] = _headings(pool, surfaces2, idf2)
    return rows


def _feature_scores(rows: dict[str, list[float]]) -> dict[str, list[float]]:
    """Raw signals become rank features: per-signal views, consensus votes and gated secondaries."""

    size = len(next(iter(rows.values())))
    ranks = {name: _ranks(values) for name, values in rows.items()}
    names = tuple(rows)
    features = {name: _percentile(ranks[name], size) for name in names}
    features.update({f"{name}~": _decay(ranks[name]) for name in names})
    decayed = [[_decay(ranks[name])[index] for index in range(size)] for name in names]
    columns = list(zip(*decayed, strict=True))
    for threshold in (10, 30, 60):
        features[f"votes{threshold}"] = [
            sum(1 for name in names if ranks[name][index] <= threshold) / len(names) for index in range(size)
        ]
    features["bestview"] = [max(column) for column in columns]
    features["secondview"] = [sorted(column)[-2] for column in columns]
    features["combmnz"] = [sum(column) / len(names) for column in columns]
    lexical = tuple(name for name in _LEXICAL_NAMES if name in ranks)
    gated = tuple(name for name in _SECONDARY_NAMES if name in ranks)
    for name in gated:
        values = _decay(ranks[name])
        features[f"{name} gated"] = [
            value if min(ranks[term][index] for term in lexical) <= _GATE else 0.0
            for index, value in enumerate(values)
        ]
    return features


def _ordered(pages: list[_Page], rows: dict[str, list[float]]) -> list[_Page]:
    features = _feature_scores(rows)
    scores = [
        sum(weight * features[name][index] for name, weight in _WEIGHTS.items() if name in features)
        for index in range(len(pages))
    ]
    return [pages[index] for index in sorted(range(len(pages)), key=lambda index: (-scores[index], index))]


async def _confirmed(memory, pages: list[_Page], limit: int) -> tuple[MemoryCitation, ...]:
    """Accept only citations that resolve to exactly one page Source, keeping pages distinct."""

    citations: list[MemoryCitation] = []
    for page in pages:
        entry = (await memory.get(GetMemoryEntryRequest(citation=page.citation))).entry
        if len(entry.sources) != 1:
            continue
        citations.append(page.citation)
        if len(citations) == limit:
            break
    return tuple(citations)


async def search_pdf_pages(
    memory,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    if limit < 1 or not question.strip():
        return ()

    pool: dict[tuple[str, str], _Page] = {}
    seen: list[tuple[str, list[tuple[str, str]]]] = []
    for query in _query_variants(question):
        hits = (await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(limit * 8, 120)))).hits
        identities: list[tuple[str, str]] = []
        for hit in hits:
            identity = (hit.entry_id, hit.entry_version_id)
            if identity not in pool:
                pool[identity] = _Page(
                    MemoryCitation(
                        memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                    ),
                    hit.text,
                )
            identities.append(identity)
        seen.append((query, identities))

    pages = [page for page in pool.values() if page.length >= _MIN_PAGE_TOKENS]
    if not pages:
        return ()
    views = [
        (query, [pool[identity] for identity in identities if pool[identity].length >= _MIN_PAGE_TOKENS])
        for query, identities in seen
    ]
    ranked = _ordered(pages, _signal_rows(_Pool(pages), question, views))
    return await _confirmed(memory, ranked[: max(limit * 3, limit)], limit)
