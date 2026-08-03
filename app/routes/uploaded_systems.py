"""
uploaded_systems.py
===================
System Comparison feature — upload finished equity + tradelist CSVs, name each,
list/delete them, and compare several at once: an overlaid equity + drawdown
chart plus a combined metrics-and-yearly table. Bypasses the backtest engine
(no Java) AND the database — this is a simple, self-contained feature whose
"library" is just a folder on disk.

Storage (no DB)
---------------
Each system is one folder under settings.UPLOADED_SYSTEMS_PATH (compareEquities):

    compareEquities/<id>/equity.csv      raw uploaded equity CSV
    compareEquities/<id>/tradelist.csv   raw uploaded tradelist CSV
    compareEquities/<id>/meta.json       {id, name, starting_capital,
                                          start_date, end_date, n_trades,
                                          created_at}

<id> is a small integer (max existing + 1), so the API and folders are keyed by
id and the user-facing name can contain anything (e.g. 'pull_back_500_5%stp').
Listing = scan the folders' meta.json; delete = remove the folder. Metrics are
computed on demand in app.services.uploaded_systems_compute, which reuses
calculate_performance so uploaded systems are measured exactly like engine
strategies.

Endpoints
---------
POST   /api/uploaded-systems            — multipart upload (name + 2 CSVs)
GET    /api/uploaded-systems            — list saved systems
DELETE /api/uploaded-systems/{id}       — remove one (its folder)
GET    /api/uploaded-systems/compare    — ?ids=1,2,3[&scale=indexed|absolute]
"""

import io
import json
import logging
import os
import shutil
from datetime import datetime
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from plotly.utils import PlotlyJSONEncoder
from starlette.responses import JSONResponse

from app.Settings import settings
from app.services.uploaded_systems_compute import (
    build_compare_payload,
    normalize_equity,
    normalize_trades,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/uploaded-systems", tags=["uploaded_systems"])

_META = "meta.json"

# Default starting capital when an upload doesn't specify one. Fixed (not the
# first equity value) so the Profit view always uses the same 100k baseline and
# the chart doesn't shift per file.
DEFAULT_STARTING_CAPITAL = 100_000.0


# ── filesystem helpers ──────────────────────────────────────────────────────

def _root() -> str:
    root = settings.UPLOADED_SYSTEMS_PATH
    os.makedirs(root, exist_ok=True)
    return root


def _dir(system_id: int) -> str:
    return os.path.join(_root(), str(system_id))


def _meta_path(system_id: int) -> str:
    return os.path.join(_dir(system_id), _META)


def _read_meta(system_id: int) -> Optional[dict]:
    path = _meta_path(system_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("Could not read meta for system %s", system_id)
        return None


def _list_meta() -> list:
    root = _root()
    out = []
    for name in os.listdir(root):
        if not name.isdigit():
            continue
        if not os.path.isdir(os.path.join(root, name)):
            continue
        meta = _read_meta(int(name))
        if meta:
            out.append(meta)
    out.sort(key=lambda m: m.get("created_at") or "")
    return out


def _next_id() -> int:
    root = _root()
    ids = [
        int(name) for name in os.listdir(root)
        if name.isdigit() and os.path.isdir(os.path.join(root, name))
    ]
    return (max(ids) + 1) if ids else 1


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
def upload_system(
    name: str = Form(...),
    starting_capital: Optional[float] = Form(None),
    equity: UploadFile = File(...),
    tradelist: UploadFile = File(...),
):
    """Validate both CSVs, store them under compareEquities/<id>/, write meta."""
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A system name is required.")
    if any(m.get("name") == name for m in _list_meta()):
        raise HTTPException(status_code=400,
                            detail=f"A system named '{name}' already exists.")

    try:
        eq_bytes = equity.file.read()
        tr_bytes = tradelist.file.read()
    finally:
        equity.file.close()
        tradelist.file.close()

    # Validate by normalising (loud, descriptive failure on a bad file).
    try:
        eq_df = normalize_equity(pd.read_csv(io.BytesIO(eq_bytes)))
        tr_df = normalize_trades(pd.read_csv(io.BytesIO(tr_bytes)))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read CSV: {e}")

    cap = float(starting_capital) if starting_capital else DEFAULT_STARTING_CAPITAL

    system_id = _next_id()
    d = _dir(system_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "equity.csv"), "wb") as f:
        f.write(eq_bytes)
    with open(os.path.join(d, "tradelist.csv"), "wb") as f:
        f.write(tr_bytes)

    meta = {
        "id": system_id,
        "name": name,
        "starting_capital": cap,
        "start_date": eq_df.index[0].date().isoformat(),
        "end_date": eq_df.index[-1].date().isoformat(),
        "n_trades": int(len(tr_df)),
        "created_at": datetime.now().isoformat(),
    }
    with open(_meta_path(system_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return meta


@router.get("")
def list_systems():
    return _list_meta()


@router.delete("/{system_id}")
def delete_system(system_id: int):
    d = _dir(system_id)
    if not os.path.isdir(d):
        raise HTTPException(status_code=404, detail="System not found")
    shutil.rmtree(d, ignore_errors=True)
    return {"deleted": system_id}


@router.patch("/{system_id}")
def update_system(system_id: int, starting_capital: float = Body(..., embed=True)):
    """Change a stored system's starting capital — the amount subtracted from
    equity in the Profit view. Rewrites only meta.json (CSVs untouched)."""
    meta = _read_meta(system_id)
    if not meta:
        raise HTTPException(status_code=404, detail="System not found")
    try:
        cap = float(starting_capital)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="starting_capital must be a number")
    if cap <= 0:
        raise HTTPException(status_code=400, detail="starting_capital must be positive")
    meta["starting_capital"] = cap
    with open(_meta_path(system_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return meta


@router.get("/compare")
def compare_systems(
    ids: str = Query(..., description="Comma-separated system ids, e.g. 1,2,3"),
    scale: str = Query("indexed", pattern="^(indexed|absolute)$"),
):
    """Overlaid equity + drawdown figure plus the combined metrics/yearly table
    for the selected systems, in the order requested."""
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be comma-separated integers")
    if not id_list:
        raise HTTPException(status_code=400, detail="Provide at least one system id")

    loaded = []
    for sid in id_list:  # preserve selection order
        meta = _read_meta(sid)
        d = _dir(sid)
        eq_path = os.path.join(d, "equity.csv")
        tr_path = os.path.join(d, "tradelist.csv")
        if not meta or not (os.path.exists(eq_path) and os.path.exists(tr_path)):
            continue
        try:
            eq_df = normalize_equity(pd.read_csv(eq_path))
            tr_df = normalize_trades(pd.read_csv(tr_path))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"{meta.get('name', sid)}: {e}")
        loaded.append({
            "id": sid,
            "name": meta["name"],
            "eq_df": eq_df,
            "tr_df": tr_df,
            "starting_capital": (meta.get("starting_capital")
                                 or DEFAULT_STARTING_CAPITAL),
        })

    if not loaded:
        raise HTTPException(status_code=404, detail="No matching systems found")

    payload = build_compare_payload(loaded, scale)
    return JSONResponse(content=json.loads(json.dumps(payload, cls=PlotlyJSONEncoder)))