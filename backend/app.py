import os
import re
import pymysql
from pymysql.constants import CLIENT
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ─── Configuration from environment variables ────────────────────────────────
DB_HOST  = os.environ["DB_HOST"]
DB_PORT  = int(os.environ.get("DB_PORT", "3306"))
DB_USER  = os.environ["DB_USER"]
DB_PASS  = os.environ["DB_PASS"]
DB_NAME  = os.environ.get("DB_NAME", "shiptrack")
DB_TABLE = os.environ.get("DB_TABLE", "Database_2026")
API_KEY  = os.environ["API_KEY"]

SKIP = "-- 不修改 --"

QUERY_COLUMNS = [
    "B_L_No",
    "清关公司",
    "实际清关费_柜",
    "Container_No",
    "POD",
    "ETA",
    "货代",
    "品名",
    "资料进度",
    "明细_开单进度",
    "账单状态",
]

VALID_VALUES: dict[str, set] = {
    "资料进度": {
        "取消", "等待草稿", "等待确认B/L 草稿", "已确认B/L 草稿",
        "等待确认F. E 草稿", "等待确认草稿", "已确认所有草稿", "已发正本至清关",
    },
    "明细_开单进度": {"做明细中", "已准备", "已做明细", "已开单"},
    "账单状态": {"已出账单", "已批账单"},
}

# ─── FastAPI app ─────────────────────────────────────────────────────────────
app = FastAPI(title="Database API", docs_url=None, redoc_url=None)

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "https://localhost").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)

# ─── Auth dependency ──────────────────────────────────────────────────────────
def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ─── DB helper ────────────────────────────────────────────────────────────────
def get_connection():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        charset="utf8mb4",
        ssl={"check_hostname": False, "verify_mode": 0},
        connect_timeout=10,
        client_flag=CLIENT.FOUND_ROWS,
    )

# ─── Request models ───────────────────────────────────────────────────────────
class QueryRequest(BaseModel):
    values: list[str]

class UpdateRequest(BaseModel):
    bl_numbers: list[str]
    资料进度: Optional[str] = None
    明细_开单进度: Optional[str] = None
    账单状态: Optional[str] = None

# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/query", dependencies=[Depends(require_api_key)])
def query(req: QueryRequest):
    if not req.values:
        raise HTTPException(400, "values list is empty")
    if len(req.values) > 100:
        raise HTTPException(400, "too many values (max 100)")

    col_names   = ", ".join(f"`{c}`" for c in QUERY_COLUMNS)
    ph_bl       = ", ".join(["%s"] * len(req.values))
    like_parts  = " OR ".join(["`Container_No` LIKE %s"] * len(req.values))
    sql = (
        f"SELECT {col_names} FROM `{DB_TABLE}` "
        f"WHERE `B_L_No` IN ({ph_bl}) OR {like_parts}"
    )
    like_values = [f"%{v}%" for v in req.values]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, req.values + like_values)
            rows = cur.fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        record = {}
        for col, val in zip(QUERY_COLUMNS, row):
            if hasattr(val, "isoformat"):
                record[col] = val.isoformat()
            else:
                record[col] = str(val) if val is not None else ""
        results.append(record)

    return {"results": results, "count": len(results)}

@app.post("/update", dependencies=[Depends(require_api_key)])
def update(req: UpdateRequest):
    if not req.bl_numbers:
        raise HTTPException(400, "bl_numbers is empty")
    if len(req.bl_numbers) > 100:
        raise HTTPException(400, "too many B/L numbers (max 100)")

    candidates = {
        "资料进度": req.资料进度,
        "明细_开单进度": req.明细_开单进度,
        "账单状态": req.账单状态,
    }
    updates = {}
    for col, val in candidates.items():
        if val is None:
            continue
        if val not in VALID_VALUES[col]:
            raise HTTPException(400, f"Invalid value '{val}' for column '{col}'")
        updates[col] = val

    if not updates:
        raise HTTPException(400, "No fields to update")

    set_clause = ", ".join(f"`{col}` = %s" for col in updates)
    sql = f"UPDATE `{DB_TABLE}` SET {set_clause} WHERE `B_L_No` = %s"

    conn = get_connection()
    success, not_found, failed = [], [], []
    try:
        with conn.cursor() as cur:
            for bl in req.bl_numbers:
                try:
                    cur.execute(sql, (*updates.values(), bl))
                    if cur.rowcount == 0:
                        not_found.append(bl)
                    else:
                        success.append(bl)
                except Exception as e:
                    failed.append({"bl": bl, "error": str(e)})
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(500, f"Commit failed: {e}")
    finally:
        conn.close()

    return {"success": success, "not_found": not_found, "failed": failed}
