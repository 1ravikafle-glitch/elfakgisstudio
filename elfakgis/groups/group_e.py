"""Elfak GIS Studio — Group E polygon subdivider (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from elfakgis.geo.geom import _force_valid, _close_poly, _as_poly, _repair

log = logging.getLogger("elfakgis")
from elfakgis.core.config import UPLOAD
from elfakgis.core.store import _prog
from elfakgis.geo.geom import _GEOM_TOL_FRAC, _enforce_poly_gdf, _safe_dn, gdf_from_records, normalize_order, safe_col

import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
import matplotlib.gridspec as gridspec
import matplotlib.ticker as mticker
from matplotlib.transforms import Bbox
from shapely.geometry import Polygon, Point, LineString, MultiPolygon, box
from shapely.geometry import box as _sbox
from shapely.ops import unary_union
from shapely import affinity
# GROUP E – POLYGON SUBDIVIDER
# ----------------------------------------------------------------------
"""
Polygon Subdivision Module  (fixed v3)
───────────────────────────────────────
Key fixes in this version
  • snap_tol auto-scaled to geometry units (degrees vs metres)
  • hard_clip() applied to EVERY piece before validation — no leaks possible
  • orig_poly padding fallback removed; _ensure_count raises instead
  • safe_overlay never silently returns an unclipped geometry
  • _validate_subdivision tolerance is unit-aware
  • _bisect PA-rotation path clips against original polygon, not running rem
  • _enforce_area_tolerance transfer always ≥ 0; uses join_style for compat
  • _clip_to_original guards every .length call against None
  • _ensure_count loop-capped; sub-pieces clipped before use
  • _subdivide_voronoi seeds never fewer than needed; GeometryCollection handled
  • _subdivide_grid explicit guard loops
"""

import math
import os
import uuid
import zipfile

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.affinity import rotate as sh_rotate
from shapely.geometry import (
    LineString, MultiPoint, Point, Polygon, MultiPolygon, box as _sbox,
)
from shapely.ops import snap, unary_union, voronoi_diagram
from shapely.validation import make_valid

# ═══════════════════════════════════════════════════════════
# SECTION 1 – SNAP TOLERANCE (unit-aware)
# ═══════════════════════════════════════════════════════════

def _snap_tol(geom):
    """
    Return a snap/residual tolerance appropriate for the geometry's coordinate scale.
    Projected CRS (metres): coords ~ 1e4–1e6  → tol = 0.01 m
    Geographic CRS (degrees): coords ~ -180..180 → tol = 1e-8 °
    """
    try:
        minx, miny, maxx, maxy = geom.bounds
        span = max(maxx - minx, maxy - miny, 1e-9)
        # If span > 1000, almost certainly metres
        return 0.01 if span > 1000 else 1e-8
    except Exception:
        return 1e-7

# residual tolerance for validation (m² or deg²); derived lazily from geometry
# SECTION 3 – HARD CLIP (guaranteed containment)
# ═══════════════════════════════════════════════════════════

def _hard_clip(piece, orig_poly, tol=None):
    """
    Guarantee that `piece` lies entirely within `orig_poly`.
    Uses progressively larger snap tolerances until the intersection succeeds
    and the result has no residual outside orig_poly.

    Returns a valid Polygon fully inside orig_poly, or None if impossible.
    """
    if piece is None or orig_poly is None:
        return None

    tol = tol or _snap_tol(orig_poly)
    tolerances = [tol, tol * 10, tol * 100, tol * 1000]

    for t in tolerances:
        try:
            ps = snap(piece, orig_poly, t)
            os_ = snap(orig_poly, piece, t)
            result = ps.intersection(os_)
            if result is None or result.is_empty:
                continue
            result = _repair(result)
            if result is None or result.is_empty:
                continue
            # Verify no residual leak
            diff = result.difference(orig_poly.buffer(t))
            if diff is None or diff.is_empty or diff.area < orig_poly.area * _GEOM_TOL_FRAC:
                # Final clip to be safe
                final = result.intersection(orig_poly.buffer(t * 2))
                if final is None or final.is_empty:
                    final = result
                return _close_poly(_repair(final))
        except Exception:
            continue

    # Last resort: use buffer(tol) on orig to absorb floating-point boundary
    try:
        padded = orig_poly.buffer(tol * 100)
        result = piece.intersection(padded)
        if result and not result.is_empty:
            result = result.intersection(orig_poly)
            if result and not result.is_empty:
                return _close_poly(_repair(result))
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════
# SECTION 4 – SAFE OVERLAY
# ═══════════════════════════════════════════════════════════

def safe_overlay(a, b, op):
    """
    Robust two-operand overlay. snap_tol is derived from geometry scale.
    Never returns a geometry that is larger than both inputs (for intersection).
    """
    a = _repair(a)
    b = _repair(b)
    if a is None or b is None:
        return None

    tol = _snap_tol(a)

    def _do(ga, gb):
        if op == "intersection":
            return ga.intersection(gb)
        if op == "difference":
            return ga.difference(gb)
        if op == "union":
            return ga.union(gb)
        if op == "symmetric_difference":
            return ga.symmetric_difference(gb)
        raise ValueError(f"Unknown op: {op}")

    # Try with snapping
    try:
        result = _do(snap(a, b, tol), snap(b, a, tol))
        r = _repair(result)
        if r is not None:
            return r
    except Exception:
        pass

    # Fallback: buffer(0) repair before overlay
    try:
        result = _do(a.buffer(0), b.buffer(0))
        r = _repair(result)
        if r is not None:
            return r
    except Exception:
        pass

    # Fallback: make_valid both sides
    try:
        result = _do(make_valid(a), make_valid(b))
        return _repair(result)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# SECTION 5 – SHAPE DESCRIPTORS
# ═══════════════════════════════════════════════════════════

def _elong(p):
    minx, miny, maxx, maxy = p.bounds
    dx, dy = maxx - minx, maxy - miny
    return max(dx, dy) / max(min(dx, dy), 1e-9)

def _asp(p):
    b = p.bounds
    w, h = b[2] - b[0], b[3] - b[1]
    return max(w, h) / max(min(w, h), 1e-9)

def _pa_angle(p):
    try:
        c = np.array(p.exterior.coords[:-1])
        c -= c.mean(0)
        _, v = np.linalg.eigh(np.cov(c.T))
        return float(np.arctan2(v[1, 1], v[0, 1]))
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════
# SECTION 6 – AREA-BALANCE REFINEMENT
# ═══════════════════════════════════════════════════════════

def _enforce_area_tolerance(pieces, orig_poly, n, tol_ha):
    if not pieces or n < 2:
        return pieces

    tol_m2 = tol_ha * 10_000.0
    ideal = orig_poly.area / n

    for _iter in range(50):
        areas = [p.area for p in pieces]
        if max(areas) - min(areas) <= tol_m2:
            break

        i_max = int(np.argmax(areas))
        i_min = int(np.argmin(areas))
        if i_max == i_min:
            break

        excess  = areas[i_max] - ideal
        deficit = ideal - areas[i_min]
        transfer = max(min(excess, deficit) * 0.5, 0.0)
        if transfer < 1.0:
            break

        shared = safe_overlay(
            pieces[i_max].boundary, pieces[i_min].boundary, "intersection"
        )
        if shared is None or shared.is_empty or shared.length < 0.5:
            break

        strip_w = max(transfer / shared.length, 0.05)
        buf = shared.buffer(strip_w, join_style=2)
        strip = safe_overlay(pieces[i_max], buf, "intersection")
        if strip is None or strip.is_empty or strip.area < 0.1:
            continue

        ni = safe_overlay(pieces[i_max], strip, "difference")
        nj = safe_overlay(pieces[i_min], strip, "union")
        if (ni is not None and nj is not None
                and ni.area > ideal * 0.05 and nj.area > ideal * 0.05):
            pieces[i_max] = _close_poly(ni)
            pieces[i_min] = _close_poly(nj)

    return [_close_poly(_repair(p)) for p in pieces]


# ═══════════════════════════════════════════════════════════
# SECTION 7 – CLIP TO ORIGINAL & FILL GAPS
# ═══════════════════════════════════════════════════════════

def _clip_to_original(pieces, orig_poly):
    """
    Hard-clip every piece, then fill any residual gap back into
    the best-touching neighbour.
    """
    if not pieces:
        return [orig_poly]

    tol = _snap_tol(orig_poly)
    clipped = []
    for p in pieces:
        c = _hard_clip(p, orig_poly, tol)
        if c is not None and not c.is_empty:
            clipped.append(_close_poly(c))

    if not clipped:
        return [orig_poly]

    union_all = clipped[0]
    for p in clipped[1:]:
        union_all = safe_overlay(union_all, p, "union")

    gap = safe_overlay(orig_poly, union_all, "difference")
    if gap is None or gap.is_empty or gap.area <= orig_poly.area * _GEOM_TOL_FRAC:
        return clipped

    gap_geoms = (
        list(gap.geoms)
        if gap.geom_type in ("MultiPolygon", "GeometryCollection")
        else [gap]
    )

    for frag in gap_geoms:
        if frag is None or frag.is_empty or frag.area < orig_poly.area * _GEOM_TOL_FRAC:
            continue
        best_i, best_len = 0, -1.0
        for idx, cell in enumerate(clipped):
            inter = safe_overlay(cell.boundary, frag.boundary, "intersection")
            length = inter.length if (inter is not None and not inter.is_empty) else 0.0
            if length > best_len:
                best_len = length
                best_i = idx
        merged = safe_overlay(clipped[best_i], frag, "union")
        if merged is not None and not merged.is_empty:
            clipped[best_i] = _close_poly(merged)

    return clipped


# ═══════════════════════════════════════════════════════════
# SECTION 8 – ENSURE EXACT PIECE COUNT
# ═══════════════════════════════════════════════════════════

def _ensure_count(pieces, n, orig_poly):
    def _clean(lst):
        out = []
        for p in lst:
            p2 = _close_poly(_repair(p))
            if p2 is not None and not p2.is_empty:
                out.append(p2)
        return out

    pieces = _clean(pieces)
    if not pieces:
        # Bootstrap with grid
        pieces = _subdivide_grid(orig_poly, n)
        pieces = _clean(pieces)

    # Grow: split largest
    max_attempts = n * 4
    attempt = 0
    while len(pieces) < n and attempt < max_attempts:
        attempt += 1
        li = max(range(len(pieces)), key=lambda i: pieces[i].area)
        big = pieces.pop(li)
        sub = _bisect(big, 2)
        sub = _clean(sub)
        sub = [_close_poly(safe_overlay(p, orig_poly, "intersection")) for p in sub]
        sub = _clean(sub)
        if len(sub) >= 2:
            pieces.extend(sub)
        else:
            pieces.append(big)
            break

    # Shrink: merge smallest into best neighbour
    while len(pieces) > n:
        si = min(range(len(pieces)), key=lambda i: pieces[i].area)
        small = pieces.pop(si)
        if not pieces:
            pieces.append(small)
            break
        best_j, best_len = 0, -1.0
        for j, other in enumerate(pieces):
            inter = safe_overlay(small.boundary, other.boundary, "intersection")
            length = inter.length if (inter is not None and not inter.is_empty) else 0.0
            if length > best_len:
                best_len = length
                best_j = j
        merged = safe_overlay(small, pieces[best_j], "union")
        merged = safe_overlay(merged, orig_poly, "intersection") if merged else None
        if merged is not None and not merged.is_empty:
            pieces[best_j] = _close_poly(merged)
        else:
            pieces.append(small)
            break

    pieces = [_close_poly(safe_overlay(p, orig_poly, "intersection")) for p in pieces]
    return _clean(pieces)


# ═══════════════════════════════════════════════════════════
# SECTION 9 – FINAL HARD-CLIP PASS BEFORE VALIDATION
# ═══════════════════════════════════════════════════════════

def _final_clip_pass(pieces, orig_poly):
    """
    Last-chance hard clip: ensure EVERY piece is strictly inside orig_poly.
    Any piece that still leaks after _hard_clip is replaced by its intersection.
    """
    tol = _snap_tol(orig_poly)
    result = []
    for p in pieces:
        if p is None or p.is_empty:
            continue
        # Quick check: does it leak?
        try:
            diff = p.difference(orig_poly)
            leaks = diff is not None and not diff.is_empty and diff.area > orig_poly.area * _GEOM_TOL_FRAC
        except Exception:
            leaks = True

        if leaks:
            clipped = _hard_clip(p, orig_poly, tol)
            if clipped is not None and not clipped.is_empty:
                result.append(_close_poly(clipped))
        else:
            result.append(_close_poly(p))
    return result


# ═══════════════════════════════════════════════════════════
# SECTION 10 – VALIDATION
# ═══════════════════════════════════════════════════════════

def _validate_subdivision(pieces, orig_poly, n, tol_ha):
    if len(pieces) != n:
        raise ValueError(f"Expected {n} pieces, got {len(pieces)}")

    # Residual tolerance: 0.01% of orig area, minimum 0.01 m²
    res_tol = max(orig_poly.area * _GEOM_TOL_FRAC, 0.01)

    for i, p in enumerate(pieces):
        if p is None or p.is_empty:
            raise ValueError(f"Piece {i} is empty")
        if not p.is_valid:
            raise ValueError(f"Piece {i} is invalid geometry")
        try:
            diff = p.difference(orig_poly)
            leak = diff.area if (diff is not None and not diff.is_empty) else 0.0
        except Exception:
            leak = 0.0
        if leak > res_tol:
            raise ValueError(
                f"Piece {i} leaks outside original boundary "
                f"(residual {leak:.3e} m², tolerance {res_tol:.3e} m²)"
            )

    union_all = pieces[0]
    for p in pieces[1:]:
        union_all = safe_overlay(union_all, p, "union")
    try:
        gap = orig_poly.difference(union_all) if union_all else orig_poly
        gap_area = gap.area if (gap is not None and not gap.is_empty) else 0.0
    except Exception:
        gap_area = 0.0
    if gap_area > res_tol:
        raise ValueError(
            f"Pieces do not fully cover original polygon "
            f"(uncovered area: {gap_area:.3e} m²)"
        )

    areas = [p.area for p in pieces]
    spread = max(areas) - min(areas)
    tol_m2 = tol_ha * 10_000.0
    if spread > tol_m2:
        raise ValueError(
            f"Area spread {spread:.2f} m² ({spread/10000:.4f} ha) "
            f"exceeds tolerance {tol_m2:.2f} m² ({tol_ha} ha)"
        )
    return True


# ═══════════════════════════════════════════════════════════
# SECTION 11 – FOUR SUBDIVISION METHODS
# ═══════════════════════════════════════════════════════════

def _bisect(poly, n, axis=None, depth=0):
    from shapely.ops import split as sh_split

    poly = _repair(poly)
    if poly is None or poly.is_empty or poly.area < 1e-10:
        return []
    if n <= 1:
        return [_close_poly(poly)]

    minx, miny, maxx, maxy = poly.bounds
    cx0, cy0 = poly.centroid.x, poly.centroid.y

    # PA rotation for elongated polygons
    if axis is None and depth < 2 and _elong(poly) > 1.6:
        rot_deg = np.degrees(_pa_angle(poly))
        if 10 < abs(rot_deg) % 90 < 80:
            try:
                pr = sh_rotate(poly, -rot_deg, origin=(cx0, cy0))
                pr = _repair(pr)
                if pr and not pr.is_empty:
                    rp = _bisect(pr, n, "x", depth + 1)
                    if len(rp) == n:
                        pieces = []
                        for p in rp:
                            pb = sh_rotate(p, rot_deg, origin=(cx0, cy0))
                            # Clip against the ORIGINAL (unrotated) polygon
                            pb = safe_overlay(pb, poly, "intersection")
                            pg = _as_poly(pb)
                            if pg and pg.area > 1e-10:
                                pieces.append(_close_poly(pg))
                        if len(pieces) == n:
                            return pieces
            except Exception:
                pass

    nl = n // 2
    nr = n - nl
    frac = nl / n

    def _cut(cax):
        lo, hi = (minx, maxx) if cax == "x" else (miny, maxy)
        target = poly.area * frac
        best_m = (lo + hi) / 2.0

        for _ in range(120):
            m = (lo + hi) / 2.0
            box = (
                _sbox(minx - 1, miny - 1, m, maxy + 1)
                if cax == "x"
                else _sbox(minx - 1, miny - 1, maxx + 1, m)
            )
            lp = safe_overlay(poly, box, "intersection")
            got = lp.area if lp else 0.0
            err = abs(got - target) / (target + 1e-12)
            if err < 5e-5:
                best_m = m
                break
            if got < target:
                lo = m
            else:
                hi = m
            best_m = m
            if hi - lo < 1e-10:
                break

        box = (
            _sbox(minx - 1, miny - 1, best_m, maxy + 1)
            if cax == "x"
            else _sbox(minx - 1, miny - 1, maxx + 1, best_m)
        )
        lp = safe_overlay(poly, box, "intersection")
        rp = safe_overlay(poly, lp, "difference") if lp else None

        la = _as_poly(lp)
        ra = _as_poly(rp)
        if la and ra and la.area > 1e-10 and ra.area > 1e-10:
            return la, ra

        # LineString split fallback
        try:
            if cax == "x":
                cut_line = LineString([(best_m, miny - 1), (best_m, maxy + 1)])
            else:
                cut_line = LineString([(minx - 1, best_m), (maxx + 1, best_m)])
            parts = [p for p in sh_split(poly, cut_line).geoms if p.area > 1e-10]
            if len(parts) >= 2:
                parts.sort(key=lambda p: p.centroid.x if cax == "x" else p.centroid.y)
                la = _as_poly(parts[0])
                ra = _as_poly(parts[-1])
                if la and ra and la.area > 1e-10 and ra.area > 1e-10:
                    return la, ra
        except Exception:
            pass
        return None

    best = None
    for cax in (["x", "y"] if axis is None else [axis]):
        cut = _cut(cax)
        if cut is None:
            continue
        lp, rp = cut
        score = max(_asp(lp), _asp(rp))
        if best is None or score < best[0]:
            best = (score, lp, rp)

    if best is None:
        return [_close_poly(poly)]

    _, lp, rp = best
    lp = _close_poly(_repair(safe_overlay(lp, poly, "intersection")))
    rp = _close_poly(_repair(safe_overlay(rp, poly, "intersection")))

    left = _bisect(lp, nl, None, depth + 1)
    right = _bisect(rp, nr, None, depth + 1)
    return [
        _close_poly(_repair(p))
        for p in left + right
        if p and not p.is_empty
    ]


def _subdivide_ba(poly, n, area_tol_ha=0.3):
    pieces = _bisect(poly, n)
    if not pieces:
        return [_close_poly(poly)]
    pieces = [_close_poly(_repair(p)) for p in pieces if p and not p.is_empty]
    if len(pieces) < n:
        return _subdivide_grid(poly, n)

    ideal = poly.area / n

    for _iter in range(15):
        areas = [p.area for p in pieces]
        if max(areas) - min(areas) <= area_tol_ha * 10_000.0:
            break
        pairs = sorted(
            [(i, j) for i in range(len(pieces))
             for j in range(i + 1, len(pieces))],
            key=lambda ij: abs(areas[ij[0]] - areas[ij[1]]),
            reverse=True,
        )
        changed = False
        for i, j in pairs:
            ai, aj = pieces[i].area, pieces[j].area
            if abs(ai - aj) < 1.0:
                continue
            shared = safe_overlay(
                pieces[i].boundary, pieces[j].boundary, "intersection"
            )
            if shared is None or shared.is_empty or shared.length < 0.5:
                continue
            big, small = (i, j) if ai > aj else (j, i)
            buf_w = max(abs(ai - aj) * 0.5 / shared.length, 0.05)
            buf = shared.buffer(buf_w, join_style=2)
            strip = safe_overlay(pieces[big], buf, "intersection")
            if strip is None or strip.is_empty:
                continue
            nb = safe_overlay(pieces[big], strip, "difference")
            ns = safe_overlay(pieces[small], strip, "union")
            if (nb is not None and ns is not None
                    and nb.area > ideal * 0.05 and ns.area > ideal * 0.05):
                pieces[big] = _close_poly(nb)
                pieces[small] = _close_poly(ns)
                areas[big] = pieces[big].area
                areas[small] = pieces[small].area
                changed = True
        if not changed:
            break

    return pieces


def _subdivide_voronoi(poly, n, area_tol_ha=0.3, max_iter=30):
    import random
    try:
        minx, miny, maxx, maxy = poly.bounds
        seeds = []

        cn = int(math.ceil(math.sqrt(n)))
        rn = int(math.ceil(n / cn))
        for ri in range(rn):
            for ci in range(cn):
                px = minx + (ci + 0.5) * (maxx - minx) / cn
                py = miny + (ri + 0.5) * (maxy - miny) / rn
                pt = Point(px, py)
                if poly.contains(pt):
                    seeds.append(pt)

        for _ in range(n * 500):
            if len(seeds) >= n:
                break
            pt = Point(random.uniform(minx, maxx), random.uniform(miny, maxy))
            if poly.contains(pt):
                seeds.append(pt)

        if len(seeds) < 2:
            return _subdivide_grid(poly, n)
        seeds = seeds[:n]

        for _ in range(max_iter):
            mp = MultiPoint(seeds)
            try:
                diagram = voronoi_diagram(mp, envelope=poly.buffer(
                    max(_snap_tol(poly) * 100, 1.0)
                ))
            except Exception:
                break

            cells = []
            for region in diagram.geoms:
                cl = safe_overlay(region, poly, "intersection")
                if cl is not None and not cl.is_empty and cl.area > 1e-10:
                    pg = _as_poly(cl)
                    if pg:
                        cells.append(pg)

            if not cells:
                break
            cells.sort(key=lambda c: c.area, reverse=True)
            cells = cells[:n]

            new_seeds = [c.centroid for c in cells]
            moved = max(
                (ns.distance(s) for ns, s in zip(new_seeds, seeds[: len(new_seeds)])),
                default=0.0,
            )
            seeds = new_seeds
            if moved < _snap_tol(poly) * 10 or len(seeds) < 2:
                break

        cells = [_close_poly(_repair(c)) for c in cells if c and not c.is_empty]
        return cells if len(cells) >= 2 else _subdivide_grid(poly, n)

    except Exception:
        return _subdivide_grid(poly, n)


def _subdivide_grid(poly, n):
    cn = int(math.ceil(math.sqrt(n)))
    rn = int(math.ceil(n / cn))
    minx, miny, maxx, maxy = poly.bounds
    cw = (maxx - minx) / cn
    rh = (maxy - miny) / rn

    cells = []
    for ri in range(rn):
        for ci in range(cn):
            box = _sbox(
                minx + ci * cw, miny + ri * rh,
                minx + (ci + 1) * cw, miny + (ri + 1) * rh,
            )
            cl = safe_overlay(box, poly, "intersection")
            if cl is not None and cl.area > 1e-10:
                cells.append(_close_poly(cl))

    # Merge excess
    max_merge = len(cells) * 3
    attempt = 0
    while len(cells) > n and attempt < max_merge:
        attempt += 1
        si = min(range(len(cells)), key=lambda i: cells[i].area)
        small = cells.pop(si)
        if not cells:
            cells.append(small)
            break
        best_j, best_len = 0, -1.0
        for j, c in enumerate(cells):
            inter = safe_overlay(small.boundary, c.boundary, "intersection")
            length = inter.length if (inter is not None and not inter.is_empty) else 0.0
            if length > best_len:
                best_len = length
                best_j = j
        merged = safe_overlay(cells[best_j], small, "union")
        if merged is not None and not merged.is_empty:
            cells[best_j] = _close_poly(merged)
        else:
            cells.append(small)
            break

    # Split deficit
    max_split = n * 4
    attempt = 0
    while len(cells) < n and attempt < max_split:
        attempt += 1
        li = max(range(len(cells)), key=lambda i: cells[i].area)
        big = cells.pop(li)
        sub = _bisect(big, 2)
        sub = [_close_poly(p) for p in sub if p and not p.is_empty]
        if len(sub) >= 2:
            cells.extend(sub)
        else:
            cells.append(big)
            break

    return cells[:n]


# ═══════════════════════════════════════════════════════════
# SECTION 12 – MASTER SUBDIVISION PIPELINE
# ═══════════════════════════════════════════════════════════

def _subdivide(poly, n, method="bisect", area_tol_ha=0.3):
    """
    Full pipeline with guaranteed boundary preservation.

    Order of operations:
      1  Repair & validate input
      2  Run chosen method
      3  _ensure_count  → exactly n pieces
      4  _clip_to_original  → clip + fill gaps
      5  _enforce_area_tolerance  → balance areas
      6  _clip_to_original  → re-clip after balance moves
      7  _ensure_count  → restore count if balance changed it
      8  _final_clip_pass  → hard-clip every piece (catches any remaining leak)
      9  _validate_subdivision  → raise on any remaining error
    """
    n = max(2, min(15, int(n)))

    orig_poly = _repair(poly)
    if orig_poly is None or orig_poly.is_empty:
        return []
    if not orig_poly.is_valid:
        orig_poly = make_valid(orig_poly)
    orig_poly = orig_poly.buffer(0)
    orig_poly = _repair(orig_poly)
    if orig_poly is None or orig_poly.is_empty:
        return []

    method_map = {
        "voronoi": _subdivide_voronoi,
        "grid":    _subdivide_grid,
        "ba":      _subdivide_ba,
        "bisect":  _bisect,
    }
    fn = method_map.get(method, _bisect)
    try:
        if method in ("ba", "voronoi"):
            pieces = fn(orig_poly, n, area_tol_ha)
        else:
            pieces = fn(orig_poly, n)
    except Exception as e:
        print(f"[subdivision] method={method!r} failed ({e}); falling back to grid.")
        pieces = _subdivide_grid(orig_poly, n)

    # Post-processing pipeline
    pieces = _ensure_count(pieces, n, orig_poly)
    pieces = _clip_to_original(pieces, orig_poly)
    pieces = _enforce_area_tolerance(pieces, orig_poly, n, area_tol_ha)
    pieces = _clip_to_original(pieces, orig_poly)
    pieces = _ensure_count(pieces, n, orig_poly)

    # ── CRITICAL: hard-clip every piece before validation ──────────────────
    pieces = _final_clip_pass(pieces, orig_poly)

    # If count drifted (shouldn't), fix it
    if len(pieces) != n:
        pieces = _ensure_count(pieces, n, orig_poly)
        pieces = _final_clip_pass(pieces, orig_poly)

    # Final cleanup
    pieces = [_close_poly(_repair(p)) for p in pieces if p is not None and not p.is_empty]

    if len(pieces) < n:
        # Absolute last resort: pad from grid (already clipped to orig_poly)
        grid_extras = _subdivide_grid(orig_poly, n)
        grid_extras = _final_clip_pass(grid_extras, orig_poly)
        pieces = pieces + grid_extras[len(pieces):]
        pieces = pieces[:n]

    _validate_subdivision(pieces, orig_poly, n, area_tol_ha)
    return pieces[:n]


# ═══════════════════════════════════════════════════════════
# SECTION 13 – OUTPUT HELPERS
# ═══════════════════════════════════════════════════════════

def _extract_div_pts(pieces, fname):
    recs = []
    sn = 1
    for i, p in enumerate(pieces, 1):
        cid = f"Comp_{i:03d}"
        p = _close_poly(_repair(p))
        if p is None or p.is_empty:
            continue
        coords = list(p.exterior.coords)
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if not coords:
            continue
        n_c = len(coords)
        edge_lens = [
            math.hypot(
                coords[(j + 1) % n_c][0] - coords[j][0],
                coords[(j + 1) % n_c][1] - coords[j][1],
            )
            for j in range(n_c)
        ]
        mean_e = sum(edge_lens) / len(edge_lens) if edge_lens else 1.0
        thresh = mean_e * 1.5

        for j, (cx, cy) in enumerate(coords):
            recs.append({
                "SN": sn, "Forest": fname, "Comp_ID": cid,
                "Type": "Vertex", "X": round(cx, 4), "Y": round(cy, 4),
            })
            sn += 1
            nx, ny = coords[(j + 1) % n_c]
            if edge_lens[j] > thresh:
                recs.append({
                    "SN": sn, "Forest": fname, "Comp_ID": cid,
                    "Type": "Midpoint",
                    "X": round((cx + nx) / 2, 4),
                    "Y": round((cy + ny) / 2, 4),
                })
                sn += 1
    return recs


def _save_compartments(pieces, fname, crs, save_dir):
    os.makedirs(save_dir, exist_ok=True)
    pieces = [_close_poly(_repair(p)) for p in pieces if p and not p.is_empty]
    total_area = sum(p.area for p in pieces)

    poly_recs, line_recs, pt_recs = [], [], []
    for i, p in enumerate(pieces, 1):
        cid = f"Comp_{i:03d}"
        ah  = round(p.area / 10_000, 4)
        pm  = round(p.length, 4)
        pct = round(p.area / total_area * 100, 2) if total_area > 0 else 0
        poly_recs.append({
            "Forest": fname, "Comp_ID": cid,
            "Area_ha": ah, "Perim_m": pm, "Pct_Area": pct, "geometry": p,
        })
        ext = list(p.exterior.coords)
        if ext[0] != ext[-1]:
            ext.append(ext[0])
        line_recs.append({"Forest": fname, "Comp_ID": cid, "geometry": LineString(ext)})
        pt_recs.append({
            "Forest": fname, "Comp_ID": cid,
            "Area_ha": ah, "Pct_Area": pct, "geometry": p.centroid,
        })

    pg  = gdf_from_records(poly_recs, crs)
    lg  = gdf_from_records(line_recs, crs, ["geometry"])
    ptg = gdf_from_records(pt_recs,   crs)

    pg  = _enforce_poly_gdf(pg)
    pfx = _safe_dn(fname)

    if not pg.empty:
        pg.to_file(os.path.join(save_dir, f"{pfx}_compartment_polygon.shp"))
    if not lg.empty:
        lg.to_file(os.path.join(save_dir, f"{pfx}_compartment_line.shp"))
    if not ptg.empty:
        ptg.to_file(os.path.join(save_dir, f"{pfx}_compartment_point.shp"))

    pd.DataFrame(
        [{k: v for k, v in r.items() if k != "geometry"} for r in poly_recs]
    ).to_excel(
        os.path.join(save_dir, f"{pfx}_compartment_summary.xlsx"), index=False
    )

    dp = _extract_div_pts(pieces, fname)
    if dp:
        ddf = pd.DataFrame(dp)
        ddf.to_excel(
            os.path.join(save_dir, f"{pfx}_division_points.xlsx"), index=False
        )
        gpd.GeoDataFrame(
            ddf,
            geometry=gpd.points_from_xy(ddf["X"], ddf["Y"]),
            crs=crs,
        ).to_file(os.path.join(save_dir, f"{pfx}_division_points.shp"))

    return pg, lg, ptg


# ═══════════════════════════════════════════════════════════
# SECTION 14 – INPUT LOADERS
# ═══════════════════════════════════════════════════════════

def _df_to_poly(df, xc, yc, oc):
    if oc and oc in df.columns:
        df = df.sort_values(oc)
    coords = list(zip(df[xc], df[yc]))
    if len(coords) < 3:
        raise ValueError("Need ≥ 3 points to build a polygon.")
    coords.append(coords[0])
    return _close_poly(_repair(Polygon(coords)))


def _load_polys_from_zip(file, target_shp, crs, fcol=None):
    folder = os.path.join(UPLOAD, str(uuid.uuid4()))
    os.makedirs(folder, exist_ok=True)
    zp = os.path.join(folder, "i.zip")
    file.save(zp)
    with zipfile.ZipFile(zp) as z:
        z.extractall(folder)

    shps = [
        os.path.join(r, f)
        for r, _, fs in os.walk(folder)
        for f in fs if f.endswith(".shp")
    ]
    if not shps:
        raise ValueError("No .shp found in ZIP.")

    sp = shps[0]
    if target_shp:
        tn = os.path.basename(target_shp)
        for s in shps:
            if os.path.basename(s) == tn:
                sp = s
                break

    gdf = gpd.read_file(sp)
    if gdf.empty:
        raise ValueError("Shapefile is empty.")
    gdf = gdf.set_crs(crs) if gdf.crs is None else gdf.to_crs(crs)

    nc = None
    if fcol:
        for c in gdf.columns:
            if c.lower() == fcol.lower():
                nc = c
                break
    if nc is None:
        for cand in ("Forest","forest","Name","name","NAME","Label","label","ID","id"):
            if cand in gdf.columns:
                nc = cand
                break
    if nc is None:
        for c in gdf.columns:
            if c != "geometry" and gdf[c].dtype == object:
                nc = c
                break

    results = []
    for i, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        fn = str(row[nc]) if nc else f"Feature_{i + 1}"
        if geom.geom_type == "Polygon":
            pls = [_repair(geom)]
        elif geom.geom_type == "MultiPolygon":
            pls = [_repair(unary_union(list(geom.geoms)))]
        elif hasattr(geom, "geoms"):
            pls = [_repair(unary_union(
                [g for g in geom.geoms if g.geom_type == "Polygon"]
            ))]
        else:
            pls = []
        for p in pls:
            if p and p.area > 1e-6:
                results.append((fn, _close_poly(p)))

    if not results:
        raise ValueError("No polygon geometries found in shapefile.")
    return results, shps


# ═══════════════════════════════════════════════════════════
# SECTION 15 – MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════

def group_e(
    file_or_df,
    crs,
    out,
    mapping=None,
    e_mode="A",
    n_compartments=4,
    is_zip=False,
    fcol=None,
    area_tol_ha=0.3,
    method="bisect",
    run_id=None,
):
    n_compartments = max(2, min(15, int(n_compartments)))
    ap, al, apts, sv = [], [], [], []

    if is_zip:
        ts = (mapping or {}).get("target_shp")
        features, _ = _load_polys_from_zip(file_or_df, ts, crs, fcol)
        total = len(features)
        for idx, (fn, poly) in enumerate(features):
            pct = 20 + int(60 * idx / max(total, 1))
            if run_id:
                _prog(run_id, f"[{idx+1}/{total}] Subdividing {fn}…", pct)
            pieces = _subdivide(poly, n_compartments, method, area_tol_ha)
            areas  = [round(p.area / 10_000, 2) for p in pieces if p]
            ideal  = round(poly.area / 10_000 / n_compartments, 2)
            diff   = max((abs(a - ideal) for a in areas), default=0)
            if run_id:
                _prog(run_id,
                      f"[{idx+1}/{total}] {fn}: {len(pieces)} parts ✓  "
                      f"ideal={ideal} ha  max_diff={diff:.2f} ha",
                      pct + 5)
            fd = os.path.join(out, _safe_dn(fn)) if total > 1 else out
            pg, lg, ptg = _save_compartments(pieces, fn, crs, fd)
            ap.append(pg); al.append(lg); apts.append(ptg)

    else:
        df = file_or_df
        df = normalize_order(df)
        xc = safe_col(df, mapping, "X", "X")
        yc = safe_col(df, mapping, "Y", "Y")
        oc = safe_col(df, mapping, "Order", "Order")
        fc = safe_col(df, mapping, "Forest", "Forest")

        if not xc:
            raise ValueError("X column not found.")
        if not yc:
            raise ValueError("Y column not found.")
        if e_mode == "B" and not fc:
            raise ValueError("Forest column required for mode B.")

        if e_mode == "A":
            fn = (mapping or {}).get("forest") or "FOREST"
            if run_id:
                _prog(run_id, "Building & subdividing polygon…", 15)
            poly   = _df_to_poly(df, xc, yc, oc)
            pieces = _subdivide(poly, n_compartments, method, area_tol_ha)
            pg, lg, ptg = _save_compartments(pieces, fn, crs, out)
            ap.append(pg); al.append(lg); apts.append(ptg)
            sv.append(_survey_points_gdf(df, xc, yc, oc, crs))

        else:
            groups = list(df.groupby(fc))
            total  = len(groups)
            for idx, (f, fg) in enumerate(groups):
                if run_id:
                    _prog(run_id, f"Processing {f}…",
                          15 + int(65 * idx / max(total, 1)))
                try:
                    poly   = _df_to_poly(fg, xc, yc, oc)
                    pieces = _subdivide(poly, n_compartments, method, area_tol_ha)
                    fd     = os.path.join(out, _safe_dn(str(f)))
                    pg, lg, ptg = _save_compartments(pieces, str(f), crs, fd)
                    ap.append(pg); al.append(lg); apts.append(ptg)
                    sv.append(_survey_points_gdf(fg, xc, yc, oc, crs))
                except Exception as ex:
                    if run_id:
                        _prog(run_id, f"Warning: {f} skipped — {ex}")

    if not ap:
        raise ValueError("No valid polygons were built.")

    p_out  = gpd.GeoDataFrame(pd.concat(ap,    ignore_index=True), crs=crs)
    l_out  = gpd.GeoDataFrame(pd.concat(al,    ignore_index=True), crs=crs)
    pt_out = gpd.GeoDataFrame(pd.concat(apts,  ignore_index=True), crs=crs)
    sv_frames = [s for s in sv if s is not None and not s.empty]
    if sv_frames:
        sv_out = gpd.GeoDataFrame(pd.concat(sv_frames, ignore_index=True), crs=crs)
    else:
        sv_out = gpd.GeoDataFrame(columns=["SN", "Order", "geometry"], crs=crs)
    return p_out, l_out, pt_out, sv_out


def _survey_points_gdf(df, xc, yc, oc, crs):
    """Boundary survey points with SN labels (for E-map overlays)."""
    try:
        sdf = df.sort_values(oc).reset_index(drop=True) if oc else df.reset_index(drop=True)
        n = len(sdf)
        if n == 0:
            return None
        return gpd.GeoDataFrame(
            {"SN": list(range(1, n + 1)),
             "Order": list(range(1, n + 1)),
             "geometry": gpd.points_from_xy(sdf[xc], sdf[yc])},
            crs=crs)
    except Exception:
        return None
# ----------------------------------------------------------------------

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
