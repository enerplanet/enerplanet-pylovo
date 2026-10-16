"""Building management endpoints."""
import json
import traceback

from fastapi import APIRouter, HTTPException

from models import CustomBuildingRequest
from src.database.database_client import DatabaseClient
from src.ai_estimation import estimate_building_energy
from utils.helpers import get_building_type_from_class, get_building_icon

router = APIRouter(tags=["building"])


@router.post("/add-custom-building")
async def add_custom_building(payload: CustomBuildingRequest):
    """
    Add a custom building to the database for grid planning.
    The building will be included in grid generation when the area overlaps.
    """
    try:
        dbc = DatabaseClient()

        # Calculate peak load based on building type and area
        if payload.demand_energy and payload.demand_energy > 0:
            peak_load = payload.demand_energy / 8760 * 3.5
            demand_energy = payload.demand_energy
        else:
            estimate = estimate_building_energy(
                payload.f_class,
                payload.area,
                None
            )
            peak_load = estimate['peak_load_kw']
            demand_energy = estimate['yearly_demand_kwh']

        building_type = get_building_type_from_class(payload.f_class, payload.area)
        icon = payload.icon or get_building_icon(payload.f_class)

        # Ensure custom_buildings table exists
        create_table_query = """
            CREATE TABLE IF NOT EXISTS custom_buildings (
                custom_building_id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                title VARCHAR(255) NOT NULL,
                f_class VARCHAR(50) NOT NULL,
                building_type VARCHAR(50) NOT NULL,
                area FLOAT NOT NULL,
                peak_load_kw FLOAT NOT NULL,
                demand_energy FLOAT,
                geom GEOMETRY(Point, 4326),
                geom_area GEOMETRY(Polygon, 4326),
                is_public BOOLEAN DEFAULT FALSE,
                icon VARCHAR(50) DEFAULT 'building-2',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_custom_buildings_user_id ON custom_buildings(user_id);
            CREATE INDEX IF NOT EXISTS idx_custom_buildings_geom ON custom_buildings USING GIST(geom);
            CREATE INDEX IF NOT EXISTS idx_custom_buildings_geom_area ON custom_buildings USING GIST(geom_area);
            CREATE INDEX IF NOT EXISTS idx_custom_buildings_is_public ON custom_buildings(is_public);
        """
        dbc.cur.execute(create_table_query)
        dbc.conn.commit()

        # Create location sharing tables
        share_tables_query = """
            CREATE TABLE IF NOT EXISTS location_shares (
                id SERIAL PRIMARY KEY,
                location_id INTEGER NOT NULL REFERENCES custom_buildings(custom_building_id) ON DELETE CASCADE,
                user_id VARCHAR(255) NOT NULL DEFAULT '',
                email VARCHAR(255) NOT NULL,
                permission VARCHAR(32) NOT NULL DEFAULT 'view',
                shared_by VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_location_shares_location ON location_shares(location_id);
            CREATE INDEX IF NOT EXISTS idx_location_shares_user ON location_shares(user_id);
            CREATE INDEX IF NOT EXISTS idx_location_shares_email ON location_shares(email);

            CREATE TABLE IF NOT EXISTS location_workspace_shares (
                id SERIAL PRIMARY KEY,
                location_id INTEGER NOT NULL REFERENCES custom_buildings(custom_building_id) ON DELETE CASCADE,
                workspace_id INTEGER NOT NULL,
                permission VARCHAR(32) NOT NULL DEFAULT 'view',
                shared_by VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_location_workspace_shares_location ON location_workspace_shares(location_id);
            CREATE INDEX IF NOT EXISTS idx_location_workspace_shares_workspace ON location_workspace_shares(workspace_id);

            CREATE TABLE IF NOT EXISTS location_group_shares (
                id SERIAL PRIMARY KEY,
                location_id INTEGER NOT NULL REFERENCES custom_buildings(custom_building_id) ON DELETE CASCADE,
                group_id VARCHAR(255) NOT NULL,
                permission VARCHAR(32) NOT NULL DEFAULT 'view',
                shared_by VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_location_group_shares_location ON location_group_shares(location_id);
            CREATE INDEX IF NOT EXISTS idx_location_group_shares_group ON location_group_shares(group_id);
        """
        try:
            dbc.cur.execute(share_tables_query)
            dbc.conn.commit()
        except Exception:
            dbc.conn.rollback()

        # Add columns if they don't exist
        alter_queries = [
            "ALTER TABLE custom_buildings ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE custom_buildings ADD COLUMN IF NOT EXISTS icon VARCHAR(50) DEFAULT 'building-2';"
        ]
        for q in alter_queries:
            try:
                dbc.cur.execute(q)
                dbc.conn.commit()
            except Exception:
                dbc.conn.rollback()

        # Insert the custom building
        insert_query = """
            INSERT INTO custom_buildings
            (user_id, title, f_class, building_type, area, peak_load_kw, demand_energy, geom, geom_area, is_public, icon)
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                %s, %s
            )
            RETURNING custom_building_id;
        """
        dbc.cur.execute(insert_query, (
            payload.user_id,
            payload.title,
            payload.f_class,
            building_type,
            payload.area,
            peak_load,
            payload.demand_energy,
            json.dumps(payload.geometry),
            json.dumps(payload.geometry_area),
            payload.is_public,
            icon
        ))
        building_id = dbc.cur.fetchone()[0]
        dbc.conn.commit()

        return {
            "status": "success",
            "building": {
                "id": building_id,
                "user_id": payload.user_id,
                "title": payload.title,
                "f_class": payload.f_class,
                "building_type": building_type,
                "icon": icon,
                "is_public": payload.is_public,
                "area": payload.area,
                "peak_load_kw": round(peak_load, 2),
                "demand_energy": payload.demand_energy
            }
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/custom-buildings/{user_id}")
async def get_custom_buildings(user_id: str):
    """Get a user's own custom buildings; other users' buildings are never returned."""
    try:
        import time as t
        start_time = t.time()
        print(f"[Custom Buildings] Starting query for user {user_id}")

        dbc = DatabaseClient()
        print(f"[Custom Buildings] DB connected in {t.time() - start_time:.2f}s")

        check_query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'custom_buildings'
            );
        """
        dbc.cur.execute(check_query)
        if not dbc.cur.fetchone()[0]:
            return {"status": "success", "buildings": [], "count": 0}

        query = """
            SELECT
                custom_building_id,
                user_id,
                title,
                f_class,
                building_type,
                area,
                peak_load_kw,
                demand_energy,
                ST_AsGeoJSON(geom, 6) as geometry,
                ST_AsGeoJSON(geom_area, 6) as geometry_area,
                COALESCE(is_public, FALSE) as is_public,
                COALESCE(icon, 'building-2') as icon,
                created_at
            FROM custom_buildings
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 100;
        """
        print(f"[Custom Buildings] Executing query...")
        dbc.cur.execute(query, (user_id,))
        print(f"[Custom Buildings] Query executed in {t.time() - start_time:.2f}s")

        rows = dbc.cur.fetchall()
        print(f"[Custom Buildings] Fetched {len(rows)} rows in {t.time() - start_time:.2f}s")

        buildings = []
        for row in rows:
            buildings.append({
                "id": row[0],
                "user_id": row[1],
                "title": row[2],
                "f_class": row[3],
                "building_type": row[4],
                "area": row[5],
                "peak_load_kw": row[6],
                "demand_energy": row[7],
                "geometry": json.loads(row[8]) if row[8] else None,
                "geometry_area": json.loads(row[9]) if row[9] else None,
                "is_public": row[10],
                "icon": row[11],
                "created_at": row[12].isoformat() if row[12] else None,
                "is_owner": row[1] == user_id
            })

        return {
            "status": "success",
            "buildings": buildings,
            "count": len(buildings)
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/custom-buildings/{building_id}")
async def delete_custom_building(building_id: int, user_id: str):
    """Delete a custom building."""
    try:
        dbc = DatabaseClient()

        query = """
            DELETE FROM custom_buildings
            WHERE custom_building_id = %s AND user_id = %s
            RETURNING custom_building_id;
        """
        dbc.cur.execute(query, (building_id, user_id))
        deleted = dbc.cur.fetchone()
        dbc.conn.commit()

        if not deleted:
            raise HTTPException(status_code=404, detail="Building not found or unauthorized")

        return {"status": "success", "deleted_id": building_id}

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
