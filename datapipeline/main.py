"""
Pylovo Data Pipeline - Main Entry Point

A state-of-the-art data pipeline for downloading and processing geospatial data
for synthetic low-voltage grid generation.

Usage (from project root):
    python -m datapipeline.main --country germany --state bayern
    python -m datapipeline.main --country austria
    python -m datapipeline.main --list-regions

Usage (from datapipeline directory):
    python main.py --country germany --state hamburg
"""

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

# Add parent directory to path so we can run from datapipeline/ directory
_this_dir = Path(__file__).resolve().parent
_project_root = _this_dir.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
from typing import Optional, List

try:
    from tqdm import tqdm
except ImportError:
    # Dummy tqdm if not available
    class tqdm:
        def __init__(self, iterable=None, total=None, **kwargs):
            self.iterable = iterable
            self.total = total
            if kwargs.get('desc'):
                print(f"Progress: {kwargs['desc']}")
        
        def __iter__(self):
            for item in self.iterable:
                yield item
                
        def __enter__(self):
            return self
            
        def __exit__(self, exc_type, exc_value, traceback):
            pass
            
        def update(self, n=1):
            pass
            
        def set_description(self, desc):
            print(f"Status: {desc}")

from datapipeline.utils import (
    setup_logging,
    get_region_config,
    get_pbf_cache_path,
    list_available_regions,
    load_settings,
    copy_local_pbf
)
from datapipeline.downloaders import (
    TransformerDownloader,
    BuildingDownloader,
    WaysDownloader,
    BoundaryDownloader,
)
from datapipeline.processors import (
    BuildingProcessor,
    TransformerProcessor,
)
from datapipeline.enrichment import (
    BAG3DEnricher,
    EPOnlineEnricher,
    CBSEnricher,
    EUBUCCOEnricher,
    CUZKLidarEnricher,
    NRWLidarEnricher,
    SachsenLidarEnricher,
    ThueringenLidarEnricher,
)

logger = setup_logging()


class DataPipeline:
    """Main data pipeline orchestrator."""

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    
    def __init__(self, country: str, state: Optional[str] = None, 
                 pbf_path: Optional[Path] = None, no_cache: bool = False):
        """
        Initialize the pipeline for a specific region.
        
        Args:
            country: Country key (e.g., 'germany', 'austria')
            state: Optional state key (e.g., 'bayern')
            pbf_path: Optional path to local PBF file
            no_cache: If True, clear cache and redownload
        """
        self.region_config = get_region_config(country, state)
        self.settings = load_settings()
        self.no_cache = no_cache
        
        # Clear cache if requested
        if no_cache:
            self._clear_cache()
        
        # If local PBF provided, copy to cache
        if pbf_path:
            pbf_path = Path(pbf_path)
            if pbf_path.exists():
                copy_local_pbf(pbf_path, self.region_config)
                logger.info(f"Using local PBF file: {pbf_path}")
            else:
                raise FileNotFoundError(f"PBF file not found: {pbf_path}")
        
        logger.info(f"Initialized pipeline for: {self.region_config['name']}")
    
    @staticmethod
    def _drop_excluded_buildings(buildings_dir: Path) -> None:
        """Remove buildings the processor excluded (under 25 m², no electricity use) from enrichment outputs.

        Enrichment reads the unprocessed Res_/Oth_ files, and the constructor imports its output in
        preference to the _processed file, so without this the exclusions are lost.
        """
        import geopandas as gpd

        for path in sorted(buildings_dir.glob("*.gpkg")):
            stem = path.stem
            cuts = [stem.find(tag) for tag in ("_enriched", "_energy", "_cbs") if tag in stem]
            if "_processed" in stem or not stem.startswith(("Res_", "Oth_")) or not cuts:
                continue
            processed = buildings_dir / f"{stem[:min(cuts)]}_processed.gpkg"
            if not processed.exists():
                continue
            gdf = gpd.read_file(path)
            kept = gdf[gdf["osm_id"].isin(gpd.read_file(processed)["osm_id"])]
            if len(kept) < len(gdf):
                kept.to_file(path, driver="GPKG")
                logger.info("Dropped %d buildings excluded by processing from %s", len(gdf) - len(kept), path.name)

    def _clear_cache(self):
        """Clear cached inputs and generated raw_data outputs for this region."""
        cache_path = get_pbf_cache_path(self.region_config)
        if cache_path.exists():
            logger.info(f"Removing cached PBF: {cache_path}")
            cache_path.unlink()

        output_base_dir = (Path(__file__).parent / self.settings["output"]["base_dir"]).resolve()
        region_output_dir = output_base_dir / self.region_config["country"]
        state = self.region_config.get("state")
        if state:
            region_output_dir = region_output_dir / state

        if region_output_dir.exists():
            logger.info(f"Clearing output directory: {region_output_dir}")
            shutil.rmtree(region_output_dir)

    def _clear_enrichment_cache(self):
        """Clear enrichment cache directories (3dbag, ep_online, cbs, eubucco) to free disk space.
        
        For eubucco, keeps small state-level caches (e.g. DE__bremen.gpkg)
        and only removes the large country-level files to avoid re-downloading.
        """
        cache_base = Path(__file__).parent / "cache"
        for cache_name in ("3dbag", "ep_online", "cbs", "eubucco"):
            cache_dir = cache_base / cache_name
            if cache_dir.exists():
                if cache_name == "eubucco":
                    # Only remove large country-level files; keep state caches
                    removed_mb = 0
                    kept_files = []
                    for f in list(cache_dir.iterdir()):
                        if not f.is_file():
                            continue
                        # State caches contain "__" (e.g. DE__bremen.gpkg)
                        if "__" in f.name:
                            kept_files.append(f.name)
                            continue
                        size_mb = f.stat().st_size / (1024 * 1024)
                        f.unlink()
                        removed_mb += size_mb
                    if removed_mb > 0:
                        logger.info(
                            "Cleared eubucco country files (%.0f MB); kept %d state cache(s): %s",
                            removed_mb, len(kept_files), ", ".join(kept_files) or "none",
                        )
                else:
                    size_mb = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file()) / (1024 * 1024)
                    if size_mb > 0:
                        logger.info(f"Clearing {cache_name} cache ({size_mb:.0f} MB): {cache_dir}")
                        shutil.rmtree(cache_dir)
                        cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _combine_building_files_for_enrichment(gpkg_files: List[Path]):
        """Merge Res/Oth files so heavy geometry-based enrichment runs only once."""
        import geopandas as gpd
        import pandas as pd

        group_column = "__pylovo_building_group"
        combined_path = gpkg_files[0].parent / "buildings_enrichment_combined.gpkg"
        frames = []
        source_by_group = {}

        for gpkg_file in gpkg_files:
            gdf = gpd.read_file(gpkg_file)
            group = "res" if gpkg_file.stem.startswith("Res_") else "oth"
            source_by_group[group] = gpkg_file
            gdf[group_column] = group
            frames.append(gdf)

        combined = gpd.GeoDataFrame(
            pd.concat(frames, ignore_index=True, sort=False),
            geometry="geometry",
            crs=frames[0].crs if frames else None,
        )
        if combined_path.exists():
            combined_path.unlink()
        combined.to_file(combined_path, driver="GPKG")
        return combined_path, source_by_group, group_column

    @staticmethod
    def _split_combined_enrichment_output(
        enriched_path: Path,
        combined_input_path: Path,
        source_by_group: dict,
        group_column: str,
    ):
        """Split one enriched combined file back into Res/Oth outputs."""
        import geopandas as gpd

        gdf = gpd.read_file(enriched_path)
        suffix = enriched_path.stem[len(combined_input_path.stem):] if enriched_path.stem.startswith(combined_input_path.stem) else ""
        if not suffix:
            suffix = "_enriched_combined"
        elif "_enriched" not in suffix:
            suffix = f"_enriched{suffix}"

        output_paths = []
        for group, source_path in source_by_group.items():
            subset = gdf[gdf[group_column] == group].copy()
            subset = subset.drop(columns=[group_column], errors="ignore")
            output_path = source_path.parent / f"{source_path.stem}{suffix}.gpkg"
            if output_path.exists():
                output_path.unlink()
            subset.to_file(output_path, driver="GPKG")
            output_paths.append(output_path)
            logger.info(
                "Building enrichment split output: %s row(s) -> %s",
                len(subset),
                output_path.name,
            )

        return output_paths
    
    def download_transformers(self, process: bool = True) -> Path:
        """Download and optionally process transformer data."""
        logger.info("=== Downloading Transformer Data ===")
        
        downloader = TransformerDownloader(self.region_config)
        output_path = downloader.download()
        
        if process:
            processor = TransformerProcessor(self.region_config)
            output_path = processor.process_geojson(output_path)
            
            # Print statistics
            stats = processor.get_statistics(output_path)
            logger.info(f"Transformer statistics: {stats}")
        
        return output_path
    
    def download_buildings(self, process: bool = True) -> Path:
        """Download and optionally process building data."""
        logger.info("=== Downloading Building Data ===")
        
        downloader = BuildingDownloader(self.region_config)
        output_path = downloader.download()
        
        if process and output_path.is_dir():
            processor = BuildingProcessor(self.region_config)

            # Process each building file.
            # Use lossless GPKG inputs so long f_classes values are preserved.
            files_to_process = []
            for prefix in ("Res", "Oth"):
                gpkg_files = sorted(
                    f for f in output_path.glob(f"{prefix}_*.gpkg")
                    if "_processed" not in f.stem
                )
                files_to_process.extend(gpkg_files)

            if not files_to_process:
                logger.warning(f"No building GPKG files found in {output_path}")

            for building_file in files_to_process:
                try:
                    processor.process_shapefile(building_file)
                except Exception as e:
                    logger.warning(f"Could not process {building_file}: {e}")

        has_res = any(output_path.glob("Res_*.gpkg"))
        has_oth = any(output_path.glob("Oth_*.gpkg"))
        if not (has_res or has_oth):
            raise FileNotFoundError(f"No building GPKG outputs were produced in {output_path}")
        
        return output_path
    
    def download_ways(self) -> Path:
        """Download street network data."""
        logger.info("=== Downloading Street Network Data ===")

        downloader = WaysDownloader(self.region_config)
        return downloader.download()

    def download_boundaries(self) -> Path:
        """Download administrative boundary data."""
        logger.info("=== Downloading Boundary Data ===")
        
        downloader = BoundaryDownloader(self.region_config)
        return downloader.download()

    def enrich_buildings(self, buildings_dir: Path,
                         skip_3dbag: bool = False,
                         skip_ep_online: bool = False,
                         skip_cbs: bool = False,
                         skip_eubucco: bool = False,
                         ep_online_api_key: Optional[str] = None) -> Path:
        """
        Enrich building data with external open datasets.

        Args:
            buildings_dir: Directory containing building GPKG files
            skip_3dbag: Skip 3D BAG enrichment
            skip_ep_online: Skip EP-Online energy label enrichment
            skip_cbs: Skip CBS statistics enrichment
            skip_eubucco: Skip EUBUCCO enrichment
            ep_online_api_key: API key for EP-Online (optional)

        Returns:
            Path to enriched building directory
        """
        logger.info("=== Enriching Building Data ===")

        gpkg_files = sorted(
            f for f in buildings_dir.glob("*.gpkg")
            if (f.stem.startswith("Res_") or f.stem.startswith("Oth_"))
            and "_processed" not in f.stem
            and "_enriched" not in f.stem
            and "_energy" not in f.stem
            and "_cbs" not in f.stem
            and not f.stem.startswith("pois_")
        )

        if not gpkg_files:
            logger.warning(f"No building GPKG files found in {buildings_dir}")
            return buildings_dir

        country = self.region_config.get("country", "").lower()
        state = str(self.region_config.get("state", "")).lower()
        is_netherlands = country == "netherlands"
        is_czech = country == "czech_republic"
        is_nrw = country == "germany" and state == "nordrhein_westfalen"
        is_saxony = country == "germany" and state == "sachsen"
        is_thuringia = country == "germany" and state == "thueringen"

        # Reuse enrichers across files to avoid repeated setup/download attempts.
        bag3d_enricher = BAG3DEnricher(self.region_config) if (not skip_3dbag and is_netherlands) else None
        ep_online_enricher = (
            EPOnlineEnricher(self.region_config, api_key=ep_online_api_key)
            if (not skip_ep_online and is_netherlands) else None
        )
        cbs_enricher = CBSEnricher(self.region_config) if (not skip_cbs and is_netherlands) else None
        eubucco_enricher = EUBUCCOEnricher(self.region_config) if (not skip_eubucco and not is_netherlands) else None
        cuzk_lidar_enricher = CUZKLidarEnricher() if (is_czech) else None
        german_lidar_enricher = None
        if is_nrw and NRWLidarEnricher is not None:
            german_lidar_enricher = NRWLidarEnricher(self.region_config)
        elif is_saxony and SachsenLidarEnricher is not None:
            german_lidar_enricher = SachsenLidarEnricher(self.region_config)
        elif is_thuringia and ThueringenLidarEnricher is not None:
            german_lidar_enricher = ThueringenLidarEnricher(self.region_config)

        can_combine_heavy_enrichment = (
            len(gpkg_files) > 1
            and bag3d_enricher is None
            and ep_online_enricher is None
            and cbs_enricher is None
            and (
                eubucco_enricher is not None
                or cuzk_lidar_enricher is not None
                or german_lidar_enricher is not None
            )
        )

        if can_combine_heavy_enrichment:
            combined_input, source_by_group, group_column = self._combine_building_files_for_enrichment(gpkg_files)
            combined_start_ts = time.monotonic()
            logger.info(
                "Building enrichment combined mode: merging %s files into %s to avoid duplicate heavy enrichment passes",
                len(gpkg_files),
                combined_input.name,
            )

            current_file = combined_input
            if eubucco_enricher is not None:
                try:
                    current_file = eubucco_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"EUBUCCO enrichment failed for {combined_input.name}: {e}")

            if german_lidar_enricher is not None:
                try:
                    current_file = german_lidar_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"German LiDAR enrichment failed for {combined_input.name}: {e}")

            if cuzk_lidar_enricher is not None:
                try:
                    current_file = cuzk_lidar_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"CUZK LiDAR enrichment failed for {combined_input.name}: {e}")

            split_outputs = self._split_combined_enrichment_output(
                current_file,
                combined_input,
                source_by_group,
                group_column,
            )
            logger.info(
                "Building enrichment combined mode complete: outputs=%s, total_elapsed=%s",
                ", ".join(path.name for path in split_outputs),
                self._format_duration(time.monotonic() - combined_start_ts),
            )
            logger.info(
                "Building enrichment complete (files=%s, total_elapsed=%s)",
                len(gpkg_files),
                self._format_duration(time.monotonic() - combined_start_ts),
            )
            self._drop_excluded_buildings(buildings_dir)
            return buildings_dir

        total_gpkg_files = len(gpkg_files)
        enrichment_start_ts = time.monotonic()
        for file_idx, gpkg_file in enumerate(gpkg_files, start=1):
            current_file = gpkg_file
            file_start_ts = time.monotonic()
            logger.info(
                "Building enrichment file %s/%s: %s (remaining_files=%s, total_elapsed=%s)",
                file_idx,
                total_gpkg_files,
                gpkg_file.name,
                total_gpkg_files - file_idx,
                self._format_duration(time.monotonic() - enrichment_start_ts),
            )

            # Step 1: 3D BAG enrichment (Netherlands only — floors, height, construction year)
            if bag3d_enricher is not None:
                try:
                    current_file = bag3d_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"3D BAG enrichment failed for {gpkg_file.name}: {e}")

            # Step 2: EP-Online energy labels (Netherlands only)
            if ep_online_enricher is not None:
                try:
                    current_file = ep_online_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"EP-Online enrichment failed for {gpkg_file.name}: {e}")

            # Step 3: CBS population/household statistics (Netherlands only)
            if cbs_enricher is not None:
                try:
                    current_file = cbs_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"CBS enrichment failed for {gpkg_file.name}: {e}")

            # Step 4: EUBUCCO enrichment (Germany and other EU countries, not Netherlands)
            if eubucco_enricher is not None:
                try:
                    current_file = eubucco_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"EUBUCCO enrichment failed for {gpkg_file.name}: {e}")

            # Step 5: German state-specific LiDAR 3D height enrichment
            if german_lidar_enricher is not None:
                try:
                    current_file = german_lidar_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"German LiDAR enrichment failed for {gpkg_file.name}: {e}")

            # Step 6: CUZK LiDAR 3D height enrichment (Czech Republic only)
            if cuzk_lidar_enricher is not None:
                try:
                    current_file = cuzk_lidar_enricher.enrich(current_file)
                except Exception as e:
                    logger.warning(f"CUZK LiDAR enrichment failed for {gpkg_file.name}: {e}")

            logger.info(
                "Building enrichment file %s/%s complete: %s -> %s (file_elapsed=%s, total_elapsed=%s, remaining_files=%s)",
                file_idx,
                total_gpkg_files,
                gpkg_file.name,
                current_file.name,
                self._format_duration(time.monotonic() - file_start_ts),
                self._format_duration(time.monotonic() - enrichment_start_ts),
                total_gpkg_files - file_idx,
            )

        logger.info(
            "Building enrichment complete (files=%s, total_elapsed=%s)",
            total_gpkg_files,
            self._format_duration(time.monotonic() - enrichment_start_ts),
        )
        self._drop_excluded_buildings(buildings_dir)
        return buildings_dir
    
    def download_all(self,
                     skip_buildings: bool = False,
                     skip_ways: bool = False,
                     skip_enrichment: bool = False,
                     ep_online_api_key: Optional[str] = None) -> dict:
        """
        Download all data types for the region.

        Args:
            skip_buildings: Skip building download (large files)
            skip_ways: Skip ways download (requires Java/osm2po)
            skip_enrichment: Skip building enrichment (3D BAG, EP-Online, CBS)
            ep_online_api_key: API key for EP-Online (optional)

        Returns:
            Dictionary of output paths
        """
        logger.info(f"Starting full data download for {self.region_config['name']}")

        outputs = {}
        failures = []

        # Define tasks (MV lines are now generated synthetically, not downloaded)
        tasks = [
            ("Boundaries", self.download_boundaries, True),
            ("Transformers", self.download_transformers, True),
            ("Buildings", self.download_buildings, not skip_buildings),
            ("Ways", self.download_ways, not skip_ways),
        ]
        
        # Create progress bar
        active_tasks = [(name, func) for name, func, enabled in tasks if enabled]
        
        with tqdm(total=len(active_tasks), desc="Overall Progress", unit="task", 
                  bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]') as pbar:
            
            for task_name, task_func in active_tasks:
                pbar.set_description(f"Processing {task_name}")
                try:
                    result = task_func()
                    outputs[task_name.lower()] = result
                except Exception as e:
                    failures.append((task_name.lower(), str(e)))
                    logger.error(f"Failed to download {task_name.lower()}: {e}")
                pbar.update(1)

        if failures:
            failure_summary = ", ".join(f"{name}: {message}" for name, message in failures)
            raise RuntimeError(f"Data download failed for {self.region_config['name']}: {failure_summary}")
        
        # Enrich buildings after download
        if not skip_buildings and not skip_enrichment and "buildings" in outputs:
            try:
                self.enrich_buildings(
                    outputs["buildings"],
                    ep_online_api_key=ep_online_api_key,
                )
            except Exception as e:
                logger.warning(f"Building enrichment failed: {e}")

        # Clear enrichment cache after successful processing to free disk space
        self._clear_enrichment_cache()

        logger.info("Data download complete!")
        logger.info(f"Outputs: {outputs}")

        return outputs


def print_available_regions():
    """Print all available regions in a formatted way."""
    regions = list_available_regions()
    
    print("\n" + "=" * 60)
    print("Available Regions")
    print("=" * 60)
    
    for country_key, country_data in regions.items():
        print(f"\n{country_key}: {country_data['name']}")
        
        if 'states' in country_data:
            print("  States:")
            for state_key, state_name in country_data['states'].items():
                print(f"    - {state_key}: {state_name}")
    
    print("\n" + "=" * 60)
    print("\nUsage examples:")
    print("  python -m datapipeline.main --country germany --state bayern")
    print("  python -m datapipeline.main --country austria")
    print("  python -m datapipeline.main --country germany --state bayern --only transformers")
    print("  python -m datapipeline.main --country germany --state bayern --pbf /path/to/bayern.osm.pbf")
    print("=" * 60 + "\n")


def main():
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Pylovo Data Pipeline - Download geospatial data for grid generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download all data for Bavaria
  python -m datapipeline.main --country germany --state bayern
  
  # Download only transformers for Austria
  python -m datapipeline.main --country austria --only transformers
  
  # List available regions
  python -m datapipeline.main --list-regions
  
  # Download without buildings (faster)
  python -m datapipeline.main --country germany --state berlin --skip-buildings
  
  # Use a local PBF file
  python -m datapipeline.main --country germany --state bayern --pbf /path/to/bayern.osm.pbf
        """
    )
    
    parser.add_argument(
        "--country", "-c",
        type=str,
        help="Country code (e.g., germany, austria, france)"
    )
    
    parser.add_argument(
        "--state", "-s",
        type=str,
        help="State code for countries with subdivisions (e.g., bayern, berlin)"
    )
    
    parser.add_argument(
        "--list-regions", "-l",
        action="store_true",
        help="List all available regions"
    )
    
    parser.add_argument(
        "--only",
        type=str,
        choices=["transformers", "buildings", "ways", "boundaries"],
        help="Download only specific data type"
    )
    
    parser.add_argument(
        "--skip-buildings",
        action="store_true",
        help="Skip building download (large files)"
    )
    
    parser.add_argument(
        "--skip-ways",
        action="store_true",
        help="Skip ways download (requires Java)"
    )

    parser.add_argument(
        "--pbf",
        type=str,
        help="Path to local PBF file (instead of downloading from Geofabrik)"
    )
    
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Clear cache and redownload all data"
    )

    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help="Skip building enrichment (3D BAG, EP-Online, CBS, EUBUCCO)"
    )

    parser.add_argument(
        "--only-enrich",
        action="store_true",
        help="Only run enrichment on existing building data (skip download)"
    )

    parser.add_argument(
        "--ep-online-key",
        type=str,
        default=os.environ.get("EP_ONLINE_API_KEY"),
        help="EP-Online API key for energy label download (or set EP_ONLINE_API_KEY env var)"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output"
    )
    
    args = parser.parse_args()
    
    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Handle list-regions
    if args.list_regions:
        print_available_regions()
        return 0
    
    # Validate required arguments
    if not args.country:
        parser.error("--country is required (or use --list-regions)")
    
    try:
        # Initialize pipeline
        pipeline = DataPipeline(args.country, args.state, args.pbf, args.no_cache)
        
        # Run specific or all downloads
        if args.only_enrich:
            # Only enrich existing building data
            buildings_dir = pipeline.download_buildings(process=False)
            pipeline.enrich_buildings(
                buildings_dir,
                ep_online_api_key=args.ep_online_key,
            )
            pipeline._clear_enrichment_cache()
        elif args.only:
            if args.only == "transformers":
                pipeline.download_transformers()
            elif args.only == "buildings":
                pipeline.download_buildings()
            elif args.only == "ways":
                pipeline.download_ways()
            elif args.only == "boundaries":
                pipeline.download_boundaries()
        else:
            pipeline.download_all(
                skip_buildings=args.skip_buildings,
                skip_ways=args.skip_ways,
                skip_enrichment=args.skip_enrichment,
                ep_online_api_key=args.ep_online_key,
            )
        
        return 0
        
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
