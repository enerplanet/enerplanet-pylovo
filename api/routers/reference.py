"""Reference data endpoints - transformer sizes, cable types, consumer categories, etc."""
import threading

from fastapi import APIRouter, HTTPException
import traceback

from src import cache
from src.config_loader import CABLE_COST_DICT, VN, V_BAND_LOW, V_BAND_HIGH
from utils.db import get_cursor
from utils.helpers import safe_float

router = APIRouter(tags=["reference"])
_REFERENCE_SCHEMA_READY = False
_REFERENCE_SCHEMA_LOCK = threading.Lock()


def _ensure_reference_schema_once() -> None:
    """Backfill optional equipment cost columns for older DBs."""
    global _REFERENCE_SCHEMA_READY
    if _REFERENCE_SCHEMA_READY:
        return

    with _REFERENCE_SCHEMA_LOCK:
        if _REFERENCE_SCHEMA_READY:
            return
        try:
            with get_cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT column_name) = 2
                    FROM information_schema.columns
                    WHERE table_schema = ANY(current_schemas(FALSE))
                      AND table_name = 'equipment_data'
                      AND column_name = ANY(
                          ARRAY['equipment_only_cost_eur', 'installed_cost_eur']
                      );
                    """
                )
                if cur.fetchone()[0]:
                    _REFERENCE_SCHEMA_READY = True
                    return

                cur.execute("SET LOCAL lock_timeout = '5s';")
                cur.execute("ALTER TABLE IF EXISTS equipment_data ADD COLUMN IF NOT EXISTS equipment_only_cost_eur INTEGER;")
                cur.execute("ALTER TABLE IF EXISTS equipment_data ADD COLUMN IF NOT EXISTS installed_cost_eur INTEGER;")
            _REFERENCE_SCHEMA_READY = True
            print("[Schema] Reference schema checks completed")
        except Exception as schema_err:
            # Keep endpoint functional for newer schemas even if this check fails.
            print(f"[Schema] Reference schema check failed: {schema_err}")


@router.get("/transformer-sizes")
async def get_transformer_sizes():
    """Get available transformer sizes from equipment_data table with costs"""
    cached_result = cache.get("transformer_sizes")
    if cached_result:
        return cached_result
    
    try:
        _ensure_reference_schema_once()
        with get_cursor() as cur:
            query = """
                SELECT
                    s_max_kva,
                    COALESCE(installed_cost_eur, cost_eur, equipment_only_cost_eur) AS effective_cost_eur,
                    typ,
                    name,
                    equipment_only_cost_eur,
                    installed_cost_eur
                FROM equipment_data
                WHERE typ = 'Transformer' AND s_max_kva IS NOT NULL
                  AND s_max_kva IN (100, 160, 250, 400, 630)
                ORDER BY s_max_kva;
            """
            cur.execute(query)
            rows = cur.fetchall()
        
        sizes = [
            {
                "kva": row[0], 
                "cost_eur": row[1],
                "type": row[2],
                "name": row[3],
                "equipment_only_cost_eur": row[4],
                "installed_cost_eur": row[5],
            } for row in rows
        ]
        
        result = {
            "status": "success",
            "sizes": sizes,
            "count": len(sizes)
        }
        
        cache.set("transformer_sizes", result, ttl=3600)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/consumer-categories")
async def get_consumer_categories():
    """Get available consumer categories (building types) with full details"""
    cached_result = cache.get("consumer_categories")
    if cached_result:
        return cached_result
    
    try:
        with get_cursor() as cur:
            query = """
                SELECT 
                    consumer_category_id,
                    definition,
                    peak_load,
                    yearly_consumption,
                    peak_load_per_m2,
                    yearly_consumption_per_m2,
                    sim_factor
                FROM consumer_categories
                ORDER BY definition;
            """
            cur.execute(query)
            rows = cur.fetchall()
        
        categories = [
            {
                "id": row[0],
                "definition": row[1],
                "peak_load_kw": safe_float(row[2]),
                "yearly_consumption_kwh": safe_float(row[3]),
                "peak_load_per_m2": safe_float(row[4]),
                "yearly_consumption_per_m2": safe_float(row[5]),
                "sim_factor": safe_float(row[6])
            } for row in rows
        ]
        
        result = {
            "status": "success",
            "categories": categories,
            "count": len(categories)
        }
        
        cache.set("consumer_categories", result, ttl=3600)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cable-types")
async def get_cable_types():
    """Get available cable types with costs and specifications"""
    cached_result = cache.get("cable_types")
    if cached_result:
        return cached_result
    
    try:
        _ensure_reference_schema_once()
        with get_cursor() as cur:
            query = """
                SELECT
                    name,
                    COALESCE(installed_cost_eur, cost_eur, equipment_only_cost_eur) AS effective_cost_eur,
                    max_i_a,
                    r_mohm_per_km,
                    x_mohm_per_km,
                    equipment_only_cost_eur,
                    installed_cost_eur
                FROM equipment_data
                WHERE typ = 'Cable'
                ORDER BY max_i_a;
            """
            cur.execute(query)
            rows = cur.fetchall()
        
        cables_from_db = [
            {
                "name": row[0],
                "cost_eur_per_m": row[1],
                "max_current_a": row[2],
                "resistance_mohm_per_km": row[3],
                "reactance_mohm_per_km": row[4],
                "equipment_only_cost_eur_per_m": row[5],
                "installed_cost_eur_per_m": row[6],
            } for row in rows
        ]
        
        cable_costs = [
            {"name": name, "cost_eur_per_m": cost}
            for name, cost in CABLE_COST_DICT.items()
        ]
        
        result = {
            "status": "success",
            "cables": cables_from_db if cables_from_db else cable_costs,
            "cable_costs": cable_costs,
            "count": len(cables_from_db) if cables_from_db else len(cable_costs)
        }
        
        cache.set("cable_types", result, ttl=3600)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/equipment-costs")
async def get_equipment_costs():
    """Get all equipment costs (transformers, cables, etc.)"""
    cached_result = cache.get("equipment_costs")
    if cached_result:
        return cached_result
    
    try:
        _ensure_reference_schema_once()
        with get_cursor() as cur:
            query = """
                SELECT
                    typ,
                    name,
                    s_max_kva,
                    max_i_a,
                    COALESCE(installed_cost_eur, cost_eur, equipment_only_cost_eur) AS effective_cost_eur,
                    equipment_only_cost_eur,
                    installed_cost_eur
                FROM equipment_data
                ORDER BY typ, s_max_kva, max_i_a;
            """
            cur.execute(query)
            rows = cur.fetchall()
        
        equipment = {}
        for row in rows:
            typ = row[0]
            if typ not in equipment:
                equipment[typ] = []
            equipment[typ].append({
                "name": row[1],
                "s_max_kva": row[2],
                "max_current_a": row[3],
                "cost_eur": row[4],
                "equipment_only_cost_eur": row[5],
                "installed_cost_eur": row[6],
            })
        
        equipment["cable_costs_config"] = [
            {"name": name, "cost_eur_per_m": cost}
            for name, cost in CABLE_COST_DICT.items()
        ]
        
        result = {
            "status": "success",
            "equipment": equipment
        }
        
        cache.set("equipment_costs", result, ttl=3600)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/voltage-settings")
async def get_voltage_settings():
    """Get voltage band settings used for grid analysis"""
    cached_result = cache.get("voltage_settings")
    if cached_result:
        return cached_result
    
    result = {
        "status": "success",
        "settings": {
            "nominal_voltage_v": VN,
            "voltage_band_low_pu": V_BAND_LOW,
            "voltage_band_high_pu": V_BAND_HIGH,
            "min_voltage_v": VN * V_BAND_LOW,
            "max_voltage_v": VN * V_BAND_HIGH
        }
    }
    
    cache.set("voltage_settings", result, ttl=None)
    return result
