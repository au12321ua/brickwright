"""Search the building examples and guides shipped with BrickWright."""

import re
import unicodedata
from functools import cache
from importlib import resources

from pydantic import BaseModel, Field

__all__ = [
    "KnowledgeEntry",
    "KnowledgeMatch",
    "KnowledgeSearch",
    "read_knowledge",
    "search_knowledge",
]

_RESOURCES = resources.files(__name__)
_IDENTIFIERS = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_WORDS = re.compile(r"[\u3400-\u9fff]+|[^\W\d_\u3400-\u9fff]+")
_COMMON_WORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "at",
        "by",
        "to",
        "on",
        "in",
        "be",
        "is",
        "are",
        "it",
        "has",
        "have",
        "do",
        "does",
        "of",
        "for",
        "why",
        "how",
        "what",
        "from",
        "can",
        "could",
        "will",
        "would",
        "i",
        "my",
        "with",
        "when",
        "then",
        "that",
        "this",
    ]
)


class KnowledgeSource(BaseModel):
    author: str
    title: str
    url: str
    license: str
    adaptation: str
    attribution_url: str | None = None


class KnowledgeExample(BaseModel):
    filename: str
    source: str


class _Summary(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str
    applicability: str


class _Content(_Summary):
    parts: tuple[str, ...]
    diagnostics: tuple[str, ...]
    steps: tuple[str, ...]
    expected: str
    sources: tuple[KnowledgeSource, ...]


class _EntryResource(_Content):
    aliases: tuple[str, ...]
    topics: tuple[str, ...]
    actions: tuple[str, ...]
    example: str | None = Field(pattern=r"^[a-zA-Z0-9_-]+\.py$")


class KnowledgeEntry(_Content):
    """A complete topic, including its example source and attribution."""

    example: KnowledgeExample | None


class KnowledgeMatch(_Summary):
    """A topic worth reading, with the index fields and terms that matched."""

    matched_on: dict[str, tuple[str, ...]]


class KnowledgeSearch(BaseModel):
    query: str
    matches: tuple[KnowledgeMatch, ...]
    message: str | None = None


def search_knowledge(query: str, *, limit: int = 3) -> KnowledgeSearch:
    """Find topics by build task, part ID, or diagnostic code."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    normalized = _normalize(query)
    identifiers = set(_IDENTIFIERS.findall(normalized))
    part_ids = {name.removesuffix(".dat") for name in identifiers}
    terms = _terms(normalized)
    ranked: list[tuple[int, KnowledgeMatch]] = []
    for entry in _entries().values():
        matches = {
            "diagnostics": identifiers.intersection(map(_normalize, entry.diagnostics)),
            "parts": part_ids.intersection(map(_normalize, entry.parts)),
            "title": terms & _terms(entry.title),
            "aliases": terms & _terms(" ".join(entry.aliases)),
            "topics": terms & _terms(" ".join(entry.topics)),
            "actions": terms & _terms(" ".join(entry.actions)),
        }
        matched_on = {
            field: tuple(sorted(overlap))
            for field, overlap in matches.items()
            if overlap
        }
        if matched_on:
            score = (
                100 * len(matches["diagnostics"])
                + 50 * len(matches["parts"])
                + 5 * len(matches["title"])
                + 4 * len(matches["aliases"])
                + 3 * len(matches["topics"])
                + 2 * len(matches["actions"])
            )
            ranked.append(
                (
                    score,
                    KnowledgeMatch(
                        id=entry.id,
                        title=entry.title,
                        applicability=entry.applicability,
                        matched_on=matched_on,
                    ),
                )
            )
    ranked.sort(key=lambda item: (-item[0], item[1].id))
    return KnowledgeSearch(
        query=query,
        matches=tuple(match for _, match in ranked[:limit]),
        message=None if ranked else "No matching knowledge entries.",
    )


def read_knowledge(entry_id: str) -> KnowledgeEntry:
    """Read a topic by the exact ID returned from search. Raises KeyError for unknown IDs."""
    data = _entries()[entry_id]
    example = None
    if data.example is not None:
        source = (_RESOURCES / "examples" / data.example).read_text(encoding="utf-8")
        example = KnowledgeExample(filename=data.example, source=source)
    return KnowledgeEntry.model_validate({**data.model_dump(), "example": example})


@cache
def _entries() -> dict[str, _EntryResource]:
    entries = {}
    for path in (_RESOURCES / "entries").iterdir():
        if not path.name.endswith(".json"):
            continue
        entry = _EntryResource.model_validate_json(path.read_text(encoding="utf-8"))
        if path.name != f"{entry.id}.json":
            raise ValueError(f"Knowledge entry {path.name!r} has ID {entry.id!r}")
        entries[entry.id] = entry
    return entries


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _terms(text: str) -> set[str]:
    terms = set()
    for word in _WORDS.findall(_normalize(text)):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.update(word[index : index + 2] for index in range(len(word) - 1))
        elif len(word) >= 2 and word not in _COMMON_WORDS:
            if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
                word = word[:-2]
            elif (
                len(word) > 3
                and word.endswith("s")
                and not word.endswith(("ss", "us", "is"))
            ):
                word = word[:-1]
            terms.add(word)
    return terms
