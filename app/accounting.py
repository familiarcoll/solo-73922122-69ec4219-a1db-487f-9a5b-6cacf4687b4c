"""重合里程核算的请求编排：一个读快照 -> 校验 -> 纯几何核算。"""
from __future__ import annotations

from . import overlap, store
from .db import read_tx
from .overlap import InputRoad, OverlapError, round_meters
from .schemas import (
    CoveredSegment,
    OverlapRequest,
    OverlapResponse,
    OverlapRoadResult,
)


def _to_input(road_id: str, geometry: dict) -> InputRoad:
    coords = [(float(x), float(y)) for x, y in geometry["coordinates"]]
    return InputRoad(road_id=road_id, coords=coords)


def run_accounting(req: OverlapRequest) -> OverlapResponse:
    """读取同一已提交快照并完成重合里程核算。

    任一路段不存在、缺点（incomplete）、顶点超限或跨 180° 经线都会让整次
    核算失败（抛出 :class:`overlap.OverlapError`，由路由层转 422）。
    """
    with read_tx() as conn:
        generation, rows = store.fetch_roads_snapshot(conn, req.road_ids)

    invalid: dict[str, str] = {}
    usable: list[InputRoad] = []
    for rid in req.road_ids:
        row = rows.get(rid)
        if row is None:
            invalid[rid] = "road does not exist"
            continue
        reason = overlap.validate_road(rid, row["geometry"], row["missing"])
        if reason is not None:
            invalid[rid] = reason
            continue
        usable.append(_to_input(rid, row["geometry"]))

    if invalid:
        # 缺点或不存在的路段使整次核算失败：不返回任何部分结果
        raise OverlapError(invalid)

    accounts, _deduped = overlap.account_overlaps(usable)

    road_results: list[OverlapRoadResult] = []
    duplicate_meters = 0.0
    counted_sum = 0.0
    for acc in accounts:
        segments = [
            CoveredSegment(
                covered_by_road_id=p.covered_by,
                coordinates=[[lon, lat] for lon, lat in p.coordinates],
                meters=round_meters(p.meters),
            )
            for p in acc.covered_pieces
        ]
        duplicate_meters += acc.covered_meters
        covered_rounded = round_meters(acc.covered_meters)
        total_rounded = round_meters(acc.total_meters)
        # 用未舍入全长减已舍入覆盖长再舍入，保证输出端
        # counted + covered == total 严格闭合（同为 6 位小数精度）
        counted_rounded = round_meters(acc.total_meters - acc.covered_meters)
        counted_sum += acc.total_meters - acc.covered_meters
        road_results.append(
            OverlapRoadResult(
                road_id=acc.road_id,
                total_meters=total_rounded,
                counted_meters=counted_rounded,
                covered_meters=covered_rounded,
                covered_segments=segments,
            )
        )

    return OverlapResponse(
        generation=generation,
        roads=road_results,
        duplicate_meters=round_meters(duplicate_meters),
        deduped_total_meters=round_meters(counted_sum),
    )
