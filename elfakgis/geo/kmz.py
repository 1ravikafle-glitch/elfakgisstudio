"""Elfak GIS Studio — run IDs & KMZ export (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from flask import abort

log = logging.getLogger("elfakgis")
# ----------------------------------------------------------------------
# Helper: Human-readable Run ID
# ----------------------------------------------------------------------

def _generate_run_id(base_name, max_len=40):
    """
    Create a human-readable run ID from a base name, with timestamp and random suffix.
    Returns: e.g. "Salghari_CF_20250101_143022_8x9k"
    """
    clean = re.sub(r'[^A-Za-z0-9_-]', '_', base_name)
    clean = re.sub(r'_+', '_', clean)
    max_base = max_len - 20
    if len(clean) > max_base:
        clean = clean[:max_base]
    clean = clean.rstrip('_')
    if not clean:
        clean = "forest"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(2)
    return f"{clean}_{ts}_{suffix}"

def _safe_runid(rid):
    rid = str(rid).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', rid):
        log.warning(f"Invalid run_id rejected: {rid!r}")
        abort(400, "Invalid run ID.")
    return rid
# ----------------------------------------------------------------------
# KMZ Generation (was missing)
# ----------------------------------------------------------------------

def generate_kmz(poly_gdf, line_gdf, pts_gdf, out_dir, run_id):
    import zipfile as zf

    def w84(gdf):
        if gdf is None or gdf.empty:
            return None
        try:
            return gdf.to_crs("EPSG:4326") if gdf.crs else None
        except:
            return None

    pw = w84(poly_gdf)
    lw = w84(line_gdf)
    ptw = w84(pts_gdf)
    ref = pw if pw is not None and not pw.empty else lw

    cx, cy, alt = 0, 0, 10000
    try:
        if ref is not None and not ref.empty:
            ref = ref[ref.geometry.notna() & ~ref.geometry.is_empty]
            if not ref.empty:
                u = ref.union_all() if hasattr(ref, "union_all") else ref.unary_union
                if u is not None and not u.is_empty:
                    c = u.centroid
                    if c is not None and not c.is_empty:
                        cx, cy = float(c.x), float(c.y)
                    minx, miny, maxx, maxy = u.bounds
                    span = max(maxx - minx, maxy - miny)
                    if math.isfinite(span) and span > 0:
                        alt = max(500, int(span * 111000 * 2))
    except Exception as e:
        log.warning(f"generate_kmz centroid fallback: {e}")

    st = """<Style id="poly_style"><LineStyle><color>ff00ff00</color><width>2</width></LineStyle><PolyStyle><color>4400cc00</color></PolyStyle></Style>
<Style id="line_style"><LineStyle><color>ff0000ff</color><width>2</width></LineStyle></Style>
<Style id="point_style"><IconStyle><color>ff0000ff</color><scale>0.8</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon></IconStyle></Style>"""

    def kml_pm(gdf, sid, nc=None):
        lines = []
        for i, row in gdf.iterrows():
            g = row.geometry
            if g is None or g.is_empty:
                continue
            if nc and nc in row.index and row[nc]:
                label = str(row[nc])
            elif "Comp_ID" in row.index and row["Comp_ID"]:
                label = str(row["Comp_ID"])
            elif "Forest" in row.index and row["Forest"]:
                label = str(row["Forest"])
            else:
                label = f"Feature {i+1}"

            def cs(c):
                return " ".join(f"{x},{y},0" for x, y in c)

            def pk(geom):
                o = cs(list(geom.exterior.coords))
                r = [f"<outerBoundaryIs><LinearRing><coordinates>{o}</coordinates></LinearRing></outerBoundaryIs>"]
                for interior in geom.interiors:
                    inn = cs(list(interior.coords))
                    r.append(f"<innerBoundaryIs><LinearRing><coordinates>{inn}</coordinates></LinearRing></innerBoundaryIs>")
                return f"<Polygon>{''.join(r)}</Polygon>"

            if g.geom_type == "Polygon":
                gk = pk(g)
            elif g.geom_type == "MultiPolygon":
                gk = f"<MultiGeometry>{''.join(pk(x) for x in g.geoms)}</MultiGeometry>"
            elif g.geom_type == "LineString":
                gk = f"<LineString><coordinates>{cs(list(g.coords))}</coordinates></LineString>"
            elif g.geom_type == "Point":
                gk = f"<Point><coordinates>{g.x},{g.y},0</coordinates></Point>"
            else:
                continue
            lines.append(f"<Placemark><name>{label}</name><styleUrl>#{sid}</styleUrl>{gk}</Placemark>")
        return "\n".join(lines)

    folders = []
    if pw is not None and not pw.empty:
        folders.append(f"<Folder><name>Polygons</name>{kml_pm(pw,'poly_style','Forest')}</Folder>")
    if lw is not None and not lw.empty:
        folders.append(f"<Folder><name>Lines</name>{kml_pm(lw,'line_style','Forest')}</Folder>")
    if ptw is not None and not ptw.empty:
        folders.append(f"<Folder><name>Points</name>{kml_pm(ptw,'point_style','SN')}</Folder>")

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
<name>Elfak GIS</name>
<LookAt><longitude>{cx}</longitude><latitude>{cy}</latitude><altitude>0</altitude><range>{alt}</range><tilt>0</tilt><heading>0</heading></LookAt>
{st}
{"".join(folders)}
</Document>
</kml>"""

    kmz = os.path.join(out_dir, "output.kmz")
    with zf.ZipFile(kmz, "w", zf.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml.encode("utf-8"))
    return {
        "url": f"/outputs/{run_id}/output.kmz",
        "lat": round(cy, 6),
        "lon": round(cx, 6),
        "alt": alt
    }


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
