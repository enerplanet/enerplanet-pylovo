"""Power flow related Pydantic models."""
from pydantic import BaseModel
from typing import Optional, List


class PowerFlowRequest(BaseModel):
    grid_result_id: int
    load_scaling: Optional[float] = 1.0  # Scale loads (e.g., 0.5 = 50% load, 1.5 = 150% load)
    building_osm_ids: Optional[List[str]] = None  # Optional: only include these buildings in power flow
    # Optional scope for model/draft-specific transformer assignments
    user_id: Optional[str] = None
    model_id: Optional[int] = None
    draft_id: Optional[str] = None
    # Site grids: both required together; the grid must be of this generated revision
    site_id: Optional[int] = None
    revision: Optional[int] = None
    # Voltage limits (EN 50160: ±10% for LV)
    min_vm_pu: Optional[float] = 0.9  # Minimum voltage in per-unit
    max_vm_pu: Optional[float] = 1.1  # Maximum voltage in per-unit
    # Convergence settings
    max_iterations: Optional[int] = 50  # Max iterations for power flow solver
    tolerance: Optional[float] = 1e-6  # Convergence tolerance


class HostingCapacityRequest(BaseModel):
    """
    Request model for EV hosting capacity calculation.
    Based on: "Methods and Tools for PV and EV Hosting Capacity Determination in
    Low Voltage Distribution Networks—A Review" (Umoh et al., Energies 2023, 16, 3609)
    """
    transformer_capacity_kva: float
    current_peak_load_kw: float
    charger_power_kw: Optional[float] = 11.0  # Level 2 charger default
    simultaneity_factor: Optional[float] = 0.8  # Per paper ref [143]: 46-85%
    # Enhanced parameters for multi-constraint analysis per paper Section 4
    nominal_voltage_v: Optional[float] = 400.0  # LV nominal voltage
    voltage_limit_pu: Optional[float] = 0.90  # Min voltage per EN 50160
    cable_impedance_ohm_per_km: Optional[float] = 0.32  # NAYY 4x150 default
    cable_length_km: Optional[float] = 0.0  # Distance from transformer
    cable_max_current_a: Optional[float] = 0.0  # Cable thermal limit
