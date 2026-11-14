"""FastAPI application: batch ingest + persisted-geometry queries."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import accounting, store
from .config import settings
from .db import init_db, read_tx
from .overlap import OverlapError
from .schemas import (
    BatchIn,
    BatchResult,
    HealthResponse,
    OverlapRequest,
    OverlapResponse,
    PointIn,
    PointsResponse,
    RoadOut,
    RoadsResponse,
    StateResponse,
)

DESCRIPTION = """
测绘队共享测点 -> 路段线形加工服务。

* `POST /api/v1/batches` 一次事务内完成测点/路段的**批量新增、更正、删除**。
* 线形在提交时按路段引用的测点顺序组装并**持久保存**为 GeoJSON
  `LineString`；查询接口只读已持久化的表，不现场组线。
* 任一被引用测点缺失时**不输出残线**，在 `missing_point_ids` 里列明缺失
  的测点 ID；补齐测点后下一次批次自动恢复线形。
* 测点更正/删除、路段引用变更会影响的所有路段在同一事务内整批重算，
  失败整体回滚，数据库保持原状。
* `GET /api/v1/state` 在**一个读快照**里返回全部测点原值与已持久化线形，
  不会出现"新坐标 + 旧线形"的混合视图。

组线是唯一可能较慢的步骤，它发生在事务之外；提交只是很短的
`BEGIN IMMEDIATE`。两个 API 进程共用同一个 SQLite 文件（WAL 模式），
写者互斥、读者不阻塞，提交前还会复核快照代号，因此并发改线不会漏更新、
也不会写回旧坐标。

## 重合里程核算

`POST /api/v1/overlap-accounting` 输入 2～12 个**完整路段 ID**，在
**同一个已提交快照**里读取它们持久化的 GeoJSON 线形，只把经纬度平面上
**精确共线且重合长度大于零**的部分视为重复登记：反向录入、不同顶点拆分
均能识别，三条以上路段的共同覆盖只计一次，单点相交/端点相触不算。

路段按 ID 升序归属共有线段，返回每条路实际计入的米数、被前序路段覆盖的
线段及去重总米数；所有长度按 WGS84 测地线计算。缺点或不存在的路段会让
整次核算失败（422），不会混用不同提交状态的数据。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Municipal Road Geometry Service",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    with read_tx() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.execute("SELECT count(*) FROM points").fetchone()
    return HealthResponse(status="ok", db=settings.db_path, journal_mode=mode)


@app.post("/api/v1/batches", response_model=BatchResult, status_code=201)
def post_batch(
    batch: BatchIn,
    pause_ms: int = Query(
        default=0,
        ge=0,
        le=30_000,
        description="演示用：规划完成后、进入写事务前暂停指定毫秒；证明组线期间"
        "不持写锁，另一进程仍可入库。",
    ),
    fail: bool = Query(
        default=False, description="演示用：在全部写入之后、提交之前注入异常以验证整批回滚。"
    ),
):
    try:
        result, attempts = store.execute_batch(
            batch, pause_ms=pause_ms, inject_failure=fail
        )
    except store.ConcurrentConflict as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except store.DemoFailure as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database busy: {exc}") from exc
    return result


@app.get("/api/v1/state", response_model=StateResponse)
def get_state() -> StateResponse:
    """全部测点原值与全部已持久化线形，来自**同一个读快照**。

    返回的每个坐标与每条线形都属于同一已提交状态，不会出现“新坐标 + 旧线形”。
    """
    with read_tx() as conn:
        generation = store.get_generation(conn)
        points = store.list_points(conn)
        roads = store.list_roads(conn)
    return StateResponse(generation=generation, points=points, roads=roads)


@app.get("/api/v1/points", response_model=PointsResponse)
def get_points() -> PointsResponse:
    with read_tx() as conn:
        points = store.list_points(conn)
    return PointsResponse(points=points, total=len(points))


@app.get("/api/v1/points/{point_id}", response_model=PointIn)
def get_point(point_id: str) -> PointIn:
    with read_tx() as conn:
        point = store.get_point(conn, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"point {point_id!r} not found")
    return point


@app.get("/api/v1/roads", response_model=RoadsResponse)
def get_roads() -> RoadsResponse:
    with read_tx() as conn:
        roads = store.list_roads(conn)
    return RoadsResponse(roads=roads, total=len(roads))


@app.get("/api/v1/roads/{road_id}", response_model=RoadOut)
def get_road(road_id: str) -> RoadOut:
    with read_tx() as conn:
        roads = store.list_roads(conn, road_id=road_id)
    if not roads:
        raise HTTPException(status_code=404, detail=f"road {road_id!r} not found")
    return roads[0]


@app.post("/api/v1/overlap-accounting", response_model=OverlapResponse)
def post_overlap_accounting(req: OverlapRequest) -> OverlapResponse:
    """重合里程核算：同一读快照内的精确共线去重，长度按 WGS84 测地线计米。

    任一路段不存在、缺点（incomplete）、顶点超过 40 个或跨越 180° 经线，
    整次核算失败并在 ``detail.invalid_roads`` 里逐条列明原因（HTTP 422）。
    """
    try:
        return accounting.run_accounting(req)
    except OverlapError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "overlap accounting failed: every road must exist and "
                "have a complete persisted LineString",
                "invalid_roads": [
                    {"road_id": rid, "reason": why}
                    for rid, why in exc.invalid.items()
                ],
            },
        ) from exc
