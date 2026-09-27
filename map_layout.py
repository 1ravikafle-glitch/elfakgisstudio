"""A4 cartographic map renderer for ElfakGISProStudio.

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
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = [
    "FreeSans", "Noto Sans Devanagari", "Noto Sans Devanagari UI",
    "DejaVu Sans", "Arial",
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
}

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
