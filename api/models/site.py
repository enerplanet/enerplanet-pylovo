"""Request models for the site-mode endpoints (/sites)."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class SiteGeometry(BaseModel):
    """GeoJSON Polygon or MultiPolygon in EPSG:4326."""
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list


class CreateSiteRequest(BaseModel):
    """Create a site and copy the OSM buildings and transformers inside it."""
    user_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=200)
    country_code: str = Field(min_length=2, max_length=10)
    geom: SiteGeometry


class ExtraLoad(BaseModel):
    """An additional load on a building, for example a heat pump."""
    category: str = Field(min_length=1, max_length=80)
    kw: float = Field(gt=0)


class BuildingOverrides(BaseModel):
    """Site-specific demand data; replaces the building's previous overrides."""
    measured_peak_kw: Optional[float] = Field(default=None, gt=0)
    measured_yearly_kwh: Optional[float] = Field(default=None, ge=0)
    dwellings: Optional[int] = Field(default=None, gt=0)
    extra_loads: List[ExtraLoad] = Field(default_factory=list)
    excluded: bool = False
    f_class: Optional[str] = Field(default=None, min_length=1, max_length=80)


class AddedBuilding(BaseModel):
    """A building from a 3D source that matches no OSM building."""
    geom: SiteGeometry
    source: Literal["city2tabula"]
    source_id: str = Field(min_length=1, max_length=200)
    f_class: str = Field(default="yes", min_length=1, max_length=80)
    floors: Optional[int] = Field(default=None, gt=0)


class BuildingPatch(BaseModel):
    """Overrides for one building; null resets it to its defaults."""
    building_id: int
    overrides: Optional[BuildingOverrides] = None


class SitePoint(BaseModel):
    """GeoJSON Point in EPSG:4326."""
    type: Literal["Point"]
    coordinates: List[float] = Field(min_length=2, max_length=2)


class SiteTransformerInput(BaseModel):
    """A station of the site. Omit transformer_id to add a new user station."""
    transformer_id: Optional[int] = None
    geom: SitePoint
    rated_kva: Optional[int] = Field(default=None, gt=0)
    assigned_building_ids: List[int] = Field(default_factory=list)
