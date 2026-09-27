"""ElfakGISProStudio — A4 layout engine (free-space + labels) (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from elfakgis.core.config import EPS, DEFAULT_PADDING

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
# LAYOUT ENGINE (FreeSpaceManager, compute_safe_rect, label engine)
# ----------------------------------------------------------------------

class FreeSpaceManager:
    """Manages free rectangles in normalized figure coordinates (0..1)."""
    def __init__(self, page=(0.0, 0.0, 1.0, 1.0)):
        self.rects = [page]

    def _merge(self):
        while True:
            merged = False
            new_rects = []
            skip = set()
            n = len(self.rects)
            for i in range(n):
                if i in skip:
                    continue
                r1 = self.rects[i]
                for j in range(i+1, n):
                    if j in skip:
                        continue
                    r2 = self.rects[j]
                    x0a, y0a, x1a, y1a = r1
                    x0b, y0b, x1b, y1b = r2

                    if abs(y0a - y0b) < EPS and abs(y1a - y1b) < EPS:
                        if abs(x0a - x1b) < EPS:
                            merged_rect = (x0b, y0a, x1a, y1a)
                            skip.add(i); skip.add(j)
                            new_rects.append(merged_rect)
                            merged = True
                            break
                        elif abs(x0b - x1a) < EPS:
                            merged_rect = (x0a, y0a, x1b, y1a)
                            skip.add(i); skip.add(j)
                            new_rects.append(merged_rect)
                            merged = True
                            break

                    if abs(x0a - x0b) < EPS and abs(x1a - x1b) < EPS:
                        if abs(y0a - y1b) < EPS:
                            merged_rect = (x0a, y0b, x1a, y1a)
                            skip.add(i); skip.add(j)
                            new_rects.append(merged_rect)
                            merged = True
                            break
                        elif abs(y0b - y1a) < EPS:
                            merged_rect = (x0a, y0a, x1a, y1b)
                            skip.add(i); skip.add(j)
                            new_rects.append(merged_rect)
                            merged = True
                            break
                    if merged:
                        break
                if merged:
                    break

            for i, r in enumerate(self.rects):
                if i not in skip:
                    new_rects.append(r)
            new_rects = [r for r in new_rects if (r[2]-r[0])*(r[3]-r[1]) > EPS]
            if not merged:
                self.rects = new_rects
                break
            else:
                self.rects = new_rects

    def subtract(self, rect):
        new_rects = []
        for (fx0, fy0, fx1, fy1) in self.rects:
            if rect[2] <= fx0 + EPS or rect[0] >= fx1 - EPS or \
               rect[3] <= fy0 + EPS or rect[1] >= fy1 - EPS:
                new_rects.append((fx0, fy0, fx1, fy1))
                continue

            if fy1 > rect[3] + EPS:
                new_rects.append((fx0, rect[3], fx1, fy1))
            if fy0 < rect[1] - EPS:
                new_rects.append((fx0, fy0, fx1, rect[1]))
            if fx0 < rect[0] - EPS:
                y0 = max(fy0, rect[1])
                y1 = min(fy1, rect[3])
                if y0 < y1 - EPS:
                    new_rects.append((fx0, y0, rect[0], y1))
            if fx1 > rect[2] + EPS:
                y0 = max(fy0, rect[1])
                y1 = min(fy1, rect[3])
                if y0 < y1 - EPS:
                    new_rects.append((rect[2], y0, fx1, y1))

        self.rects = new_rects
        self._merge()

    def get_best_rect(self, poly_aspect=None, center_weight=0.4):
        if not self.rects:
            return (0.0, 0.0, 1.0, 1.0)

        def score(r):
            x0, y0, x1, y1 = r
            w = x1 - x0
            h = y1 - y0
            area = w * h
            if area < EPS:
                return -1e9
            aspect = w / h if h > EPS else 1.0
            thinness = min(aspect, 1.0/aspect) if aspect > 0 else 0.0
            thin_penalty = 1.0 - (1.0 - thinness) * 0.5
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            dist = ((cx - 0.5)**2 + (cy - 0.5)**2)**0.5
            center_score = 1.0 - dist * 2.0
            if poly_aspect is not None and poly_aspect > 0:
                aspect_penalty = min(abs(poly_aspect - aspect) / max(poly_aspect, aspect, EPS), 1.0)
                aspect_score = 1.0 - aspect_penalty * 0.8
            else:
                aspect_score = 1.0
            return area * thin_penalty * (0.6 * aspect_score + 0.4 * max(center_score, 0))

        best = max(self.rects, key=score)
        return (best[0], best[1], best[2]-best[0], best[3]-best[1])

def get_default_layout_state():
    return {
        "ov-legend": {"left": 74, "top": 76, "width": 24, "height": 18, "visible": True, "padding": 0.025, "rotation": 0},
        "ov-north":  {"left": 86, "top": 2,  "width": 10, "height": 15, "visible": True, "padding": 0.02,  "rotation": 0},
        "ov-scale":  {"left": 2,  "top": 86, "width": 18, "height": 10, "visible": True, "padding": 0.02,  "rotation": 0},
        "ov-title":  {"left": 25, "top": 2,  "width": 50, "height": 8,  "visible": True, "padding": 0.015, "rotation": 0},
        "ov-area":   {"left": 74, "top": 12, "width": 20, "height": 6,  "visible": True, "padding": 0.015, "rotation": 0},
    }

def _rotated_bbox(rect, rotation_deg, center=None):
    x, y, w, h = rect
    if center is None:
        cx, cy = x + w/2, y + h/2
    else:
        cx, cy = center
    angle = math.radians(rotation_deg)
    corners = [(-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)]
    rot_corners = []
    for dx, dy in corners:
        rx = dx * math.cos(angle) - dy * math.sin(angle)
        ry = dx * math.sin(angle) + dy * math.cos(angle)
        rot_corners.append((cx + rx, cy + ry))
    xs = [p[0] for p in rot_corners]
    ys = [p[1] for p in rot_corners]
    return (min(xs), min(ys), max(xs), max(ys))

def compute_safe_rect(layout_state, poly_aspect=None):
    mgr = FreeSpaceManager()
    for key, item in layout_state.items():
        if not item.get('visible', True):
            continue
        left = item.get('left', 0) / 100.0
        top = item.get('top', 0) / 100.0
        width = item.get('width', 0) / 100.0
        height = item.get('height', 0) / 100.0
        bottom = 1.0 - top - height
        pad = item.get('padding', DEFAULT_PADDING)
        rot = item.get('rotation', 0)
        base = (left - pad, bottom - pad, left + width + pad, top + height + pad)
        if abs(rot) > EPS:
            cx = (base[0] + base[2]) / 2
            cy = (base[1] + base[3]) / 2
            bbox = _rotated_bbox((base[0], base[1], base[2]-base[0], base[3]-base[1]), rot, (cx, cy))
            bbox = (max(0, bbox[0]), max(0, bbox[1]), min(1, bbox[2]), min(1, bbox[3]))
            if bbox[0] < bbox[2] and bbox[1] < bbox[3]:
                mgr.subtract(bbox)
        else:
            mgr.subtract(base)
    return mgr.get_best_rect(poly_aspect)

# Label engine with coordinate-system correction
def _place_labels(ax, gdf, label_col, excluded_overlay_rects, fig,
                  fontsize=5.5, color='black', offset=8):
    if gdf is None or gdf.empty or label_col not in gdf.columns:
        return []

    trans = ax.transData.inverted()
    data_excluded = []
    for rect in excluded_overlay_rects:
        corners = [(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])]
        data_corners = [trans.transform_point(p) for p in corners]
        xs = [p[0] for p in data_corners]
        ys = [p[1] for p in data_corners]
        data_excluded.append((min(xs), min(ys), max(xs), max(ys)))

    placed = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        cx, cy = geom.centroid.x, geom.centroid.y

        best_pos = None
        best_score = None
        offsets = [(0, offset), (offset, offset), (offset, 0), (offset, -offset),
                   (0, -offset), (-offset, -offset), (-offset, 0), (-offset, offset)]
        for scale in [1.0, 0.6, 0.3]:
            for dx, dy in offsets:
                px, py = cx + dx*scale, cy + dy*scale
                label_text = str(row[label_col])
                size = 0.2 * fontsize * len(label_text) * 0.05
                rect = (px - size, py - size, px + size, py + size)

                overlap = False
                for ex in data_excluded:
                    if not (rect[2] <= ex[0] or rect[0] >= ex[2] or rect[3] <= ex[1] or rect[1] >= ex[3]):
                        overlap = True
                        break
                if overlap:
                    continue

                overlap_placed = False
                for p in placed:
                    if not (rect[2] <= p[0] or rect[0] >= p[2] or rect[3] <= p[1] or rect[1] >= p[3]):
                        overlap_placed = True
                        break
                if overlap_placed:
                    continue

                dist = ((px - cx)**2 + (py - cy)**2)**0.5
                if best_score is None or dist < best_score:
                    best_score = dist
                    best_pos = (px, py, rect)

        if best_pos is None:
            for scale in [1.0, 0.6, 0.3]:
                for dx, dy in offsets:
                    px, py = cx + dx*scale, cy + dy*scale
                    overlap = False
                    for ex in data_excluded:
                        if ex[0] <= px <= ex[2] and ex[1] <= py <= ex[3]:
                            overlap = True
                            break
                    if not overlap:
                        best_pos = (px, py, (px-0.01, py-0.01, px+0.01, py+0.01))
                        break
                if best_pos:
                    break
            if best_pos is None:
                best_pos = (cx, cy, (cx-0.01, cy-0.01, cx+0.01, cy+0.01))

        px, py, rect = best_pos
        placed.append(rect)
        ax.annotate(str(row[label_col]),
                    xy=(cx, cy),
                    xytext=(px, py),
                    textcoords="data",
                    ha='left' if px > cx else 'right',
                    va='bottom' if py > cy else 'top',
                    fontsize=fontsize,
                    fontweight='bold',
                    color=color,
                    path_effects=[pe.Stroke(linewidth=1.8, foreground='white'), pe.Normal()],
                    zorder=9)
    return placed


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
