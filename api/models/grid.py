"""Grid-related Pydantic models."""
from pydantic import BaseModel
from typing import Optional, List


class GridRequest(BaseModel):
    geom: dict  # GeoJSON Polygon or MultiPolygon
    user_id: Optional[str] = None  # User ID for filtering custom buildings
    model_id: Optional[int] = None  # Model ID for filtering user-placed transformers (existing models)
    draft_id: Optional[str] = None  # Draft ID for new models (before saving)
    include_boundary: Optional[bool] = False  # Include administrative boundary in response (extra DB work)
    include_private_buildings: Optional[bool] = True  # Include user's private custom buildings
    excluded_building_ids: Optional[List[int]] = None  # List of custom building IDs to exclude


class GridStatisticsRequest(BaseModel):
    grid_result_ids: List[int]
