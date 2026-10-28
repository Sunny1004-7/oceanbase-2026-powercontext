"""Query-aware, bounded literal evidence selection without models or I/O.

Offsets always refer to the original string. Scoring uses a separate lexical
view; a small beam compares complete serialized envelopes, including citations.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuild,
    PreparedContextFragment,
    PreparedContextOmissions,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

ALGORITHM_VERSION = "literal-beam-1"
_STOP = frozenset([
    "a",
    "an",
    "the",
    "of",
    "to",
    "in",
    "on",
    "at",
    "for",
    "from",
    "by",
    "with",
    "as",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "does",
    "do",
    "did",
    "how",
    "what",
    "when",
    "where",
    "which",
    "who",
    "whether",
    "can",
    "could",
    "would",
    "should",
    "have",
    "has",
    "had",
    "than",
    "then",
    "show",
    "shows",
])
_ABBREVIATIONS = frozenset([
    "dr",
    "mr",
    "mrs",
    "ms",
    "prof",
    "vs",
    "etc",
    "e.g",
    "i.e",
    "fig",
    "figs",
    "al",
    "st",
    "no",
    "approx",
    "inc",
    "u.s",
])
_RESULT = frozenset([
    "find",
    "found",
    "observe",
    "observed",
    "result",
    "demonstrate",
    "conclude",
    "retain",
    "reduce",
    "increase",
    "decrease",
    "lower",
    "inhibit",
    "suppress",
    "activate",
    "predict",
    "bind",
    "express",
    "detect",
    "improve",
    "associate",
    "induce",
    "restore",
    "reverse",
    "survive",
])
_WORD = re.compile(r"[a-z]+|\d+")
Span = tuple[int, int, int]
Selection = tuple[Span, ...]


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def terms(text: str) -> tuple[str, ...]:
    """Normalize only the scoring view; never use it to derive coordinates."""
    words = _WORD.findall(unicodedata.normalize("NFKC", text).casefold())
    return tuple(_stem(word) for word in words if word not in _STOP)


def sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Find literal sentence/line intervals, protecting decimals and abbreviations."""
    spans = []
    start = 0
    for match in re.finditer(r"[.!?]+(?=\s|$)|\n+", text):
        end = match.end()
        if match.group().startswith("."):
            previous = re.search(r"([\w.]+)\.$", text[:end])
            if previous and (previous[1].casefold() in _ABBREVIATIONS or len(previous[1]) == 1):
                continue
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            spans.append((left, right))
        start = end
    if text[start:].strip():
        left = start
        while text[left].isspace():
            left += 1
        spans.append((left, len(text.rstrip())))
    return tuple(spans)


@dataclass(frozen=True)
class _Document:
    address: MemoryEntryAddress
    text: str
    sentences: tuple[tuple[int, int], ...]
    words: tuple[frozenset[str], ...]


def render_fragments(fragments: Sequence[PreparedContextFragment]) -> str:
    """Render the entire charged wire representation in its final encoding."""
    items = [
        {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,
                "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),
                "entry_id": fragment.address.entry_id,
                "entry_version_id": fragment.address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }
        for fragment in fragments
    ]
    encoded = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((
        TRUST_POLICY,
        "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n" + encoded + "\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1",
    ))


def _documents(groups: Sequence[PreparedMemoryCandidates]) -> tuple[_Document, ...]:
    result = []
    seen = set()
    for group in groups:
        for hit in group.hits:
            address = MemoryEntryAddress(
                memory=ArtifactAddress(scope_id=group.scope_id, artifact=hit.memory_ref),
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            key = (
                group.scope_id,
                hit.memory_ref.family,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            if key in seen:
                continue
            seen.add(key)
            spans = sentence_spans(hit.text)
            result.append(_Document(address, hit.text, spans, tuple(frozenset(terms(hit.text[a:b])) for a, b in spans)))
    return tuple(result)


def _weights(query: str, documents: Sequence[_Document]) -> dict[str, float]:
    sentences = [words for doc in documents for words in doc.words]
    frequency = Counter(word for words in sentences for word in words)
    return {word: 1 + math.log(1 + (len(sentences) + 1) / (frequency[word] + 1)) for word in set(terms(query))}


def _relevance(words: frozenset[str], weights: dict[str, float]) -> float:
    matched = words.intersection(weights)
    if not matched:
        return 0.0
    overlap = sum(weights[word] for word in matched)
    coverage = overlap / max(1.0, sum(weights.values()))
    # A complete result sentence has value beyond repeated topic headings.
    assertion = 1.12 if words.intersection(_RESULT) else 1.0
    numeric = 0.12 if any(word.isdigit() for word in words) else 0.0
    return overlap * (0.65 + coverage + numeric) * assertion


def _options(documents: Sequence[_Document], weights: dict[str, float]) -> tuple[Span, ...]:  # noqa: C901 - bounded span alternatives
    options: dict[Span, float] = {}
    for doc_id, doc in enumerate(documents):
        scores = [_relevance(words, weights) for words in doc.words]
        ranked = sorted((i for i, score in enumerate(scores) if score > 0), key=lambda i: (-scores[i], i))[:8]
        if not ranked:
            continue
        for i in ranked:
            for low, high in [
                (i, i),
                (max(0, i - 1), i),
                (i, min(len(scores) - 1, i + 1)),
                (max(0, i - 1), min(len(scores) - 1, i + 1)),
            ]:
                start, end = doc.sentences[low][0], doc.sentences[high][1]
                options[doc_id, start, end] = sum(scores[low : high + 1])
            # A long sentence may not fit; clauses and word-centred windows
            # still remain continuous literal intervals with original offsets.
            start, end = doc.sentences[i]
            if end - start > 600:
                for match in re.finditer(r"[^;:]+[;:]?", doc.text[start:end]):
                    a, b = start + match.start(), start + match.end()
                    value = _relevance(frozenset(terms(doc.text[a:b])), weights)
                    if value:
                        options[doc_id, a, b] = value
                words = list(re.finditer(r"\S+", doc.text[start:end]))
                for index, word in enumerate(words):
                    if set(terms(word.group())).intersection(weights):
                        a = start + words[max(0, index - 20)].start()
                        b = start + words[min(len(words) - 1, index + 30)].end()
                        options[doc_id, a, b] = _relevance(frozenset(terms(doc.text[a:b])), weights) * 0.8
        if len(doc.text.encode("utf-8")) <= 1800:
            options[doc_id, 0, len(doc.text)] = sum(scores)
    # Keep per-document possibilities as well as the strongest global spans;
    # rank ties follow caller order, never document labels or content hashes.
    return tuple(sorted(options, key=lambda span: (-options[span], span))[:96])


def _merge(spans: Sequence[Span], documents: Sequence[_Document]) -> Selection:
    merged: list[Span] = []
    for doc, a, b in sorted(set(spans)):
        if merged and merged[-1][0] == doc:
            previous, low, high = merged[-1]
            if a <= high or (a - high <= 3 and documents[doc].text[high:a].isspace()):
                merged[-1] = previous, low, max(high, b)
                continue
        merged.append((doc, a, b))
    return tuple(merged)


def _fragments(selection: Selection, documents: Sequence[_Document]) -> tuple[PreparedContextFragment, ...]:
    return tuple(
        PreparedContextFragment(documents[doc].address, a, b, documents[doc].text[a:b]) for doc, a, b in selection
    )


def _utility(selection: Selection, documents: Sequence[_Document], weights: dict[str, float]) -> float:
    covered: Counter[str] = Counter()
    utility = 0.0
    for doc_id, doc in enumerate(documents):
        ranges = [(a, b) for candidate, a, b in selection if candidate == doc_id]
        if not ranges:
            continue
        selected_scores = []
        for (start, end), words in zip(doc.sentences, doc.words, strict=True):
            overlap = sum(max(0, min(end, b) - max(start, a)) for a, b in ranges) / (end - start)
            if overlap <= 0:
                selected_scores.append(0.0)
                continue
            relevance = _relevance(words, weights) * min(1.0, overlap)
            selected_scores.append(relevance)
            utility += relevance * 0.4
            for word in words.intersection(weights):
                covered[word] += min(1.0, overlap)
        # Nearby context supports antecedents and methods even when it shares
        # few query words; it is a modest bonus, not an equal allocation.
        peak = max(selected_scores, default=0.0)
        for i, ((start, end), score) in enumerate(zip(doc.sentences, selected_scores, strict=True)):
            if (
                not score
                and any(a <= start and b >= end for a, b in ranges)
                and ((i and selected_scores[i - 1]) or (i + 1 < len(selected_scores) and selected_scores[i + 1]))
            ):
                utility += peak * 0.12
    utility += sum(weights[word] * min(1.5, count) ** 0.5 for word, count in covered.items())
    return utility


def build_evidence(
    request: PrepareContextRequest, groups: Sequence[PreparedMemoryCandidates], limit: int
) -> PreparedContextBuild:
    documents = _documents(groups)
    weights = _weights(request.query, documents)
    options = _options(documents, weights)
    beam: list[tuple[float, int, Selection]] = [(0.0, 0, ())]
    best = beam[0]
    evaluated: dict[Selection, tuple[float, int, Selection]] = {(): best}
    for _ in range(min(limit, 4)):
        candidates = {item[2]: item for item in beam}
        for _, _, selected in beam:
            for option in options:
                trial = _merge((*selected, option), documents)
                if trial == selected or len(trial) > limit:
                    continue
                item = evaluated.get(trial)
                if item is None:
                    size = len(render_fragments(_fragments(trial, documents)).encode("utf-8"))
                    item = (_utility(trial, documents, weights), size, trial)
                    evaluated[trial] = item
                if item[1] <= request.max_bytes:
                    candidates[trial] = item
        beam = sorted(candidates.values(), key=lambda item: (-item[0], item[1], item[2]))[:24]
        if beam[0] == best:
            break
        best = beam[0]
    fragments = _fragments(best[2], documents)
    origins = tuple(fragment.address for fragment in fragments)
    omissions = PreparedContextOmissions(dropped_items=max(0, len(documents) - len({span[0] for span in best[2]})))
    if not fragments:
        return PreparedContextBuild(
            context=PreparedContext(status="empty", content=None, content_bytes=0), origins=(), omissions=omissions
        )
    content = render_fragments(fragments)
    return PreparedContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=len(content.encode("utf-8"))),
        origins=origins,
        fragments=fragments,
        omissions=omissions,
    )
