"""A4 cartographic map renderer for Elfak GIS Studio.

Reference-style survey map on A4 portrait:
  - outer neatline enclosing the whole sheet
  - lat/long graticule labels on all 4 sides + light interior grid
  - title block (name / map type / area in ha) — all caller-editable text
  - compass-star north arrow, legend, true-scale bar, projection block
  - fixed layout slots so nothing ever overlaps
  - per-module variants: boundary (B/W), compartments (E), slope (F/H)

All text (title, subtitle, area, legend title) comes from arguments so the
Composer tab can re-render edited text through /compose and /export_layout.
"""

import gc
import math
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
from matplotlib.lines import Line2D
import numpy as np

try:
    from pyproj import Transformer
    _HAS_PYPROJ = True
except ImportError:  # pragma: no cover
    _HAS_PYPROJ = False

FIG_W, FIG_H, DPI = 8.27, 11.69, 300

# GNU FreeSans covers Latin + Devanagari + symbols in one file, so mixed
# Nepali/English titles never hit missing-glyph tofu. Noto/DejaVu are backup.
# NOTE: DejaVu Sans ships *inside* matplotlib, so it is listed first — on
# minimal servers (Render free) the first family hits instantly instead of
# scoring every system font per text call (measured 1.6s/render wasted).
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = [
    "DejaVu Sans", "FreeSans", "Noto Sans Devanagari",
    "Noto Sans Devanagari UI", "Arial",
]
plt.rcParams["axes.unicode_minus"] = False

SLOPE_CLASSES = [
    {"range": "< 19°", "color": "#2e8b57"},
    {"range": "19–31°", "color": "#ffd23e"},
    {"range": "> 31°", "color": "#ef4444"},
]

COMP_COLORS = [
    "#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3", "#a6d854",
    "#ffd92f", "#e5c494", "#b3b3b3", "#7fcdbb", "#c994c7",
    "#fdbf6f", "#a8ddb5", "#9ebcda", "#fdae6b", "#bcbddc",
]

POINT_TYPE_COLORS = {
    "Vertex": "#e5484d",
    "Boundary": "#1d1d1f",
    "Divider": "#0a84ff",
}


def _disp_comp(v):
    """Comp_001 / C1 → Compartment-1 for map labels and legends."""
    m = re.match(r"^(?:Comp|C)[_-]?0*(\d+)$", str(v).strip(), re.I)
    return f"Compartment-{int(m.group(1))}" if m else str(v)


def _sub_ha(sub):
    try:
        if "Area_ha" in sub.columns:
            return float(sub["Area_ha"].sum())
        return float(sub.geometry.area.sum() / 10000.0)
    except Exception:
        return 0.0

MODULE_SUBTITLES = {
    "A": "Boundary Map",
    "B": "Segmented Forest Map",
    "C": "Sample Plot Map",
    "D": "Multi-Forest Map",
    "E": "Compartment Subdivision Map",
    "F": "Slope Analysis Map",
    "G": "Survey Point Map",
    "H": "Sample Point Slope Map",
    "I": "Thesis Locator Map",
}

# Thesis Map (Group I) palette — matches the reference 4-panel figure.
THESIS_COLORS = {
    "nepal": "#FFFFFF",
    "province": "#00E400",
    "district": "#FF0000",
    "study": "#D9CFF7",
}
THESIS_FIG_W, THESIS_FIG_H, THESIS_BAR_CM = 11.69, 8.27, 3.0
# Thesis typography: Times-like serif throughout (bold for headings,
# normal for body/scale text). Per-call family keeps threaded renders safe.
# NOTE: DejaVu Serif ships inside matplotlib and is listed first so minimal
# servers resolve it instantly (Times New Roman is absent on Linux and forced
# full font-list scoring on every text call).
_THESIS_FONT = ["DejaVu Serif", "Liberation Serif", "Times New Roman", "serif"]


def _thesis_simplify(gdf, tol):
    """Display-only simplification (never touches saved shapefiles).

    Locator panels draw 400k+ vertices; at panel scale anything below ~1px
    (~span/1500) is invisible, so simplify to it. Falls back to the original
    layer on any failure."""
    if gdf is None or getattr(gdf, "empty", True) or tol <= 0:
        return gdf
    try:
        out = gdf.copy()
        out.geometry = out.geometry.simplify(tol, preserve_topology=True)
        out = out[~out.geometry.is_empty]
        return out if not out.empty else gdf
    except Exception:
        return gdf

EPS = 1e-9


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def _nice_step(span, target=4):
    raw = max(span, EPS) / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if mag * m >= raw:
            return mag * m
    return mag * 10


def _fmt_lon(dd):
    neg = dd < 0
    dd = abs(dd)
    d = int(dd)
    m = int((dd - d) * 60)
    s = int(round(((dd - d) * 60 - m) * 60))
    if s == 60:
        s, m = 0, m + 1
    if m == 60:
        m, d = 0, d + 1
    return f"{d}°{m:02d}'{s:02d}\"{'W' if neg else 'E'}"


def _fmt_lat(dd):
    neg = dd < 0
    dd = abs(dd)
    d = int(dd)
    m = int((dd - d) * 60)
    s = int(round(((dd - d) * 60 - m) * 60))
    if s == 60:
        s, m = 0, m + 1
    if m == 60:
        m, d = 0, d + 1
    return f"{d}°{m:02d}'{s:02d}\"{'S' if neg else 'N'}"


def _utm_zone_and_cm(epsg):
    try:
        code = int(epsg)
    except (TypeError, ValueError):
        return None, None
    if 32601 <= code <= 32660:
        zone = code - 32600
    elif 32701 <= code <= 32760:
        zone = code - 32700
    else:
        return None, None
    return zone, zone * 6 - 183


def _crs_info(gdf):
    """Return dict with zone / central meridian / projected-ness for labels."""
    info = {"zone": None, "cm": None, "projected": False, "epsg": None}
    try:
        crs = getattr(gdf, "crs", None)
        if crs is None:
            return info
        epsg = crs.to_epsg()
        info["epsg"] = epsg
        zone, cm = _utm_zone_and_cm(epsg)
        if zone is not None:
            info.update(zone=zone, cm=cm, projected=True)
    except Exception:
        pass
    return info


def _to_latlon(xs, ys, src_crs):
    if not _HAS_PYPROJ or src_crs is None:
        return None
    try:
        tr = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
        lon, lat = tr.transform(list(xs), list(ys))
        return np.asarray(lon), np.asarray(lat)
    except Exception:
        return None


def _total_area_ha(poly_gdf, area_ha=None):
    if area_ha is not None:
        try:
            return float(area_ha)
        except (TypeError, ValueError):
            pass
    try:
        if poly_gdf is not None and not poly_gdf.empty:
            if "Area_ha" in poly_gdf.columns:
                return float(poly_gdf["Area_ha"].sum())
            return float(poly_gdf.geometry.area.sum() / 10000.0)
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------
# Layout pieces (all in fixed, non-overlapping slots)
# ----------------------------------------------------------------------

def _draw_neatline(fig):
    ax = fig.add_axes([0, 0, 1, 1], frameon=False, zorder=1)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    rect = mpatches.Rectangle((0.055, 0.035), 0.89, 0.93, fill=False,
                              edgecolor="black", linewidth=1.6, zorder=2)
    ax.add_patch(rect)
    return ax


def _draw_titles(fig, title, subtitle, area_ha, area_text=None):
    y = 0.945
    if title:
        fig.text(0.5, y, str(title), ha="center", va="top",
                 fontsize=17, fontweight="bold", color="black")
        y -= 0.028
    if subtitle:
        fig.text(0.5, y, str(subtitle), ha="center", va="top",
                 fontsize=12.5, color="black")
        y -= 0.024
    if area_text:
        fig.text(0.5, y, str(area_text), ha="center", va="top",
                 fontsize=11, color="black")
    elif area_ha is not None:
        fig.text(0.5, y, f"Area: {area_ha:.2f} ha", ha="center", va="top",
                 fontsize=11, color="black")


def _draw_north_star(ax):
    """Compass star at upper-right inside the map (axes fraction)."""
    cx, cy, R = 0.872, 0.895, 0.068
    for ang, label, long in ((90, "N", True), (0, "E", True),
                             (270, "S", True), (180, "W", True)):
        a = math.radians(ang)
        tip = (cx + R * math.cos(a), cy + R * math.sin(a))
        base1 = (cx + 0.16 * R * math.cos(a + math.pi / 2),
                 cy + 0.16 * R * math.sin(a + math.pi / 2))
        base2 = (cx + 0.16 * R * math.cos(a - math.pi / 2),
                 cy + 0.16 * R * math.sin(a - math.pi / 2))
        ax.add_patch(plt.Polygon([tip, base1, (cx, cy)], closed=True,
                                 facecolor="black", edgecolor="black",
                                 linewidth=0.6, transform=ax.transAxes, zorder=12))
        ax.add_patch(plt.Polygon([tip, base2, (cx, cy)], closed=True,
                                 facecolor="white", edgecolor="black",
                                 linewidth=0.6, transform=ax.transAxes, zorder=12))
        lx, ly = cx + 1.32 * R * math.cos(a), cy + 1.32 * R * math.sin(a)
        ax.text(lx, ly, label, transform=ax.transAxes, ha="center", va="center",
                fontsize=9, fontweight="bold", color="black", zorder=13)
    # short diagonal ticks
    for ang in (45, 135, 225, 315):
        a = math.radians(ang)
        x1, y1 = cx + 0.30 * R * math.cos(a), cy + 0.30 * R * math.sin(a)
        x2, y2 = cx + 0.55 * R * math.cos(a), cy + 0.55 * R * math.sin(a)
        ax.plot([x1, x2], [y1, y2], color="black", linewidth=0.9,
                transform=ax.transAxes, zorder=12)


def _draw_scale_bar(fig, dw_m):
    """True-scale alternating bar: width derived from the real map scale
    (printed A4 at 100% measures correctly against the graticule)."""
    AXW = 0.83
    target = max(dw_m * 0.30, EPS)
    mag = 10 ** math.floor(math.log10(target))
    total = mag
    for m in (1, 2, 5, 10):
        if mag * m <= target * 1.12:
            total = mag * m
    unit = "Kilometers" if total >= 1000 else "Meters"
    div = total / 1000.0 if total >= 1000 else total
    n = 4
    bar_w = total / max(dw_m, EPS) * AXW
    x0, y = 0.5 - bar_w / 2, 0.062
    seg = bar_w / n
    ax = fig.add_axes([0, 0, 1, 1], frameon=False, zorder=6)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    for i in range(n):
        ax.add_patch(mpatches.Rectangle(
            (x0 + i * seg, y), seg, 0.008, fill=True,
            facecolor="black" if i % 2 == 0 else "white",
            edgecolor="black", linewidth=1.0, zorder=7))
    for i in range(n + 1):
        val = div * i / n
        txt = f"{val:g}"
        if i == 0:
            txt = "0"
        ax.text(x0 + i * seg, y + 0.011, txt, ha="center", va="bottom",
                fontsize=7.5, color="black", zorder=8)
    ax.text(x0 + bar_w + 0.008, y + 0.001, unit, ha="left", va="bottom",
            fontsize=7.5, color="black", zorder=8)


def _draw_projection_block(fig, info):
    zone = info.get("zone")
    cm = info.get("cm")
    if zone is not None:
        lines = [
            f"Coordinate System: WGS 1984 UTM Zone {zone}N",
            "Projection: Transverse Mercator",
            "Datum: WGS 1984",
            "False Easting: 500,000.0000",
            "False Northing: 0.0000",
            f"Central Meridian: {cm:.4f}" if cm is not None else "Central Meridian: —",
            "Scale Factor: 0.9996",
            "Latitude Of Origin: 0.0000",
            "Units: Meter",
        ]
    else:
        epsg = info.get("epsg")
        lines = [
            f"Coordinate System: EPSG:{epsg}" if epsg else "Coordinate System: —",
            "Datum: WGS 1984",
            "Units: Meter",
        ]
    fig.text(0.905, 0.150, "\n".join(lines), ha="right", va="top",
             fontsize=7, color="black", linespacing=1.45,
             fontfamily="sans-serif")


def _draw_legend(fig, title, rows, custom=None):
    """rows: list of (matplotlib-handle, label). Bottom-left slot.
    custom: optional user labels applied in order; blanks keep auto text."""
    rows = list(rows or [])
    if custom:
        custom = [str(s).strip() for s in custom]
        rows = [(h, custom[i] if i < len(custom) and custom[i] else lbl)
                for i, (h, lbl) in enumerate(rows)]
    if len(rows) > 9:
        more = len(rows) - 8
        rows = rows[:8] + [(
            Line2D([0], [0], marker="None", color="w", linestyle="None"),
            f"+{more} more")]
    x, y_top = 0.095, 0.150
    fig.text(x, y_top, str(title or "Legend"), ha="left", va="top",
             fontsize=11, fontweight="bold", color="black")
    y = y_top - 0.022
    for handle, label in rows[:9]:
        fig.legend(handles=[handle], labels=[str(label)], loc="upper left",
                   bbox_to_anchor=(x, y), fontsize=8.5, frameon=False,
                   handletextpad=0.5, borderpad=0.1, labelspacing=0.35)
        y -= 0.0175


def _legend_handles_boundary(dot_label="Boundary Point"):
    dot = Line2D([0], [0], marker="o", color="w", markerfacecolor="black",
                 markersize=6, linestyle="None")
    line = Line2D([0], [0], color="black", linewidth=2.2)
    forest = mpatches.Patch(facecolor="white", edgecolor="black", linewidth=1.6)
    return [(dot, dot_label), (line, "Boundary"), (forest, "Forest")]


def legend_spec(poly_gdf=None, pts_gdf=None, label_col=None, module=None,
                slope_areas=None):
    """Single source of truth for legend content AND plot styling.

    Returns dict with:
      mode: 'slope' | 'g' | 'comp' | 'forest' | 'boundary'
      rows: [(kind, color, label)] kind in 'patch' | 'dot' | 'line'
      slope_col / slope_order / slope_colors (slope mode)
      key_col / colors (comp & forest modes)
      pt_types (g mode), label_fmt ('comp' when Comp_ID labels)
    The renderer and the /map_texts endpoint both use this — they can
    never disagree.
    """
    mod = str(module or "").upper()
    has_pts = pts_gdf is not None and not pts_gdf.empty
    rows = []
    spec = {"mode": "boundary", "rows": rows}

    is_slope = slope_areas is not None and len(slope_areas) > 0
    if is_slope and poly_gdf is not None and not poly_gdf.empty:
        col = None
        for cand in ("Slope_Range", "Slope", "Description", "slope_range"):
            if cand in poly_gdf.columns:
                col = cand
                break
        order = [c["range"] for c in SLOPE_CLASSES]
        colors = {c["range"]: c["color"] for c in SLOPE_CLASSES}
        for rng in order:
            if col is not None and poly_gdf[poly_gdf[col] == rng].empty:
                continue
            ha = slope_areas.get(rng, 0.0)
            rows.append(("patch", colors[rng], f"{rng} — {ha:.2f} ha"))
        spec.update(mode="slope", slope_col=col, slope_order=order,
                    slope_colors=colors)
        return spec

    if (mod == "G" and pts_gdf is not None and not pts_gdf.empty
            and "Point_Type" in pts_gdf.columns):
        for t, color in POINT_TYPE_COLORS.items():
            n = int((pts_gdf["Point_Type"] == t).sum())
            if n:
                rows.append(("dot", color, f"{t} Point — {n}"))
        rows.append(("line", "black", "Boundary"))
        rows.append(("patch", "white", "Forest"))
        spec.update(mode="g",
                    pt_types=[t for t in POINT_TYPE_COLORS
                              if (pts_gdf["Point_Type"] == t).any()])
        return spec

    if (poly_gdf is not None and not poly_gdf.empty
            and "Comp_ID" in poly_gdf.columns
            and poly_gdf["Comp_ID"].nunique() > 1):
        comps = list(poly_gdf["Comp_ID"].unique())
        colors = {c: COMP_COLORS[i % len(COMP_COLORS)]
                  for i, c in enumerate(comps)}
        for c in comps:
            sub = poly_gdf[poly_gdf["Comp_ID"] == c]
            rows.append(("patch", colors[c],
                         f"{_disp_comp(c)} ({_sub_ha(sub):.2f} ha)"))
        if has_pts:
            rows.append(("dot", "black", "Survey Point (SN)"))
        spec.update(mode="comp", key_col="Comp_ID", colors=colors,
                    label_fmt=(_disp_comp if label_col == "Comp_ID" else None))
        return spec

    if (mod in ("B", "D") and poly_gdf is not None and not poly_gdf.empty
            and "Forest" in poly_gdf.columns
            and poly_gdf["Forest"].nunique() > 1):
        forests = list(poly_gdf["Forest"].unique())
        colors = {f: COMP_COLORS[i % len(COMP_COLORS)]
                  for i, f in enumerate(forests)}
        for f in forests:
            sub = poly_gdf[poly_gdf["Forest"] == f]
            rows.append(("patch", colors[f],
                         f"{f} ({_sub_ha(sub):.2f} ha)"))
        spec.update(mode="forest", key_col="Forest", colors=colors)
        return spec

    if mod == "C":
        dot_label = "Sample Plot (SN)"
    elif has_pts:
        dot_label = "Survey Point (SN)"
    else:
        dot_label = "Boundary Point"
    rows.extend([("dot", "black", dot_label),
                 ("line", "black", "Boundary"),
                 ("patch", "white", "Forest")])
    return spec


def _spec_handles(spec):
    """Convert legend_spec rows into matplotlib handles."""
    out = []
    for kind, color, label in spec["rows"]:
        if kind == "patch":
            out.append((mpatches.Patch(facecolor=color, edgecolor="black"),
                        label))
        elif kind == "dot":
            out.append((Line2D([0], [0], marker="o", color="w",
                               markerfacecolor=color, markersize=6,
                               linestyle="None"), label))
        else:
            out.append((Line2D([0], [0], color=color, linewidth=2.2), label))
    return out


# ----------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------

def render_a4(path, poly_gdf=None, line_gdf=None, pts_gdf=None,
              label_col=None, point_label_col=None,
              title=None, subtitle=None, module=None,
              legend_title=None, area_ha=None, area_text=None,
              legend_labels=None, show_point_labels=True,
              slope_areas=None, dpi=DPI):
    """Render the reference-style A4 survey map. Returns the path."""
    gdfs = [g for g in (poly_gdf, line_gdf, pts_gdf)
            if g is not None and not g.empty]

    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=dpi)
    fig.patch.set_facecolor("white")
    _draw_neatline(fig)

    area = _total_area_ha(poly_gdf, area_ha)
    sub = subtitle or MODULE_SUBTITLES.get(str(module or "").upper(), "")
    _draw_titles(fig, title, sub, area, area_text)

    AX = [0.085, 0.175, 0.83, 0.685]
    ax = fig.add_axes(AX, zorder=3)
    ax.set_facecolor("white")

    if not gdfs:
        ax.axis("off")
        ax.text(0.5, 0.5, "No geometry to display", transform=ax.transAxes,
                ha="center", va="center", fontsize=14, color="black")
        for spine in ax.spines.values():
            spine.set_visible(True)
            spine.set_edgecolor("black")
            spine.set_linewidth(1.2)
        fig.savefig(path, dpi=dpi, facecolor="white")
        plt.close(fig)
        gc.collect()
        return path

    minx = miny = maxx = maxy = None
    for g in gdfs:
        try:
            b = g.total_bounds
        except Exception:
            continue
        if b is None or len(b) != 4 or not np.all(np.isfinite(b)):
            continue
        minx = b[0] if minx is None else min(minx, b[0])
        miny = b[1] if miny is None else min(miny, b[1])
        maxx = b[2] if maxx is None else max(maxx, b[2])
        maxy = b[3] if maxy is None else max(maxy, b[3])
    if minx is None:
        minx, miny, maxx, maxy = 0.0, 0.0, 1.0, 1.0
    data_w = max(maxx - minx, 0.0)
    data_h = max(maxy - miny, 0.0)
    if max(data_w, data_h) < 1.0:
        # degenerate (e.g. near-collinear survey): open a 20 m window
        cx0, cy0 = (minx + maxx) / 2, (miny + maxy) / 2
        minx, maxx, miny, maxy = cx0 - 10, cx0 + 10, cy0 - 10, cy0 + 10
        data_w = data_h = 20.0

    ax_aspect = AX[2] * FIG_W / (AX[3] * FIG_H)
    margin = max(data_w, data_h) * 0.10
    dw, dh = data_w + 2 * margin, data_h + 2 * margin
    if dw / dh > ax_aspect:
        dh = dw / ax_aspect
    else:
        dw = dh * ax_aspect
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    x0, x1, y0, y1 = cx - dw / 2, cx + dw / 2, cy - dh / 2, cy + dh / 2
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)

    # ── graticule: UTM ticks, lat/long labels, light interior grid ──
    step = _nice_step(x1 - x0, 5)
    xticks = np.arange(math.floor(x0 / step) * step, x1 + step * 0.5, step)
    step_y = _nice_step(y1 - y0, 6)
    yticks = np.arange(math.floor(y0 / step_y) * step_y, y1 + step_y * 0.5, step_y)
    ax.set_xticks(xticks)
    ax.set_yticks(yticks)
    src_crs = getattr(poly_gdf, "crs", None) or getattr(line_gdf, "crs", None) \
        or getattr(pts_gdf, "crs", None)
    # Small sites (<2 km): DMS seconds repeat, so label true UTM metres;
    # large sites: label lat/long like a survey sheet.
    use_metres = (x1 - x0) < 2000
    lon = lat = None
    if not use_metres:
        ll = _to_latlon(xticks, np.full_like(xticks, y0), src_crs)
        ll_y = _to_latlon(np.full_like(yticks, x0), yticks, src_crs)
        if ll is not None:
            lon = ll[0]
        if ll_y is not None:
            lat = ll_y[1]
    if lon is not None:
        xlabels = [_fmt_lon(v) for v in lon]
    else:
        xlabels = [f"{v:,.0f}" for v in xticks]
    # blank the corner ticks so bottom/top labels never collide with the
    # rotated latitude labels at the neatline corners
    if len(xlabels) > 2:
        xlabels[0] = ""
        xlabels[-1] = ""
    ax.set_xticklabels(xlabels, fontsize=7.5, color="black")
    if lat is not None:
        ylabels = [_fmt_lat(v) for v in lat]
    else:
        ylabels = [f"{v:,.0f}" for v in yticks]
    if len(ylabels) > 2:
        ylabels[0] = ""
        ylabels[-1] = ""
    ax.set_yticklabels(ylabels, fontsize=7.5, color="black")
    ax.tick_params(axis="x", which="major", direction="in", length=5, width=1.0,
                   colors="black", pad=4, labelsize=7.5,
                   bottom=True, top=False)
    ax.tick_params(axis="y", which="major", direction="in", length=5, width=1.0,
                   colors="black", pad=4, labelsize=7.5,
                   left=True, right=False)
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(0)
    for lbl in ax.get_yticklabels():
        lbl.set_rotation(90)
        lbl.set_ha("center")
        lbl.set_va("top")
    # mirror ticks on top/right (labels only outside neatline via second axes)
    # dotted light grid: dash endpoints can't be mistaken for extra ticks
    ax.grid(True, which="major", color="#B5B5B5", linewidth=0.5,
            linestyle=":", alpha=0.8, zorder=1)
    for spine in ax.spines.values():
        spine.set_edgecolor("black")
        spine.set_linewidth(1.2)
    # top + right lat/long labels (outside frame)
    top_ax = fig.add_axes(AX, frameon=False, zorder=4)
    top_ax.set_xlim(x0, x1)
    top_ax.set_ylim(y0, y1)
    top_ax.set_xticks(xticks)
    top_ax.set_yticks(yticks)
    if lon is not None:
        txlabels = [_fmt_lon(v) for v in lon]
    else:
        txlabels = [f"{v:,.0f}" for v in xticks]
    if len(txlabels) > 2:
        txlabels[0] = ""
        txlabels[-1] = ""
    top_ax.set_xticklabels(txlabels, fontsize=7.5, color="black")
    if lat is not None:
        tylabels = [_fmt_lat(v) for v in lat]
    else:
        tylabels = [f"{v:,.0f}" for v in yticks]
    if len(tylabels) > 2:
        tylabels[0] = ""
        tylabels[-1] = ""
    top_ax.set_yticklabels(tylabels, fontsize=7.5, color="black")
    top_ax.tick_params(axis="x", which="major", direction="in", length=5, width=1.0,
                       colors="black", pad=4, labelbottom=False, labeltop=True,
                       bottom=False, top=True)
    top_ax.tick_params(axis="y", which="major", direction="in", length=5, width=1.0,
                       colors="black", pad=4, labelleft=False, labelright=True,
                       left=False, right=True)
    for lbl in top_ax.get_yticklabels():
        lbl.set_rotation(90)
        lbl.set_ha("center")
        lbl.set_va("bottom")

    # ── geometries + per-module legend (single source: legend_spec) ──
    # NOTE: geopandas .plot() forces aspect='equal', adjustable='box', which
    # would shift/resize the frame. Lock equal aspect into the DATA limits so
    # the frame rect stays exactly at spec (scale bar math depends on it).
    ax.set_aspect('equal', adjustable='datalim')
    spec = legend_spec(poly_gdf, pts_gdf, label_col, module, slope_areas)
    mode = spec["mode"]
    pts_plotted = False

    if mode == "slope" and poly_gdf is not None and not poly_gdf.empty:
        col = spec["slope_col"]
        if col is not None:
            for rng in spec["slope_order"]:
                sub = poly_gdf[poly_gdf[col] == rng]
                if sub.empty:
                    continue
                sub.plot(ax=ax, facecolor=spec["slope_colors"][rng],
                         edgecolor="black", linewidth=0.9, zorder=3)
            rest = poly_gdf[~poly_gdf[col].isin(spec["slope_order"])]
            if not rest.empty:
                rest.plot(ax=ax, facecolor="#bbbbbb", edgecolor="black",
                          linewidth=0.9, zorder=3)
        else:
            poly_gdf.plot(ax=ax, facecolor="#9fd3ae", edgecolor="black",
                          linewidth=1.0, zorder=3)
    elif mode == "g":
        if poly_gdf is not None and not poly_gdf.empty:
            poly_gdf.plot(ax=ax, facecolor="none", edgecolor="black",
                          linewidth=1.8, zorder=4)
        for t in spec["pt_types"]:
            sub = pts_gdf[pts_gdf["Point_Type"] == t]
            sub.plot(ax=ax, color=POINT_TYPE_COLORS[t], markersize=16,
                     marker="o", zorder=6)
        pts_plotted = True
        idcol = None
        for cand in ("Point_ID", "SN", "sn", "Order", "ID"):
            if cand in pts_gdf.columns:
                idcol = cand
                break
        if idcol is not None and show_point_labels:
            _label_points(ax, pts_gdf, idcol,
                          fontsize=7 if len(pts_gdf) <= 80 else 5.5)
    elif mode in ("comp", "forest"):
        key_col = spec["key_col"]
        for k, color in spec["colors"].items():
            sub = poly_gdf[poly_gdf[key_col] == k]
            sub.plot(ax=ax, facecolor=color, edgecolor="black",
                     linewidth=1.2, zorder=3)
        if label_col and label_col in poly_gdf.columns:
            _label_polys(ax, poly_gdf, label_col, fmt=spec.get("label_fmt"))
    else:
        if poly_gdf is not None and not poly_gdf.empty:
            poly_gdf.plot(ax=ax, facecolor="none", edgecolor="black",
                          linewidth=1.8, zorder=4)
    legend_rows = _spec_handles(spec)

    if line_gdf is not None and not line_gdf.empty:
        line_gdf.plot(ax=ax, color="black", linewidth=1.4, zorder=4)

    if pts_gdf is not None and not pts_gdf.empty and not pts_plotted:
        pts_gdf.plot(ax=ax, color="black", markersize=14, marker="o", zorder=6)
        lbl = point_label_col
        if lbl is None:
            for cand in ("SN", "sn", "Order", "Point_ID", "ID"):
                if cand in pts_gdf.columns:
                    lbl = cand
                    break
        if lbl is not None and lbl in pts_gdf.columns and show_point_labels:
            _label_points(ax, pts_gdf, lbl,
                          fontsize=7 if len(pts_gdf) <= 80 else 5.5)

    # lock the frame exactly (undo any box adjustments from plotting libs)
    # and keep the graticule overlay axes pixel-aligned with it
    ax.set_position(AX)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    top_ax.set_xlim(x0, x1)
    top_ax.set_ylim(y0, y1)

    _draw_north_star(ax)
    _draw_legend(fig, legend_title, legend_rows, custom=legend_labels)
    _draw_scale_bar(fig, x1 - x0)
    ref = poly_gdf if poly_gdf is not None and not poly_gdf.empty else (
        line_gdf if line_gdf is not None and not line_gdf.empty else pts_gdf)
    _draw_projection_block(fig, _crs_info(ref))

    fig.savefig(path, dpi=dpi, facecolor="white")
    plt.close(fig)
    gc.collect()
    return path


def _label_points(ax, pts_gdf, col, fontsize=7):
    placed = []
    offsets = [(5, 5), (5, -5), (-5, 5), (-5, -5), (8, 0), (-8, 0)]
    for _, row in pts_gdf.iterrows():
        g = row.geometry
        if g is None or g.is_empty:
            continue
        px, py = float(g.x), float(g.y)
        best = (px + 5, py + 5)
        for dx, dy in offsets:
            cx, cy = px + dx, py + dy
            if all(abs(cx - ex) >= 6 or abs(cy - ey) >= 6 for ex, ey in placed):
                best = (cx, cy)
                break
        placed.append(best)
        ax.annotate(str(row[col]), xy=(px, py), xytext=best, textcoords="data",
                    fontsize=fontsize, color="black", ha="left", va="bottom",
                    path_effects=[pe.Stroke(linewidth=1.6, foreground="white"),
                                  pe.Normal()],
                    zorder=9)


def _label_polys(ax, poly_gdf, col, fontsize=8, fmt=None):
    for _, row in poly_gdf.iterrows():
        g = row.geometry
        if g is None or g.is_empty:
            continue
        try:
            c = g.centroid
        except Exception:
            continue
        val = row[col]
        txt = fmt(val) if fmt else str(val)
        ax.text(c.x, c.y, txt, ha="center", va="center",
                fontsize=fontsize, fontweight="bold", color="black",
                path_effects=[pe.Stroke(linewidth=2.2, foreground="white"),
                              pe.Normal()],
                zorder=8)


# ----------------------------------------------------------------------
# Thesis Map (Group I) — 4-panel A4-landscape locator figure
#
#   TL: Nepal (all districts)   TR: Province (its districts)
#   BL: District                BR: Study area + legend
# ----------------------------------------------------------------------

def _thesis_fit(ax, gdf, margin_frac=0.08):
    """Fit data limits to a fixed panel frame (keeps equal aspect)."""
    try:
        b = gdf.total_bounds
    except Exception:
        b = None
    if b is None or len(b) != 4 or not np.all(np.isfinite(b)):
        return None
    minx, miny, maxx, maxy = (float(v) for v in b)
    dw, dh = max(maxx - minx, EPS), max(maxy - miny, EPS)
    pos = ax.get_position()
    aspect = (pos.width * THESIS_FIG_W) / max(pos.height * THESIS_FIG_H, EPS)
    m = max(dw, dh) * margin_frac
    dwm, dhm = dw + 2 * m, dh + 2 * m
    if dwm / dhm > aspect:
        dhm = dwm / aspect
    else:
        dwm = dhm * aspect
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    ax.set_xlim(cx - dwm / 2, cx + dwm / 2)
    ax.set_ylim(cy - dhm / 2, cy + dhm / 2)
    ax.set_aspect("equal", adjustable="datalim")
    return (cx - dwm / 2, cx + dwm / 2, cy - dhm / 2, cy + dhm / 2)


def _thesis_clear_legend(ax, study_gdf, leg_box):
    """Guarantee the study map never touches the legend.

    leg_box is the legend's true extent in axes fraction (measured
    after drawing). If the study bbox intersects it (plus padding),
    expand the view rightward/downward so the map shifts up-left until
    clear (max 4 nudges). No-op for normal centered studies.
    """
    try:
        if study_gdf is None or study_gdf.empty or not leg_box:
            return
        b = study_gdf.total_bounds
    except Exception:
        return
    lx0, ly0, lx1, ly1 = leg_box
    pad = 0.015
    lx0, ly0, lx1, ly1 = lx0 - pad, ly0 - pad, lx1 + pad, ly1 + pad
    for _ in range(6):
        try:
            x0, x1 = ax.get_xlim()
            y0, y1 = ax.get_ylim()
            if not (math.isfinite(x0) and math.isfinite(x1) and x1 > x0):
                return
            sx0 = (b[0] - x0) / (x1 - x0)
            sy0 = (b[1] - y0) / (y1 - y0)
            sx1 = (b[2] - x0) / (x1 - x0)
            sy1 = (b[3] - y0) / (y1 - y0)
        except Exception:
            return
        if sx1 < lx0 or sx0 > lx1 or sy1 < ly0 or sy0 > ly1:
            return
        # translate (never rescale): slide the view right so the map
        # moves left, away from the legend; span (and scale) untouched
        dx = x1 - x0
        if sx0 <= 0.02:
            return  # already at the left frame: stop, legend wins
        ax.set_xlim(x0 + 0.10 * dx, x1 + 0.10 * dx)


def _thesis_m_per_cm(ax, span_m):
    """Exact ground metres per printed centimetre for a panel."""
    pos = ax.get_position()
    width_cm = max(pos.width * THESIS_FIG_W * 2.54, EPS)
    return max(span_m, EPS) / width_cm


def _thesis_north(ax, where="tr"):
    """Slim two-tone N kite (reference-thesis style).

    where='tr' (default): top-right. where='br': bottom-right — used for
    the district panel so connector arrows travel through clean space.
    """
    if where == "br":
        cx, cy, H, HW = 0.91, 0.035, 0.085, 0.014
        n_y = cy + H + 0.010
        n_va = "bottom"
    else:
        cx, cy, H, HW = 0.905, 0.78, 0.115, 0.017
        n_y = cy + H + 0.012
        n_va = "bottom"
    ax.add_patch(plt.Polygon(
        [(cx, cy + H), (cx - HW, cy), (cx, cy)], closed=True,
        facecolor="black", edgecolor="black", linewidth=0.8,
        transform=ax.transAxes, zorder=12))
    ax.add_patch(plt.Polygon(
        [(cx, cy + H), (cx + HW, cy), (cx, cy)], closed=True,
        facecolor="white", edgecolor="black", linewidth=0.8,
        transform=ax.transAxes, zorder=12))
    ax.text(cx, n_y, "N", transform=ax.transAxes,
            ha="center", va=n_va, fontsize=11, fontweight="bold",
                family=_THESIS_FONT,
            color="black", zorder=13)


def _thesis_study_poly(ax, study_gdf, lw=1.2):
    """True study-boundary overlay: the polygon's natural shape, lavender
    fill with a dark navy outline. Linewidth is in points, so the true
    outline still plots even when the site covers only a few pixels."""
    try:
        if study_gdf is None or study_gdf.empty:
            return
        study_gdf.plot(ax=ax, facecolor=THESIS_COLORS["study"],
                       edgecolor="#0d1b6e", linewidth=lw, zorder=10)
    except Exception:
        pass


def _thesis_panel_title(ax, text):
    """Small bold heading at the panel's top-left (below the frame)."""
    ax.text(0.03, 0.94, str(text), transform=ax.transAxes,
            ha="left", va="top", fontsize=10, fontweight="bold",
                family=_THESIS_FONT,
            color="black", zorder=13,
            path_effects=[pe.Stroke(linewidth=2.0, foreground="white"),
                          pe.Normal()])


def _thesis_nice(v):
    """Round up to a 1/2/5-series nice number."""
    if v <= 0 or not math.isfinite(v):
        return 1.0
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if mag * m >= v:
            return mag * m
    return mag * 10


def _thesis_scalebar(ax, x1, x0):
    """Alternating B/W bar + '1 cm = X' label, 100% exact by construction.

    e = exact ground metres per printed cm (final limits ÷ printed panel
    width). The bar represents the round total T ≈ e × 3 cm and is drawn
    exactly T/span wide, so every printed millimetre measures truthfully
    and the '1 cm = e' label is the true scale (3 significant figures).
    """
    span_m = max(x1 - x0, EPS)
    e = _thesis_m_per_cm(ax, span_m)
    total = _thesis_nice(e * THESIS_BAR_CM)
    n = 4
    unit = "Kilometers" if total >= 1000 else "Meters"
    div = total / 1000.0 if total >= 1000 else total
    # exact scale text: e is the true ground-per-cm (3 s.f.)
    if e >= 1000:
        cm_txt, cm_unit = f"{e / 1000.0:.3g}", "km"
    else:
        cm_txt, cm_unit = f"{e:.3g}", "m"
    ax.text(0.02, 0.145, f"1 cm = {cm_txt} {cm_unit}",
            transform=ax.transAxes, ha="left", va="bottom",
            fontsize=7.5, color="black", zorder=13,
            family=_THESIS_FONT)
    y, x_start, frac = 0.055, 0.02, (total / span_m)
    seg = frac / n
    for i in range(n):
        ax.add_patch(mpatches.Rectangle(
            (x_start + i * seg, y), seg, 0.028, transform=ax.transAxes,
            fill=True, facecolor="black" if i % 2 == 0 else "white",
            edgecolor="black", linewidth=0.9, zorder=12))
    for i in range(n + 1):
        v = div * i / n
        txt = "0" if i == 0 else f"{v:.4g}"
        ax.text(x_start + i * seg, y + 0.032, txt, transform=ax.transAxes,
                ha="center", va="bottom", fontsize=6.5, color="black",
                    family=_THESIS_FONT,
                zorder=13)
    ax.text(x_start + frac + 0.01, y + 0.002, unit, transform=ax.transAxes,
            ha="left", va="bottom", fontsize=6.5, color="black", zorder=13,
            family=_THESIS_FONT)


def render_thesis_map(path, nepal_districts_gdf, province_gdf,
                      prov_districts_gdf, district_gdf, study_gdf,
                      province_name="Province", district_name="District",
                      cf_name="Study Area", title=None, dpi=DPI,
                      legend_labels=None, legend_title="Legend",
                      panel_titles=None):
    """Render the 4-panel thesis locator map (A4 landscape). Returns path.

    legend_labels: optional Composer overrides in legend order
    (study, district, province, Nepal); blanks keep auto text.
    panel_titles: optional 4 per-panel headings
    (Nepal, province, district, study); blanks keep auto text.
    """
    C = THESIS_COLORS
    # Display-only simplification: sub-pixel vertices are invisible but cost
    # minutes on weak CPUs (412k verts in the Nepal layer). Tolerance scales
    # with the Nepal extent so small-district maps keep full detail.
    try:
        _span = max(
            float(nepal_districts_gdf.total_bounds[2]
                  - nepal_districts_gdf.total_bounds[0]),
            float(nepal_districts_gdf.total_bounds[3]
                  - nepal_districts_gdf.total_bounds[1]))
        _tol = _span / 1500.0
        nepal_districts_gdf = _thesis_simplify(nepal_districts_gdf, _tol)
        province_gdf = _thesis_simplify(province_gdf, _tol)
        prov_districts_gdf = _thesis_simplify(prov_districts_gdf, _tol)
        district_gdf = _thesis_simplify(district_gdf, _tol)
    except Exception:
        pass
    fig = plt.figure(figsize=(THESIS_FIG_W, THESIS_FIG_H), dpi=dpi)
    fig.patch.set_facecolor("white")

    # outer neatline
    _ov = fig.add_axes([0, 0, 1, 1], frameon=False, zorder=1)
    _ov.set_xlim(0, 1)
    _ov.set_ylim(0, 1)
    _ov.axis("off")
    _ov.add_patch(mpatches.Rectangle(
        (0.03, 0.03), 0.94, 0.94, fill=False, edgecolor="black",
        linewidth=1.4, zorder=2))
    if title:
        fig.text(0.5, 0.945, str(title), ha="center", va="top",
                 fontsize=14, fontweight="bold", color="black",
                 family=_THESIS_FONT)

    import matplotlib.gridspec as _gs
    grid = _gs.GridSpec(2, 2, left=0.055, right=0.945, bottom=0.075,
                        top=0.895 if title else 0.93,
                        wspace=0.07, hspace=0.14)
    ax1 = fig.add_subplot(grid[0, 0])  # Nepal
    ax2 = fig.add_subplot(grid[0, 1])  # Province
    ax3 = fig.add_subplot(grid[1, 0])  # District
    ax4 = fig.add_subplot(grid[1, 1])  # Study area
    panels = (ax1, ax2, ax3, ax4)
    for ax in panels:
        ax.set_facecolor("white")
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_edgecolor("black")
            spine.set_linewidth(1.2)

    # ── TL: Nepal ──
    if nepal_districts_gdf is not None and not nepal_districts_gdf.empty:
        nepal_districts_gdf.plot(ax=ax1, facecolor=C["nepal"],
                                 edgecolor="black", linewidth=0.3, zorder=3)
    if province_gdf is not None and not province_gdf.empty:
        province_gdf.plot(ax=ax1, facecolor=C["province"],
                          edgecolor="black", linewidth=0.5, zorder=4)
    if district_gdf is not None and not district_gdf.empty:
        district_gdf.plot(ax=ax1, facecolor=C["district"],
                          edgecolor="black", linewidth=0.5, zorder=5)
    _thesis_study_poly(ax1, study_gdf, lw=1.2)
    _thesis_fit(ax1, nepal_districts_gdf)

    # ── TR: Province ──
    if prov_districts_gdf is not None and not prov_districts_gdf.empty:
        prov_districts_gdf.plot(ax=ax2, facecolor=C["province"],
                                edgecolor="black", linewidth=0.5, zorder=3)
    if district_gdf is not None and not district_gdf.empty:
        district_gdf.plot(ax=ax2, facecolor=C["district"],
                          edgecolor="black", linewidth=0.7, zorder=4)
    _thesis_study_poly(ax2, study_gdf, lw=1.4)
    _thesis_fit(ax2, prov_districts_gdf
                  if prov_districts_gdf is not None
                  and not prov_districts_gdf.empty else province_gdf)

    # ── BL: District ──
    if district_gdf is not None and not district_gdf.empty:
        district_gdf.plot(ax=ax3, facecolor=C["district"],
                          edgecolor="black", linewidth=1.0, zorder=3)
    _thesis_study_poly(ax3, study_gdf, lw=1.2)
    _thesis_fit(ax3, district_gdf)

    # ── BR: Study area ──
    if district_gdf is not None and not district_gdf.empty:
        try:
            district_gdf.plot(ax=ax4, facecolor="white",
                              edgecolor="black", linewidth=1.0, zorder=2)
        except Exception:
            pass
    if study_gdf is not None and not study_gdf.empty:
        study_gdf.plot(ax=ax4, facecolor=C["study"],
                       edgecolor="black", linewidth=1.2, zorder=3)
    _thesis_fit(ax4, study_gdf
                  if study_gdf is not None and not study_gdf.empty
                  else district_gdf, margin_frac=0.05)

    # legend inside BR panel (Composer-editable via legend_labels).
    # Drawn before the measurement draw so its true extent can push
    # the map clear (legend itself is axes-anchored: unaffected).
    auto = [str(cf_name), f"{district_name} District",
            f"{province_name} Province", "Nepal"]
    if legend_labels:
        custom = [str(s).strip() for s in legend_labels]
        labels = [custom[i] if i < len(custom) and custom[i] else auto[i]
                  for i in range(4)]
    else:
        labels = auto
    fills = [C["study"], C["district"], C["province"], C["nepal"]]
    leg = ax4.legend(
        handles=[mpatches.Patch(facecolor=f, edgecolor="black")
                 for f in fills],
        labels=labels,
               loc="lower right", bbox_to_anchor=(0.985, 0.03),
               fontsize=8.5, title=str(legend_title or "Legend"),
        title_fontsize=10,
        prop={"family": _THESIS_FONT},
        frameon=True, facecolor="white", edgecolor="black",
        handletextpad=0.5, borderpad=0.6, labelspacing=0.5)
    leg.get_title().set_family(_THESIS_FONT)
    leg.get_title().set_fontweight("bold")

    # per-panel headings (Composer-editable; blanks keep auto text)
    dd = str(district_name).replace("_", " ").title()
    auto_panels = ["Map of Nepal", f"Map of {province_name} Province",
                   f"Map of {dd} District", "Map of Study Area"]
    if panel_titles:
        custom = [str(s).strip() for s in panel_titles]
        panels_txt = [custom[i] if i < len(custom) and custom[i] else auto_panels[i]
                      for i in range(4)]
    else:
        panels_txt = auto_panels
    for ax, txt in zip(panels, panels_txt):
        if txt:
            _thesis_panel_title(ax, txt)

    # Equal-aspect with adjustable datalim lets matplotlib expand the data
    # limits; read them back AFTER a draw so every scale bar is exact.
    try:
        fig.canvas.draw()
    except Exception:
        pass
    # true legend extent (axes fraction) → shift the map clear if needed
    try:
        bb = leg.get_window_extent(
            renderer=fig.canvas.get_renderer()).transformed(
                ax4.transAxes.inverted())
        _thesis_clear_legend(ax4, study_gdf,
                             (bb.x0, bb.y0, bb.x1, bb.y1))
        fig.canvas.draw()
    except Exception:
        pass
    for ax, corner in zip(panels, ("tr", "tr", "br", "tr")):
        _thesis_north(ax, where=corner)
        try:
            x0, x1 = ax.get_xlim()
            if math.isfinite(x0) and math.isfinite(x1) and x1 > x0:
                _thesis_scalebar(ax, x1, x0)
        except Exception:
            pass

    # connectors: true study boundary (district panel) → study map.
    # Each tail is the nearest point of the real boundary to the
    # study panel's frame corner, so arrows always start exactly on
    # the boundary. Each head stops just off the study polygon inside
    # the study panel (minimum gap, never touching the map).
    try:
        if (study_gdf is not None and not study_gdf.empty
                and district_gdf is not None and not district_gdf.empty):
            from shapely.geometry import Point as _Pt
            from shapely.ops import nearest_points as _nearest
            fig.canvas.draw()
            inv = fig.transFigure.inverted()
            pos4 = ax4.get_position()
            boundary = study_gdf.geometry.union_all().boundary
            uni = study_gdf.geometry.union_all()
            bb = uni.bounds
            # gap scales with study size, floored so degenerate/tiny
            # studies still keep a visible minimum gap
            ax4x0, ax4x1 = ax4.get_xlim()
            gap = max(0.035 * max(bb[2] - bb[0], bb[3] - bb[1]),
                      0.004 * max(ax4x1 - ax4x0, EPS))
            pairs = []
            for corner in ((pos4.x0 + 0.004, pos4.y1 - 0.004),
                           (pos4.x0 + 0.004, pos4.y0 + 0.004)):
                disp = fig.transFigure.transform(corner)
                dx, dy = ax3.transData.inverted().transform(disp)
                tail = _nearest(boundary, _Pt(dx, dy))[0]
                # head: nearest study-polygon point to this corner,
                # backed off toward the corner by the minimum gap
                cdx, cdy = ax4.transData.inverted().transform(disp)
                near = _nearest(boundary, _Pt(cdx, cdy))[0]
                vx, vy = cdx - near.x, cdy - near.y
                vl = math.hypot(vx, vy) or 1.0
                hx, hy = (near.x + vx / vl * gap,
                          near.y + vy / vl * gap)
                hp = ax4.transData.transform((hx, hy))
                hfx, hfy = inv.transform(hp)
                p = ax3.transData.transform((tail.x, tail.y))
                fx, fy = inv.transform(p)
                pairs.append(((fx, fy), (hfx, hfy)))
            for (fx, fy), corner in pairs:
                fig.add_artist(plt.Line2D(
                    [fx, corner[0]], [fy, corner[1]], color="#333333",
                    linewidth=1.0, linestyle="-", zorder=11, alpha=0.75,
                    transform=fig.transFigure))
                # arrowhead hovering off the study map (minimum gap)
                ang = math.atan2(corner[1] - fy, corner[0] - fx)
                L, Wd = 0.012, 0.006
                bx, by = (corner[0] - L * math.cos(ang),
                          corner[1] - L * math.sin(ang))
                fig.add_artist(plt.Polygon(
                    [corner,
                     (bx - Wd * math.sin(ang), by + Wd * math.cos(ang)),
                     (bx + Wd * math.sin(ang), by - Wd * math.cos(ang))],
                    closed=True, facecolor="#333333", edgecolor="#333333",
                    linewidth=0.5, transform=fig.transFigure, zorder=12,
                    alpha=0.75))
    except Exception:
        pass

    fig.savefig(path, dpi=dpi, facecolor="white")
    plt.close(fig)
    gc.collect()
    return path
