"""In-memory test doubles for the SLT TalentHub MongoDB collection and API."""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError


@dataclass
class _BulkResult:
    upserted_count: int
    modified_count: int


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def sort(self, key: str, direction: int = 1) -> _Cursor:
        self._docs.sort(key=lambda d: str(d.get(key, "")), reverse=(direction < 0))
        return self

    def limit(self, n: int) -> _Cursor:
        if n and n > 0:
            self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeInternCollection:
    """Minimal in-memory PyMongo Collection double enforcing schema and unique Trainee_ID."""

    def __init__(self, docs: list[dict[str, Any]] | None = None):
        self.docs: list[dict[str, Any]] = []
        self.indexes: list[dict[str, Any]] = [{"name": "_id_", "key": {"_id": 1}}]
        if docs:
            for d in docs:
                self._insert(d)

    def _validate(self, doc: dict[str, Any]) -> None:
        for req in ("Trainee_ID", "Trainee_Name", "field_of_spec_name"):
            v = doc.get(req)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{req} must be a non-empty string")
        for dt_key in ("Training_StartDate", "Training_EndDate"):
            if not isinstance(doc.get(dt_key), datetime):
                raise ValueError(f"{dt_key} must be a datetime")

    def _insert(self, doc: dict[str, Any]) -> None:
        item = deepcopy(doc)
        self._validate(item)
        if any(x["Trainee_ID"] == item["Trainee_ID"] for x in self.docs):
            raise DuplicateKeyError(f"Duplicate Trainee_ID: {item['Trainee_ID']}")
        item.setdefault("_id", ObjectId())
        self.docs.append(item)

    def create_index(self, keys, unique: bool = False, name: str | None = None):
        field = keys[0][0] if isinstance(keys, list) else keys
        idx_name = name or f"{field}_1"
        if not any(i["name"] == idx_name for i in self.indexes):
            self.indexes.append({"name": idx_name, "key": {field: 1}, "unique": unique})
        return idx_name

    def count_documents(self, query: dict[str, Any]) -> int:
        return len(list(self.find(query)))

    def delete_many(self, query: dict[str, Any]):
        if not query:
            self.docs.clear()
        else:
            matched = {id(d) for d in self._match_docs(query)}
            self.docs = [d for d in self.docs if id(d) not in matched]

    def bulk_write(self, ops: list[UpdateOne], ordered: bool = False) -> _BulkResult:
        upserted = 0
        modified = 0
        for op in ops:
            if not isinstance(op, UpdateOne):
                raise TypeError("FakeInternCollection only supports UpdateOne")
            flt = op._filter
            update_doc = op._doc.get("$set", {})
            existing = next((d for d in self.docs if all(d.get(k) == v for k, v in flt.items())), None)
            if existing is None:
                new_doc = {**flt, **deepcopy(update_doc)}
                self._validate(new_doc)
                new_doc.setdefault("_id", ObjectId())
                self.docs.append(new_doc)
                upserted += 1
            else:
                merged = {**existing, **deepcopy(update_doc)}
                self._validate(merged)
                if any(existing.get(k) != merged.get(k) for k in update_doc):
                    modified += 1
                existing.clear()
                existing.update(merged)
        return _BulkResult(upserted_count=upserted, modified_count=modified)

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if k == "$or":
                if not any(self._matches(doc, sub) for sub in v):
                    return False
            elif isinstance(v, dict) and "$regex" in v:
                flags = re.IGNORECASE if "i" in v.get("$options", "") else 0
                if not re.search(v["$regex"], str(doc.get(k, "")), flags):
                    return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    def _match_docs(self, query: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not query:
            return [deepcopy(d) for d in self.docs]
        return [deepcopy(d) for d in self.docs if self._matches(d, query)]

    def find(self, query: dict[str, Any] | None = None) -> _Cursor:
        return _Cursor(self._match_docs(query))

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for d in self.docs:
            if self._matches(d, query):
                return deepcopy(d)
        return None
