"""Member 4 (M2): paper-scoped, exact-match registry of normalized LaTeX elements.

Public contract (see m2_contract.py):
    Registry(arxiv_id).element_id(kind, content, parent_id, occurrence) -> str
    Registry(arxiv_id).rows() -> list[dict]

Leaf nodes are deduplicated by (kind, normalized content) across all versions.
Titled nodes distinguish repeated titles by occurrence, independent of parent.
Structural nodes with no semantic text distinguish parent + occurrence.
No fuzzy matching, case folding, or punctuation removal is performed.
"""
from __future__ import annotations

from collections import Counter

LEAF_KINDS = frozenset(("sentence", "formula", "figure"))
TITLED_KINDS = frozenset(("heading", "paragraph"))
PREFIX = {"document": "d", "heading": "h", "paragraph": "p", "list": "l",
          "item": "i", "sentence": "s", "formula": "f", "figure": "g"}


def normalize_content(text: str) -> str:
    """Collapse whitespace only, preserving meaningful LaTeX and case."""
    if not isinstance(text, str):
        raise TypeError("element content must be a string")
    return " ".join(text.split())


class Registry:
    """ID registry scoped to one arXiv paper, shared by its versions.

    IDs are deterministic for a fixed traversal order, not content hashes.
    Member 1 processes all versions of one paper in ascending version order.
    """

    def __init__(self, arxiv_id: str):
        if not isinstance(arxiv_id, str) or not arxiv_id.strip():
            raise ValueError("arxiv_id must be a non-empty string")
        self.arxiv_id = arxiv_id.strip()
        self._prefix = self.arxiv_id.replace(".", "-").replace("/", "-")
        self._lookup: dict[tuple, str] = {}
        self._counts: Counter[str] = Counter()
        self._rows: list[dict] = []

    def element_id(self, kind: str, content: str,
                   parent_id: str | None, occurrence: int) -> str:
        """Return an old ID for an equivalent element, otherwise allocate one.

        Member 1 passes parent_id=None for headings/paragraphs (their identity
        is independent of location) and for nodes directly under a root.
        An occurrence is how many equal sibling/named nodes preceded this one.
        """
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 0:
            raise ValueError("occurrence must be a non-negative integer")
        if parent_id is not None and not isinstance(parent_id, str):
            raise TypeError("parent_id must be a string or None")
        cleaned = normalize_content(content)
        if kind in LEAF_KINDS:
            key = (kind, cleaned)
        elif kind in TITLED_KINDS:
            key = (kind, cleaned, occurrence)
        else:
            key = (kind, cleaned, parent_id, occurrence)
        previous = self._lookup.get(key)
        if previous is not None:
            return previous
        letter = PREFIX.get(kind, "x")
        self._counts[letter] += 1
        new_id = f"{self._prefix}#{letter}{self._counts[letter]}"
        self._lookup[key] = new_id
        self._rows.append({"element_id": new_id, "arxiv_id": self.arxiv_id,
                           "kind": kind, "content": cleaned})
        return new_id

    def rows(self) -> list[dict]:
        """Return a defensive copy with the exact ELEMENT_FIELDS contract."""
        return [row.copy() for row in self._rows]
