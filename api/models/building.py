"""Building-related Pydantic models."""
from pydantic import BaseModel
from typing import Literal, Optional, List


class EstimateEnergyRequest(BaseModel):
    building_type: str
    area_m2: float
    year_of_construction: Optional[int] = None
    household_size: Optional[int] = None
    num_floors: Optional[int] = None
    energy_label: Optional[str] = None
    hot_water_electric: bool = False


class EstimateEnergyBatchRequest(BaseModel):
    buildings: List[EstimateEnergyRequest]


class CustomBuildingRequest(BaseModel):
    user_id: str
    title: str
    f_class: str  # 'residential', 'commercial', 'industrial', 'public'
    area: float  # Building area in m²
    demand_energy: Optional[float] = None  # Optional override for energy demand
    geometry: dict  # GeoJSON Point (building center)
    geometry_area: dict  # GeoJSON Polygon (building footprint)
    is_public: Literal[False] = False  # Custom buildings are private; true is rejected with 422
    icon: Optional[str] = None  # Icon name for the building type


class CustomBuildingDeleteRequest(BaseModel):
    user_id: str
    building_id: int
