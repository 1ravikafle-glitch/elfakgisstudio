"""Elfak GIS Studio — map plotting helpers & render_map (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from map_layout import render_a4 as _render_a4
from map_layout import legend_spec, MODULE_SUBTITLES

log = logging.getLogger("elfakgis")
from elfakgis.core.config import EPS, LABEL_FS, LABEL_COL, GRID_COL, GRID_LW, TICK_FS
from elfakgis.geo.layout import _place_labels

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
# COMMON MAP PLOTTING HELPERS
# ----------------------------------------------------------------------

_COMP_COLORS_RICH = [
    "#2196F3","#FF9800","#4CAF50","#9C27B0","#F48FB1",
    "#607D8B","#CDDC39","#00BCD4","#FF5722","#795548",
    "#E91E63","#009688","#FFC107","#3F51B5","#8BC34A"
]

def _get_bounds(gdf):
    if gdf is None or gdf.empty:
        return None
    bounds = gdf.total_bounds
    if bounds is None or any(np.isnan(b) for b in bounds):
        return None
    return bounds

def _get_combined_bounds(gdfs):
    all_bounds = []
    for gdf in gdfs:
        b = _get_bounds(gdf)
        if b is not None:
            all_bounds.append(b)
    if not all_bounds:
        return (0, 0, 1, 1)
    minx = min(b[0] for b in all_bounds)
    miny = min(b[1] for b in all_bounds)
    maxx = max(b[2] for b in all_bounds)
    maxy = max(b[3] for b in all_bounds)
    return (minx, miny, maxx, maxy)

def _fit_bounds_to_axes(ax, bounds, safe_rect, fig_w, fig_h):
    minx, miny, maxx, maxy = bounds
    data_w = maxx - minx
    data_h = maxy - miny
    if data_w < EPS and data_h < EPS:
        data_w = data_h = 1.0

    pos = ax.get_position()
    ax_w = pos.width * fig_w
    ax_h = pos.height * fig_h
    ax_aspect = ax_w / ax_h if ax_h > EPS else 1.0

    data_aspect = data_w / data_h if data_h > EPS else 1.0

    extent = max(data_w, data_h)
    margin = 0.05 * extent
    margin = min(margin, 0.15 * extent)
    margin = max(margin, 0.02 * (ax_w + ax_h) / 2)

    data_w_m = data_w + 2 * margin
    data_h_m = data_h + 2 * margin

    if data_w_m / data_h_m > ax_aspect:
        new_w = data_w_m
        new_h = new_w / ax_aspect
    else:
        new_h = data_h_m
        new_w = new_h * ax_aspect

    center_x = (minx + maxx) / 2
    center_y = (miny + maxy) / 2
    half_w = new_w / 2
    half_h = new_h / 2
    ax.set_xlim(center_x - half_w, center_x + half_w)
    ax.set_ylim(center_y - half_h, center_y + half_h)

def _plot_polygons(ax, poly_gdf, label_col, excluded_overlay_rects, fig):
    if poly_gdf is None or poly_gdf.empty:
        return
    if "Comp_ID" in poly_gdf.columns:
        comps = poly_gdf["Comp_ID"].unique()
        color_map = {comp: _COMP_COLORS_RICH[i % len(_COMP_COLORS_RICH)] for i, comp in enumerate(comps)}
        poly_gdf = poly_gdf.copy()
        poly_gdf["display_color"] = poly_gdf["Comp_ID"].map(color_map)
        poly_gdf.plot(ax=ax, column="display_color", categorical=True,
                      edgecolor="#111111", linewidth=1.6)
    else:
        poly_gdf.plot(ax=ax, facecolor="#d4edda", edgecolor="#1a3a22", linewidth=1.5)

    if label_col and label_col in poly_gdf.columns:
        _place_labels(ax, poly_gdf, label_col, excluded_overlay_rects, fig,
                      fontsize=8, color="#1565C0", offset=6)

def _plot_points(ax, pts_gdf, point_label_col, excluded_overlay_rects, fig):
    if pts_gdf is None or pts_gdf.empty:
        return
    pts_gdf.plot(ax=ax, color="#ff0000", markersize=16, zorder=8, marker="o")
    if point_label_col and point_label_col in pts_gdf.columns:
        _place_labels(ax, pts_gdf, point_label_col, excluded_overlay_rects, fig,
                      fontsize=5.5, color="black", offset=4)

def _plot_lines(ax, line_gdf):
    if line_gdf is not None and not line_gdf.empty:
        line_gdf.plot(ax=ax, color="#000000", linewidth=1.2)

def _add_north_arrow(fig, pos=(0.93, 0.95), size=0.045):
    """Baked north arrow — used only for slope/Group H maps."""
    x, y = pos
    ax_ov = fig.add_axes([0,0,1,1], frameon=False, zorder=20)
    ax_ov.set_xlim(0,1); ax_ov.set_ylim(0,1); ax_ov.axis("off")
    hw = size * 0.35
    # upper black half
    tri_up = plt.Polygon([[x, y],[x-hw, y-size*0.6],[x+hw, y-size*0.6]],
        closed=True, facecolor="black", edgecolor="black", linewidth=0.8,
        transform=ax_ov.transAxes, zorder=21)
    ax_ov.add_patch(tri_up)
    # lower white half
    tri_dn = plt.Polygon([[x, y-size*1.6],[x-hw, y-size*0.6],[x+hw, y-size*0.6]],
        closed=True, facecolor="white", edgecolor="black", linewidth=0.8,
        transform=ax_ov.transAxes, zorder=21)
    ax_ov.add_patch(tri_dn)
    ax_ov.text(x, y+size*0.35, "N", transform=ax_ov.transAxes,
        ha="center", va="bottom", fontsize=11, fontweight="bold", color="black", zorder=22)

def _add_scale_bar(fig, ax, n_segments=3, bar_h=0.010):
    """Baked scale bar — used only for slope/Group H maps. Auto-computes distance."""
    xlim = ax.get_xlim()
    map_span_m = xlim[1] - xlim[0]
    fig_w_in   = fig.get_figwidth()
    ax_pos     = ax.get_position()
    ax_w_in    = max(ax_pos.width * fig_w_in, 1.0)
    m_per_in   = map_span_m / ax_w_in
    seg_m      = ax_w_in * 0.06 * m_per_in
    mag = 10 ** math.floor(math.log10(max(seg_m, 1)))
    for mult in [1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000]:
        if mag * mult >= seg_m * 0.7:
            seg_m = mag * mult; break
    total_frac = (seg_m * n_segments) / m_per_in / fig_w_in
    x = 0.5 - total_frac / 2; y = 0.032
    ax_ov = fig.add_axes([0,0,1,1], frameon=False, zorder=20)
    ax_ov.set_xlim(0,1); ax_ov.set_ylim(0,1); ax_ov.axis("off")
    seg_frac = total_frac / n_segments
    colors = ["black","white"] * (n_segments // 2 + 1)
    for i in range(n_segments):
        ax_ov.add_patch(mpatches.FancyBboxPatch(
            (x + i*seg_frac, y), seg_frac, bar_h,
            boxstyle="square,pad=0", facecolor=colors[i], edgecolor="black",
            linewidth=0.8, transform=ax_ov.transAxes, zorder=21))
    def _fmt(m):
        if m == 0: return "0"
        return f"{int(m//1000)} km" if m >= 1000 and m%1000==0 else f"{int(m)} m"
    for i in range(n_segments+1):
        ax_ov.text(x+i*seg_frac, y+bar_h+0.004, _fmt(i*seg_m),
            transform=ax_ov.transAxes, ha="center", va="bottom", fontsize=6.5, color="black", zorder=22)

def _draw_slope_table(ax, slope_areas):
    """Draw a compact slope area table on the axes."""
    table_data = []
    for cls, info in SLOPE_CLASSES.items():
        area = slope_areas.get(info['range'], 0)
        table_data.append([info['range'], f"{area:.2f} ha"])
    total = sum(slope_areas.values())
    table_data.append(["Total", f"{total:.2f} ha"])
    table = ax.table(cellText=table_data, colLabels=["Slope", "Area"],
                     loc='lower left', bbox=[0.02, 0.02, 0.3, 0.15],
                     cellLoc='center', colWidths=[0.15, 0.10])
    table.auto_set_font_size(False)
    table.set_fontsize(7)
    for (row, col), cell in table.get_celld().items():
        if row == 0:
            cell.set_text_props(weight='bold', color='white')
            cell.set_facecolor('#1a5276')
        else:
            cell.set_facecolor('#f8f9fa')

SLOPE_CLASSES = {
    1: {"range": "< 19°",   "color": "#2e8b57", "label": "Gentle"},
    2: {"range": "19–31°",  "color": "#ffd700", "label": "Moderate"},
    3: {"range": "> 31°",   "color": "#ef4444", "label": "Steep"},
}

def _setup_utm_grid(ax, xmin, xmax, ymin, ymax):
    """UTM tick labels on all 4 sides, dashed grid lines."""
    xspan = xmax - xmin; yspan = ymax - ymin
    def _nice(span, t=6):
        raw = span / t
        mag = 10 ** math.floor(math.log10(max(raw, 1)))
        for m in [1, 2, 5, 10]:
            if mag * m >= raw * 0.7: return mag * m
        return mag * 10
    ax.xaxis.set_major_locator(mticker.MultipleLocator(_nice(xspan)))
    ax.yaxis.set_major_locator(mticker.MultipleLocator(_nice(yspan)))
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v)}"))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v)}"))
    ax.tick_params(axis='both', which='major',
                   direction='in', length=5, width=0.8, labelsize=TICK_FS, color='black')
    try:
        ax.secondary_xaxis('top').tick_params(
            axis='x', direction='in', length=5, width=0.8, color='black', labeltop=False)
        ax.secondary_yaxis('right').tick_params(
            axis='y', direction='in', length=5, width=0.8, color='black', labelright=False)
    except Exception:
        pass
    ax.grid(True, which='major', color=GRID_COL, linewidth=GRID_LW, linestyle='--', alpha=0.7, zorder=1)
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(90); lbl.set_ha('right')


def _label_points_export(ax, pts_gdf, sn_col):
    """Label survey points with SN numbers; white-stroke outline."""
    if sn_col not in pts_gdf.columns:
        for alias in ["SN","sn","No","no","ID","id","Order","order","FID"]:
            if alias in pts_gdf.columns: sn_col = alias; break
        else: return
    placed = []
    offsets = [(4,4),(4,-4),(-4,4),(-4,-4),(6,0),(-6,0),(0,6),(0,-6)]
    for _, row in pts_gdf.iterrows():
        g = row.geometry
        if g is None or g.is_empty: continue
        px, py = g.x, g.y
        best = next(
            (c for dx, dy in offsets
             for c in [(px+dx, py+dy)]
             if all(abs(c[0]-ex)>=5 or abs(c[1]-ey)>=5 for ex,ey in placed)),
            (px+4, py+4)
        )
        placed.append(best)
        ax.annotate(str(row[sn_col]), xy=(px,py), xytext=best, textcoords="data",
            fontsize=LABEL_FS, color=LABEL_COL, ha="left", va="bottom",
            path_effects=[pe.Stroke(linewidth=1.5, foreground="white"), pe.Normal()], zorder=8)


def render_map(path, poly_gdf=None, line_gdf=None, pts_gdf=None,
               label_col=None, point_label_col=None,
               safe_rect=None, layout_state=None,
               title=None, slope_mode=False, summary_rows=None,
               slope_areas=None, subtitle=None, legend_title=None,
               area_ha=None, area_text=None, legend_labels=None,
               show_point_labels=True, module=None, orientation="auto"):
    """Reference-style A4 survey map (neatline, graticule, titles, north
    star, legend, true scale bar, projection block — fixed non-overlap slots).

    All text comes from arguments so /compose and /export_layout can
    re-render user-edited text. Extra legacy params (safe_rect,
    layout_state, summary_rows) are accepted for compatibility.
    orientation: 'auto' (default) matches the sheet to the data shape —
        landscape data → A4 landscape, portrait data → A4 portrait — so
        the saved file looks exactly like the preview without folding.
    """
    mod = module or ("F" if slope_mode else None)
    return _render_a4(path, poly_gdf=poly_gdf, line_gdf=line_gdf,
                      pts_gdf=pts_gdf, label_col=label_col,
                      point_label_col=point_label_col, title=title,
                      subtitle=subtitle, module=mod,
                      legend_title=legend_title or "Legend",
                      area_ha=area_ha, area_text=area_text,
                      legend_labels=legend_labels,
                      show_point_labels=show_point_labels,
                      slope_areas=slope_areas, orientation=orientation)


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
