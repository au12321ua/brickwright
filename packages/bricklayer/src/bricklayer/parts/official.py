"""Identify parts and inspect their capabilities in a frozen LDraw release."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache, cached_property
from typing import Any, ClassVar, Literal, NotRequired, TypedDict

from ..geometry import Connector, ConnectorType, Pose
from ..ldraw import partdata
from ..ldraw.connectivity import ConnectorRaw
from ..ldraw.conversion import LDrawVec
from ..ldraw.meshes import DATA_DIR

OFFICIAL_PARTS_VERSION = "2026-10"

_CATALOG_SCHEMA = 1
_WORD = re.compile(r"[a-z0-9]+")
_LDRAW_LIBRARY_URL = "https://library.ldraw.org/"

ConnectorQuery = ConnectorType | str | Iterable[ConnectorType | str]


class PartKind(StrEnum):
    """The identity of a top-level LDraw catalog entry."""

    PART = "part"
    ALIAS = "alias"
    REDIRECT = "redirect"
    PATTERN = "pattern"
    STICKER = "sticker"
    ASSEMBLY = "assembly"
    FLEXIBLE_SECTION = "flexible_section"
    PHYSICAL_COLOUR = "physical_colour"


class CapabilityStatus(StrEnum):
    """What the catalog can establish about a particular use of a part."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"
    UNVERIFIED = "unverified"
    OUTSIDE_VERSION = "outside_version"


@dataclass(frozen=True, slots=True)
class PartDataSource:
    """A source file or archive member, relative to its documented data set.

    An optional donor transform takes the donor into this part's raw LDraw
    frame. Its 12 numbers are the translation in LDU then the row-major 3x3
    matrix, in LDraw type-1 order. A missing transform is unrecorded.
    """

    file: str
    part_id: str
    sha256: str
    url: str = ""
    author: str = ""
    license: str = ""
    donor_id: str | None = None
    transform_ldraw: tuple[float, ...] | None = None
    note: str = ""
    citations: tuple[PartDataSource, ...] = ()


@dataclass(frozen=True, slots=True)
class PartCapability:
    """A capability and the extraction evidence that determines its status."""

    status: CapabilityStatus
    reason_code: str
    reason: str
    sources: tuple[PartDataSource, ...] = ()

    @property
    def available(self) -> bool:
        """Whether this capability supplies usable data, including partial data."""
        return self.status in (CapabilityStatus.AVAILABLE, CapabilityStatus.PARTIAL)


@dataclass(frozen=True, slots=True)
class PartEndpoint:
    """One endpoint, in the same order and local coordinates as the SDK record.

    The raw position and slide span are in LDraw units. The original orientation
    is recorded as angles in degrees or unit vectors in remapped stud axes.
    Convert raw.pos with LDrawVec.to_position() before applying ldraw_to_local.
    The local Connector uses stud-axis unit directions and reach in studs.
    """

    index: int
    connector: Connector
    raw: ConnectorRaw
    ldraw_to_local: Pose
    source: PartDataSource

    position_units: ClassVar[tuple[str, str, str]] = ("stud", "stud", "brick")
    raw_position_units: ClassVar[str] = "LDU"


class _ConnectorSourceRow(TypedDict):
    file: str
    count: int
    sha256: str
    donor_id: NotRequired[str]
    transform_ldraw: NotRequired[list[float]]
    note: NotRequired[str]


class _PartRow(TypedDict):
    name: str
    kind: str
    category: str
    ldraw_type: str
    author: str
    license: str
    sha256: str
    visual: str
    model: bool
    geometry_issues: NotRequired[list[str]]
    bounds: NotRequired[list[str]]
    collider: NotRequired[list[str]]
    connector_types: NotRequired[list[str]]
    connector_sources: NotRequired[list[_ConnectorSourceRow]]
    reference_id: NotRequired[str]
    reference_ldraw: NotRequired[list[float]]
    canonical_id: NotRequired[str | None]
    base: NotRequired[str]
    components: NotRequired[list[str]]
    supplemental: NotRequired[list[str]]
    frame: NotRequired[list[str]]
    popularity: NotRequired[int]


class _CatalogData(TypedDict):
    schema: int
    version: str
    inputs: dict[str, object]
    parts: dict[str, _PartRow]
    status_counts: dict[str, dict[str, int]]
    mountings: dict[str, object]


@dataclass(frozen=True)
class PartCapabilities:
    """Identity and capability information for one ID in an official release.

    The empty identity (in_version is false) distinguishes an out-of-version
    ID from an official entry with incomplete SDK data. Endpoints are loaded
    on demand. Related IDs leave the queried identity and pose unchanged.
    """

    part_id: str
    version: str
    _row: _PartRow | None = field(repr=False, compare=False)

    @property
    def in_version(self) -> bool:
        """Whether the ID is a top-level entry in this release."""
        return self._row is not None

    @property
    def name(self) -> str | None:
        """The name derived from the official file's first line."""
        return self._row["name"] if self._row is not None else None

    @property
    def kind(self) -> PartKind | None:
        """The LDraw entry's role, such as a part, alias, or assembly."""
        return PartKind(self._row["kind"]) if self._row is not None else None

    @property
    def category(self) -> str | None:
        """The official category where present, with a name-based fallback."""
        return self._row["category"] if self._row is not None else None

    @property
    def ldraw_type(self) -> str | None:
        """The LDraw !LDRAW_ORG file type, including modifiers."""
        return self._row["ldraw_type"] if self._row is not None else None

    @property
    def canonical_id(self) -> str | None:
        """The last official ID in a replacement chain, or this ID for a part.

        An unresolved replacement chain has no canonical ID.
        """
        return (
            self._row.get("canonical_id", self.part_id)
            if self._row is not None
            else None
        )

    @property
    def reference_id(self) -> str | None:
        """The immediate target of an official alias or moved number."""
        return self._row.get("reference_id") if self._row is not None else None

    @property
    def reference_transform_ldraw(self) -> tuple[float, ...] | None:
        """The immediate target's raw type-1 transform into this part's frame."""
        values = self._row.get("reference_ldraw") if self._row is not None else None
        return tuple(values) if values is not None else None

    @property
    def variant_base_id(self) -> str | None:
        """The base part linked by an LDraw decoration suffix and name."""
        return self._row.get("base") if self._row is not None else None

    @property
    def aliases(self) -> tuple[str, ...]:
        """Alternate and retired official IDs resolving to the same identity."""
        canonical = self.canonical_id
        if canonical is None:
            return ()
        return tuple(
            part_id
            for part_id in _catalog(self.version).aliases.get(canonical, ())
            if part_id != self.part_id
        )

    @property
    def variants(self) -> tuple[str, ...]:
        """Patterned or stickered variants linked to this official identity."""
        base = self.variant_base_id or self.canonical_id
        if base is None:
            return ()
        return tuple(
            part_id
            for part_id in _catalog(self.version).variants.get(base, ())
            if part_id != self.part_id
        )

    @property
    def component_ids(self) -> tuple[str, ...]:
        """Parts directly referenced by an assembly file."""
        return tuple(self._row.get("components", ())) if self._row is not None else ()

    @property
    def related_ids(self) -> tuple[str, ...]:
        """IDs connected by a documented identity, decoration, or assembly."""
        identity = (self.canonical_id, self.reference_id, self.variant_base_id)
        related = {
            part_id for part_id in identity if part_id and part_id != self.part_id
        }
        return tuple(
            sorted(related.union(self.aliases, self.variants, self.component_ids))
        )

    @property
    def limitations(self) -> tuple[str, ...]:
        """Modeling limits implied by the entry's documented role."""
        if self.category == "Sticker":
            return ("The SDK has no model for applying an adhesive sticker.",)
        if self.kind == PartKind.FLEXIBLE_SECTION or (self.name or "").startswith(
            "Hose Flexible "
        ):
            return ("The SDK treats the supplied geometry as rigid.",)
        if self.kind == PartKind.ASSEMBLY:
            return (
                "A shortcut describes the components in a single recorded arrangement.",
            )
        return ()

    @cached_property
    def constructible(self) -> PartCapability:
        """Whether the existing SDK accepts this ID in PartSpec and Element."""
        if self._row is None:
            return self._outside_release()
        if self._row["model"]:
            return PartCapability(
                CapabilityStatus.AVAILABLE,
                "sdk_constructible",
                "The SDK modeling catalog contains this ID and its measured bounds.",
                self._bounds_sources(),
            )
        reason = self.collision.reason
        if self.collision.available:
            reason = (
                "The SDK has no modeling catalog row with measured bounds for this ID."
            )
        if self.reference_id is not None:
            reason += f" The LDraw entry points to {self.reference_id}."
        return PartCapability(
            CapabilityStatus.MISSING,
            "missing_modeling_metadata"
            if self.collision.available
            else self.collision.reason_code,
            reason,
            self.geometry.sources,
        )

    @cached_property
    def geometry(self) -> PartCapability:
        """Status of the visual part definition in the external LDraw release."""
        if self._row is None:
            return self._outside_release()
        status = CapabilityStatus(self._row["visual"])
        if status in (CapabilityStatus.PARTIAL, CapabilityStatus.MISSING):
            code = "unresolved_geometry_references"
            reason = "Unresolved LDraw geometry dependencies: " + ", ".join(
                self._row["geometry_issues"]
            )
        elif status == CapabilityStatus.UNVERIFIED:
            code = "visual_geometry_unverified"
            reason = "No renderable primitives were identified in the LDraw definition."
        else:
            code = "visual_geometry_in_snapshot"
            reason = "The definition and its visual dependencies resolve in this LDraw release."
        return PartCapability(
            status, code, reason, (self._ldraw_source(), *self._bounds_sources())
        )

    @property
    def bounds_ldraw(self) -> tuple[LDrawVec, LDrawVec] | None:
        """Packaged measured corners in the part's raw LDraw frame, in LDU."""
        if self._row is None or "bounds" not in self._row:
            return None
        return partdata.record(self.part_id).ldraw_bounds

    @property
    def frame_sources(self) -> tuple[PartDataSource, ...]:
        """Explicit changes to the seating frame beyond the connector-derived origin."""
        if self._row is None or "frame" not in self._row:
            return ()
        path, digest = self._row["frame"]
        return (
            PartDataSource(
                path, self.part_id, digest, author="BrickLayer contributors"
            ),
        )

    @cached_property
    def collision(self) -> PartCapability:
        """Whether a collision mesh is shipped for this ID."""
        if self._row is None:
            return self._outside_release()
        if "collider" not in self._row:
            return PartCapability(
                CapabilityStatus.MISSING,
                "missing_collision_mesh",
                "No collision mesh was resolved in the packaged mesh store.",
                (self._ldraw_source(),),
            )
        path, digest = self._row["collider"]
        supplement = self._supplement().get("collider")
        if supplement:
            source = PartDataSource(
                path,
                self.part_id,
                digest,
                url=_LDRAW_LIBRARY_URL,
                author="BrickLayer contributors and the LDraw part contributors",
                license="LDraw attribution in NOTICE",
                donor_id=self.part_id,
                transform_ldraw=tuple(supplement["source_to_part_ldraw"]),
                note=supplement["method"],
                citations=self._supplement_files(supplement["files"]),
            )
        else:
            source = PartDataSource(
                path,
                self.part_id,
                digest,
                url="https://github.com/kulits/BrickNet",
                author="Peter Kulits and the LDraw part contributors",
                license="MIT with LDraw attribution in NOTICE",
            )
        return PartCapability(
            CapabilityStatus.AVAILABLE,
            "collision_mesh_packaged",
            "The SDK packages a collision mesh in the part's raw LDraw frame.",
            (source,),
        )

    @cached_property
    def connectivity(self) -> PartCapability:
        """Status of connector annotations, independently of specific mating tests."""
        if self._row is None:
            return self._outside_release()
        if sources := self._connector_sources():
            return PartCapability(
                CapabilityStatus.PARTIAL,
                "connector_annotations_present",
                "The SDK has annotated endpoints. Coverage can be partial.",
                sources,
            )
        if self.category == "Sticker":
            return PartCapability(
                CapabilityStatus.NOT_APPLICABLE,
                "sticker_application_unsupported",
                self.limitations[0],
                (self._ldraw_source(),),
            )
        return PartCapability(
            CapabilityStatus.MISSING
            if self._row["model"]
            else CapabilityStatus.UNVERIFIED,
            "missing_connector_annotations",
            "The packaged data contains no connector annotations for this ID.",
            (self._ldraw_source(),),
        )

    @cached_property
    def endpoints(self) -> tuple[PartEndpoint, ...]:
        """Effective endpoints with their individual coordinates and provenance."""
        if self._row is None or "connector_sources" not in self._row:
            return ()
        record = partdata.record(self.part_id)
        sources = (
            source
            for metadata, source in zip(
                self._row["connector_sources"], self.connectivity.sources, strict=True
            )
            for _ in range(metadata["count"])
        )
        return tuple(
            PartEndpoint(
                index,
                connector,
                raw,
                record.ldraw_to_element_pose,
                self._endpoint_source(source, raw.type),
            )
            for index, (connector, raw, source) in enumerate(
                zip(record.connectors, record.raw_connectors, sources, strict=True)
            )
        )

    def _outside_release(self) -> PartCapability:
        return PartCapability(
            CapabilityStatus.OUTSIDE_VERSION,
            "part_outside_version",
            f"{self.part_id!r} is outside the LDraw {self.version} top-level catalog.",
        )

    def _ldraw_source(self) -> PartDataSource:
        assert self._row is not None
        return PartDataSource(
            f"parts/{self.part_id}.dat",
            self.part_id,
            self._row["sha256"],
            _LDRAW_LIBRARY_URL,
            self._row["author"],
            self._row["license"],
            self.reference_id,
            self.reference_transform_ldraw,
        )

    def _bounds_sources(self) -> tuple[PartDataSource, ...]:
        if self._row is None or "bounds" not in self._row:
            return ()
        path, digest = self._row["bounds"]
        return (PartDataSource(path, self.part_id, digest, _LDRAW_LIBRARY_URL),)

    def _connector_sources(self) -> tuple[PartDataSource, ...]:
        assert self._row is not None
        return tuple(
            PartDataSource(
                row["file"],
                self.part_id,
                row["sha256"],
                url="https://github.com/kulits/BrickNet"
                if row["file"] == "part_connectivity.json"
                else "",
                author="Peter Kulits"
                if row["file"] == "part_connectivity.json"
                else "BrickLayer contributors",
                license="MIT",
                donor_id=row.get("donor_id"),
                transform_ldraw=tuple(row["transform_ldraw"])
                if "transform_ldraw" in row
                else None,
                note=row.get("note", ""),
            )
            for row in self._row.get("connector_sources", ())
        )

    def _supplement(self) -> dict[str, Any]:
        if self._row is None or "supplemental" not in self._row:
            return {}
        file, _ = self._row["supplemental"]
        return _supplement_data(file)["parts"][self.part_id]

    def _supplement_files(self, paths: list[str]) -> tuple[PartDataSource, ...]:
        assert self._row is not None
        file, _ = self._row["supplemental"]
        files = _supplement_data(file)["files"]
        return tuple(
            PartDataSource(path, self.part_id, url=_LDRAW_LIBRARY_URL, **files[path])
            for path in paths
        )

    def _endpoint_source(self, source: PartDataSource, name: str) -> PartDataSource:
        if source.file == "part_connectivity.json":
            return source
        supplement = self._supplement().get("connectors", {}).get(name)
        if supplement is None:
            return source
        return PartDataSource(
            source.file,
            source.part_id,
            source.sha256,
            author=source.author,
            license=source.license,
            note=supplement["method"],
            citations=self._supplement_files(supplement["files"]),
        )


@cache
def _supplement_data(file: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / file).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class _Catalog:
    version: str
    data: _CatalogData

    @cached_property
    def ids(self) -> frozenset[str]:
        return frozenset(self.data["parts"])

    @cached_property
    def aliases(self) -> dict[str, tuple[str, ...]]:
        return self._related_to("canonical_id")

    @cached_property
    def variants(self) -> dict[str, tuple[str, ...]]:
        return self._related_to("base")

    def _related_to(
        self, field: Literal["canonical_id", "base"]
    ) -> dict[str, tuple[str, ...]]:
        ids: dict[str, list[str]] = defaultdict(list)
        for part_id, row in self.data["parts"].items():
            related_id = row.get(field)
            if related_id and related_id != part_id:
                ids[related_id].append(part_id)
        return {part_id: tuple(sorted(related)) for part_id, related in ids.items()}

    @cached_property
    def words(self) -> dict[str, set[str]]:
        index: dict[str, set[str]] = defaultdict(set)
        for part_id, row in self.data["parts"].items():
            for word in set(_WORD.findall(f"{part_id} {row['name']}".casefold())):
                index[word].add(part_id)
        return dict(index)

    @cached_property
    def connector_types(self) -> dict[str, set[str]]:
        index: dict[str, set[str]] = defaultdict(set)
        for part_id, row in self.data["parts"].items():
            for name in row.get("connector_types", ()):
                index[name].add(part_id)
        return dict(index)

    @cached_property
    def ranks(self) -> dict[str, int]:
        def key(part_id: str) -> tuple[bool, bool, bool, bool, int, str]:
            row = self.data["parts"][part_id]
            independent = row["kind"] in (PartKind.PART, PartKind.PATTERN)
            return (
                not row["model"],
                "collider" not in row,
                not independent,
                "connector_types" not in row,
                -row.get("popularity", 0),
                part_id,
            )

        return {part_id: rank for rank, part_id in enumerate(sorted(self.ids, key=key))}

    def ids_with_connectors(self, connector_type: ConnectorQuery) -> set[str]:
        registry = partdata.all_connector_types()
        return {
            part_id
            for kind in partdata.resolve_connector_types(connector_type)
            if registry.get(kind.name) is kind
            for part_id in self.connector_types.get(kind.name, ())
        }


@cache
def _catalog(version: str) -> _Catalog:
    if version != OFFICIAL_PARTS_VERSION:
        raise ValueError(f"LDraw catalog release {version!r} is unavailable.")
    path = DATA_DIR / f"official_catalog_{version}.json"
    data: _CatalogData = json.loads(path.read_text(encoding="utf-8"))
    if data["version"] != version or data["schema"] != _CATALOG_SCHEMA:
        raise ValueError(
            f"Unexpected official catalog version or schema in {path.name}."
        )
    return _Catalog(version, data)


def _normalized_id(value: str) -> str:
    return value.casefold().strip().removesuffix(".dat")


def lookup_part(
    part_id: str, *, version: str = OFFICIAL_PARTS_VERSION
) -> PartCapabilities:
    """Query any top-level ID in the packaged LDraw release.

    Handles case and a .dat suffix. An ID outside the release has no official
    identity and reports outside_version capabilities.
    """
    part_id = _normalized_id(part_id)
    catalog = _catalog(version)
    if part_id not in catalog.ids:
        return PartCapabilities(part_id, version, None)
    return _known_part(part_id, version)


@cache
def _known_part(part_id: str, version: str) -> PartCapabilities:
    return PartCapabilities(part_id, version, _catalog(version).data["parts"][part_id])


def official_part_ids(*, version: str = OFFICIAL_PARTS_VERSION) -> frozenset[str]:
    """Every top-level ID in the frozen official release."""
    return _catalog(version).ids


def official_catalog_summary(
    *, version: str = OFFICIAL_PARTS_VERSION
) -> dict[str, dict[str, int]]:
    """Count all official IDs by status within each capability."""
    return {
        capability: counts.copy()
        for capability, counts in _catalog(version).data["status_counts"].items()
    }


def parts_with_connectors(
    connector_type: ConnectorQuery, *, version: str = OFFICIAL_PARTS_VERSION
) -> tuple[str, ...]:
    """Return constructible official IDs with at least one matching connector.

    Names can select a registered type or group. An iterable matches any of
    its types. Ranking favors independent parts with useful SDK data.
    """
    catalog = _catalog(version)
    ids = (
        part_id
        for part_id in catalog.ids_with_connectors(connector_type)
        if catalog.data["parts"][part_id]["model"]
    )
    return tuple(sorted(ids, key=catalog.ranks.__getitem__))


def search_official_parts(
    query: str = "",
    *,
    version: str = OFFICIAL_PARTS_VERSION,
    kind: PartKind | str | None = None,
    constructible: bool | None = None,
    geometry: bool | None = None,
    collision: bool | None = None,
    connectivity: bool | None = None,
    connector_type: ConnectorQuery | None = None,
    limit: int | None = 20,
) -> tuple[PartCapabilities, ...]:
    """Search the official release by exact ID or by all name terms.

    Capability filters select for or against available data. Connector types
    and groups match any of their members. An exact ID comes first, then
    results favor independent parts with usable modeling data. Use limit=None
    to list all matches.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative or None")
    catalog = _catalog(version)
    selected_kind = PartKind(kind) if kind is not None else None
    exact = _normalized_id(query)
    words = _WORD.findall(query.casefold())
    candidates: set[str] | frozenset[str] = catalog.ids
    if words:
        candidates = set.intersection(
            *(catalog.words.get(word, set()) for word in words)
        )
    if exact in catalog.ids:
        candidates = candidates | {exact}
    if connector_type is not None:
        candidates = candidates & catalog.ids_with_connectors(connector_type)
    ranked = sorted(
        candidates, key=lambda part_id: (part_id != exact, catalog.ranks[part_id])
    )
    results: list[PartCapabilities] = []
    for part_id in ranked:
        if limit is not None and len(results) >= limit:
            break
        row = catalog.data["parts"][part_id]
        if selected_kind is not None and row["kind"] != selected_kind:
            continue
        capabilities = (
            (constructible, row["model"]),
            (geometry, row["visual"] in ("available", "partial")),
            (collision, "collider" in row),
            (connectivity, "connector_types" in row),
        )
        if all(
            required is None or required == actual for required, actual in capabilities
        ):
            results.append(_known_part(part_id, version))
    return tuple(results)
