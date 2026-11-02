"""Daylight on room floors: direct sun, skylight and light reflected inside the room.

For each measurement point:

- direct sun: clear-sky direct normal irradiance x the cosine on the plane x the
  transmission toward the sun (0 in shadow, the glass transmission through windows);
- skylight: the sky component (CIE overcast sky, 145 patches, through openings and glass)
  x the clear-sky diffuse light of the moment;
- reflected light: the light entering the room through its glazing, spread over the
  room surfaces by the integrating-sphere (split-flux) method with standard reflectances.

Metrics: illuminance (lux) at each sampled moment of the day, hours of direct sun over the
day, and the daylight factor (%), which uses the overcast sky alone and so does not depend
on the date or time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from . import sky
from .geometry import Scene
from .grid import grid_in_polygon, polygon_area, polygon_perimeter
from .raycast import transmission_matrix
from .solar import EFFICACY_DIFFUSE, EFFICACY_DIRECT, Location, SunSample, day_samples

RAY_OFFSET = 0.05  # points sit this far above the floor so the floor itself casts no shadow
WALL_REACH = 0.75  # glazing this far outside a room outline (a wall thickness) still lights it
SUNLIT = 0.05  # transmission above which a point counts as in direct sun


@dataclass
class Room:
    id: str
    name: str
    outline: list[tuple[float, float]]
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    floor_z: float = 0.0
    height: float = 3.0


@dataclass
class Settings:
    grid: float = 0.5  # m between measurement points
    work_plane: float = 0.0  # height above the floor: 0 on the floor, 0.85 for a desk-height work plane
    step_minutes: int = 15
    floor_reflectance: float = 0.2
    wall_reflectance: float = 0.5
    ceiling_reflectance: float = 0.7
    ground_reflectance: float = 0.2


@dataclass
class Result:
    location: Location
    day: date
    settings: Settings
    rooms: list[Room]
    points: np.ndarray  # (N, 3) measurement points
    room_of_point: np.ndarray  # (N,) index into rooms
    samples: list[SunSample]
    sun_transmission: np.ndarray  # (N, S)
    sky_component: np.ndarray  # (N,) share of the unobstructed horizontal sky illuminance
    room_sky_flux: np.ndarray  # (R,) glazing flux factor for skylight, per unit outdoor horizontal illuminance
    room_sun_flux: np.ndarray  # (R, S) glazing flux factor for direct sun, per unit direct normal illuminance
    room_ground_flux: np.ndarray  # (R,) glazing flux factor for light reflected by the ground
    room_reflection: np.ndarray  # (R,) reflected illuminance per unit of flux entering the room

    @property
    def times(self) -> list[str]:
        return [s.time.strftime("%H:%M") for s in self.samples]

    def illuminance(self, index: int) -> np.ndarray:
        """Lux on the floor (or work plane) at one sampled moment."""
        s = self.samples[index]
        direct_normal = EFFICACY_DIRECT * s.direct_normal
        diffuse = EFFICACY_DIFFUSE * s.diffuse_horizontal
        direct = direct_normal * max(s.direction[2], 0.0) * self.sun_transmission[:, index]
        skylight = diffuse * self.sky_component
        outdoor_horizontal = direct_normal * max(s.direction[2], 0.0) + diffuse
        entering = (direct_normal * self.room_sun_flux[:, index] + diffuse * self.room_sky_flux
                    + outdoor_horizontal * self.room_ground_flux)
        reflected = (entering * self.room_reflection)[self.room_of_point]
        return direct + skylight + reflected

    def sun_hours(self) -> np.ndarray:
        """Hours of direct sun (through openings or glass) over the day."""
        lit = (self.sun_transmission > SUNLIT).sum(axis=1)
        return lit * self.settings.step_minutes / 60.0

    def daylight_factor(self) -> np.ndarray:
        """Daylight factor in %: indoor / outdoor illuminance under the CIE overcast sky."""
        reflected = ((self.room_sky_flux + self.room_ground_flux) * self.room_reflection)[self.room_of_point]
        return 100.0 * (self.sky_component + reflected)


def _distance_to_outline(point: np.ndarray, ring: np.ndarray) -> float:
    a, b = ring, np.roll(ring, -1, axis=0)
    ab = b - a
    t = np.clip(((point - a) * ab).sum(axis=1) / np.maximum((ab * ab).sum(axis=1), 1e-12), 0, 1)
    return float(np.min(np.linalg.norm(a + ab * t[:, None] - point, axis=1)))


def _room_of_glass(rooms: list[Room], centroid: np.ndarray) -> int | None:
    from .grid import _inside

    best, best_distance = None, WALL_REACH
    for index, room in enumerate(rooms):
        if not (room.floor_z - 0.5 <= centroid[2] <= room.floor_z + room.height + 0.5):
            continue
        ring = np.asarray(room.outline, dtype=np.float64)
        if _inside(centroid[None, :2], ring)[0]:
            return index
        distance = _distance_to_outline(centroid[:2], ring)
        if distance < best_distance:
            best, best_distance = index, distance
    return best


def analyze(scene: Scene, rooms: list[Room], location: Location, day: date, settings: Settings | None = None,
            progress: Callable[[float, str], None] | None = None) -> Result:
    settings = settings or Settings()
    report = progress or (lambda fraction, message: None)

    # Measurement points.
    points, owners = [], []
    for index, room in enumerate(rooms):
        cells = grid_in_polygon(room.outline, room.holes, settings.grid)
        z = room.floor_z + max(settings.work_plane, 0.0) + RAY_OFFSET
        points.append(np.column_stack([cells, np.full(len(cells), z)]))
        owners.append(np.full(len(cells), index))
    points = np.concatenate(points) if points else np.zeros((0, 3))
    room_of_point = np.concatenate(owners).astype(np.int64) if owners else np.zeros(0, dtype=np.int64)

    samples = day_samples(location, day, settings.step_minutes)
    sun_dirs = np.array([s.direction for s in samples]) if samples else np.zeros((0, 3))
    sky_dirs, sky_omega = sky.patches()
    weights = sky.overcast_weights(sky_dirs, sky_omega)

    # Glazing: which room it lights, and which of its two sides faces outside.
    glass, glass_room = [], []
    for pane in scene.glass:
        room = _room_of_glass(rooms, np.asarray(pane["centroid"]))
        if room is not None:
            glass.append(pane)
            glass_room.append(room)
    glass_points, glass_normals = [], []
    for pane in glass:
        n = np.asarray(pane["normal"], dtype=np.float64)
        c = np.asarray(pane["centroid"], dtype=np.float64)
        for side in (n, -n):
            glass_points.append(c + side * 0.15)
            glass_normals.append(side)
    glass_points = np.array(glass_points) if glass_points else np.zeros((0, 3))

    directions = np.concatenate([sun_dirs, sky_dirs]) if len(sun_dirs) else sky_dirs
    everything = np.concatenate([points, glass_points]) if len(glass_points) else points
    report(0.0, "Tracing sunlight and skylight")
    light = transmission_matrix(everything, directions, scene, lambda f: report(f, "Tracing sunlight and skylight"))
    floor_light, glass_light = light[: len(points)], light[len(points):]
    sun_count = len(sun_dirs)

    sun_transmission = floor_light[:, :sun_count] * (sun_dirs[:, 2] > 0)[None, :] if sun_count else np.zeros((len(points), 0))
    sky_component = floor_light[:, sun_count:] @ weights

    # Light entering each room through its glazing.
    room_count = len(rooms)
    room_sky = np.zeros(room_count)
    room_sun = np.zeros((room_count, sun_count))
    room_ground = np.zeros(room_count)
    for k, pane in enumerate(glass):
        sides = []
        for j in (0, 1):
            normal = glass_normals[2 * k + j]
            vertical = sky.plane_weights(sky_dirs, sky_omega, normal)
            sides.append((glass_light[2 * k + j, sun_count:] @ vertical, j, normal))
        sky_seen, j, normal = max(sides, key=lambda item: item[0])  # the outside sees more sky
        tau_area = pane["transmission"] * pane["area"]
        room = glass_room[k]
        room_sky[room] += tau_area * sky_seen
        # Light reflected by the ground, seen by the share (1 - n_z) / 2 of the pane's view.
        room_ground[room] += tau_area * settings.ground_reflectance * (1 - normal[2]) / 2
        if sun_count:
            cos_incidence = np.clip(sun_dirs @ normal, 0, None)
            room_sun[room] += tau_area * glass_light[2 * k + j, :sun_count] * cos_incidence

    # Integrating sphere: E_reflected = flux_in x first reflectance / (A_total x (1 - mean reflectance)).
    room_reflection = np.zeros(room_count)
    for index, room in enumerate(rooms):
        floor = polygon_area(room.outline, room.holes)
        walls = polygon_perimeter(room.outline) * room.height
        total = 2 * floor + walls
        if total <= 0:
            continue
        mean = (floor * settings.floor_reflectance + floor * settings.ceiling_reflectance
                + walls * settings.wall_reflectance) / total
        first = (settings.floor_reflectance + settings.wall_reflectance) / 2  # daylight lands low in the room first
        room_reflection[index] = first / (total * (1 - mean))

    report(1.0, "Done")
    return Result(location, day, settings, rooms, points, room_of_point, samples, sun_transmission, sky_component,
                  room_sky, room_sun, room_ground, room_reflection)


def summary(values: np.ndarray, unit: str, target: float | None = None) -> dict:
    if len(values) == 0:
        return {"unit": unit, "points": 0}
    result = {"unit": unit, "points": int(len(values)), "min": float(values.min()), "max": float(values.max()),
              "mean": float(values.mean()), "median": float(np.median(values))}
    if target is not None:
        result["target"] = target
        result["shareAtOrAboveTarget"] = float((values >= target).mean())
    return result
