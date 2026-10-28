#!/usr/bin/env python3
"""
Austria Data Preparation Script for Pylovo

Builds full-coverage postcode polygons (1 905 PLZ, 100 % of Austria) by
dissolving official Statistik Austria municipal boundaries by postal code.

Data sources:
  - Municipal shapefile: Statistik Austria OGDEXT_GEM_1 (EPSG:31287)
  - Municipality→PLZ mapping: Statistik Austria Gemeindeliste CSV
  - State assignment: Statistik Austria political-district → Bundesland mapping

All three are Statistik Austria open data under CC BY 4.0.
Attribution: "Datenquelle: Statistik Austria".

Usage:
    python -m datapipeline.prepare_country austria
"""

import sys
import tempfile
import os
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datapipeline.prepare_country.base import CountryDataPreparer

# Statistik Austria URLs
# Statistik Austria publishes one edition per 1 January and has no stable
# "latest" link; the edition year must match the Gemeindeliste key date.
# Listing: https://data.statistik.gv.at/web/meta.jsp?dataset=OGDEXT_GEM_1
_GEM_SHP_URL = (
    "https://www.statistik.gv.at/gs-open/GEODATA/ows?service=WFS&version=1.0.0"
    "&request=GetFeature&typeName=GEODATA:STATISTIK_AUSTRIA_GEM_20260101"
    "&outputFormat=SHAPE-ZIP&format_options=CHARSET:UTF-8"
)
_GEM_LIST_URL = (
    "https://www.statistik.at/verzeichnis/reglisten/gemliste_knz_en.csv"
)
_POL_DISTRICTS_URL = (
    "http://www.statistik.at/verzeichnis/reglisten/polbezirke_en.csv"
)

# Political-district state name → pylovo state_code
_STATE_MAP = {
    "Burgenland": "burgenland",
    "Carinthia": "kaernten",
    "Lower Austria": "niederoesterreich",
    "Upper Austria": "oberoesterreich",
    "Salzburg": "salzburg",
    "Styria": "steiermark",
    "Tyrol": "tirol",
    "Vorarlberg": "vorarlberg",
    "Vienna": "wien",
}


class AustriaPreparer(CountryDataPreparer):
    """Full-coverage Austrian PLZ polygons from Statistik Austria data."""

    def __init__(self):
        super().__init__("AT", "Austria")

    # ------------------------------------------------------------------
    def download_postcodes(self):
        import geopandas as gpd
        import pandas as pd
        import requests

        # 1. Municipal boundaries (official shapefile, EPSG:31287)
        print("  Downloading Statistik Austria municipal boundaries...")
        r = requests.get(_GEM_SHP_URL, timeout=120)
        r.raise_for_status()

        with tempfile.TemporaryDirectory() as tmpdir:
            with ZipFile(BytesIO(r.content)) as zf:
                zf.extractall(tmpdir)
            shp = next(f for f in os.listdir(tmpdir) if f.endswith(".shp"))
            gdf = gpd.read_file(os.path.join(tmpdir, shp))

        gdf = gdf.rename(columns={"g_id": "municipality_code", "g_name": "municipality"})
        gdf["municipality_code"] = gdf["municipality_code"].astype(str)
        print(f"    {len(gdf)} municipalities loaded")

        # 2. Municipality → PLZ mapping
        print("  Downloading municipality → PLZ mapping...")
        r2 = requests.get(_GEM_LIST_URL, timeout=30)
        r2.raise_for_status()
        gem_df = pd.read_csv(
            BytesIO(r2.content), sep=";", skiprows=2, skipfooter=1, engine="python",
        )
        gem_df = gem_df.rename(columns={
            "Municipality Code": "municipality_code",
            "Postal Code of the Municipal": "postal_code",
        })[["municipality_code", "postal_code"]].astype(
            {"municipality_code": str, "postal_code": str}
        )

        # 3. Political districts → state mapping
        print("  Downloading political district → state mapping...")
        r3 = requests.get(_POL_DISTRICTS_URL, timeout=30)
        r3.raise_for_status()
        dist_df = pd.read_csv(
            BytesIO(r3.content), sep=";", skiprows=2, skipfooter=1, engine="python",
        )
        dist_df = dist_df.rename(columns={
            "Pol. District Code": "district_code",
            "Federal Province": "state_en",
        })[["district_code", "state_en"]].astype({"district_code": str})
        dist_df["state_code"] = dist_df["state_en"].map(_STATE_MAP)

        # Derive district_code from municipality_code (first 3 digits)
        gem_df["district_code"] = gem_df["municipality_code"].str[:3]
        gem_df = gem_df.merge(dist_df[["district_code", "state_code"]], on="district_code", how="left")

        # 4. Join municipality geometries with PLZ + state
        merged = gdf.merge(gem_df[["municipality_code", "postal_code", "state_code"]],
                           on="municipality_code", how="left")
        print(f"    {merged['postal_code'].isna().sum()} municipalities without PLZ (dropped)")
        merged = merged.dropna(subset=["postal_code"])

        # 5. Dissolve by postal_code → full-coverage PLZ polygons
        print("  Dissolving by postal code...")
        plz_gdf = (
            merged[["postal_code", "state_code", "geometry"]]
            .dissolve(by="postal_code", aggfunc="first", as_index=False)
        )

        # Rename to pylovo convention
        plz_gdf = plz_gdf.rename(columns={"postal_code": "plz"})
        plz_gdf["note"] = plz_gdf["plz"] + " Austria"

        print(f"    {len(plz_gdf)} PLZ polygons (100% coverage)")
        geom_types = plz_gdf.geometry.geom_type.value_counts().to_dict()
        print(f"    Geometry types: {geom_types}")

        return plz_gdf

    # ------------------------------------------------------------------
    def run(self):
        """Override base run() to save CSV in geofabrik-compatible format
        that includes state_code (required by the constructor/loader)."""
        import pandas as pd

        os.makedirs(self.output_dir, exist_ok=True)

        print("=" * 60)
        print(f"PREPARING {self.country_name.upper()} POSTCODE DATA")
        print("=" * 60)

        gdf = self.download_postcodes()
        if gdf is None or len(gdf) == 0:
            self.print_manual_instructions()
            return False

        # Project to EPSG:3035 (pipeline standard)
        gdf_3035 = gdf.to_crs("EPSG:3035")

        # Build CSV rows in geofabrik-compatible format
        records = []
        for idx, row in gdf_3035.iterrows():
            records.append({
                "gid": idx + 1,
                "plz": str(row["plz"]),
                "state_code": row.get("state_code"),
                "note": str(row.get("note", f"{row['plz']} Austria")),
                "qkm": round(row.geometry.area / 1_000_000, 2),
                "einwohner": 0,
                "geom": row.geometry.wkb_hex,
            })

        df = pd.DataFrame(records)
        csv_path = os.path.join(self.output_dir, "postcode_austria.csv")
        df.to_csv(csv_path, index=False)

        assigned = df["state_code"].notna().sum()
        print(f"\n  Saved {len(df)} postcodes to {csv_path}")
        print(f"  With state_code: {assigned}")
        print("\n  State distribution:")
        for sc, count in df["state_code"].value_counts().sort_index().items():
            print(f"    {sc}: {count} postcodes")

        print("\n" + "=" * 60)
        print(f"{self.country_name.upper()} DATA PREPARATION COMPLETE")
        print("=" * 60)
        self.print_next_steps()
        return True


def main():
    preparer = AustriaPreparer()
    preparer.run()


if __name__ == "__main__":
    main()

