"""Generate Markdown documentation for BrickLayer."""

from collections.abc import Callable
from fractions import Fraction
from functools import cache
from inspect import getdoc
from types import FunctionType
from typing import Any

from .. import colors, construction, geometry, modeling, parts
from ..colors import Color, Finish
from ..construction import (
    COMPONENT_TYPES,
    CONSTRUCTION_HELPERS,
    VARIANT_ENUMS,
    fill,
    registry,
)
from ..construction.registry import valid_part_kwarg_combos
from ..geometry import PLATE, HeightLike, connections
from ..io import writer
from ..io.writer import to_ldraw, write_ldraw
from ..ldraw import get_connector_type
from ..modeling import articulation, assembly, transforms
from ..modeling.build import LegoBuild, NameRef
from ..modeling.placement import Placement
from ..modeling.transforms import BUILD_TRANSFORMATIONS
from ..parts import CATALOG_TYPES, DISCOVERY, Element, element
from .utils import (
    ClassDoc,
    bullet,
    document_definitions,
    document_function,
    document_members,
    document_module,
    heading,
    paragraphs,
)

_PLACEMENT_MEMBERS = (
    "add",
    "add_raw",
    "bounding_box",
    "brick_count",
    "copy",
    "deepcopy",
    "extend",
    "flattened_placements",
    "list_elements",
    "name_of",
    "named",
    "names",
    "placements",
    "remove",
    "stack",
)
_CONNECTION_MEMBERS = (
    "connect",
    "connection_endpoints",
    "connectors",
    "find_connection_candidates",
    "place_connection",
    "trial_connections",
)
_MOTION_MEMBERS = (
    "check_motion",
    "flattened_joints",
    "joint",
    "joints",
    "posed",
    "set_motion",
)
_VALIDATION_MEMBERS = (
    "colliding_placements",
    "connected_components",
    "floating_placements",
    "is_connected",
    "is_valid",
    "num_connected_components",
)

_TOPICS = {
    "parts.md": (
        "Find and choose parts",
        "IDs, search, availability, factories and colors.",
    ),
    "placement.md": (
        "Place and compose",
        "Stack, repeat, name and nest parts or models.",
    ),
    "geometry.md": (
        "Coordinates and transforms",
        "Grid units, local frames, poses and model transforms.",
    ),
    "connections.md": (
        "Connect and trial-fit",
        "Endpoint position and axis, pair lookup, candidate poses and trial evidence.",
    ),
    "motion.md": (
        "Set and check motion",
        "Joint motion ranges, poses and sampled interference.",
    ),
    "validation-export.md": (
        "Check and export",
        "Collisions, unsupported parts, connected components and LDraw export.",
    ),
}


@cache
def api_reference() -> str:
    """Compile the full agent reference from module and API docstrings."""
    import bricklayer

    return (
        paragraphs(
            document_module(bricklayer, 1),
            _geometry(),
            _connectors(),
            _parts(),
            document_module(construction),
            *(describe_component_factory(f, 3) for f in _factories()),
            document_definitions(COMPONENT_TYPES + VARIANT_ENUMS, 3),
            _list_available_colors(),
            _placement(
                _PLACEMENT_MEMBERS
                + _CONNECTION_MEMBERS
                + _MOTION_MEMBERS
                + _VALIDATION_MEMBERS
            ),
            _assembly(),
            _fill_helpers(),
            _motion(),
            _transformations(),
        )
        + "\n"
    )


def api_reference_pages() -> dict[str, str]:
    """Compile focused Markdown pages from the installed SDK and its registries."""
    factories = _factories()
    argument_tables = {
        factory: table
        for factory in factories
        if (table := list_valid_kwarg_combos(factory, 2))
    }
    pages = {
        "index.md": paragraphs(
            "# BrickLayer API by task",
            "\n".join(
                bullet(f"[{title}]({file}) — {description}")
                for file, (title, description) in _TOPICS.items()
            ),
        ),
        "parts.md": paragraphs(
            _parts(),
            document_definitions(
                [
                    ClassDoc(cls, constructor=False)
                    for cls in (
                        parts.PartCapabilities,
                        parts.PartCapability,
                        parts.PartDataSource,
                        parts.PartEndpoint,
                        parts.VerifiedMounting,
                    )
                ],
                3,
            ),
            "### Color",
            getdoc(Color) or "",
            "Look up constants in the [color palette](colors.md).",
            document_module(construction),
            *(
                paragraphs(
                    document_function(factory, 3),
                    f"[Valid arguments for `{factory.__name__}`](factories/{factory.__name__}.md)."
                    if factory in argument_tables
                    else "",
                )
                for factory in factories
            ),
            document_definitions(COMPONENT_TYPES + VARIANT_ENUMS, 3),
        ),
        "placement.md": paragraphs(_placement(_PLACEMENT_MEMBERS), _fill_helpers()),
        "geometry.md": paragraphs(_geometry(), _transformations()),
        "connections.md": paragraphs(
            _connectors(),
            _assembly(),
            "### Inspect trial evidence",
            "Pass a dict as `evidence` to the candidate lookup and trial calls. The "
            "lookup records endpoint samples and filters. Trials record checks, rejection "
            "counts and failed sample poses. Read a trial's `diagnostics` and "
            "`motion_report` before choosing a candidate.",
            "### LegoBuild connection methods",
            document_members(LegoBuild, members=_CONNECTION_MEMBERS),
            "For joint travel and sampling, see [motion](motion.md).",
        ),
        "motion.md": paragraphs(
            _motion(),
            "### LegoBuild motion methods",
            document_members(LegoBuild, members=_MOTION_MEMBERS),
            "For endpoint and mounting parameters, see [connections](connections.md).",
        ),
        "validation-export.md": paragraphs(
            "### LegoBuild validation methods",
            document_members(LegoBuild, members=_VALIDATION_MEMBERS),
            "Use [the record workflow](../flow.md) for build validation and verified delivery.",
            document_module(writer),
            document_definitions([to_ldraw, write_ldraw], 3),
            "Use `add_step` to mark an LDraw build step.",
            document_members(
                LegoBuild, members=("add_step", "remove_step", "step_markers", "steps")
            ),
        ),
        "colors.md": _list_available_colors(1),
        **{
            f"factories/{factory.__name__}.md": paragraphs(
                f"# `{factory.__name__}` argument combinations",
                list_valid_kwarg_combos(factory, 2)
                or "No combinations are enumerated.",
                "See [parts](../parts.md) for the factory's signature and usage.",
            )
            for factory, table in argument_tables.items()
        },
    }
    for file, (title, _) in _TOPICS.items():
        pages[file] = paragraphs(f"# {title}", pages[file], "[API index](index.md).")
    return {file: content.rstrip() + "\n" for file, content in pages.items()}


def _factories() -> list[FunctionType]:
    return sorted(registry.COMPONENT_FACTORIES, key=lambda factory: factory.__name__)


def _fill_helpers() -> str:
    return paragraphs(
        document_module(fill), document_definitions(CONSTRUCTION_HELPERS, 3)
    )


def _transformations() -> str:
    return paragraphs(
        document_module(transforms), document_definitions(BUILD_TRANSFORMATIONS, 3)
    )


def _geometry() -> str:
    return paragraphs(
        document_module(geometry),
        document_definitions(
            [
                ClassDoc(geometry.Anchor, members=()),
                geometry.Axis,
                ClassDoc(
                    geometry.LegoGeometry,
                    members=(
                        "alignment_box",
                        "alignment_width",
                        "alignment_depth",
                        "alignment_height",
                        "center",
                        "width",
                        "depth",
                        "height",
                        "find_connector",
                        "find_connectors",
                        "rotated_footprint",
                    ),
                    constructor=False,
                ),
                geometry.Direction,
                ClassDoc(geometry.Mat3, members=("IDENTITY", "entries", "transpose")),
                ClassDoc(
                    geometry.Position,
                    members=(
                        "ZERO",
                        "align_to_grid",
                        "is_grid_aligned",
                        "to_stud_units",
                        "from_stud_units",
                    ),
                ),
                ClassDoc(
                    geometry.Pose,
                    members=(
                        "IDENTITY",
                        "rotation",
                        "translation",
                        "inverse",
                        "apply",
                        "compose",
                        "from_rotation",
                    ),
                ),
                ClassDoc(geometry.Rotation, members=("matrix", "rotate")),
                ClassDoc(geometry.Vec3, members=("x", "y", "z", "norm")),
                geometry.rotation_about,
                geometry.to_bricks,
                geometry.to_plates,
                ("PLATE", PLATE),
                ("HeightLike", HeightLike),
            ],
            3,
        ),
    )


def _connectors() -> str:
    return paragraphs(
        document_module(connections),
        document_definitions(
            [
                ClassDoc(
                    geometry.Connector,
                    members=("type", "position", "axis", "side", "length"),
                    constructor=False,
                ),
                ClassDoc(geometry.ConnectorType, members=("name",), constructor=False),
                geometry.JointKind,
                ClassDoc(
                    geometry.Mounting,
                    members=("angle", "offset", "flip", "swing_x", "swing_y"),
                ),
                ClassDoc(geometry.Fixed, members=()),
                ClassDoc(geometry.Revolute, members=("angle",)),
                ClassDoc(geometry.Prismatic, members=("offset",)),
                ClassDoc(geometry.Cylindrical, members=("offset", "angle")),
                ClassDoc(geometry.Ball, members=("swing_x", "swing_y", "twist")),
                get_connector_type,
            ],
            3,
        ),
    )


def _parts() -> str:
    return paragraphs(
        document_module(parts),
        document_definitions(
            DISCOVERY
            + [
                ClassDoc(cls, members=(), constructor=False)
                if cls is parts.PartShape
                else ClassDoc(cls, members=())
                if issubclass(cls, parts.PartShape)
                else cls
                for cls in CATALOG_TYPES
            ],
            3,
        ),
        document_module(element),
        document_definitions(
            [
                ClassDoc(
                    Element,
                    members=("ldraw_id", "color", "spec", "bounding_box", "connectors"),
                )
            ],
            3,
        ),
    )


def _placement(members: tuple[str, ...]) -> str:
    return paragraphs(
        document_module(modeling),
        document_definitions(
            [
                ClassDoc(
                    Placement,
                    members=("bounding_box", "connectors", "pose", "to_world"),
                    constructor=False,
                ),
                ClassDoc(
                    LegoBuild,
                    members=members,
                ),
                ("NameRef", NameRef),
            ],
            3,
        ),
    )


def _assembly() -> str:
    return paragraphs(
        document_module(assembly),
        document_definitions(
            [
                assembly.AssemblyIntent,
                assembly.find_connection_pairs,
                ClassDoc(
                    assembly.ConnectionEndpoint,
                    members=("part_id", "connector", "path"),
                    constructor=False,
                ),
                ClassDoc(
                    assembly.ConnectionPair,
                    members=(
                        "parent",
                        "child",
                        "joint_kind",
                        "pose",
                        "mounting",
                        "verification_scope",
                        "reason",
                        "requires_trial",
                    ),
                    constructor=False,
                ),
                ClassDoc(
                    assembly.ConnectionCandidate,
                    members=(
                        "parent",
                        "child",
                        "content",
                        "joint_kind",
                        "pose",
                        "mounting",
                        "slide_ranges",
                        "reason",
                        "verification_scope",
                    ),
                    constructor=False,
                ),
                ClassDoc(
                    assembly.ConnectionTrial,
                    members=(
                        "candidate",
                        "passed",
                        "checks",
                        "diagnostics",
                        "joint_kind",
                        "motion_report",
                    ),
                    constructor=False,
                ),
                ClassDoc(assembly.AssemblyError, members=(), constructor=False),
            ],
            3,
        ),
    )


def _motion() -> str:
    return paragraphs(
        document_module(articulation),
        document_definitions(
            [
                ClassDoc(articulation.MotionRange, members=("lower", "upper")),
                ClassDoc(
                    articulation.Joint,
                    members=("kind", "motion"),
                    constructor=False,
                ),
                ClassDoc(
                    articulation.MotionReport,
                    members=(
                        "passed",
                        "samples_checked",
                        "collisions",
                        "disconnections",
                        "floating",
                    ),
                    constructor=False,
                ),
                ClassDoc(
                    articulation.MotionCollision,
                    members=("positions", "placements"),
                    constructor=False,
                ),
                ClassDoc(
                    articulation.MotionDisconnection,
                    members=("positions", "joint"),
                    constructor=False,
                ),
                ClassDoc(
                    articulation.MotionFloating,
                    members=("positions", "placements"),
                    constructor=False,
                ),
            ],
            3,
        ),
    )


def _list_available_colors(level: int = 3) -> str:
    """List every color, grouped into a subsection per surface finish."""
    blocks = [document_module(colors, level)]
    for finish in Finish:
        names = [f"`{c!r}`" for c in Color if c.finish is finish]
        if not names:
            continue
        blocks.append(
            f"{heading(finish.display_name, level + 1)}\n\n{finish.__doc__}\n\n{', '.join(names)}"
        )
    return "\n\n".join(blocks)


def describe_component_factory(factory: Callable, heading_level: int = 1) -> str:
    """Detailed description of a given component factory."""
    return paragraphs(
        document_function(factory, heading_level),
        list_valid_kwarg_combos(factory, heading_level + 1),
    )


def list_valid_kwarg_combos(factory: Callable, heading_level: int = 1) -> str:
    """The argument sets that produce valid parts: the shared kwarg names,
    then a tuple of values for each valid combination.

    Kwarg names and arguments are listed in the order given by :func:`valid_part_kwarg_combos`.
    """
    combos = tuple(dict(combo) for combo in valid_part_kwarg_combos(factory) or [])
    if not combos:
        return ""
    # Every combo carries the same keys in the same order.
    names = combos[0].keys()
    rows = tuple(tuple(combo[name] for name in names) for combo in combos)
    return "\n".join(
        (
            heading(
                f"Valid argument combinations `({', '.join(names)})`", heading_level
            ),
            "",
            *(
                bullet(f"`({', '.join(_format_kwarg(v) for v in row)})`")
                for row in rows
            ),
        )
    )


def _format_kwarg(value: Any) -> str:
    """Render an argument value as the literal a caller would type (so `Fraction(1,3)` becomes `1/3`)."""
    return str(value) if isinstance(value, Fraction) else repr(value)
