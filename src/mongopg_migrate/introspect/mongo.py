"""Mongo introspection: sampling, per-field type/variance inference, and
polymorphic-shape detection.

Implements PRD §6 step 2 ("samples each Mongo collection ... infers field
names, types, nesting, and type variance") and the §10 risk on sampling
bias ("sample size needs to scale with collection size, or scan fully below
a size threshold").
"""

from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from bson import Decimal128, ObjectId
from pymongo import MongoClient
from pymongo.database import Database

from mongopg_migrate.mapping.schema import MappingFile

# --- sampling policy (PRD §10: sampling bias) --------------------------------

FULL_SCAN_THRESHOLD = 5_000
MIN_SAMPLE = 1_000
SAMPLE_FRACTION = 0.02
MAX_SAMPLE = 50_000


def sample_size_for(document_count: int) -> int:
    """Below FULL_SCAN_THRESHOLD, scan fully. Above it, sample a fraction of
    the collection, floored at MIN_SAMPLE and capped at MAX_SAMPLE, so rare
    type variance and rare polymorphic shapes are still likely to surface on
    large collections without introspecting all of them."""
    if document_count <= FULL_SCAN_THRESHOLD:
        return document_count
    return min(max(MIN_SAMPLE, int(document_count * SAMPLE_FRACTION)), MAX_SAMPLE)


# --- BSON type inference ------------------------------------------------------


def bson_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, ObjectId):
        return "objectid"
    if isinstance(value, datetime.datetime):
        return "date"
    if isinstance(value, int):
        return "int"
    if isinstance(value, (float, Decimal128)):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bytes):
        return "binary"
    return type(value).__name__


# --- per-field statistics ------------------------------------------------------

ARRAY_ITEMS_SAMPLED_PER_DOC = 20
DISCRIMINATOR_MAX_CARDINALITY = 10
DISCRIMINATOR_MIN_PURITY = 0.8


@dataclass
class FieldStats:
    path: str
    present_count: int = 0
    null_count: int = 0
    bson_types: set[str] = field(default_factory=set)
    is_array: bool = False
    array_item_kind: str | None = None  # "scalar" | "object" | None
    array_item_types: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "present_count": self.present_count,
            "null_count": self.null_count,
            "bson_types": sorted(self.bson_types),
            "is_array": self.is_array,
            "array_item_kind": self.array_item_kind,
            "array_item_types": sorted(self.array_item_types),
        }


@dataclass
class CollectionSchema:
    name: str
    document_count: int
    sampled_count: int
    fields: dict[str, FieldStats]
    shape_signature_counts: dict[frozenset, int]
    polymorphism_candidate: bool
    discriminator_field: str | None

    def top_level_field_names(self) -> set[str]:
        return {p for p in self.fields if "." not in p and "[]" not in p}


def _walk_document(doc: dict, prefix: str, fields: dict[str, FieldStats]) -> None:
    for key, value in doc.items():
        path = f"{prefix}{key}"
        stats = fields.setdefault(path, FieldStats(path=path))
        stats.present_count += 1
        if value is None:
            stats.null_count += 1
            stats.bson_types.add("null")
            continue
        t = bson_type_name(value)
        stats.bson_types.add(t)
        if isinstance(value, dict):
            _walk_document(value, prefix=f"{path}.", fields=fields)
        elif isinstance(value, list):
            stats.is_array = True
            for item in value[:ARRAY_ITEMS_SAMPLED_PER_DOC]:
                if isinstance(item, dict):
                    stats.array_item_kind = "object"
                    _walk_document(item, prefix=f"{path}[].", fields=fields)
                else:
                    stats.array_item_kind = stats.array_item_kind or "scalar"
                    stats.array_item_types.add(bson_type_name(item))


def _detect_polymorphism(
    signature_counts: dict[frozenset, int], documents: list[dict]
) -> tuple[bool, str | None]:
    """Flag shape variance within one collection and try to find a
    discriminator field whose value predicts the document shape (PRD §6
    step 3: "detection of polymorphic document shapes ... e.g. a `type`
    discriminator field implying multiple target mappings")."""
    total = sum(signature_counts.values())
    if total == 0 or len(signature_counts) <= 1:
        return False, None

    # Only care about shapes that show up often enough to be a real variant,
    # not one-off malformed documents.
    significant = [sig for sig, n in signature_counts.items() if n / total >= 0.05]
    if len(significant) <= 1:
        return False, None

    # Look for a low-cardinality scalar field whose value predicts the shape.
    value_to_signatures: dict[str, dict[str, Counter]] = {}
    for doc in documents:
        sig = frozenset(doc.keys())
        if sig not in significant:
            continue
        sig_label = ",".join(sorted(sig))
        for key, value in doc.items():
            if not isinstance(value, str):
                continue
            value_to_signatures.setdefault(key, {}).setdefault(value, Counter())[sig_label] += 1

    best_field, best_purity = None, 0.0
    for key, values in value_to_signatures.items():
        if len(values) > DISCRIMINATOR_MAX_CARDINALITY or len(values) < 2:
            continue
        purities = []
        for sig_counts in values.values():
            n = sum(sig_counts.values())
            purities.append(max(sig_counts.values()) / n)
        avg_purity = sum(purities) / len(purities)
        if avg_purity >= DISCRIMINATOR_MIN_PURITY and avg_purity > best_purity:
            best_field, best_purity = key, avg_purity

    return True, best_field


def introspect_collection(
    db: Database, name: str, *, sample_size: int | None = None, mongo_filter: dict | None = None
) -> CollectionSchema:
    """`mongo_filter`, when given, restricts introspection to documents
    matching it (PRD §7 P0 discriminator filtering) — without it, sampling
    a collection shared by multiple filtered entities sees every variant's
    fields at once, so a field that only exists on a different discriminator
    value looks "observed" for an entity that never actually has it. Note
    this switches the count from `estimated_document_count()` (fast,
    metadata-only, collection-wide) to `count_documents()` (a real scan) —
    unavoidable since Mongo has no cheap way to estimate a filtered count.
    """
    coll = db[name]
    mongo_filter = mongo_filter or {}
    document_count = coll.count_documents(mongo_filter) if mongo_filter else coll.estimated_document_count()
    n = sample_size if sample_size is not None else sample_size_for(document_count)

    if n >= document_count:
        cursor = coll.find(mongo_filter)
    else:
        pipeline = ([{"$match": mongo_filter}] if mongo_filter else []) + [{"$sample": {"size": n}}]
        cursor = coll.aggregate(pipeline)

    fields: dict[str, FieldStats] = {}
    signature_counts: Counter = Counter()
    documents: list[dict] = []
    sampled = 0
    for doc in cursor:
        sampled += 1
        signature_counts[frozenset(doc.keys())] += 1
        documents.append(doc)
        _walk_document(doc, prefix="", fields=fields)

    is_poly, discriminator = _detect_polymorphism(signature_counts, documents)

    return CollectionSchema(
        name=name,
        document_count=document_count,
        sampled_count=sampled,
        fields=fields,
        shape_signature_counts=dict(signature_counts),
        polymorphism_candidate=is_poly,
        discriminator_field=discriminator,
    )


def introspect_database(
    uri: str, *, collections: list[str] | None = None, sample_size: int | None = None
) -> dict[str, CollectionSchema]:
    client: MongoClient = MongoClient(uri)
    try:
        db = client.get_default_database()
        if db is None:
            raise ValueError(
                "MONGO_URI must include a default database, e.g. "
                "mongodb://host:27017/mydb"
            )
        names = collections or db.list_collection_names()
        return {
            name: introspect_collection(db, name, sample_size=sample_size) for name in names
        }
    finally:
        client.close()


def list_collection_names(uri: str) -> set[str]:
    """Every collection that actually exists in the source database —
    deliberately separate from `introspect_database`/`introspect_entities`
    (both of which only ever look at collections the mapping file already
    knows about). Used by `validate_collection_coverage` (mapping/schema.py)
    to catch a collection that's silently absent from the mapping file
    entirely, not just an unmapped field inside one that's already there."""
    client: MongoClient = MongoClient(uri)
    try:
        db = client.get_default_database()
        if db is None:
            raise ValueError("MONGO_URI must include a default database, e.g. mongodb://host:27017/mydb")
        return set(db.list_collection_names())
    finally:
        client.close()


def introspect_entities(
    uri: str, mapping: MappingFile, *, sample_size: int | None = None
) -> dict[str, CollectionSchema]:
    """Like `introspect_database`, but keyed by *entity name* and filter-
    aware: samples `entity.source` restricted to `entity.mongo_filter()`
    (PRD §7 P0 discriminator filtering), not the whole shared collection.
    This is what `validate-mapping`'s unmapped-field check should use for a
    mapping with any `filter`-bearing entity — `introspect_database`, keyed
    by collection name, can't even represent two entities sharing one
    source collection, let alone sample them separately.
    """
    client: MongoClient = MongoClient(uri)
    try:
        db = client.get_default_database()
        if db is None:
            raise ValueError(
                "MONGO_URI must include a default database, e.g. "
                "mongodb://host:27017/mydb"
            )
        return {
            name: introspect_collection(
                db, entity.source, sample_size=sample_size, mongo_filter=entity.mongo_filter()
            )
            for name, entity in mapping.entities.items()
        }
    finally:
        client.close()
