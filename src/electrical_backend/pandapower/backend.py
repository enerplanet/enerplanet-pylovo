"""
Pandapower backend implementation for pylovo.

This module implements IElectricalBackend using pandapower as the simulation engine.
Pandapower uses pandas DataFrames to represent network components.

Key features:
    - DataFrame-based network representation
    - Simple component creation via pp.create_*() functions
    - Built-in power flow solvers (Newton-Raphson, etc.)
"""

import logging
import io
import warnings
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, Optional

import numpy as np
import pandapower as pp

from ..core.backend_base import IElectricalBackend
from ..core.specs import (
    BusSpec,
    ComponentSpec,
    LineSpec,
    LoadSpec,
    TransformerSpec,
    ExtGridSpec,
    normalize_cable_name,
)
from src.config_loader import V_BAND_HIGH, V_BAND_LOW, EQUIPMENT_DATA


class PandapowerBackendError(Exception):
    """Exception raised by Pandapower backend operations."""


class PandapowerBackend(IElectricalBackend):
    """
    Pandapower implementation of IElectricalBackend.

    Manages pandapower network lifecycle and component creation.
    Designed to be a drop-in replacement for direct pp.create_*() calls.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize pandapower backend."""
        self.logger = logger or logging.getLogger(__name__)
        self.net = None
        self._circuit_name = None
        self._bus_cache: Dict[str, int] = {}
        self._fixed_trafos: set = set()

    def initialize_circuit(
        self, name: str, source_bus: str, primary_kv: float,
    ) -> None:
        """Initialize pandapower network."""
        try:
            self.net = pp.create_empty_network(name=name)
            self._circuit_name = name
            self._bus_cache = {}
            self._fixed_trafos = set()
        except Exception as e:
            self.logger.error(f"Failed to initialize circuit: {e}")
            raise PandapowerBackendError(f"Circuit initialization failed: {e}") from e

    def create_component(self, spec: ComponentSpec) -> Any:
        """Create pandapower component from specification."""
        if self.net is None:
            raise PandapowerBackendError(
                "Backend not initialized. Call initialize_circuit() first."
            )

        try:
            if isinstance(spec, BusSpec):
                return self._create_bus(spec)
            elif isinstance(spec, TransformerSpec):
                return self._create_transformer(spec)
            elif isinstance(spec, LineSpec):
                return self._create_line(spec)
            elif isinstance(spec, LoadSpec):
                return self._create_load(spec)
            elif isinstance(spec, ExtGridSpec):
                return self._create_ext_grid(spec)
            else:
                raise PandapowerBackendError(
                    f"Unknown component spec type: {type(spec).__name__}"
                )
        except Exception as e:
            self.logger.error(f"Failed to create component {spec.name}: {e}")
            raise PandapowerBackendError(f"Component creation failed: {e}") from e

    # =========================================================================
    # Private Component Creation Methods
    # =========================================================================

    def _create_bus(self, spec: BusSpec) -> int:
        """Create bus from specification."""
        zone = spec.zone if spec.zone is not None else "n"
        bus_idx = pp.create_bus(
            self.net,
            name=spec.name,
            vn_kv=spec.voltage_kv,
            geodata=spec.coordinates,
            max_vm_pu=V_BAND_HIGH,
            min_vm_pu=V_BAND_LOW,
            type="n",
            zone=zone
        )
        self._bus_cache[spec.name] = bus_idx
        self.logger.debug(f"Created bus: {spec.name} (vn={spec.voltage_kv}kV)")
        return bus_idx

    def _create_transformer(self, spec: TransformerSpec) -> int:
        """Create transformer from specification."""
        mv_bus = self._get_bus_index(spec.bus1)
        lv_bus = self._get_bus_index(spec.bus2)

        sn_mva = spec.kva / 1000.0

        # Pandapower standard types for 20/0.4 kV
        valid_std_sizes = [0.25, 0.4, 0.63]
        if sn_mva in valid_std_sizes:
            std_type = f"{sn_mva} MVA 20/0.4 kV"
            trafo_idx = pp.create_transformer(
                self.net,
                hv_bus=mv_bus,
                lv_bus=lv_bus,
                std_type=std_type,
                name=spec.name,
                parallel=spec.parallel
            )
        else:
            # Typical 20/0.4 kV transformer parameters per size
            # vkr = resistive short-circuit voltage (%), vk = total short-circuit voltage (%)
            # pfe = iron losses (kW), i0 = no-load current (%)
            trafo_params = {
                0.1:   {'vkr': 1.2, 'vk': 4.0, 'pfe': 0.35, 'i0': 0.5},
                0.16:  {'vkr': 1.3, 'vk': 4.0, 'pfe': 0.46, 'i0': 0.45},
                0.8:   {'vkr': 1.325, 'vk': 6.0, 'pfe': 1.9, 'i0': 0.3},
                1.0:   {'vkr': 1.2, 'vk': 6.0, 'pfe': 2.3, 'i0': 0.28},
                1.25:  {'vkr': 1.15, 'vk': 6.0, 'pfe': 2.8, 'i0': 0.26},
                1.6:   {'vkr': 1.1, 'vk': 6.0, 'pfe': 3.5, 'i0': 0.24},
                2.0:   {'vkr': 1.0, 'vk': 6.0, 'pfe': 4.2, 'i0': 0.22},
                2.5:   {'vkr': 0.95, 'vk': 6.0, 'pfe': 5.0, 'i0': 0.20},
            }
            params = trafo_params.get(sn_mva, {
                'vkr': 1.0, 'vk': 6.0, 'pfe': sn_mva * 2.5, 'i0': 0.5
            })
            trafo_idx = pp.create_transformer_from_parameters(
                self.net,
                hv_bus=mv_bus,
                lv_bus=lv_bus,
                sn_mva=sn_mva,
                vn_hv_kv=20.0,
                vn_lv_kv=0.4,
                vkr_percent=params['vkr'],
                vk_percent=params['vk'],
                pfe_kw=params['pfe'],
                i0_percent=params['i0'],
                name=spec.name,
                parallel=spec.parallel
            )
        
        if spec.fixed_rating:
            self._fixed_trafos.add(trafo_idx)
        self.logger.debug(f"Created transformer: {spec.name} (kva={spec.kva})")
        return trafo_idx

    def _create_line(self, spec: LineSpec) -> int:
        """Create line/cable from specification."""
        from_bus = self._get_bus_index(spec.bus1)
        to_bus = self._get_bus_index(spec.bus2)

        std_type = spec.cable_name if spec.cable_name else "NAYY_4_150"

        line_idx = pp.create_line(
            self.net,
            from_bus=from_bus,
            to_bus=to_bus,
            length_km=spec.length_km,
            std_type=std_type,
            name=spec.name,
            geodata=spec.coordinates,
            parallel=spec.parallel
        )
        self.logger.debug(
            f"Created line: {spec.name} (length={spec.length_km:.3f}km, type={std_type})"
        )
        return line_idx

    def _create_load(self, spec: LoadSpec) -> int:
        """Create load from specification."""
        bus = self._get_bus_index(spec.bus)
        p_mw = spec.kw / 1000.0
        q_mvar = spec.kvar / 1000.0

        load_idx = pp.create_load(
            self.net,
            bus=bus,
            p_mw=p_mw,
            q_mvar=q_mvar,
            name=spec.name,
            max_p_mw=spec.max_p_mw
        )
        self.logger.debug(
            f"Created load: {spec.name} (kw={spec.kw:.1f}, kvar={spec.kvar:.1f})"
        )
        return load_idx

    def _create_ext_grid(self, spec: ExtGridSpec) -> int:
        """Create external grid from specification."""
        bus = self._get_bus_index(spec.bus)
        ext_grid_idx = pp.create_ext_grid(
            self.net, bus=bus, vm_pu=spec.vm_pu, name=spec.name
        )
        return ext_grid_idx

    def _get_bus_index(self, bus_name: str) -> int:
        """Get bus index from name using cache."""
        if bus_name in self._bus_cache:
            return self._bus_cache[bus_name]

        buses = self.net.bus[self.net.bus.name == bus_name]
        if buses.empty:
            raise ValueError(f"Bus not found: {bus_name}")

        bus_idx = buses.index[0]
        self._bus_cache[bus_name] = bus_idx
        return bus_idx

    # =========================================================================
    # Cable Registration
    # =========================================================================

    def register_cable_types(self, cables: list) -> None:
        """Register cable standard types from equipment data."""
        for cable in cables:
            name, r_ohm_per_km, x_ohm_per_km, max_i_ka = cable
            normalized = normalize_cable_name(name)
            q_mm2 = int(name.split("_")[-1])

            pp.create_std_type(
                self.net,
                {
                    "r_ohm_per_km": float(r_ohm_per_km),
                    "x_ohm_per_km": float(x_ohm_per_km),
                    "max_i_ka": float(max_i_ka),
                    "c_nf_per_km": float(0),
                    "q_mm2": q_mm2
                },
                name=normalized,
                element="line",
            )
        self.logger.debug(f"Created {len(cables)} standard cable types")

    # =========================================================================
    # Power Flow & Analysis
    # =========================================================================

    def _sanitize_network_for_power_flow(self) -> None:
        """Normalize in_service flags and deactivate elements connected to inactive buses."""
        if self.net is None:
            return

        if len(self.net.bus) == 0:
            return

        bus_in_service = self.net.bus["in_service"].fillna(True).astype(bool)

        if len(self.net.line) > 0:
            line_from_active = bus_in_service.reindex(self.net.line["from_bus"]).fillna(False).to_numpy()
            line_to_active = bus_in_service.reindex(self.net.line["to_bus"]).fillna(False).to_numpy()
            line_active_mask = line_from_active & line_to_active
            self.net.line.loc[~line_active_mask, "in_service"] = False

            # Avoid zero/negative impedance segments that can destabilize Jacobian factorization.
            bad_len = self.net.line["length_km"] <= 0
            if bad_len.any():
                self.net.line.loc[bad_len, "length_km"] = 0.001

        if len(self.net.trafo) > 0:
            hv_active = bus_in_service.reindex(self.net.trafo["hv_bus"]).fillna(False).to_numpy()
            lv_active = bus_in_service.reindex(self.net.trafo["lv_bus"]).fillna(False).to_numpy()
            trafo_active_mask = hv_active & lv_active
            self.net.trafo.loc[~trafo_active_mask, "in_service"] = False

        if len(self.net.load) > 0:
            load_bus_active = bus_in_service.reindex(self.net.load["bus"]).fillna(False).to_numpy()
            self.net.load.loc[~load_bus_active, "in_service"] = False

            # Guard against NaN/inf loads.
            for col in ("p_mw", "q_mvar"):
                if col in self.net.load.columns:
                    values = self.net.load[col].astype(float)
                    self.net.load[col] = values.where(np.isfinite(values), 0.0)

    def solve_power_flow(self) -> bool:
        """
        Solve power flow using BFSW with Newton-Raphson fallback.
        BFSW is primary for radial efficiency, NR is fallback for robustness.
        """
        if self.net is None:
            raise PandapowerBackendError("No network available for power flow analysis")

        # Check for empty network
        if len(self.net.bus) == 0:
            self.logger.warning("Cannot run power flow: network has no buses")
            return False
        
        if len(self.net.line) == 0 and len(self.net.trafo) == 0:
            self.logger.warning("Cannot run power flow: network has no lines or transformers")
            return False

        # Detailed network diagnostics
        self._log_network_diagnostics()

        # Check connectivity before running power flow
        isolated_count = 0
        try:
            from pandapower.topology import unsupplied_buses
            # Get buses with loads that have no path to external grid
            isolated = unsupplied_buses(self.net)
            isolated_count = len(isolated)
            if isolated_count > 0:
                self.logger.debug(f"Network has {isolated_count} isolated buses - deactivating them")
                # Deactivate isolated buses and their connected elements
                for bus_idx in isolated:
                    self.net.bus.at[bus_idx, 'in_service'] = False
                    # Deactivate loads on isolated buses
                    load_mask = self.net.load['bus'] == bus_idx
                    self.net.load.loc[load_mask, 'in_service'] = False
        except Exception as e:
            self.logger.debug(f"Connectivity check failed: {e}")

        self._sanitize_network_for_power_flow()

        # Verify we still have active buses
        active_buses = self.net.bus[self.net.bus['in_service'] == True]
        if len(active_buses) < 2:
            self.logger.warning("Not enough active buses for power flow")
            return False

        attempts = [
            ("BFSW", {"algorithm": "bfsw", "init": "auto", "max_iteration": 100}),
            ("NR-auto", {"algorithm": "nr", "init": "auto", "max_iteration": 200}),
            ("Iwamoto-NR", {"algorithm": "iwamoto_nr", "init": "auto", "max_iteration": 200}),
            ("NR-dc", {"algorithm": "nr", "init": "dc", "max_iteration": 200}),
            ("NR-flat", {"algorithm": "nr", "init": "flat", "max_iteration": 200}),
        ]

        reinforcement_round = 0
        max_reinforcement_rounds = 6

        while True:
            last_error = None
            converged = False
            for label, kwargs in attempts:
                try:
                    self.logger.debug("Solving power flow (%s)...", label)
                    self._sanitize_network_for_power_flow()
                    sink = io.StringIO()
                    with redirect_stdout(sink), redirect_stderr(sink), warnings.catch_warnings():
                        warnings.filterwarnings(
                            "ignore",
                            message="invalid value encountered in divide",
                            category=RuntimeWarning,
                        )
                        pp.runpp(
                            self.net,
                            check_connectivity=True,
                            **kwargs,
                        )
                    if self.net.converged:
                        self.logger.info("Power flow converged (%s)", label)
                        converged = True
                        break
                except Exception as e:
                    last_error = e
                    message = str(e)
                    if label == "BFSW" and "negative axis 1 index: -1" in message:
                        self.logger.debug("BFSW skipped due to pandapower sparse-index issue: %s", message)
                    else:
                        self.logger.warning("%s failed with error: %s", label, e)

            if not converged:
                if last_error is not None:
                    self.logger.error("Power flow calculation failed after all fallbacks: %s", last_error)
                else:
                    self.logger.warning("Power flow did not converge after all fallbacks")
                self._log_convergence_diagnostics()
                return False

            if self._operating_limits_satisfied():
                return True

            if reinforcement_round >= max_reinforcement_rounds:
                self.logger.warning(
                    "Power flow converged but operating limits remain violated after %s reinforcement rounds.",
                    reinforcement_round,
                )
                self._log_convergence_diagnostics()
                return False

            if not self._reinforce_network_constraints():
                self.logger.warning(
                    "Power flow converged but voltage/loading limits remain violated and no further reinforcement is possible."
                )
                self._log_convergence_diagnostics()
                return False

            reinforcement_round += 1
            self.logger.info(
                "Applied reinforcement round %s to satisfy voltage/loading limits; rerunning power flow.",
                reinforcement_round,
            )

    def _operating_limits_satisfied(self) -> bool:
        """Check voltage and loading constraints on a converged network."""
        if self.net is None:
            return False
        if not hasattr(self.net, "res_bus") or self.net.res_bus.empty:
            return False

        vm_pu = self.net.res_bus["vm_pu"].dropna()
        if vm_pu.empty:
            return False
        vm_min = float(vm_pu.min())
        vm_max = float(vm_pu.max())

        max_line_loading = 0.0
        if hasattr(self.net, "res_line") and not self.net.res_line.empty:
            line_loading = self.net.res_line["loading_percent"].dropna()
            if not line_loading.empty:
                max_line_loading = float(line_loading.max())

        max_trafo_loading = 0.0
        if hasattr(self.net, "res_trafo") and not self.net.res_trafo.empty and "loading_percent" in self.net.res_trafo.columns:
            trafo_loading = self.net.res_trafo["loading_percent"].dropna()
            if not trafo_loading.empty:
                max_trafo_loading = float(trafo_loading.max())

        return (
            vm_min >= V_BAND_LOW
            and vm_max <= V_BAND_HIGH
            and max_line_loading <= 100.0
            and max_trafo_loading <= 100.0
        )

    def _reinforce_network_constraints(self) -> bool:
        """Upgrade lines on constrained LV paths until voltage/loading limits can be met."""
        if self.net is None or self.net.bus.empty or self.net.line.empty:
            return False
        if not hasattr(self.net, "res_bus") or self.net.res_bus.empty:
            return False

        low_voltage_buses = self.net.res_bus[self.net.res_bus["vm_pu"] < V_BAND_LOW].index.tolist()
        overloaded_lines = []
        if hasattr(self.net, "res_line") and not self.net.res_line.empty:
            overloaded_lines = self.net.res_line[self.net.res_line["loading_percent"] > 100.0].index.tolist()
        overloaded_trafos = []
        if hasattr(self.net, "res_trafo") and not self.net.res_trafo.empty and "loading_percent" in self.net.res_trafo.columns:
            overloaded_trafos = self.net.res_trafo[self.net.res_trafo["loading_percent"] > 100.0].index.tolist()
        overloaded_trafos = [idx for idx in overloaded_trafos if idx not in self._fixed_trafos]

        lines_to_upgrade = set(int(idx) for idx in overloaded_lines)
        if low_voltage_buses:
            lines_to_upgrade.update(self._collect_lines_on_low_voltage_paths(low_voltage_buses))

        if not lines_to_upgrade and not overloaded_trafos:
            return False

        upgraded = 0
        for line_idx in sorted(lines_to_upgrade):
            if self._upgrade_line_capacity(line_idx):
                upgraded += 1
        for trafo_idx in sorted(int(idx) for idx in overloaded_trafos):
            if self._upgrade_transformer_capacity(trafo_idx):
                upgraded += 1
        return upgraded > 0

    def _collect_lines_on_low_voltage_paths(self, low_voltage_buses: list[int]) -> set[int]:
        """Collect candidate lines between LV root buses and low-voltage buses."""
        from pandapower.topology import create_nxgraph
        import networkx as nx

        if self.net is None or self.net.trafo.empty:
            return set()

        lv_roots = [int(bus) for bus in self.net.trafo["lv_bus"].dropna().astype(int).unique().tolist()]
        if not lv_roots:
            return set()

        graph = create_nxgraph(self.net, include_trafos=False, respect_switches=False)
        candidate_lines: set[int] = set()

        for bus in low_voltage_buses:
            bus = int(bus)
            best_path = None
            for root in lv_roots:
                try:
                    path = nx.shortest_path(graph, root, bus)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                if best_path is None or len(path) < len(best_path):
                    best_path = path

            if best_path and len(best_path) >= 2:
                for start_bus, end_bus in zip(best_path[:-1], best_path[1:]):
                    mask = (
                        ((self.net.line["from_bus"] == start_bus) & (self.net.line["to_bus"] == end_bus))
                        | ((self.net.line["from_bus"] == end_bus) & (self.net.line["to_bus"] == start_bus))
                    )
                    candidate_lines.update(int(idx) for idx in self.net.line[mask].index.tolist())
            elif bus in lv_roots:
                incident_mask = (self.net.line["from_bus"] == bus) | (self.net.line["to_bus"] == bus)
                candidate_lines.update(int(idx) for idx in self.net.line[incident_mask].index.tolist())

        return candidate_lines

    def _upgrade_line_capacity(self, line_idx: int) -> bool:
        """Increase conductor size or parallel count for a line."""
        if self.net is None or line_idx not in self.net.line.index:
            return False

        current_type = normalize_cable_name(str(self.net.line.at[line_idx, "std_type"]))
        next_type = self._get_next_larger_line_type(current_type)
        if next_type is not None and next_type != current_type:
            pp.change_std_type(self.net, line_idx, next_type, element="line")
            return True

        parallel = self.net.line.at[line_idx, "parallel"] if "parallel" in self.net.line.columns else 1
        try:
            parallel = int(parallel)
        except (TypeError, ValueError):
            parallel = 1
        if parallel < 8:
            self.net.line.at[line_idx, "parallel"] = parallel + 1
            return True
        return False

    def _get_next_larger_line_type(self, current_type: str) -> str | None:
        """Return the next larger registered line std_type by section/current rating."""
        if self.net is None:
            return None

        line_types = self.net.std_types.get("line", {})
        if current_type not in line_types:
            return None

        current_q = float(line_types[current_type].get("q_mm2", 0.0))
        current_i = float(line_types[current_type].get("max_i_ka", 0.0))

        ordered = sorted(
            (
                (name, float(attrs.get("q_mm2", 0.0)), float(attrs.get("max_i_ka", 0.0)))
                for name, attrs in line_types.items()
            ),
            key=lambda item: (item[1], item[2], item[0]),
        )
        for name, q_mm2, max_i_ka in ordered:
            if q_mm2 > current_q or max_i_ka > current_i:
                return name
        return None

    def _upgrade_transformer_capacity(self, trafo_idx: int) -> bool:
        """Increase transformer capacity to the next available standard size."""
        if self.net is None or trafo_idx not in self.net.trafo.index:
            return False

        trafo = self.net.trafo.loc[trafo_idx]
        parallel = int(trafo.get("parallel", 1) or 1)
        current_total_kva = float(trafo.get("sn_mva", 0.0)) * 1000.0 * parallel
        if current_total_kva <= 0:
            return False

        next_capacity = self._get_next_transformer_capacity_kva(current_total_kva)
        if next_capacity is None or next_capacity <= current_total_kva:
            return False

        per_unit_mva = (next_capacity / parallel) / 1000.0
        self.net.trafo.at[trafo_idx, "sn_mva"] = per_unit_mva
        return True

    @staticmethod
    def _get_next_transformer_capacity_kva(current_total_kva: float) -> float | None:
        transformer_sizes = sorted(
            float(v)
            for v in EQUIPMENT_DATA[EQUIPMENT_DATA["typ"] == "Transformer"]["s_max_kva"].dropna().tolist()
        )
        for capacity in transformer_sizes:
            if capacity > current_total_kva:
                return capacity
        return None

    def _log_network_diagnostics(self) -> None:
        """Log detailed network diagnostics for debugging power flow issues."""
        if self.net is None:
            return
        
        # Basic counts
        n_buses = len(self.net.bus)
        n_lines = len(self.net.line)
        n_loads = len(self.net.load)
        n_trafos = len(self.net.trafo)
        n_ext_grid = len(self.net.ext_grid)
        
        self.logger.debug(f"Network: buses={n_buses}, lines={n_lines}, loads={n_loads}, trafos={n_trafos}, ext_grid={n_ext_grid}")
        
        # Check for external grid
        if n_ext_grid == 0:
            self.logger.warning("DIAGNOSTIC: No external grid (slack bus) defined!")
        
        # Check line connections
        if n_lines > 0:
            from_buses = set(self.net.line['from_bus'].unique())
            to_buses = set(self.net.line['to_bus'].unique())
            connected_buses = from_buses | to_buses
            all_buses = set(self.net.bus.index)
            unconnected = all_buses - connected_buses
            
            # Exclude transformer buses from unconnected check
            if n_trafos > 0:
                trafo_buses = set(self.net.trafo['hv_bus'].unique()) | set(self.net.trafo['lv_bus'].unique())
                unconnected = unconnected - trafo_buses
            
            if unconnected:
                self.logger.info(f"DIAGNOSTIC: {len(unconnected)} buses not connected by lines")
        
        # Check for zero-length lines
        if n_lines > 0:
            zero_length = self.net.line[self.net.line['length_km'] <= 0]
            if len(zero_length) > 0:
                self.logger.warning(f"DIAGNOSTIC: {len(zero_length)} lines with zero/negative length!")
        
        # Check load values
        if n_loads > 0:
            total_p = self.net.load['p_mw'].sum()
            total_q = self.net.load['q_mvar'].sum()
            zero_loads = len(self.net.load[self.net.load['p_mw'] == 0])
            self.logger.debug(f"DIAGNOSTIC: Total load P={total_p*1000:.1f}kW, Q={total_q*1000:.1f}kVar, zero_loads={zero_loads}")
        
        # Check transformer capacity vs load
        if n_trafos > 0 and n_loads > 0:
            trafo_capacity = self.net.trafo['sn_mva'].sum() * 1000  # kVA
            total_load_kva = (self.net.load['p_mw'].sum()**2 + self.net.load['q_mvar'].sum()**2)**0.5 * 1000
            if total_load_kva > trafo_capacity:
                self.logger.warning(f"DIAGNOSTIC: Load ({total_load_kva:.0f}kVA) exceeds transformer capacity ({trafo_capacity:.0f}kVA)!")

    def _log_convergence_diagnostics(self) -> None:
        """Log diagnostics when power flow fails to converge."""
        if self.net is None:
            return
        
        self.logger.info("=== CONVERGENCE FAILURE DIAGNOSTICS ===")
        
        # Check bus voltages if available
        if hasattr(self.net, 'res_bus') and len(self.net.res_bus) > 0:
            vm_pu = self.net.res_bus['vm_pu']
            valid_vm = vm_pu.dropna()
            if len(valid_vm) > 0:
                vm_min = valid_vm.min()
                vm_max = valid_vm.max()
                self.logger.info(f"Bus voltages: min={vm_min:.3f}pu, max={vm_max:.3f}pu")
                
                # Find problematic buses
                low_v = self.net.res_bus[self.net.res_bus['vm_pu'] < 0.9]
                high_v = self.net.res_bus[self.net.res_bus['vm_pu'] > 1.1]
                if len(low_v) > 0:
                    self.logger.info(f"Low voltage buses (<0.9pu): {len(low_v)}")
                if len(high_v) > 0:
                    self.logger.info(f"High voltage buses (>1.1pu): {len(high_v)}")
            
            nan_buses = vm_pu.isna().sum()
            if nan_buses > 0:
                self.logger.info(f"Buses with NaN voltage: {nan_buses}")
        
        # Check line loading if available
        if hasattr(self.net, 'res_line') and len(self.net.res_line) > 0:
            loading = self.net.res_line['loading_percent'].dropna()
            if len(loading) > 0:
                loading_max = loading.max()
                overloaded = self.net.res_line[self.net.res_line['loading_percent'] > 100]
                self.logger.info(f"Max line loading: {loading_max:.1f}%, overloaded lines: {len(overloaded)}")

    def get_circuit_metrics(self) -> Dict[str, Any]:
        """Get circuit metrics after power flow solution."""
        if self.net is None:
            return {}

        metrics = {
            "name": self._circuit_name,
            "num_buses": len(self.net.bus),
            "num_lines": len(self.net.line),
            "num_transformers": len(self.net.trafo),
            "num_loads": len(self.net.load),
        }

        if hasattr(self.net, 'converged'):
            metrics["converged"] = self.net.converged

        if hasattr(self.net, 'res_bus') and not self.net.res_bus.empty:
            vm_pu = self.net.res_bus.vm_pu
            metrics["min_voltage_pu"] = float(vm_pu.min())
            metrics["max_voltage_pu"] = float(vm_pu.max())
            metrics["avg_voltage_pu"] = float(vm_pu.mean())

        if hasattr(self.net, 'res_line') and not self.net.res_line.empty:
            metrics["total_losses_mw"] = float(self.net.res_line.pl_mw.sum())
            metrics["max_line_loading_percent"] = float(self.net.res_line.loading_percent.max())

        if hasattr(self.net, 'res_trafo') and not self.net.res_trafo.empty:
            if "loading_percent" in self.net.res_trafo.columns:
                metrics["max_trafo_loading_percent"] = float(self.net.res_trafo.loading_percent.max())

        if hasattr(self.net, 'trafo') and not self.net.trafo.empty:
            parallel = self.net.trafo["parallel"] if "parallel" in self.net.trafo.columns else 1
            metrics["total_transformer_capacity_kva"] = float((self.net.trafo.sn_mva * parallel).sum() * 1000.0)

        return metrics

    # =========================================================================
    # Export & Cleanup
    # =========================================================================

    def export_to_format(self, filename: Optional[str] = None) -> str:
        """Export circuit to JSON format."""
        if self.net is None:
            raise PandapowerBackendError("No network available for export")

        try:
            if filename:
                pp.to_json(self.net, filename=filename)
                with open(filename, 'r') as f:
                    json_str = f.read()
                self.logger.info(f"Exported to JSON file: {filename}")
            else:
                json_str = pp.to_json(self.net)
                self.logger.info("Exported to JSON")
            return json_str

        except Exception as e:
            self.logger.error(f"JSON export failed: {e}")
            raise PandapowerBackendError(f"JSON export failed: {e}") from e

    def cleanup(self) -> None:
        """Clean up network resources."""
        if self.net:
            self.net = None
            self._bus_cache = {}
            self.logger.debug("Cleaned up network")
        self._circuit_name = None

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_cable_types(self) -> list[str]:
        """Get all registered cable type names."""
        if self.net is None:
            return []
        return list(self.net.std_types.get("line", {}).keys())

    def get_component_count(self, component_type: str) -> int:
        """Get component count by type."""
        if self.net is None:
            return 0
        type_map = {
            "buses": "bus",
            "lines": "line",
            "loads": "load",
            "transformers": "trafo",
        }
        df_name = type_map.get(component_type, component_type)
        df = getattr(self.net, df_name, None)
        return len(df) if df is not None else 0

    def get_bus_coordinates(self, bus_name: str) -> tuple[float, float] | None:
        """Get bus geographic coordinates."""
        if self.net is None or self.net.bus_geodata.empty:
            return None
        try:
            bus_idx = self._get_bus_index(bus_name)
        except ValueError:
            return None
        if bus_idx not in self.net.bus_geodata.index:
            return None
        row = self.net.bus_geodata.loc[bus_idx]
        return (float(row["x"]), float(row["y"]))

    # =========================================================================
    # Update Methods
    # =========================================================================

    def set_bus_coordinates(self, bus_name: str, x: float, y: float) -> None:
        """Set bus geographic coordinates."""
        if self.net is None:
            return
        try:
            bus_idx = self._get_bus_index(bus_name)
            self.net.bus_geodata.at[bus_idx, "x"] = x
            self.net.bus_geodata.at[bus_idx, "y"] = y
        except ValueError:
            pass

    def set_bus_zone(self, bus_name: str, zone: str) -> None:
        """Set bus zone attribute."""
        if self.net is None:
            return
        try:
            bus_idx = self._get_bus_index(bus_name)
            self.net.bus.at[bus_idx, "zone"] = zone
        except ValueError:
            pass

    def set_transformer_rating(self, trafo_name: str, rating_mva: float) -> None:
        """Set transformer rated power."""
        if self.net is None:
            return
        trafo_df = self.net.trafo[self.net.trafo["name"] == trafo_name]
        if not trafo_df.empty:
            trafo_idx = trafo_df.index[0]
            self.net.trafo.at[trafo_idx, "sn_mva"] = rating_mva

    def get_connected_lines_count(self, bus_name: str) -> int:
        """Get the number of lines connected to a bus."""
        if self.net is None:
            return 0
        try:
            bus_idx = self.net.bus[self.net.bus["name"] == bus_name].index[0]
            # Count lines where this bus is either from_bus or to_bus
            lines_from = len(self.net.line[self.net.line["from_bus"] == bus_idx])
            lines_to = len(self.net.line[self.net.line["to_bus"] == bus_idx])
            return lines_from + lines_to
        except (ValueError, IndexError):
            return 0
