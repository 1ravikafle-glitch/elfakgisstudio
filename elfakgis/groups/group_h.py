"""ElfakGISProStudio — Group H sample-point GIS maps (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from elfakgis.core.config import DPI, OUTPUT
from elfakgis.core.store import _prog
from elfakgis.geo.render import SLOPE_CLASSES, _add_north_arrow, _add_scale_bar

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

try:
    import rasterio
    from rasterio.mask import mask as rio_mask
    from rasterio.features import shapes as rio_shapes
    from rasterio.plot import show
    import scipy.ndimage as ndi
    _HAS_RASTERIO = True
except ImportError:
    _HAS_RASTERIO = False
# GROUP H – SAMPLE POINT BASED GIS MAPS
# ----------------------------------------------------------------------

def _validate_zip_components(zip_path, required=[".shp", ".dbf", ".shx", ".prj"]):
    with zipfile.ZipFile(zip_path, 'r') as z:
        names = z.namelist()
        missing = [ext for ext in required if not any(n.endswith(ext) for n in names)]
        if missing:
            raise ValueError(f"ZIP missing required components: {', '.join(missing)}")
    return True

def _extract_shp(zip_path, target_crs=None):
    _validate_zip_components(zip_path)
    tmp = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(tmp)
        shp_files = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith('.shp')]
        if not shp_files:
            raise ValueError("No .shp file found in ZIP.")
        gdf = gpd.read_file(shp_files[0])
        if gdf.crs is None:
            raise ValueError("Shapefile has no CRS. Please assign one.")
        if target_crs is not None and str(gdf.crs) != target_crs:
            gdf = gdf.to_crs(target_crs)
        gdf.geometry = gdf.geometry.buffer(0)
        gdf = gdf[~gdf.geometry.is_empty]
        return gdf
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _read_points(file_path, crs):
    if file_path.endswith('.zip'):
        gdf = _extract_shp(file_path, target_crs=crs)
        if not all(gdf.geom_type.isin(['Point', 'MultiPoint'])):
            raise ValueError("Shapefile must contain Point geometries.")
        return gdf
    else:
        df = pd.read_excel(file_path)
        if 'Latitude' in df.columns and 'Longitude' in df.columns:
            gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.Longitude, df.Latitude), crs="EPSG:4326")
            gdf = gdf.to_crs(crs)
        elif 'Easting' in df.columns and 'Northing' in df.columns:
            gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.Easting, df.Northing), crs=crs)
        else:
            raise ValueError("Excel must contain 'Latitude/Longitude' or 'Easting/Northing' columns.")
        if 'Point_ID' not in df.columns:
            raise ValueError("Excel must contain 'Point_ID' column.")
        if df['Point_ID'].duplicated().any():
            raise ValueError("Duplicate Point_ID values found.")
        if gdf.geometry.is_empty.any():
            raise ValueError("Some points have empty geometry (NaN coordinates).")
        return gdf

def _clip_to_boundary(gdf, boundary_gdf):
    if gdf.geom_type.iloc[0] in ['Point', 'MultiPoint']:
        boundary_union = boundary_gdf.unary_union
        gdf = gdf[gdf.geometry.within(boundary_union)]
    else:
        gdf = gpd.overlay(gdf, boundary_gdf, how='intersection')
    return gdf

def _compute_slope_raster(dem_path, boundary_gdf, out_dir):
    import scipy.ndimage as ndi
    with rasterio.open(dem_path) as src:
        out_image, out_transform = rio_mask(src, boundary_gdf.geometry, crop=True)
        dem = out_image[0].astype(np.float32)
        nodata = src.nodata or -9999
        dem[dem == nodata] = np.nan
        rx = abs(out_transform[0])
        ry = abs(out_transform[4])
        valid = ~np.isnan(dem)
        if not valid.any():
            raise ValueError("No valid DEM data inside boundary.")
        if (~valid).any():
            ind = ndi.distance_transform_edt(~valid, return_distances=False, return_indices=True)
            filled = dem[tuple(ind)]
        else:
            filled = dem
        dzdx = ndi.sobel(filled, axis=1) / (8.0 * rx)
        dzdy = ndi.sobel(filled, axis=0) / (8.0 * ry)
        slope = np.degrees(np.arctan(np.sqrt(dzdx**2 + dzdy**2)))
        classes = np.zeros_like(slope, dtype=np.uint8)
        classes[(slope < 19) & ~np.isnan(slope)] = 1
        classes[(slope >= 19) & (slope <= 31) & ~np.isnan(slope)] = 2
        classes[(slope > 31) & ~np.isnan(slope)] = 3
        out_meta = src.meta.copy()
        out_meta.update({
            "height": classes.shape[0],
            "width": classes.shape[1],
            "transform": out_transform,
            "dtype": "uint8",
            "nodata": 0
        })
        out_path = os.path.join(out_dir, "slope_classified.tif")
        with rasterio.open(out_path, "w", **out_meta) as dst:
            dst.write(classes, 1)
    return out_path

def _raster_to_polygons(raster_path, value_map=None):
    from rasterio.features import shapes
    from shapely.geometry import shape
    with rasterio.open(raster_path) as src:
        image = src.read(1)
        transform = src.transform
        results = []
        for geom, val in shapes(image, transform=transform, connectivity=8):
            if val == 0:
                continue
            results.append({
                "class": int(val),
                "geometry": shape(geom)
            })
    gdf = gpd.GeoDataFrame(results, crs=src.crs)
    gdf = gdf[gdf.geometry.area > 1e-6]
    return gdf

def _draw_slope_table_compact(ax, slope_areas):
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

def process_group_h(boundary_zip, compartments_zip, dem_file, satellite_file,
                    sample_points_file, survey_points_file=None,
                    crs="EPSG:32644", out_dir=None, run_id=None):
    if out_dir is None:
        out_dir = os.path.join(OUTPUT, run_id or str(uuid.uuid4()))
    os.makedirs(out_dir, exist_ok=True)

    _prog(run_id, "Loading boundary...", 10)
    boundary_gdf = _extract_shp(boundary_zip, target_crs=crs)
    if len(boundary_gdf) != 1:
        raise ValueError("Boundary must contain exactly one polygon.")
    if boundary_gdf.geom_type.iloc[0] not in ['Polygon', 'MultiPolygon']:
        raise ValueError("Boundary must be a polygon.")
    boundary_gdf.geometry = boundary_gdf.geometry.buffer(0)
    boundary_gdf = boundary_gdf[~boundary_gdf.geometry.is_empty]

    _prog(run_id, "Loading compartments...", 15)
    compartments_gdf = _extract_shp(compartments_zip, target_crs=crs)
    compartments_gdf = _clip_to_boundary(compartments_gdf, boundary_gdf)
    if compartments_gdf.empty:
        raise ValueError("No compartments inside boundary after clipping.")
    if 'Comp_ID' not in compartments_gdf.columns:
        compartments_gdf['Comp_ID'] = [f"C{i+1:03d}" for i in range(len(compartments_gdf))]

    _prog(run_id, "Processing DEM...", 25)
    with rasterio.open(dem_file) as src:
        dem_bounds = box(*src.bounds)
    boundary_bounds = boundary_gdf.unary_union.bounds
    if not dem_bounds.intersects(box(*boundary_bounds)):
        raise ValueError("DEM does not overlap the boundary.")
    slope_raster = _compute_slope_raster(dem_file, boundary_gdf, out_dir)
    slope_poly = _raster_to_polygons(slope_raster)
    slope_poly = _clip_to_boundary(slope_poly, boundary_gdf)
    slope_areas = {}
    for cls, info in SLOPE_CLASSES.items():
        sub = slope_poly[slope_poly['class'] == cls]
        slope_areas[info['range']] = sub.geometry.area.sum() / 10000

    _prog(run_id, "Processing satellite image...", 35)
    sat_clip_path = os.path.join(out_dir, "satellite_clipped.tif")
    with rasterio.open(satellite_file) as src:
        out_image, out_transform = rio_mask(src, boundary_gdf.geometry, crop=True)
        out_meta = src.meta.copy()
        out_meta.update({
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform
        })
        with rasterio.open(sat_clip_path, "w", **out_meta) as dst:
            dst.write(out_image)
    del out_image
    gc.collect()

    _prog(run_id, "Loading sample points...", 45)
    sample_gdf = _read_points(sample_points_file, crs)
    sample_gdf = _clip_to_boundary(sample_gdf, boundary_gdf)
    if sample_gdf.empty:
        raise ValueError("No sample points inside boundary after clipping.")

    _prog(run_id, "Loading survey points...", 50)
    if survey_points_file:
        survey_gdf = _read_points(survey_points_file, crs)
        survey_gdf = _clip_to_boundary(survey_gdf, boundary_gdf)
    else:
        survey_gdf = None

    _prog(run_id, "Generating maps...", 55)

    def create_figure():
        fig = plt.figure(figsize=(10, 7.5), dpi=DPI)
        fig.patch.set_facecolor('white')
        ax = fig.add_subplot(111)
        ax.set_facecolor('white')
        ax.set_aspect('equal')
        return fig, ax

    # Map 1: Slope Map
    # Display-only simplification: real-DEM slope polygons carry thousands of
    # jagged vertices; sub-pixel ones are invisible but cost RAM + CPU.
    try:
        _hb = boundary_gdf.total_bounds
        _hstol = max(_hb[2] - _hb[0], _hb[3] - _hb[1]) / 800.0
        _slope_disp = slope_poly.copy()
        _slope_disp.geometry = _slope_disp.geometry.simplify(
            _hstol, preserve_topology=True)
        _slope_disp = _slope_disp[~_slope_disp.geometry.is_empty]
        if _slope_disp.empty:
            _slope_disp = slope_poly
    except Exception:
        _slope_disp = slope_poly
    fig1, ax1 = create_figure()
    for cls, info in SLOPE_CLASSES.items():
        sub = _slope_disp[_slope_disp['class'] == cls]
        if not sub.empty:
            sub.plot(ax=ax1, facecolor=info['color'], edgecolor='black', linewidth=0.2, label=info['range'])
    boundary_gdf.boundary.plot(ax=ax1, color='black', linewidth=2)
    compartments_gdf.plot(ax=ax1, facecolor='none', edgecolor='gray', linewidth=0.5)
    for _, row in compartments_gdf.iterrows():
        ax1.annotate(row['Comp_ID'], xy=(row.geometry.centroid.x, row.geometry.centroid.y),
                     ha='center', va='center', fontsize=6, color='black')
    handles = [mpatches.Patch(facecolor=info['color'], label=info['range']) for cls, info in SLOPE_CLASSES.items()]
    _add_north_arrow(fig1)
    _add_scale_bar(fig1, ax1)
    _draw_slope_table_compact(ax1, slope_areas)
    ax1.set_title("Slope Map", fontsize=14, weight='bold')
    fig1.savefig(os.path.join(out_dir, "Slope_Map.png"), dpi=DPI, bbox_inches='tight')
    fig1.savefig(os.path.join(out_dir, "Slope_Map.pdf"), bbox_inches='tight')
    fig1.savefig(os.path.join(out_dir, "Slope_Map.svg"), bbox_inches='tight')
    plt.close(fig1)
    gc.collect()
    # Slope vectors served Map 1 only — release before the imagery maps.
    try:
        del slope_poly, _slope_disp
    except Exception:
        pass
    gc.collect()
    _prog(run_id, "Slope Map generated.", 60)

    # Map 2: Satellite Map (decimated display; full-res clip stays on disk)
    fig2, ax2 = create_figure()
    with rasterio.open(sat_clip_path) as src:
        _k = max(1, max(src.width, src.height) // 1200)
        if _k > 1:
            from affine import Affine
            from rasterio.enums import Resampling
            _arr = src.read(
                out_shape=(src.count, int(src.height / _k),
                           int(src.width / _k)),
                resampling=Resampling.bilinear)
            show(_arr, transform=src.transform * Affine.scale(_k, _k),
                 ax=ax2, title='')
            del _arr
        else:
            show(src, ax=ax2, title='')
    boundary_gdf.boundary.plot(ax=ax2, color='yellow', linewidth=2)
    compartments_gdf.plot(ax=ax2, facecolor='none', edgecolor='white', linewidth=0.5)
    for _, row in compartments_gdf.iterrows():
        ax2.annotate(row['Comp_ID'], xy=(row.geometry.centroid.x, row.geometry.centroid.y),
                     ha='center', va='center', fontsize=6, color='white')
    _add_north_arrow(fig2)
    _add_scale_bar(fig2, ax2)
    ax2.set_title("Satellite Map", fontsize=14, weight='bold')
    fig2.savefig(os.path.join(out_dir, "Satellite_Map.png"), dpi=DPI, bbox_inches='tight')
    fig2.savefig(os.path.join(out_dir, "Satellite_Map.pdf"), bbox_inches='tight')
    fig2.savefig(os.path.join(out_dir, "Satellite_Map.svg"), bbox_inches='tight')
    plt.close(fig2)
    gc.collect()
    _prog(run_id, "Satellite Map generated.", 68)

    # Map 3: Sub-compartment Map
    fig3, ax3 = create_figure()
    import random
    random.seed(42)
    comps = compartments_gdf.copy()
    comps['color'] = ["#" + ''.join(random.choices('0123456789ABCDEF', k=6)) for _ in range(len(comps))]
    comps.plot(ax=ax3, facecolor=comps['color'], edgecolor='black', linewidth=0.5)
    boundary_gdf.boundary.plot(ax=ax3, color='black', linewidth=2)
    for _, row in comps.iterrows():
        area_ha = row.geometry.area / 10000
        ax3.annotate(f"{row['Comp_ID']}\n{area_ha:.1f}ha",
                     xy=(row.geometry.centroid.x, row.geometry.centroid.y),
                     ha='center', va='center', fontsize=6, color='black')
    handles = [mpatches.Patch(facecolor=row['color'], label=row['Comp_ID']) for _, row in comps.iterrows()]
    _add_north_arrow(fig3)
    _add_scale_bar(fig3, ax3)
    ax3.legend(handles=handles, title="Compartments", loc='lower right')
    ax3.set_title("Sub-compartment Map", fontsize=14, weight='bold')
    fig3.savefig(os.path.join(out_dir, "SubCompartment_Map.png"), dpi=DPI, bbox_inches='tight')
    fig3.savefig(os.path.join(out_dir, "SubCompartment_Map.pdf"), bbox_inches='tight')
    fig3.savefig(os.path.join(out_dir, "SubCompartment_Map.svg"), bbox_inches='tight')
    plt.close(fig3)
    gc.collect()
    _prog(run_id, "Sub-compartment Map generated.", 76)

    # Map 4: Sample Plot Map
    fig4, ax4 = create_figure()
    boundary_gdf.boundary.plot(ax=ax4, color='black', linewidth=2)
    compartments_gdf.plot(ax=ax4, facecolor='none', edgecolor='gray', linewidth=0.5)
    sample_gdf.plot(ax=ax4, color='red', markersize=30, marker='o')
    for _, row in sample_gdf.iterrows():
        ax4.annotate(row['Point_ID'], xy=(row.geometry.x, row.geometry.y),
                     xytext=(5, 5), textcoords='offset points', fontsize=6, color='red')
    _add_north_arrow(fig4)
    _add_scale_bar(fig4, ax4)
    handles = [mpatches.Patch(facecolor='red', label='Sample Plots')]
    ax4.legend(handles=handles, loc='lower right')
    ax4.set_title("Sample Plot Map", fontsize=14, weight='bold')
    fig4.savefig(os.path.join(out_dir, "SamplePlot_Map.png"), dpi=DPI, bbox_inches='tight')
    fig4.savefig(os.path.join(out_dir, "SamplePlot_Map.pdf"), bbox_inches='tight')
    fig4.savefig(os.path.join(out_dir, "SamplePlot_Map.svg"), bbox_inches='tight')
    plt.close(fig4)
    gc.collect()
    _prog(run_id, "Sample Plot Map generated.", 84)

    # Map 5: Boundary Survey Point Map
    fig5, ax5 = create_figure()
    boundary_gdf.boundary.plot(ax=ax5, color='black', linewidth=2)
    compartments_gdf.plot(ax=ax5, facecolor='none', edgecolor='gray', linewidth=0.5)
    if survey_gdf is not None:
        survey_gdf.plot(ax=ax5, color='blue', markersize=20, marker='^')
        for _, row in survey_gdf.iterrows():
            ax5.annotate(row['Point_ID'], xy=(row.geometry.x, row.geometry.y),
                         xytext=(5, 5), textcoords='offset points', fontsize=6, color='blue')
    _add_north_arrow(fig5)
    _add_scale_bar(fig5, ax5)
    handles = [mpatches.Patch(facecolor='blue', label='Survey Points')]
    ax5.legend(handles=handles, loc='lower right')
    ax5.set_title("Boundary Survey Point Map", fontsize=14, weight='bold')
    fig5.savefig(os.path.join(out_dir, "BoundarySurveyPoint_Map.png"), dpi=DPI, bbox_inches='tight')
    fig5.savefig(os.path.join(out_dir, "BoundarySurveyPoint_Map.pdf"), bbox_inches='tight')
    fig5.savefig(os.path.join(out_dir, "BoundarySurveyPoint_Map.svg"), bbox_inches='tight')
    plt.close(fig5)
    gc.collect()
    _prog(run_id, "Boundary Survey Point Map generated.", 92)

    # Map 6: Survey Point Map (no compartments)
    fig6, ax6 = create_figure()
    boundary_gdf.boundary.plot(ax=ax6, color='black', linewidth=2)
    if survey_gdf is not None:
        survey_gdf.plot(ax=ax6, color='blue', markersize=20, marker='^')
        for _, row in survey_gdf.iterrows():
            ax6.annotate(row['Point_ID'], xy=(row.geometry.x, row.geometry.y),
                         xytext=(5, 5), textcoords='offset points', fontsize=6, color='blue')
    _add_north_arrow(fig6)
    _add_scale_bar(fig6, ax6)
    handles = [mpatches.Patch(facecolor='blue', label='Survey Points')]
    ax6.legend(handles=handles, loc='lower right')
    ax6.set_title("Survey Point Map", fontsize=14, weight='bold')
    fig6.savefig(os.path.join(out_dir, "SurveyPoint_Map.png"), dpi=DPI, bbox_inches='tight')
    fig6.savefig(os.path.join(out_dir, "SurveyPoint_Map.pdf"), bbox_inches='tight')
    fig6.savefig(os.path.join(out_dir, "SurveyPoint_Map.svg"), bbox_inches='tight')
    plt.close(fig6)
    gc.collect()
    _prog(run_id, "Survey Point Map generated.", 98)

    zip_path = os.path.join(out_dir, "GroupH_Maps.zip")
    with zipfile.ZipFile(zip_path, 'w') as z:
        for fname in os.listdir(out_dir):
            if fname.endswith(('.png', '.pdf', '.svg')):
                z.write(os.path.join(out_dir, fname), fname)

    _prog(run_id, "All maps generated and packaged.", 100)
    return zip_path, out_dir

# ----------------------------------------------------------------------

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
