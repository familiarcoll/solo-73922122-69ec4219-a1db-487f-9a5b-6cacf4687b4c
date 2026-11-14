"""重合里程核算 API 的功能测试。

覆盖：精确共线重合、反向录入、不同顶点拆分、三路以上覆盖只计一次、
单点相交不算、平行不共线不算、缺点/不存在/顶点超限/跨 180° 整次失败、
请求校验、WGS84 测地线长度与快照一致性。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "db_path", str(tmp_path / "gis.db"))
    with TestClient(app) as c:
        yield c


def post_batch(client, body):
    r = client.post("/api/v1/batches", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def account(client, ids):
    return client.post("/api/v1/overlap-accounting", json={"road_ids": ids})


def seed_roads(client, roads: dict[str, list[tuple[float, float]]]):
    """用一组 point id 直接落库若干 LineString 路段。"""
    points, road_refs = [], []
    for rid, coords in roads.items():
        pids = [f"{rid}_P{i}" for i in range(len(coords))]
        for pid, (lon, lat) in zip(pids, coords):
            points.append({"id": pid, "lon": lon, "lat": lat})
        road_refs.append({"id": rid, "point_ids": pids})
    post_batch(client, {"points": points, "roads": road_refs})


# ---------------------------------------------------------------------------
# 请求校验
# ---------------------------------------------------------------------------


def test_rejects_wrong_cardinality_and_dupes(client):
    seed_roads(client, {"R1": [(0, 30), (0.01, 30)], "R2": [(0, 30), (0.01, 31)]})
    r = account(client, ["R1"])
    assert r.status_code == 422
    r = account(client, ["R1", "R1"])
    assert r.status_code == 422
    r = account(client, [f"X{i}" for i in range(13)])
    assert r.status_code == 422


def test_nonexistent_and_incomplete_roads_fail_the_whole_accounting(client):
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.01, 30.0)],
            "R2": [(0.01, 30.0), (0.02, 30.0)],
        },
    )
    # 删掉 R2 引用的测点 -> R2 变 incomplete
    missing_pid = client.get("/api/v1/roads/R2").json()["point_ids"][0]
    post_batch(client, {"delete_points": [missing_pid]})

    r = account(client, ["R1", "R2", "GHOST"])
    assert r.status_code == 422
    detail = r.json()["detail"]
    invalid = {item["road_id"]: item["reason"] for item in detail["invalid_roads"]}
    assert set(invalid) == {"R2", "GHOST"}
    assert "incomplete" in invalid["R2"]
    assert "does not exist" in invalid["GHOST"]
    # 整次失败：不返回任何路段结果
    assert "roads" not in r.json()


# ---------------------------------------------------------------------------
# 几何判定
# ---------------------------------------------------------------------------


def test_basic_collinear_overlap_and_wgs84_length(client):
    # 0.01° 经度在纬度 30° 的测地线长 ≈ 111319.9 * cos(30°) * 0.01
    seed_roads(
        client,
        {
            "R1": [(0.00, 30.0), (0.01, 30.0), (0.02, 30.0), (0.03, 30.0)],
            "R2": [(0.02, 30.0), (0.03, 30.0), (0.04, 30.0), (0.05, 30.0)],
        },
    )
    r = account(client, ["R1", "R2"])
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {x["road_id"]: x for x in body["roads"]}

    assert body["generation"] == client.get("/api/v1/state").json()["generation"]

    r1, r2 = by_id["R1"], by_id["R2"]
    assert r1["covered_meters"] == 0.0
    assert r1["counted_meters"] == r1["total_meters"]
    assert r1["covered_segments"] == []

    # R2 的第一段 (0.02->0.03) 与 R1 重合
    assert len(r2["covered_segments"]) == 1
    seg = r2["covered_segments"][0]
    assert seg["covered_by_road_id"] == "R1"
    assert seg["coordinates"] == [[0.02, 30.0], [0.03, 30.0]]
    expected_m = pytest.approx(964.862802, abs=1e-4)
    assert seg["meters"] == expected_m
    assert r2["covered_meters"] == expected_m
    assert r2["counted_meters"] == pytest.approx(
        r2["total_meters"] - r2["covered_meters"], abs=2e-6
    )
    assert body["deduped_total_meters"] == pytest.approx(
        r1["counted_meters"] + r2["counted_meters"], abs=2e-6
    )
    assert body["duplicate_meters"] == r2["covered_meters"]


def test_reverse_direction_is_detected(client):
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.01, 30.0), (0.02, 30.0)],
            "R2": [(0.02, 30.0), (0.01, 30.0), (0.0, 30.0)],  # 完全反向
        },
    )
    r = account(client, ["R1", "R2"])
    body = r.json()
    r2 = next(x for x in body["roads"] if x["road_id"] == "R2")
    assert r2["counted_meters"] == 0.0
    assert r2["covered_meters"] == pytest.approx(r2["total_meters"], abs=2e-6)
    # 坐标按 R2 自身顶点顺序（反向）输出
    assert r2["covered_segments"][0]["coordinates"][0] == [0.02, 30.0]
    assert r2["covered_segments"][-1]["coordinates"][-1] == [0.0, 30.0]


def test_different_vertex_splits_are_detected(client):
    # 同一条直线，R1 顶点稀疏，R2 在内部额外插点
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.04, 30.0)],
            "R2": [
                (0.01, 30.0),
                (0.02, 30.0),
                (0.025, 30.0),
                (0.03, 30.0),
                (0.05, 30.0),
            ],
        },
    )
    r = account(client, ["R1", "R2"])
    r2 = next(x for x in r.json()["roads"] if x["road_id"] == "R2")
    # 重合区间 0.01 -> 0.04，归属 R1；R2 只剩 0.04->0.05 计入
    assert len(r2["covered_segments"]) == 1
    seg = r2["covered_segments"][0]
    assert seg["covered_by_road_id"] == "R1"
    assert seg["coordinates"][0] == [0.01, 30.0]
    assert seg["coordinates"][-1] == [0.04, 30.0]
    # 内部原有顶点应保留在输出坐标里
    assert [0.02, 30.0] in seg["coordinates"]
    assert [0.03, 30.0] in seg["coordinates"]


def test_three_way_cover_counted_once_and_attributed_to_earliest(client):
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.03, 30.0)],
            "R2": [(0.01, 30.0), (0.04, 30.0)],
            "R3": [(0.02, 30.0), (0.05, 30.0)],
        },
    )
    r = account(client, ["R1", "R2", "R3"])
    body = r.json()
    by_id = {x["road_id"]: x for x in body["roads"]}

    # R3: 0.02-0.03 最早由 R1 覆盖；0.03-0.04 最早由 R2 覆盖；0.04-0.05 独有
    r3 = by_id["R3"]
    owners = [(s["covered_by_road_id"], s["coordinates"][0][0], s["coordinates"][-1][0])
              for s in r3["covered_segments"]]
    assert ("R1", 0.02, 0.03) in owners
    assert ("R2", 0.03, 0.04) in owners
    # 0.02-0.03 同时被 R1、R2 覆盖，只出现一次
    assert sum(1 for s in r3["covered_segments"] if s["covered_by_road_id"] == "R1") == 1

    # 去重总量 = 并集 0.00 -> 0.05 的测地线长
    from app.overlap import geodesic_length

    expected_union = geodesic_length([(0.0, 30.0), (0.05, 30.0)])
    assert body["deduped_total_meters"] == pytest.approx(expected_union, abs=1e-4)
    # 账面闭合：每条路 counted + covered == total（输出为 6 位小数）
    for road in body["roads"]:
        assert road["counted_meters"] + road["covered_meters"] == pytest.approx(
            road["total_meters"], abs=2e-6
        )


def test_point_touch_and_endpoint_contact_are_not_duplicates(client):
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.01, 30.0)],
            "R2": [(0.01, 30.0), (0.02, 30.0)],   # 端点相触
            "R3": [(0.01, 30.0), (0.01, 30.01)],  # 仅单点相交、不同方向
        },
    )
    r = account(client, ["R1", "R2", "R3"])
    assert r.status_code == 200, r.text
    for road in r.json()["roads"]:
        assert road["covered_meters"] == 0.0
        assert road["counted_meters"] == road["total_meters"]


def test_parallel_distinct_lines_are_not_collinear(client):
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.02, 30.0)],
            "R2": [(0.0, 30.0005), (0.02, 30.0005)],  # 平行但偏离约 55 米
        },
    )
    r = account(client, ["R1", "R2"])
    r2 = next(x for x in r.json()["roads"] if x["road_id"] == "R2")
    assert r2["covered_meters"] == 0.0


def test_non_collinear_bend_has_no_overlap(client):
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.02, 30.0)],
            "R2": [(0.0, 30.0), (0.02, 30.002)],  # 同起点但有折角
        },
    )
    r = account(client, ["R1", "R2"])
    r2 = next(x for x in r.json()["roads"] if x["road_id"] == "R2")
    assert r2["covered_meters"] == 0.0


def test_results_sorted_by_road_id_and_partial_overlap_pieces(client):
    seed_roads(
        client,
        {
            "ZB": [(0.01, 30.0), (0.02, 30.0)],
            "ZA": [(0.0, 30.0), (0.03, 30.0)],
        },
    )
    r = account(client, ["ZB", "ZA"])  # 乱序输入
    body = r.json()
    assert [x["road_id"] for x in body["roads"]] == ["ZA", "ZB"]
    zb = next(x for x in body["roads"] if x["road_id"] == "ZB")
    assert zb["counted_meters"] == 0.0
    assert zb["covered_segments"][0]["covered_by_road_id"] == "ZA"


# ---------------------------------------------------------------------------
# 顶点上限与 180° 经线
# ---------------------------------------------------------------------------


def test_road_with_more_than_40_vertices_fails_accounting(client):
    coords41 = [(i * 0.001, 30.0) for i in range(41)]
    seed_roads(client, {"BIG": coords41})
    seed_roads(client, {"R1": [(0.0, 30.0), (0.01, 30.0)]})
    r = account(client, ["BIG", "R1"])
    assert r.status_code == 422
    invalid = {i["road_id"]: i["reason"] for i in r.json()["detail"]["invalid_roads"]}
    assert "BIG" in invalid and "40" in invalid["BIG"]
    # 原入库功能不受影响，路段仍然可读
    assert client.get("/api/v1/roads/BIG").status_code == 200


def test_antimeridian_crossing_road_fails_accounting(client):
    seed_roads(
        client,
        {
            "CROSS": [(179.995, 30.0), (-179.995, 30.0)],  # 相邻经度差 > 180
            "R1": [(0.0, 30.0), (0.01, 30.0)],
        },
    )
    r = account(client, ["CROSS", "R1"])
    assert r.status_code == 422
    invalid = {i["road_id"]: i["reason"] for i in r.json()["detail"]["invalid_roads"]}
    assert "CROSS" in invalid and "180" in invalid["CROSS"]


def test_exactly_180_degree_gap_is_not_antimeridian_crossing(client):
    # 相邻经度差恰好 180（不 >180）不视为跨越，可参与核算
    seed_roads(
        client,
        {
            "EDGE": [(120.0, 30.0), (-60.0, 30.0)],
            "R1": [(0.0, 30.0), (0.01, 30.0)],
        },
    )
    r = account(client, ["EDGE", "R1"])
    assert r.status_code == 200, r.text


def test_gapped_and_bent_overlaps_are_piecewise(client):
    # R2 的前段与 R1 共线重合、后段独有，中间带额外拆分点 -> 单段并集
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.01, 30.0), (0.02, 30.0)],
            "R2": [(0.005, 30.0), (0.01, 30.0), (0.015, 30.0), (0.03, 30.0)],
        },
    )
    r2 = next(x for x in account(client, ["R1", "R2"]).json()["roads"] if x["road_id"] == "R2")
    assert len(r2["covered_segments"]) == 1
    seg = r2["covered_segments"][0]
    assert seg["coordinates"][0] == [0.005, 30.0]
    assert seg["coordinates"][-1] == [0.02, 30.0]
    assert [0.01, 30.0] in seg["coordinates"]

    # L 形折线：只有直的那一段与另一条路共线
    seed_roads(
        client,
        {
            "R3": [(10.0, 20.0), (10.02, 20.0), (10.02, 20.02)],
            "R4": [(10.0, 20.0), (10.02, 20.0)],
        },
    )
    r4 = next(x for x in account(client, ["R3", "R4"]).json()["roads"] if x["road_id"] == "R4")
    assert r4["counted_meters"] == 0.0
    assert r4["covered_segments"][0]["covered_by_road_id"] == "R3"


def test_accounting_uses_one_committed_snapshot(client):
    seed_roads(
        client,
        {
            "R1": [(0.0, 30.0), (0.02, 30.0)],
            "R2": [(0.0, 30.0), (0.02, 30.0)],
        },
    )
    r1 = account(client, ["R1", "R2"]).json()
    assert r1["generation"] >= 1
    # 一次更正会推进 generation，核算响应里的 generation 必须与随后 state 一致
    pids = client.get("/api/v1/roads/R1").json()["point_ids"]
    post_batch(
        client,
        {"points": [{"id": pids[0], "lon": 0.0001, "lat": 30.0}]},
    )
    state = client.get("/api/v1/state").json()
    r2 = account(client, ["R1", "R2"]).json()
    assert r2["generation"] == state["generation"]
    assert r2["generation"] > r1["generation"]
