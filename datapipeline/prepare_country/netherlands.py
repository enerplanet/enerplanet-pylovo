#!/usr/bin/env python3
"""
Netherlands Data Preparation Script for Pylovo

Downloads and converts Dutch CBS postcode and population data to Pylovo format.

Usage:
    python -m datapipeline.prepare_netherlands_data

Output files:
    - raw_data/netherlands/postcode_netherlands.csv (postcode boundaries)
    - raw_data/netherlands/municipal_register.csv (population data)

Data sources:
    - CBS (Statistics Netherlands): https://www.cbs.nl/
    - PDOK: https://www.pdok.nl/
"""

import os
import sys
import re
import shutil
import zipfile
import requests
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # Go up to pylovo root
sys.path.insert(0, str(PROJECT_ROOT))

# Check for required packages
try:
    import geopandas as gpd
    import pandas as pd
    from shapely import wkb
except ImportError as e:
    print("=" * 60)
    print("ERROR: Missing required packages")
    print("=" * 60)
    print(f"Missing: {e}")
    print("\nInstall with:")
    print("  pip install geopandas pandas shapely requests openpyxl")
    print("=" * 60)
    sys.exit(1)


# Configuration
CBS_PC4_URL = "https://geodata.cbs.nl/files/PDOK/cbs_pc4_2023_v2.zip"
CBS_KERNCIJFERS_URL = "https://www.cbs.nl/-/media/_excel/2024/35/kwb-2022.zip"  # Key figures per postcode

# PDOK CBS gebiedsindelingen WFS - generalised province polygons. The CBS
# Kerncijfers PC4 files (xlsx and gpkg) carry no province/municipality column,
# so state_code is derived by a point-in-polygon join against these.
PDOK_PROVINCE_WFS = (
    "https://service.pdok.nl/cbs/gebiedsindelingen/2025/wfs/v1_0"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeName=provincie_gegeneraliseerd&outputFormat=application/json&srsName=EPSG:3035"
)
CBS_POSTCODE_DOWNLOADS_PAGE_URL = (
    "https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/gegevens-per-postcode"
)

RAW_DATA_DIR = PROJECT_ROOT / "raw_data"
NETHERLANDS_DIR = RAW_DATA_DIR / "netherlands"
DOWNLOAD_DIR = NETHERLANDS_DIR / "downloads"

# Province mapping (CBS province code to state_code)
PROVINCE_MAPPING = {
    "Drenthe": "drenthe",
    "Flevoland": "flevoland",
    "Friesland": "friesland",
    "Fryslân": "friesland",
    "Gelderland": "gelderland",
    "Groningen": "groningen",
    "Limburg": "limburg",
    "Noord-Brabant": "noord_brabant",
    "Noord-Holland": "noord_holland",
    "Overijssel": "overijssel",
    "Utrecht": "utrecht",
    "Zeeland": "zeeland",
    "Zuid-Holland": "zuid_holland",
}


def _normalize_pc4_value(value) -> str | None:
    """Normalize a postcode value to a 4-digit PC4 string."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    match = re.search(r"(\d{4})", text)
    if not match:
        return None
    return match.group(1)


def _clean_numeric_series(series: pd.Series) -> pd.Series:
    """Convert to numeric and treat CBS secrecy sentinel values as missing."""
    out = pd.to_numeric(series, errors="coerce")
    # CBS uses -99997 as "0-4 / geheim"; treat all <= -99997 as missing.
    out = out.mask(out <= -99997)
    return out


def _read_cbs_pc4_multilevel_excel(excel_file: Path) -> pd.DataFrame | None:
    """
    Read current CBS PC4 workbook format (e.g. `pc4_2024_v1.xlsx`) with 3-row headers.

    Returns a normalized DataFrame with canonical columns such as:
    `PC4`, `aantal_inwoners`, `aantal_huishoudens`, `gemiddelde_huishoudensgrootte`.
    """
    try:
        xls = pd.ExcelFile(excel_file)
    except Exception:
        return None

    for sheet_name in xls.sheet_names:
        try:
            df = pd.read_excel(excel_file, sheet_name=sheet_name, header=[5, 6, 7])
        except Exception:
            continue

        if not isinstance(df.columns, pd.MultiIndex):
            continue

        def _col_tuple(col) -> tuple[str, str, str]:
            vals = []
            for part in col:
                part_s = "" if pd.isna(part) else str(part)
                vals.append(part_s.strip())
            while len(vals) < 3:
                vals.append("")
            return tuple(vals[:3])

        columns = [_col_tuple(c) for c in df.columns]
        if not any(c[2] == "Postcode-4" for c in columns):
            continue

        col_map = {columns[i]: df.columns[i] for i in range(len(columns))}
        pc4_col = next((col_map[c] for c in columns if c[2] == "Postcode-4"), None)
        pop_col = next((col_map[c] for c in columns if c[0] == "Inwoners" and c[1] == "Totaal" and c[2] == "Totaal"), None)
        hh_col = next((col_map[c] for c in columns if c[0] == "Huishouden" and c[1] == "Totaal" and c[2] == "Totaal"), None)
        hh_size_col = next((col_map[c] for c in columns if c[0] == "Huishouden" and c[1] == "Grootte" and "Huishoudgrootte" in c[2]), None)

        if pc4_col is None:
            continue

        data = pd.DataFrame({
            "PC4": df[pc4_col].map(_normalize_pc4_value),
        })
        if pop_col is not None:
            data["aantal_inwoners"] = _clean_numeric_series(df[pop_col])
        if hh_col is not None:
            data["aantal_huishoudens"] = _clean_numeric_series(df[hh_col])
        if hh_size_col is not None:
            data["gemiddelde_huishoudensgrootte"] = _clean_numeric_series(df[hh_size_col])

        # Drop units row ("Code") and any footer/blank rows.
        data = data[data["PC4"].notna()].copy()
        data = data.drop_duplicates(subset=["PC4"], keep="first")
        data = data.sort_values("PC4").reset_index(drop=True)

        print(f"   [OK] Parsed CBS PC4 workbook format from sheet '{sheet_name}' ({len(data)} rows)")
        print(f"   Columns: {list(data.columns)}")
        return data

    return None


def download_file(url: str, dest_path: Path, description: str = "", expect_zip: bool = False) -> bool:
    """Download a file with progress indication."""
    print(f"[DL] Downloading {description or url}...")
    
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(tmp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = (downloaded / total_size) * 100
                    print(f"\r   Progress: {pct:.1f}%", end="", flush=True)

        if expect_zip and not zipfile.is_zipfile(tmp_path):
            print("\r   [FAIL] Downloaded file is not a valid ZIP archive")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

        tmp_path.replace(dest_path)
        
        print(f"\r   [OK] Downloaded: {dest_path.name} ({downloaded / 1024 / 1024:.1f} MB)")
        return True
        
    except requests.RequestException as e:
        print(f"\n   [FAIL] Download failed: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"\n   [FAIL] Download failed: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def is_valid_zip(path: Path) -> bool:
    """Return True if file exists and is a valid zip archive."""
    return path.exists() and zipfile.is_zipfile(path)


def discover_cbs_pc4_download_urls() -> list[str]:
    """
    Discover current CBS PC4 postcode ZIP downloads from the CBS postcode page.

    The previous hardcoded KWB media URL is unstable and often removed.
    CBS now publishes PC4 ZIPs via download.cbs.nl links on the postcode page.
    """
    print("[DISCOVER] Looking up current CBS PC4 download links...")
    try:
        response = requests.get(CBS_POSTCODE_DOWNLOADS_PAGE_URL, timeout=60)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"   [WARN] Could not load CBS postcode page: {e}")
        return []

    html = response.text
    matches = re.findall(
        r"https://download\.cbs\.nl/postcode/\d{4}-cbs_pc4_[^\"'\s>]+\.zip",
        html,
        flags=re.IGNORECASE,
    )

    urls = []
    seen = set()
    for url in matches:
        if url not in seen:
            seen.add(url)
            urls.append(url)

    if urls:
        print(f"   [OK] Found {len(urls)} PC4 ZIP link(s); newest candidate: {urls[0]}")
    else:
        print("   [WARN] No PC4 ZIP links found on CBS postcode page")
    return urls


def extract_zip(zip_path: Path, extract_dir: Path) -> list:
    """Extract zip file and return list of extracted files."""
    print(f"[EXTRACT] Extracting {zip_path.name}...")
    
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_dir)
        extracted = zf.namelist()
    
    print(f"   [OK] Extracted {len(extracted)} files")
    return extracted


def find_geopackage(directory: Path) -> Path:
    """Find geopackage file in directory."""
    for ext in ['*.gpkg', '*.shp', '*.geojson']:
        files = list(directory.glob(f"**/{ext}"))
        if files:
            return files[0]
    raise FileNotFoundError(f"No geodata file found in {directory}")


def process_postcode_boundaries(download_dir: Path) -> gpd.GeoDataFrame:
    """Download and process CBS PC4 boundaries."""
    print("\n" + "=" * 60)
    print("STEP 1: Processing Postcode Boundaries (PC4)")
    print("=" * 60)
    
    # Download
    zip_path = download_dir / "cbs_pc4.zip"
    if zip_path.exists() and not is_valid_zip(zip_path):
        print(f"   [WARN] Cached file is not a valid ZIP, re-downloading: {zip_path.name}")
        zip_path.unlink(missing_ok=True)

    if not zip_path.exists():
        candidate_urls = [CBS_PC4_URL] + discover_cbs_pc4_download_urls()
        seen = set()
        candidate_urls = [u for u in candidate_urls if not (u in seen or seen.add(u))]
        downloaded = False
        for idx, url in enumerate(candidate_urls, start=1):
            desc = "CBS PC4 boundaries" if idx == 1 else f"CBS PC4 postcode ZIP (fallback {idx - 1})"
            if download_file(url, zip_path, desc, expect_zip=True):
                downloaded = True
                break
        if not downloaded:
            raise RuntimeError("Failed to download PC4 data")
    else:
        print(f"   [INFO] Using cached: {zip_path.name}")
    
    # Extract
    extract_dir = download_dir / "cbs_pc4"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(exist_ok=True)
    extract_zip(zip_path, extract_dir)
    
    # Load geodata
    geo_file = find_geopackage(extract_dir)
    print(f"[LOAD] Loading {geo_file.name}...")
    gdf = gpd.read_file(geo_file)
    print(f"   [OK] Loaded {len(gdf)} postcode areas")
    
    # Show columns for debugging
    print(f"   Columns: {list(gdf.columns)}")
    
    return gdf


def process_kerncijfers(download_dir: Path) -> pd.DataFrame:
    """Download and process CBS Kerncijfers (key figures per postcode)."""
    print("\n" + "=" * 60)
    print("STEP 2: Processing Population Data (Kerncijfers)")
    print("=" * 60)
    
    # Download
    zip_path = download_dir / "kwb.zip"
    if zip_path.exists() and not is_valid_zip(zip_path):
        print(f"   [WARN] Cached file is not a valid ZIP, re-downloading: {zip_path.name}")
        zip_path.unlink(missing_ok=True)

    if not zip_path.exists():
        # Prefer current links discovered from the CBS postcode page to avoid legacy 404s.
        candidate_urls = discover_cbs_pc4_download_urls() + [CBS_KERNCIJFERS_URL]
        seen = set()
        candidate_urls = [u for u in candidate_urls if not (u in seen or seen.add(u))]
        downloaded = False

        for idx, url in enumerate(candidate_urls, start=1):
            desc = "CBS PC4 postcode ZIP" if idx == 1 else f"CBS postcode ZIP (fallback {idx - 1})"
            if download_file(url, zip_path, desc, expect_zip=True):
                downloaded = True
                break

        if not downloaded:
            print("   [WARN] Could not download Kerncijfers automatically")
            print("   Please download manually from:")
            print(f"   {CBS_POSTCODE_DOWNLOADS_PAGE_URL}")
            return None
    else:
        print(f"   [INFO] Using cached: {zip_path.name}")
    
    # Extract
    extract_dir = download_dir / "kwb"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(exist_ok=True)
    extract_zip(zip_path, extract_dir)
    
    # Find Excel file
    excel_files = [
        p for p in (list(extract_dir.glob("**/*.xlsx")) + list(extract_dir.glob("**/*.xls")))
        if not p.name.startswith("~$")
    ]
    if not excel_files:
        print("   [WARN] No Excel file found in Kerncijfers archive")
        return None

    # Prefer PC4-specific workbook names when multiple files are present.
    excel_files = sorted(
        excel_files,
        key=lambda p: (
            0 if "pc4" in p.name.lower() else 1,
            p.name.lower(),
        ),
    )
    excel_file = excel_files[0]
    print(f"[LOAD] Loading {excel_file.name}...")

    # First try the current CBS PC4 workbook format with multi-row headers.
    df = _read_cbs_pc4_multilevel_excel(excel_file)
    if df is not None:
        return df

    # Fallback for older single-header workbooks.
    df = None
    try:
        # Try common sheet names for PC4 data
        for sheet_name in ['PC4', 'Postcode4', 'Data', None]:
            try:
                if sheet_name:
                    df = pd.read_excel(excel_file, sheet_name=sheet_name)
                else:
                    df = pd.read_excel(excel_file)
                print(f"   [OK] Loaded {len(df)} rows from sheet '{sheet_name or 'default'}'")
                break
            except:
                continue
    except Exception as e:
        print(f"   [WARN] Could not load Excel: {e}")
        return None

    if df is None:
        print("   [WARN] Could not parse Excel workbook with known layouts")
        return None

    print(f"   Columns: {list(df.columns)[:10]}...")
    return df


def _assign_state_by_province_join(gdf_pc4: gpd.GeoDataFrame, postcode_col: str) -> dict:
    """Map each PC4 to a pylovo state_code by point-in-polygon against PDOK provinces.

    gdf_pc4 must already be in EPSG:3035. Returns {pc4_str: state_code}. Empty on
    any failure so the caller can fall back to the deferred spatial assignment
    that main_constructor does from building locations.
    """
    try:
        resp = requests.get(PDOK_PROVINCE_WFS, timeout=60)
        resp.raise_for_status()
        gdf_prov = gpd.read_file(resp.text)
    except Exception as e:
        print(f"   [WARN] Could not fetch PDOK province boundaries: {e}")
        return {}

    if gdf_prov.empty or "statnaam" not in gdf_prov.columns:
        print("   [WARN] PDOK province response has no 'statnaam' field")
        return {}

    gdf_prov = gdf_prov.to_crs("EPSG:3035")[["statnaam", "geometry"]]
    points = gdf_pc4[[postcode_col, "geometry"]].copy()
    points["geometry"] = points.geometry.representative_point()

    joined = gpd.sjoin(points, gdf_prov, how="left", predicate="within")
    pc_to_state = {}
    for _, row in joined.iterrows():
        pc = _normalize_pc4_value(row[postcode_col]) or str(row[postcode_col]).strip()
        state_code = PROVINCE_MAPPING.get(row.get("statnaam"))
        if state_code:
            pc_to_state[pc] = state_code
    print(f"   [OK] Province join mapped {len(pc_to_state)}/{len(points)} postcodes to states")
    return pc_to_state


def convert_to_pylovo_postcode(gdf: gpd.GeoDataFrame, df_pop: pd.DataFrame = None) -> pd.DataFrame:
    """Convert CBS geodata to Pylovo postcode format."""
    print("\n" + "=" * 60)
    print("STEP 3: Converting to Pylovo Format")
    print("=" * 60)
    
    # Reproject to EPSG:3035 (European standard)
    print("[PROC] Reprojecting to EPSG:3035...")
    gdf = gdf.to_crs("EPSG:3035")
    
    # Identify postcode column in GDF
    postcode_col = None
    for col in ['postcode4', 'PC4', 'pc4', 'postcode', 'POSTCODE']:
        if col in gdf.columns:
            postcode_col = col
            break
    
    if not postcode_col:
        print(f"   Available columns: {list(gdf.columns)}")
        raise ValueError("Could not find postcode column in data")
    
    print(f"   Using postcode column: {postcode_col}")
    
    # Prepare province mapping if population data is available
    pc_to_state = {}
    pc_to_pop = {}
    pc_to_households = {}
    pc_to_avg_hh_size = {}

    if df_pop is not None:
        print("   Merging population and province data...")
        
        # Identify columns in population data
        pop_pc_col = None
        for col in ['PC4', 'postcode4', 'Postcode', 'WijkenEnBuurten']:
            if col in df_pop.columns:
                pop_pc_col = col
                break
        
        pop_prov_col = None
        for col in ['PV_NAAM', 'provincienaam', 'Provincienaam']:
            if col in df_pop.columns:
                pop_prov_col = col
                break
                
        pop_val_col = None
        for col in ['aantal_inwoners', 'APTS', 'inwoners', 'population', 'bevolking', 'AantalInwoners_5']:
            if col in df_pop.columns:
                pop_val_col = col
                break

        pop_households_col = None
        for col in ['aantal_huishoudens', 'aantal_part_huishoudens', 'ParticuliereHuishoudens_1']:
            if col in df_pop.columns:
                pop_households_col = col
                break

        pop_avg_hh_size_col = None
        for col in ['gemiddelde_huishoudensgrootte', 'GemiddeldeHuishoudensgrootte_2']:
            if col in df_pop.columns:
                pop_avg_hh_size_col = col
                break
        
        if pop_pc_col:
            for _, row in df_pop.iterrows():
                try:
                    pc = str(int(row[pop_pc_col])) if pd.notna(row[pop_pc_col]) and isinstance(row[pop_pc_col], (int, float)) else str(row[pop_pc_col]).strip()
                    
                    # Map province to state_code
                    if pop_prov_col and pd.notna(row.get(pop_prov_col)):
                        prov_name = row[pop_prov_col].strip()
                        state_code = PROVINCE_MAPPING.get(prov_name)
                        if state_code:
                            pc_to_state[pc] = state_code
                    
                    # Get population
                    if pop_val_col and pd.notna(row.get(pop_val_col)):
                        try:
                            pc_to_pop[pc] = int(row[pop_val_col])
                        except:
                            pass

                    if pop_households_col and pd.notna(row.get(pop_households_col)):
                        try:
                            pc_to_households[pc] = int(float(row[pop_households_col]))
                        except Exception:
                            pass

                    if pop_avg_hh_size_col and pd.notna(row.get(pop_avg_hh_size_col)):
                        try:
                            pc_to_avg_hh_size[pc] = float(row[pop_avg_hh_size_col])
                        except Exception:
                            pass
                except Exception:
                    continue
            
            if pop_prov_col:
                print(f"   [OK] Mapped {len(pc_to_state)} postcodes to states")

    if not pc_to_state:
        print("   Population data carries no province column; deriving state_code by spatial join...")
        pc_to_state = _assign_state_by_province_join(gdf, postcode_col)

    # Find area column in GDF
    area_col = None
    for col in ['oppervlakte_land_in_ha', 'OPP_LAND', 'area', 'oppervlakte']:
        if col in gdf.columns:
            area_col = col
            break
    
    # Build output dataframe
    print("[BUILD] Building output dataframe...")
    
    output_data = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        pc = _normalize_pc4_value(row[postcode_col]) or str(row[postcode_col]).strip()
        
        # Convert geometry to WKB hex
        if geom is not None and not geom.is_empty:
            geom_wkb = geom.wkb_hex
        else:
            geom_wkb = None
        
        # Get area in km²
        if area_col and pd.notna(row.get(area_col)):
            area_km2 = float(row[area_col]) / 100  # Convert from ha to km²
        elif geom is not None:
            area_km2 = geom.area / 1_000_000  # Convert from m² to km²
        else:
            area_km2 = None
        
        # Get population (prefer from external file, fallback to GDF column)
        population = pc_to_pop.get(pc)
        if population is None:
             # Find population column in GDF
            for col in ['aantal_inwoners', 'APTS', 'inwoners', 'population', 'bevolking']:
                if col in gdf.columns and pd.notna(row.get(col)):
                    population = int(row[col])
                    break

        cbs_households = pc_to_households.get(pc)
        if cbs_households is None:
            for col in ['aantal_part_huishoudens', 'aantal_huishoudens']:
                if col in gdf.columns and pd.notna(row.get(col)):
                    try:
                        cbs_households = int(float(row[col]))
                    except Exception:
                        pass
                    break

        cbs_avg_household_size = pc_to_avg_hh_size.get(pc)
        if cbs_avg_household_size is None:
            for col in ['gemiddelde_huishoudensgrootte']:
                if col in gdf.columns and pd.notna(row.get(col)):
                    try:
                        cbs_avg_household_size = float(row[col])
                    except Exception:
                        pass
                    break
        
        state_code = pc_to_state.get(pc)
        
        output_data.append({
            'gid': idx + 1,
            'plz': pc,
            'country_code': 'NL',
            'state_code': state_code,
            'note': f"{pc} Netherlands",
            'qkm': area_km2,
            'einwohner': population,
            'cbs_households': cbs_households,
            'cbs_avg_household_size': cbs_avg_household_size,
            'geom': geom_wkb
        })
    
    df_out = pd.DataFrame(output_data)
    
    # Check for missing state codes
    missing_state = df_out['state_code'].isna().sum()
    if missing_state > 0:
        print(f"   [WARN] Warning: {missing_state} postcodes have no state_code")
    
    print(f"   [OK] Created {len(df_out)} postcode records")
    
    return df_out


def convert_to_pylovo_municipal(df_pop: pd.DataFrame, df_postcode: pd.DataFrame) -> pd.DataFrame:
    """Convert CBS population data to Pylovo municipal_register format."""
    print("\n[PROC] Converting municipal register data...")
    
    if df_pop is None:
        print("   [WARN] No population data available, creating from postcode data")
        # Create basic municipal register from postcode data
        output_data = []
        for _, row in df_postcode.iterrows():
            output_data.append({
                'plz': row['plz'],
                'country_code': 'NL',
                'state_code': row.get('state_code'),
                'pop': row.get('einwohner'),
                'cbs_households': row.get('cbs_households'),
                'cbs_avg_household_size': row.get('cbs_avg_household_size'),
                'area': row.get('qkm'),
                'lat': None,  # Would need centroid calculation
                'lon': None,
                'ags': None,  # Municipality code not available
                'name_city': None,
                'fed_state': None,
                'regio7': None,
                'regio5': None,
                'pop_den': None
            })
        return pd.DataFrame(output_data)
    
    # Process actual CBS kerncijfers data
    # Column mapping depends on CBS file structure
    # This is a template - adjust based on actual columns
    
    postcode_col = None
    for col in ['PC4', 'plz', 'postcode4', 'Postcode']:
        if col in df_pop.columns:
            postcode_col = col
            break
    
    if not postcode_col:
        print(f"   [WARN] Could not find postcode column. Available: {list(df_pop.columns)[:10]}")
        return None
    
    postcode_area_map = {}
    postcode_state_map = {}
    if df_postcode is not None and not df_postcode.empty:
        for _, row in df_postcode.iterrows():
            pc = _normalize_pc4_value(row.get("plz"))
            if pc is None:
                continue
            postcode_area_map[pc] = row.get("qkm")
            postcode_state_map[pc] = row.get("state_code")

    output_data = []
    for _, row in df_pop.iterrows():
        pc4 = _normalize_pc4_value(row[postcode_col])
        if pc4 is None:
            continue
        if postcode_area_map and pc4 not in postcode_area_map:
            # Keep municipal register aligned with the postcode boundary dataset used in this run.
            continue
        pop_val = row.get('APTS', row.get('aantal_inwoners', row.get('pop')))
        area_val = row.get('OPP_TOT', row.get('oppervlakte_totaal'))
        if pd.isna(area_val):
            area_val = postcode_area_map.get(pc4)
        households_val = row.get('aantal_huishoudens', row.get('ParticuliereHuishoudens_1'))
        avg_hh_size_val = row.get('gemiddelde_huishoudensgrootte', row.get('GemiddeldeHuishoudensgrootte_2'))
        output_data.append({
            'plz': pc4,
            'country_code': 'NL',
            'state_code': postcode_state_map.get(pc4),
            'pop': pop_val,
            'cbs_households': households_val,
            'cbs_avg_household_size': avg_hh_size_val,
            'area': area_val,
            'lat': None,
            'lon': None,
            'ags': row.get('GM_CODE', row.get('gemeentecode')),
            'name_city': row.get('GM_NAAM', row.get('gemeentenaam')),
            'fed_state': row.get('PV_NAAM', row.get('provincienaam')),
            'regio7': None,
            'regio5': None,
            'pop_den': None
        })
    
    df_out = pd.DataFrame(output_data)
    
    # Calculate population density where possible
    mask = (df_out['pop'].notna()) & (df_out['area'].notna()) & (df_out['area'] > 0)
    df_out.loc[mask, 'pop_den'] = df_out.loc[mask, 'pop'] / df_out.loc[mask, 'area']
    
    print(f"   [OK] Created {len(df_out)} municipal register records")
    return df_out


def save_outputs(df_postcode: pd.DataFrame, df_municipal: pd.DataFrame):
    """Save output files to Netherlands directory."""
    print("\n" + "=" * 60)
    print("STEP 4: Saving Output Files")
    print("=" * 60)
    
    # Ensure Netherlands directory exists
    NETHERLANDS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Save postcode CSV
    postcode_file = NETHERLANDS_DIR / "postcode_netherlands.csv"
    df_postcode.to_csv(postcode_file, index=False)
    print(f"   [OK] Saved: {postcode_file}")
    print(f"     Records: {len(df_postcode)}")
    
    # Save municipal register
    municipal_file = NETHERLANDS_DIR / "municipal_register.csv"
    
    if df_municipal is not None:
        df_municipal.to_csv(municipal_file, index=False)
        print(f"   [OK] Saved: {municipal_file}")
        print(f"     Records: {len(df_municipal)}")
    
    # Print instructions
    print("\n" + "=" * 60)
    print("[OK] DONE!")
    print("=" * 60)
    print(f"""
Output files saved to: {NETHERLANDS_DIR}

Next steps:
1. Run datapipeline for Netherlands:
   make datapipeline COUNTRY=netherlands STATE=flevoland

2. Load data into database:
   make constructor COUNTRY=netherlands STATE=flevoland

3. Generate grids:
   make grid COUNTRY=netherlands STATE=flevoland WORKERS=10
""")


def main():
    print("=" * 60)
    print("Netherlands Data Preparation for Pylovo")
    print("=" * 60)
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Output directory: {NETHERLANDS_DIR}")
    
    # Create directories
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    NETHERLANDS_DIR.mkdir(parents=True, exist_ok=True)
    
    try:
        # Step 1: Download and process postcode boundaries
        gdf_pc4 = process_postcode_boundaries(DOWNLOAD_DIR)
        
        # Step 2: Download and process population data
        df_kerncijfers = process_kerncijfers(DOWNLOAD_DIR)
        
        # Step 3: Convert to Pylovo format
        df_postcode = convert_to_pylovo_postcode(gdf_pc4, df_kerncijfers)
        df_municipal = convert_to_pylovo_municipal(df_kerncijfers, df_postcode)
        
        # Step 4: Save outputs
        save_outputs(df_postcode, df_municipal)
        
    except Exception as e:
        print(f"\n[ERROR] Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
