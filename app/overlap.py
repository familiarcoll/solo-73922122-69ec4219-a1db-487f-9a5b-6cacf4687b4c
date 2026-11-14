"""重合里程核算（纯几何，无 FastAPI / 无 SQL）。

判定规则（对应需求）
--------------------
* 只在**经纬度平面**上判定：两小段方向向量的归一化叉积为零（精确共线）
  且落在同一条无限直线上，才认为共线；平行但不共线的两条路不算。
* 共线后把两端投影到直线参数轴上，重合区间长度严格大于零
  （> :data:`EPS`，按平面相对量度）才算重复；单点相交、端点相触不算。
* 反向录入自然成立：方向相反时投影区间仍可求交，与方向无关。
* 不同顶点拆分不影响：按“小线段 × 小线段”逐对求交，内部顶点只是切分点。
* 三条以上覆盖只计一次：对每条路，把与所有**前序路段**（ID 升序）的重合
  区间做并集，归属到最早覆盖它的那条前序路。

长度
----
平面只负责“找重合”；所有对外长度按 WGS84 测地线（geographiclib，
``a=6378137.0, f=1/298.257223563``）计算米数。重合弧段的长度用
“整段测地线长按弧长比例切分”得到，保证任意拆分下长度可加：
``covered + counted == 路段全长``（四舍五入到 :data:`METER_DECIMALS`
位后仍闭合）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from geographiclib.geodesic import Geodesic

# WGS84 椭球
WGS84 = Geodesic(6378137.0, 1.0 / 298.257223563)

# 平面共线/同线判定的相对容限：叉积或点到线距离相对线段长度小于该值即视为零。
# 1e-12 远小于任何真实道路的非共线偏差，只吸收浮点运算误差，实现“精确共线”。
EPS = 1e-12

# 对外米数保留的小数位（1e-6 m ≈ 微米）
METER_DECIMALS = 6


class OverlapError(ValueError):
    """输入路段无法参与核算（不存在 / 缺点 / 顶点超限 / 跨 180° 经线）。"""

    def __init__(self, invalid: dict[str, str]):
        self.invalid = dict(sorted(invalid.items()))
        super().__init__("; ".join(f"{rid}: {why}" for rid, why in self.invalid.items()))


@dataclass(frozen=True)
class InputRoad:
    road_id: str
    coords: list[tuple[float, float]]  # [(lon, lat), ...]，已校验可用


@dataclass(frozen=True)
class CoveredPiece:
    """归属于某条前序路的一段重合弧（在当前路上的子弧，按顶点顺序）。"""

    covered_by: str
    coordinates: list[tuple[float, float]]
    meters: float


@dataclass
class RoadAccount:
    road_id: str
    total_meters: float          # 该路完整测地线长
    counted_meters: float        # 实际计入（未被前序路覆盖的部分）
    covered_meters: float        # 被前序路覆盖的去重总长
    covered_pieces: list[CoveredPiece]


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

MAX_VERTICES = 40


def validate_road(
    road_id: str,
    geometry: dict | None,
    missing_point_ids: list[str],
) -> str | None:
    """检查一条已持久化路段能否参与核算。

    返回 ``None`` 表示通过；否则返回原因字符串。``geometry`` 为 GeoJSON
    LineString（dict）或 ``None``（缺点的 incomplete 路段）。
    """
    if geometry is None:
        if missing_point_ids:
            return (
                "road is incomplete (missing referenced points: "
                f"{', '.join(missing_point_ids)})"
            )
        return "road has no persisted LineString geometry"
    if geometry.get("type") != "LineString":
        return f"geometry type {geometry.get('type')!r} is not LineString"
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        return "LineString must contain at least 2 coordinates"
    if len(coords) > MAX_VERTICES:
        return f"LineString has {len(coords)} vertices, limit is {MAX_VERTICES}"
    for i, xy in enumerate(coords):
        if (
            not isinstance(xy, list)
            or len(xy) < 2
            or not all(isinstance(v, (int, float)) for v in xy[:2])
        ):
            return f"coordinate #{i} is not a numeric [lon, lat] pair"
    # 不跨 180° 经线：任一相邻顶点的经度差不超过 180°
    for i in range(1, len(coords)):
        if abs(coords[i][0] - coords[i - 1][0]) > 180.0:
            return "road crosses the 180th meridian (antimeridian), which is not supported"
    return None


# ---------------------------------------------------------------------------
# 平面工具
# ---------------------------------------------------------------------------


def _sub(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    return (a[0] - b[0], a[1] - b[1])


def _cross(u: tuple[float, float], v: tuple[float, float]) -> float:
    return u[0] * v[1] - u[1] * v[0]


def _dot(u: tuple[float, float], v: tuple[float, float]) -> float:
    return u[0] * v[0] + u[1] * v[1]


def _norm(u: tuple[float, float]) -> float:
    return math.hypot(u[0], u[1])


def _collinear_same_line(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
) -> bool:
    """经纬度平面上两小段是否精确共线且在同一条直线上。"""
    u = _sub(p2, p1)
    v = _sub(q2, q1)
    lu = _norm(u)
    lv = _norm(v)
    if lu == 0.0 or lv == 0.0:
        return False  # 退化零长段，交由持久化侧的唯一顶点约束，这里保守排除
    scale = max(lu, lv)
    # 方向平行（含反向）：归一化叉积为零
    if abs(_cross(u, v)) > EPS * lu * lv:
        return False
    # 同一条无限直线：q1 到直线 p1p2 的有向距离（相对量度）为零
    if abs(_cross(u, _sub(q1, p1))) > EPS * lu * scale:
        return False
    return True


# ---------------------------------------------------------------------------
# 测地线长度
# ---------------------------------------------------------------------------


def geodesic_length(coords: list[tuple[float, float]]) -> float:
    """WGS84 测地线总长（米），逐顶点段求和。"""
    total = 0.0
    for a, b in zip(coords, coords[1:]):
        total += WGS84.Inverse(a[1], a[0], b[1], b[0])["s12"]
    return total


@dataclass
class _Arc:
    """一条路上的弧段：顶点坐标 + 逐段累计弧长（平面参数，仅用于切分定位）。"""

    coords: list[tuple[float, float]]
    seg_len: list[float]          # 每段平面长度
    cum: list[float]              # cum[i] = 前 i 段平面长度之和，cum[0] = 0

    @property
    def length(self) -> float:
        return self.cum[-1]


def _build_arc(coords: list[tuple[float, float]]) -> _Arc:
    seg_len = [
        _norm(_sub(b, a)) for a, b in zip(coords, coords[1:])
    ]
    cum = [0.0]
    for s in seg_len:
        cum.append(cum[-1] + s)
    return _Arc(coords=coords, seg_len=seg_len, cum=cum)


def _point_at(arc: _Arc, s: float) -> tuple[float, float]:
    """弧上平面弧长参数 s 处的插值坐标（s 以弧起点为零）。"""
    if s <= 0.0:
        return arc.coords[0]
    if s >= arc.length:
        return arc.coords[-1]
    # 定位所在段
    lo, hi = 0, len(arc.seg_len)
    while lo < hi:
        mid = (lo + hi) // 2
        if arc.cum[mid + 1] < s:
            lo = mid + 1
        else:
            hi = mid
    i = lo
    span = arc.seg_len[i]
    t = 0.0 if span == 0.0 else (s - arc.cum[i]) / span
    a, b = arc.coords[i], arc.coords[i + 1]
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _sub_arc(arc: _Arc, s0: float, s1: float) -> list[tuple[float, float]]:
    """取弧上 [s0, s1] 子弧的完整坐标序列（含落在区间内的原有顶点）。"""
    s0 = min(max(s0, 0.0), arc.length)
    s1 = min(max(s1, s0), arc.length)
    out: list[tuple[float, float]] = [_point_at(arc, s0)]
    for i, c in enumerate(arc.coords[1:-1], start=1):
        if arc.cum[i] > s0 + EPS * arc.length and arc.cum[i] < s1 - EPS * arc.length:
            out.append(c)
    end = _point_at(arc, s1)
    if end != out[-1]:
        out.append(end)
    return out


# ---------------------------------------------------------------------------
# 重合核算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Interval:
    lo: float
    hi: float
    covered_by: str


def _pair_overlaps(
    target: _Arc,
    other: _Arc,
    other_id: str,
) -> list[_Interval]:
    """求 other 与 target 的全部正长度重合，参数轴取 target 的平面弧长。"""
    out: list[_Interval] = []
    for i in range(len(target.seg_len)):
        p1, p2 = target.coords[i], target.coords[i + 1]
        u = _sub(p2, p1)
        lu = target.seg_len[i]
        if lu == 0.0:
            continue
        tol = EPS * lu
        for j in range(len(other.seg_len)):
            q1, q2 = other.coords[j], other.coords[j + 1]
            if not _collinear_same_line(p1, p2, q1, q2):
                continue
            base = target.cum[i]
            # other 两端在 p1->p2 轴上的投影参数（target 全局弧长坐标）
            tq1 = base + _dot(_sub(q1, p1), u) / lu
            tq2 = base + _dot(_sub(q2, p1), u) / lu
            lo = max(base, min(tq1, tq2))
            hi = min(base + lu, max(tq1, tq2))
            if hi - lo > tol:  # 严格正长度；端点相触/单点相交被排除
                out.append(_Interval(lo, hi, other_id))
    return out


def _merge_union(intervals: list[_Interval]) -> list[_Interval]:
    """合并相交/相切的区间；并集归属取最早（最小 ID）的覆盖者。"""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda x: (x.lo, x.hi, x.covered_by))
    merged: list[_Interval] = []
    for iv in ordered:
        if not merged or iv.lo > merged[-1].hi:
            merged.append(_Interval(iv.lo, iv.hi, iv.covered_by))
            continue
        last = merged[-1]
        owner = min(last.covered_by, iv.covered_by)
        merged[-1] = _Interval(last.lo, max(last.hi, iv.hi), owner)
    return merged


def _attribute_pieces(
    target: _Arc, union: list[_Interval], raw: list[_Interval]
) -> list[CoveredPiece]:
    """把并集的每一段归属到“最早覆盖它的前序路”。

    做法：按前序路 ID 升序逐条把它的原始重合区间从未归属余量中扣掉，
    扣下来的碎片即归该路所有；天然保证三路以上覆盖只计一次。
    余量只表示“尚未归属的并集部分”，用不带归属的裸区间表示。
    """
    remaining: list[tuple[float, float]] = [(iv.lo, iv.hi) for iv in union]
    pieces: list[CoveredPiece] = []
    for owner in sorted({iv.covered_by for iv in raw}):
        claim = _merge_union([iv for iv in raw if iv.covered_by == owner])
        left: list[tuple[float, float]] = []
        for rem_lo, rem_hi in remaining:
            tail_lo = rem_lo
            for c in claim:
                if c.hi <= rem_lo or c.lo >= rem_hi:
                    continue
                lo = max(rem_lo, c.lo)
                hi = min(rem_hi, c.hi)
                if hi - lo <= 0.0:
                    continue
                if lo > tail_lo:
                    left.append((tail_lo, lo))
                coords = _sub_arc(target, lo, hi)
                pieces.append(
                    CoveredPiece(covered_by=owner, coordinates=coords, meters=0.0)
                )
                tail_lo = hi
            if tail_lo < rem_hi:
                left.append((tail_lo, rem_hi))
        remaining = left
        if not remaining:
            break
    pieces.sort(key=lambda p: (_point_param(target, p.coordinates[0]), p.covered_by))
    return pieces


def _point_param(arc: _Arc, point: tuple[float, float]) -> float:
    """顶点坐标回到弧长参数（用于排序输出碎片）。"""
    best, best_s = math.inf, 0.0
    for i in range(len(arc.seg_len)):
        a, b = arc.coords[i], arc.coords[i + 1]
        u = _sub(b, a)
        lu = arc.seg_len[i]
        if lu == 0.0:
            continue
        t = _dot(_sub(point, a), u) / (lu * lu)
        t = min(max(t, 0.0), 1.0)
        proj = (a[0] + u[0] * t, a[1] + u[1] * t)
        d = _norm(_sub(point, proj))
        if d < best:
            best, best_s = d, arc.cum[i] + t * lu
    return best_s


def _piece_meters(
    target_coords: list[tuple[float, float]],
    target_total_m: float,
    target_arc: _Arc,
    pieces: list[CoveredPiece],
) -> list[CoveredPiece]:
    """按平面弧长比例把整路测地线长分配到各重合碎片，保证可加。

    碎片边界落在原顶点上时，比例切分与逐段测地线求和完全一致；
    边界落在段内时按该段测地线长线性切分（短段内误差可忽略）。
    """
    # 每段的测地线长
    geo_seg = [
        WGS84.Inverse(a[1], a[0], b[1], b[0])["s12"]
        for a, b in zip(target_coords, target_coords[1:])
    ]

    def meters_between(s0: float, s1: float) -> float:
        if s1 <= s0:
            return 0.0
        total = 0.0
        lo, hi = 0, len(target_arc.seg_len)
        for i in range(lo, hi):
            seg_lo, seg_hi = target_arc.cum[i], target_arc.cum[i + 1]
            if seg_hi <= s0 or seg_lo >= s1:
                continue
            span = target_arc.seg_len[i]
            f0 = 0.0 if span == 0 else min(max((s0 - seg_lo) / span, 0.0), 1.0)
            f1 = 0.0 if span == 0 else min(max((s1 - seg_lo) / span, 0.0), 1.0)
            total += geo_seg[i] * (f1 - f0)
        return total

    out: list[CoveredPiece] = []
    for p in pieces:
        s0 = _point_param(target_arc, p.coordinates[0])
        s1 = _point_param(target_arc, p.coordinates[-1])
        out.append(
            CoveredPiece(
                covered_by=p.covered_by,
                coordinates=p.coordinates,
                meters=meters_between(s0, s1),
            )
        )
    # 修正浮点漂移：碎片长度之和不超过全长
    assert sum(p.meters for p in out) <= target_total_m + 1e-9
    return out


def account_overlaps(roads: list[InputRoad]) -> tuple[list[RoadAccount], float]:
    """核算多条路之间的重合里程。

    ``roads`` 已通过 :func:`validate_road` 校验；内部按路段 ID 升序处理，
    每条路只与它前面的路比较，重合归属到最早覆盖者。

    返回 ``(每条路的核算结果, 去重总米数)``。
    """
    ordered = sorted(roads, key=lambda r: r.road_id)
    arcs: dict[str, _Arc] = {r.road_id: _build_arc(r.coords) for r in ordered}
    totals: dict[str, float] = {
        r.road_id: geodesic_length(r.coords) for r in ordered
    }

    accounts: list[RoadAccount] = []
    for idx, road in enumerate(ordered):
        rid = road.road_id
        target = arcs[rid]
        raw: list[_Interval] = []
        for prev in ordered[:idx]:
            raw.extend(_pair_overlaps(target, arcs[prev.road_id], prev.road_id))
        union = _merge_union(raw)
        pieces = _attribute_pieces(target, union, raw)
        pieces = _piece_meters(road.coords, totals[rid], target, pieces)
        covered_m = sum(p.meters for p in pieces)
        # 闭合到全长，吸收比例切分的浮点误差
        counted_m = max(totals[rid] - covered_m, 0.0)
        accounts.append(
            RoadAccount(
                road_id=rid,
                total_meters=totals[rid],
                counted_meters=counted_m,
                covered_meters=covered_m,
                covered_pieces=pieces,
            )
        )

    deduped = sum(a.counted_meters for a in accounts)
    return accounts, deduped


def round_meters(value: float) -> float:
    return round(value + 0.0, METER_DECIMALS)
