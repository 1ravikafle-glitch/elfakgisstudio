"""ElfakGISProStudio — Group I Thesis Locator Map.

4-panel A4-landscape figure from one bundled base file + one study upload:

  TL: Nepal (all 77 districts)   TR: Province (its districts)
  BL: District                   BR: Study area + legend

Base: data/nepal/local_unit.shp (777 local units, geographic Everest datum;
STATE_CODE 1-7 + DISTRICT columns). All rendered layers are reprojected to
the run's UTM zone (the app's 44N/45N/… setting), so every panel is metric
and scale bars are exact.

Province/district are chosen manually from dropdowns (GET /thesis_options).
Study area per run: SHP-ZIP polygon OR CSV/Excel boundary (same columns as
A–E, interpreted in the selected UTM zone).
"""

import os
import tempfile
import zipfile
from functools import lru_cache

import geopandas as gpd
from shapely.ops import unary_union

from elfakgis.core.config import NEPAL_BASE_SHP, PROVINCE_NAMES
from elfakgis.core.store import _prog
from elfakgis.geo.geom import get_crs, read_input, safe_polygon
from elfakgis.geo.kmz import generate_kmz

import logging
log = logging.getLogger("elfakgis")

_CODE_BY_NAME = {v.lower(): k for k, v in PROVINCE_NAMES.items()}


def _norm_name(s):
    return str(s or "").strip()


def _match_district(name, candidates):
    """Case-insensitive district match; returns the canonical file spelling."""
    want = _norm_name(name).upper()
    for c in candidates:
        if str(c).upper() == want:
            return c
    raise ValueError(
        f"District '{name}' not found in base file. "
        f"Pick one from the Thesis Map dropdown.")


def _match_province(sel):
    """Accept '3', 'Bagmati', 'Province-3' → (code, official name)."""
    s = _norm_name(sel)
    if s.isdigit() and int(s) in PROVINCE_NAMES:
        code = int(s)
        return code, PROVINCE_NAMES[code]
    low = s.lower().replace("province", "").strip(" -_")
    if low.isdigit() and int(low) in PROVINCE_NAMES:
        return int(low), PROVINCE_NAMES[int(low)]
    for alias, code in (
            ("sudur pashchim", 7), ("sudurpaschim", 7), ("sudurpashchim", 7)):
        if low == alias:
            return code, PROVINCE_NAMES[code]
    if low in _CODE_BY_NAME:
        code = _CODE_BY_NAME[low]
        return code, PROVINCE_NAMES[code]
    raise ValueError(
        f"Province '{sel}' not recognised. Use 1–7 or an official name "
        f"({', '.join(PROVINCE_NAMES.values())}).")


@lru_cache(maxsize=1)
def _load_base():
    """Bundled local-unit base (cached in worker memory after first use)."""
    if not os.path.exists(NEPAL_BASE_SHP):
        raise ValueError(
            "Nepal base file missing (data/nepal/local_unit.shp). "
            "Re-install the base bundle.")
    w = gpd.read_file(NEPAL_BASE_SHP)
    if w.empty:
        raise ValueError("Nepal base file is empty.")
    for col in ("STATE_CODE", "DISTRICT"):
        if col not in w.columns:
            raise ValueError(
                f"Nepal base file lacks '{col}' column — cannot build "
                f"province/district layers.")
    return w


def thesis_panel_titles(prov_name, dist_name):
    """Auto per-panel headings (Composer shows these as edit placeholders)."""
    dd = str(dist_name or "District").replace("_", " ").title()
    return ["Map of Nepal", f"Map of {prov_name} Province",
            f"Map of {dd} District", "Map of Study Area"]


def thesis_options():
    """Dropdown data: provinces + district list per province."""
    w = _load_base()
    provinces = [{"code": c, "name": n} for c, n in sorted(PROVINCE_NAMES.items())]
    by_prov = {}
    for code, name in sorted(PROVINCE_NAMES.items()):
        sub = w[w["STATE_CODE"] == code]
        by_prov[name] = sorted(sub["DISTRICT"].unique().tolist())
    return {"provinces": provinces, "districts": by_prov}


def thesis_layers(province_sel, district_sel, study_gdf):
    """Dissolve + reproject base layers into the study's CRS.

    Returns (nepal_districts, province_poly, prov_districts, district_poly,
    study, prov_name, dist_name) — all in study_gdf.crs (the run UTM zone).
    """
    code, prov_name = _match_province(province_sel)
    w = _load_base()
    dist_name = _match_district(district_sel, w["DISTRICT"].unique().tolist())
    target = study_gdf.crs

    prov_units = w[w["STATE_CODE"] == code]
    if prov_units.empty:
        raise ValueError(f"No units found for province '{prov_name}'.")
    dist_units = w[w["DISTRICT"] == dist_name]
    if dist_units.empty:
        raise ValueError(f"No units found for district '{dist_name}'.")
    actual_code = int(dist_units["STATE_CODE"].iloc[0])
    if actual_code != code:
        actual = PROVINCE_NAMES.get(actual_code, str(actual_code))
        raise ValueError(
            f"District '{dist_name}' is in {actual} Province, "
            f"not {prov_name}. Fix the dropdown selection.")

    nepal_districts = w.dissolve(by="DISTRICT", as_index=False)[
        ["DISTRICT", "geometry"]].to_crs(target)
    province_poly = gpd.GeoDataFrame(
        geometry=[unary_union(prov_units.geometry)],
        crs=w.crs).to_crs(target)
    prov_districts = prov_units.dissolve(by="DISTRICT", as_index=False)[
        ["DISTRICT", "geometry"]].to_crs(target)
    district_poly = gpd.GeoDataFrame(
        geometry=[unary_union(dist_units.geometry)],
        crs=w.crs).to_crs(target)
    return (nepal_districts, province_poly, prov_districts, district_poly,
            prov_name, dist_name)


def _study_from_zip(file_storage, target_crs):
    tmp = tempfile.mkdtemp(prefix="thesis_study_")
    zp = os.path.join(tmp, "study.zip")
    file_storage.save(zp)
    with zipfile.ZipFile(zp) as z:
        z.extractall(tmp)
    shps = [os.path.join(r, f) for r, _, fs in os.walk(tmp)
            for f in fs if f.lower().endswith(".shp")]
    if not shps:
        raise ValueError("No .shp found in ZIP. Include .shp + .dbf + .shx + .prj.")
    gdf = gpd.read_file(shps[0])
    if gdf.empty:
        raise ValueError("Study shapefile is empty.")
    gdf = gdf.set_crs(target_crs) if gdf.crs is None else gdf.to_crs(target_crs)
    geoms = []
    for g in gdf.geometry:
        if g is None or g.is_empty:
            continue
        if g.geom_type == "Polygon":
            geoms.append(g)
        elif g.geom_type == "MultiPolygon":
            geoms.extend([p for p in g.geoms if not p.is_empty])
    if not geoms:
        raise ValueError("No polygon geometry in study shapefile.")
    return gpd.GeoDataFrame(geometry=[safe_polygon(
        list(max(geoms, key=lambda p: p.area).exterior.coords))],
        crs=target_crs)


def _study_from_table(df, target_crs, mapping=None):
    """Same X/Y/Order auto-detection as modules A–E (coords are UTM metres)."""
    from elfakgis.geo.geom import safe_col
    mapping = mapping or {}
    xc = safe_col(df, mapping, "X", "X")
    yc = safe_col(df, mapping, "Y", "Y")
    oc = safe_col(df, mapping, "Order", "Order")
    if not xc or not yc:
        raise ValueError("Could not detect X/Y columns. Map them in the form.")
    if oc:
        df = df.sort_values(oc)
    coords = list(zip(df[xc].astype(float), df[yc].astype(float)))
    if len(coords) < 3:
        raise ValueError("Need at least 3 boundary points.")
    return gpd.GeoDataFrame(geometry=[safe_polygon(coords)], crs=target_crs)


def render_thesis_run(out_png, province_sel, district_sel, study_gdf,
                      cf_name, title=None, legend_labels=None,
                      legend_title="Legend", panel_titles=None, run_id=None):
    """Shared render entry for /run_thesis, /compose and /export_layout."""
    from map_layout import render_thesis_map
    (nepal_d, prov, prov_d, dist,
     prov_name, dist_name) = thesis_layers(province_sel, district_sel,
                                           study_gdf)
    render_thesis_map(
        out_png,
        nepal_districts_gdf=nepal_d,
        province_gdf=prov,
        prov_districts_gdf=prov_d,
        district_gdf=dist,
        study_gdf=study_gdf,
        province_name=prov_name,
        district_name=dist_name,
        cf_name=cf_name or "Study Area",
        title=title if title else f"Location Map of {cf_name}",
        legend_labels=legend_labels,
        legend_title=legend_title or "Legend",
        panel_titles=panel_titles,
    )
    return prov_name, dist_name


def group_thesis(file_storage, province_sel, district_sel, crs, out_dir,
                 mapping=None, cf_name="Study Area", run_id=None):
    """Build the 4 panel layers (all in the run UTM zone) + render."""
    _prog(run_id, "Reading study area…", 15)
    fname = (file_storage.filename or "").lower()
    utm = crs or get_crs("44")
    if fname.endswith(".zip"):
        study = _study_from_zip(file_storage, utm)
    elif fname.endswith((".csv", ".xls", ".xlsx")):
        df = read_input(file_storage)
        study = _study_from_table(df, utm, mapping)
    else:
        raise ValueError("Study file must be .zip (SHP) or .csv/.xls/.xlsx.")
    if study.empty:
        raise ValueError("Study area is empty after reading.")

    _prog(run_id, "Dissolving province / district…", 45)
    (nepal_d, prov, prov_d, dist,
     prov_name, dist_name) = thesis_layers(province_sel, district_sel, study)
    try:
        area_ha = float(study.geometry.area.sum() / 10000.0)
    except Exception:
        area_ha = None

    _prog(run_id, "Saving study boundary…", 60)
    study_out = study.copy()
    study_out["CF_Name"] = cf_name
    if area_ha is not None:
        study_out["Area_ha"] = area_ha
    try:
        study_out.to_file(os.path.join(out_dir, "thesis_study.shp"))
    except Exception as e:
        log.warning("thesis study shp save failed: %s", e)

    _prog(run_id, "Rendering thesis map…", 85)
    from map_layout import render_thesis_map
    render_thesis_map(
        os.path.join(out_dir, "output.png"),
        nepal_districts_gdf=nepal_d,
        province_gdf=prov,
        prov_districts_gdf=prov_d,
        district_gdf=dist,
        study_gdf=study,
        province_name=prov_name,
        district_name=dist_name,
        cf_name=cf_name or "Study Area",
        title=f"Location Map of {cf_name}" if cf_name else "Location Map",
    )
    kmz_url = None
    try:
        kmz_url = generate_kmz(study, gpd.GeoDataFrame(),
                               gpd.GeoDataFrame(), out_dir, run_id)
    except Exception as e:
        log.warning("thesis kmz failed: %s", e)

    _prog(run_id, "Complete.", 100)
    return {
        "province": prov_name,
        "district": dist_name,
        "cf_name": cf_name,
        "area_ha": area_ha,
        "kmz_url": kmz_url,
    }
