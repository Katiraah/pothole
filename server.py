# server.py
from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
import psycopg2.extras

app = Flask(__name__)
CORS(app)
import os

DB_CONFIG = {
    "host": os.environ.get("DB_HOST"),
    "dbname": os.environ.get("DB_NAME"),
    "user": os.environ.get("DB_USER"),
    "password": os.environ.get("DB_PASSWORD"),
    "port": os.environ.get("DB_PORT"),
    "sslmode": os.environ.get("DB_SSLMODE")
}


def get_connection():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        return None

@app.route("/api/roads")
def get_roads():
    conn = get_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Query: Get latest pothole counts per road and calculate RCI with LINEAR SCALE
        query = """
            WITH latest_inspections AS (
                -- Find the latest date recorded for each specific road
                SELECT road_gid, MAX(timestamp::date) AS latest_date
                FROM parc.roadpothole
                GROUP BY road_gid
            ),
            current_counts AS (
                -- Count potholes for that specific latest date
                SELECT 
                    p.road_gid, 
                    COUNT(p.id) AS pothole_count
                FROM parc.roadpothole p
                JOIN latest_inspections li 
                    ON p.road_gid = li.road_gid 
                    AND p.timestamp::date = li.latest_date
                GROUP BY p.road_gid
            )
            SELECT
                r.gid,
                r.roadname,
                r.roadtype,
                r.roadagency,
                r.roadcode,
                r.roadclass,
                r.county,
                ROUND(r.length_km::numeric, 2) AS length_km,
                ROUND(r.length_m::numeric, 0) AS length_m,
                COALESCE(cc.pothole_count, 0) AS pothole_count,
                -- Density Calculation (potholes per km)
                CASE
                    WHEN r.length_km > 0 
                    THEN ROUND((COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0))::numeric, 2)
                    ELSE 0
                END AS density_per_km,
                -- RCI Calculation: LINEAR SCALE (0-10 potholes/km maps to 1.0-0.0)
                -- Formula: RCI = MAX(0, 1.0 - (density / 10))
                CASE
                    WHEN r.length_km > 0 THEN
                        ROUND(
                            GREATEST(0, 1.0 - (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) / 10.0)::numeric,
                            2
                        )
                    ELSE 1.0
                END AS rci_value,
                -- Condition Label based on RCI
                CASE
                    WHEN r.length_km > 0 AND (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) = 0 THEN 'Perfect'
                    WHEN r.length_km > 0 AND (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) <= 2.0 THEN 'Good'
                    WHEN r.length_km > 0 AND (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) <= 6.0 THEN 'Average'
                    ELSE 'Poor'
                END AS condition,
                -- Color Mapping based on RCI
                CASE
                    WHEN r.length_km > 0 AND (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) = 0 THEN '#228B22'  -- Perfect: Forest Green
                    WHEN r.length_km > 0 AND (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) <= 2.0 THEN '#32CD32'  -- Good: Lime Green
                    WHEN r.length_km > 0 AND (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) <= 6.0 THEN '#FFA500'  -- Average: Orange
                    ELSE '#FF0000'  -- Poor: Red
                END AS road_color,
                ST_AsGeoJSON(r.geom)::json AS geometry
            FROM parc.jujaroads r
            LEFT JOIN current_counts cc ON r.gid = cc.road_gid
            WHERE r.geom IS NOT NULL
            ORDER BY r.roadname ASC
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "roads": [dict(r) for r in rows]})
        
    except Exception as e:
        print(f"❌ Error in get_roads: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/stats")
def get_stats():
    conn = get_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # 1. Total Roads
        cur.execute("SELECT COUNT(*) as count FROM parc.jujaroads WHERE geom IS NOT NULL")
        total_roads = cur.fetchone()['count']
        
        # 2. Stats based on Per-Road Max Date with NEW RCI LOGIC
        stats_query = """
            WITH latest_inspections AS (
                SELECT road_gid, MAX(timestamp::date) AS latest_date
                FROM parc.roadpothole
                GROUP BY road_gid
            ),
            current_counts AS (
                SELECT 
                    p.road_gid, 
                    COUNT(p.id) AS pothole_count
                FROM parc.roadpothole p
                JOIN latest_inspections li 
                    ON p.road_gid = li.road_gid 
                    AND p.timestamp::date = li.latest_date
                GROUP BY p.road_gid
            )
            SELECT 
                SUM(COALESCE(cc.pothole_count, 0)) as total_potholes,
                -- Count Poor Roads (density > 6.0 potholes/km, RCI < 0.4)
                COUNT(CASE WHEN (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) > 6.0 THEN 1 END) as poor_roads,
                -- Count Average Roads (density > 2.0 and <= 6.0, RCI 0.4-0.8)
                COUNT(CASE WHEN (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) > 2.0 AND (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) <= 6.0 THEN 1 END) as average_roads,
                -- Count Good Roads (density > 0 and <= 2.0, RCI 0.8-1.0)
                COUNT(CASE WHEN COALESCE(cc.pothole_count, 0) > 0 AND (COALESCE(cc.pothole_count, 0) / NULLIF(r.length_km, 0)) <= 2.0 THEN 1 END) as good_roads,
                -- Count Perfect Roads (density = 0, RCI = 1.0)
                COUNT(CASE WHEN COALESCE(cc.pothole_count, 0) = 0 THEN 1 END) as perfect_roads
            FROM parc.jujaroads r
            LEFT JOIN current_counts cc ON r.gid = cc.road_gid
            WHERE r.length_km > 0
        """
        
        cur.execute(stats_query)
        stats = cur.fetchone()
        
        cur.close()
        conn.close()
        
        response_stats = {
            "total_roads": total_roads or 0,
            "total_potholes": stats['total_potholes'] or 0,
            "poor_roads": stats['poor_roads'] or 0,
            "average_roads": stats['average_roads'] or 0,
            "good_roads": (stats['good_roads'] or 0) + (stats['perfect_roads'] or 0)  # Combine good and perfect
        }
        
        print(f"✅ Stats calculated (Linear RCI Logic): {response_stats}")
        return jsonify({"success": True, "stats": response_stats})
        
    except Exception as e:
        print(f"❌ Error in get_stats: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/juja_boundary")
def get_juja_boundary():
    conn = get_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ST_AsGeoJSON(ST_Collect(geom))::json as boundary_geojson
            FROM parc.juja
            WHERE geom IS NOT NULL
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()
        
        if row and row['boundary_geojson']:
            return jsonify({"success": True, "boundary": row['boundary_geojson']})
        return jsonify({"success": False, "error": "No boundary data found"})
        
    except Exception as e:
        print(f"❌ Error in get_juja_boundary: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/timeline")
def get_timeline():
    try:
        road_gid = request.args.get('road_gid')
        date_from = request.args.get('from', '2020-01-01')
        date_to = request.args.get('to', '2099-12-31')
        
        if not road_gid:
            return jsonify({"success": False, "error": "road_gid is required"}), 400

        conn = get_connection()
        if not conn:
            return jsonify({"success": False, "error": "Database connection failed"}), 500
            
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cur.execute("""
            SELECT 
                p.timestamp::date AS period,
                COUNT(p.id) AS pothole_count,
                -- Calculate RCI for each timeline point
                CASE
                    WHEN r.length_km > 0 THEN
                        ROUND(
                            GREATEST(0, 1.0 - (COUNT(p.id)::float / r.length_km) / 10.0)::numeric,
                            2
                        )
                    ELSE 1.0
                END AS rci_value
            FROM parc.roadpothole p
            JOIN parc.jujaroads r ON p.road_gid = r.gid
            WHERE p.road_gid = %s
              AND p.timestamp >= %s
              AND p.timestamp <= %s
            GROUP BY p.timestamp::date, r.length_km
            ORDER BY period ASC
        """, (road_gid, date_from, date_to))
        timeline_points = cur.fetchall()
        
        cur.execute("""
            SELECT COUNT(p.id) AS current_potholes
            FROM parc.roadpothole p
            WHERE p.road_gid = %s
              AND p.timestamp >= %s
              AND p.timestamp <= %s
              AND p.timestamp::date = (
                  SELECT MAX(timestamp::date)
                  FROM parc.roadpothole
                  WHERE road_gid = %s
                    AND timestamp >= %s
                    AND timestamp <= %s
              )
        """, (road_gid, date_from, date_to, road_gid, date_from, date_to))
        
        current_result = cur.fetchone()
        current_potholes = current_result['current_potholes'] if current_result else 0
        
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "points": [dict(r) for r in timeline_points],
            "current_potholes": current_potholes
        })
        
    except Exception as e:
        print(f"❌ Error in get_timeline: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

(* if __name__ == "__main__":
    print("🚀 Starting Road Vision API Server...")
    print("📊 Server running on http://localhost:5000")
    app.run(debug=True, port=5000, threaded=True)
 *)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))  # Render gives you PORT
    app.run(host="0.0.0.0", port=port)




    
