"""Render fixed LDraw cameras with the regular BrickWright mesh pipeline."""

from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, Field, FiniteFloat, model_validator

from .tools.rendering import _angle_array, lit_scene
from .tools.software_rendering import angle_to_view_dir, angle_up

Vec3 = tuple[FiniteFloat, FiniteFloat, FiniteFloat]
Pixels = Annotated[int, Field(strict=True, gt=0)]


class CameraView(BaseModel):
    eye: Vec3
    target: Vec3
    up: Vec3
    vertical_fov_degrees: Annotated[FiniteFloat, Field(gt=0, lt=180)]

    @model_validator(mode="after")
    def validate_basis(self) -> CameraView:
        direction = np.subtract(self.target, self.eye)
        distance = np.linalg.norm(direction)
        up_length = np.linalg.norm(self.up)
        if not np.isfinite(distance) or distance == 0:
            raise ValueError(
                "camera eye and target must have a finite nonzero distance"
            )
        if not np.isfinite(up_length) or up_length == 0:
            raise ValueError("camera up vector must have finite nonzero length")
        if (
            np.linalg.norm(
                np.cross(direction / distance, np.asarray(self.up) / up_length)
            )
            < 1e-8
        ):
            raise ValueError("camera up vector cannot be parallel to the sightline")
        return self


class Cameras(BaseModel):
    image_pixels: tuple[Pixels, Pixels]
    projection: Literal["perspective"] = "perspective"
    views: dict[str, CameraView] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_view_names(self) -> Cameras:
        if any(
            re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name) is None
            for name in self.views
        ):
            raise ValueError("invalid camera view name")
        if len({name.casefold() for name in self.views}) != len(self.views):
            raise ValueError("camera view names must be distinct regardless of case")
        return self


def render_model(
    model: Path,
    cameras: Cameras,
    output_dir: Path,
    *,
    library: Path | None = None,
) -> dict[str, Path]:
    """Write one transparent RGBA PNG per fixed perspective view."""
    import pygfx as gfx
    from PIL import Image
    from rendercanvas.offscreen import RenderCanvas

    width, height = cameras.image_pixels
    aspect = width / height
    # pygfx bases FOV on the mean of both frame dimensions.
    camera = gfx.PerspectiveCamera(aspect=aspect, zoom=2 / (1 + aspect))
    with TemporaryDirectory() as scratch:
        glb = Path(scratch) / "model.glb"
        _convert_sorted(model, glb, library)
        _, scene = lit_scene(glb, camera)

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    canvas = RenderCanvas(size=(width, height), pixel_ratio=1)
    try:
        renderer = gfx.renderers.WgpuRenderer(canvas)
        canvas.request_draw(lambda: None)
        for name, view in cameras.views.items():
            _position_camera(camera, view)
            # Rendering outside the callback propagates GPU errors to the caller.
            renderer.render(scene, camera)
            frame = canvas.draw()
            if frame is None or frame.shape != (height, width, 4):
                raise RuntimeError(f"could not read RGBA frame for {name}")
            path = output_dir / f"{name}.png"
            Image.fromarray(frame).save(path)
            paths[name] = path
    finally:
        canvas.close()
    return paths


def _position_camera(camera, view: CameraView) -> None:
    from ldraw2mesh.convert import DEFAULT_SCALE
    from ldraw2mesh.transform import LDRAW_TO_GLTF

    axes = LDRAW_TO_GLTF[:3, :3]
    eye = (axes @ view.eye) * DEFAULT_SCALE
    target = (axes @ view.target) * DEFAULT_SCALE
    distance = float(np.linalg.norm(eye - target))
    camera.fov = view.vertical_fov_degrees
    camera.depth_range = (distance / 100, distance * 10)
    camera.local.position = eye
    camera.world.reference_up = axes @ view.up
    camera.look_at(target)


def _convert_sorted(model: Path, glb: Path, library: Path | None) -> None:
    from ldraw2mesh import EmptySceneError, load_scene
    from ldraw2mesh._native import load_color_table
    from ldraw2mesh.gltf import write_gltf
    from ldraw2mesh.library import resolve_library
    from ldraw2mesh.scene import build_jobs

    library = resolve_library(library)
    jobs = build_jobs(load_scene(model, ldraw_library=library, gaps=True))
    if not any(job.triangles.size for job in jobs):
        raise EmptySceneError(f"{model} produced no renderable geometry")
    # The native loader groups geometry in a hash map. Order depth ties consistently.
    jobs.sort(key=lambda job: (job.path_name, job.color))
    for job in jobs:
        job.transforms = np.asarray(
            sorted(job.transforms, key=lambda matrix: np.asarray(matrix).tobytes())
        )
    write_gltf(jobs, load_color_table(str(library)), glb)


class MatchedAngle(BaseModel):
    azimuth: FiniteFloat
    elevation: Annotated[FiniteFloat, Field(ge=-90, le=90)]


def render_matched_model(model: Path, camera: MatchedAngle, output: Path) -> None:
    """Frame a checked model at the standard matched-viewpoint angles."""
    from PIL import Image

    with TemporaryDirectory() as scratch:
        glb = Path(scratch) / "model.glb"
        _convert_sorted(model, glb, None)
        frame = _angle_array(
            glb,
            angle_to_view_dir(camera.azimuth, camera.elevation),
            angle_up(camera.elevation),
        )
    Image.fromarray(frame).save(output)
