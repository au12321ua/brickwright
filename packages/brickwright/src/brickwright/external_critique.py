"""Camera matching and visual critique instructions for host agents."""

from shlex import quote

from .record import Record
from .tools.software_rendering import MAX_ELEVATION

DEFAULT_CRITIQUE_ROUNDS = 2


def external_viewpoint_prompt(record: Record) -> str:
    """Explain how to find a camera angle that matches the attached target."""
    record_id = quote(record.id)
    records_dir = quote(str(record.path.parent.resolve()))
    return f"""\
Use the reference image and `build.py` to estimate which camera angle shows the
same facing and tilt. Render an initial view and adjust the angles as needed.

    brickwright external render-angle {record_id} --azimuth <A> --elevation <E> --records-dir {records_dir}

- Azimuth ranges from -180 to 180 degrees. 0 faces the front and positive angles
  swing the camera to the build's right.
- Elevation ranges from -{MAX_ELEVATION:g} to {MAX_ELEVATION:g} degrees. 0 is level and
  positive angles look down from above.

The command writes the current image to {record.viewpoint_path.resolve()}.
Keep the matched render and record the azimuth and elevation you used."""


def external_critique_instructions(
    record: Record, *, rounds: int = DEFAULT_CRITIQUE_ROUNDS
) -> str:
    """Show the record-specific steps for reviewing a build against its target."""
    reference = record.prompt_image_path
    if reference is None:
        raise ValueError("external critique requires a prompt image")

    return f"""\
# Review the build against its target

- Record: {record.id}
- Reference image: {reference.resolve()}
- Build source: {record.build_path.resolve()}

{external_viewpoint_prompt(record)}

Compare the matched render with the target for shape, proportions, color, and
missing or extra features. Use structural checks for connections that the render
cannot show. You can perform a self-review or ask an independent host reviewer.

For a flow record, use `brickwright external review-instructions` with the matched
angles to prepare a review of the checked snapshot. Use `external status` to see
the current review and delivery gates. Read `references/review.md` for the report
format.

Revise the build and re-run `brickwright external validate` after making changes.
Review again if needed, up to {rounds} rounds."""
