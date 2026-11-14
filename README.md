# 市政道路线形加工服务

测绘队共享测点 → 路段线形（GeoJSON LineString）加工与入库服务。
FastAPI + SQLite（WAL），两个 API 进程共用一个数据库文件，Docker Compose 一键启动。

## 启动

```bash
docker compose up --build -d
```

* API A：<http://localhost:8001>  Swagger UI：<http://localhost:8001/docs>
* API B：<http://localhost:8002>  Swagger UI：<http://localhost:8002/docs>
* 两个进程共用卷 `gis-data:/data/gis.db`（命名卷，`docker compose down` 不会丢数据；要清空请 `docker compose down -v`）。

跑一遍样例（可选，会自动做在线演示，见文末）：

```bash
docker compose --profile demo run --rm demo
```

重启验证（数据仍在）：

```bash
docker compose restart api-a api-b
curl -s http://localhost:8001/api/v1/state | head -c 400
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/batches` | 一个批次=一个事务：批量新增/更正测点与路段、批量删除；返回受影响路段的最终状态 |
| POST | `/api/v1/overlap-accounting` | **重合里程核算**：2～12 个完整路段 ID，精确共线去重，WGS84 测地线计米 |
| GET | `/api/v1/state` | **一个读快照**返回全部测点原值 + 全部已持久化线形（含 `generation`） |
| GET | `/api/v1/points` / `/api/v1/points/{id}` | 查询测点 |
| GET | `/api/v1/roads` / `/api/v1/roads/{id}` | 查询路段（只读已持久化的 `geometry`，不现场组线） |
| GET | `/health` | 健康检查（含 WAL 模式） |

批次请求体（所有字段可选，按 `points → roads → delete_roads → delete_points` 顺序生效）：

```json
{
  "points":       [{"id": "PT-001", "lon": 116.397428, "lat": 39.909230}],
  "roads":        [{"id": "RD-101", "point_ids": ["PT-001", "PT-002", "PT-003"]}],
  "delete_points": ["PT-004"],
  "delete_roads":  ["RD-102"]
}
```

* 路段 `point_ids`：≥ 2 个、有序、批内不可重复；允许引用**尚不存在**的测点（该路段进入 `incomplete`）。
* 已存在 id 视为**更正**（upsert）：测点改坐标、路段改引用顺序/成员。
* 同一 id 不能在同一批次里既 upsert 又 delete（返回 422）。
* 演示用查询参数：`pause_ms`（组线暂停，**事务外**）、`fail=true`（写完后注入异常以演示回滚）。
  `/docs` 的 “Try it out” 可直接选中这两个参数演示。

响应里的路段：

```json
{
  "id": "RD-101",
  "point_ids": ["PT-001", "PT-002", "PT-003"],
  "status": "ok",                 // ok | incomplete
  "geometry": {"type": "LineString", "coordinates": [[116.397428, 39.909230], ...]},
  "missing_point_ids": [],        // incomplete 时按路段顺序列出缺失测点 ID
  "revision": 3,                  // 每受影响一次 +1
  "updated_at": "2026-11-13T07:42:30.316Z"
}
```

## 关键行为

1. **组线即持久化**：线形在提交事务内按 `point_ids` 顺序组装，写入 `roads.geometry`；
   查询只 SELECT 该列，绝不临时组线。
2. **缺点不输出残线**：任一引用测点不存在 → `geometry = null`、`status = incomplete`、
   `missing_point_ids` 列明缺失 ID（按路段顺序去重）。补点后**下一次批次**自动恢复线形，无需额外调用。
3. **影响面完整**：更正/删除测点、改变路段引用时，所有受影响路段（含引用该测点的全部路段）在
   **同一事务**里重算；未受影响的路段不重写（`revision` 不变）。
4. **整批原子**：批次中途任何异常（含写完后注入）整体回滚，`generation` 不前进，库保持原状。
5. **重启保留**：SQLite 文件在命名卷里，`docker compose restart` / 重建容器后数据仍在。

## 重合里程核算（`POST /api/v1/overlap-accounting`）

养护 GIS 数据员汇总多批道路里程时，同一段路可能被重复登记。本接口在
**同一个已提交快照**内读取 2～12 个路段持久化的 GeoJSON 线形，核算共有线段：

```json
{ "road_ids": ["RD-101", "RD-102", "RD-103"] }
```

判定与归属规则：

* **平面精确共线 + 正长度**：只在经纬度平面上方向向量归一化叉积为零、且落在
  同一条直线上的两小段才视为共线（相对容限 `1e-12`，只吸收浮点误差）；
  重合区间长度严格大于零才算重复——**单点相交、端点相触不算**，平行不共线不算。
* **反向录入**：方向相反的两条路照常识别（投影求交与方向无关）。
* **不同顶点拆分**：按“小线段 × 小线段”逐对求交，内部顶点只是切分点，
  稀疏顶点与密集顶点的两条路也能正确求交。
* **三条以上覆盖只计一次**：路段按 **ID 升序**处理，每条路只与前序路比较，
  每段重合归属到**最早覆盖它的前序路段**（`covered_segments[].covered_by_road_id`）。
* **长度按 WGS84 测地线**（`geographiclib`，椭球 `a=6378137, f=1/298.257223563`）
  计算米数；重合弧按弧长比例切分整段测地线长，任意顶点拆分下
  `counted_meters + covered_meters == total_meters` 严格闭合（6 位小数）。

响应：

```json
{
  "generation": 7,
  "roads": [
    {
      "road_id": "RD-101",
      "total_meters": 1710.365866,
      "counted_meters": 855.182932,
      "covered_meters": 855.182934,
      "covered_segments": [
        {
          "covered_by_road_id": "RD-100",
          "coordinates": [[116.31, 39.9], [116.32, 39.9]],
          "meters": 855.182934
        }
      ]
    }
  ],
  "duplicate_meters": 855.182934,
  "deduped_total_meters": 3420.731733
}
```

* `total_meters` 路段全长；`counted_meters` 实际计入去重总量的米数；
  `covered_meters` 被前序路段覆盖的去重总长；`covered_segments` 为归属明细。
* `duplicate_meters` 为全部重复米数（每条重合弧只计一次）；
  `deduped_total_meters` 为去重后总米数（各路段 `counted_meters` 之和）。
* `generation` 标明本次读取的提交代号，与同时刻 `GET /api/v1/state` 一致。

失败语义（HTTP 422，`detail.invalid_roads` 逐条列明，整次核算不出部分结果）：

* 路段 ID 不存在；
* 路段缺点（`incomplete`，无持久化线形）；
* 线形顶点数超过 **40**；
* 路段相邻顶点经度差 > 180°，即**跨越 180° 经线**。

所有路段在一个延迟读事务（同一 WAL 快照）内一次性读出，因此并发改线时
绝不会混用不同提交状态的数据；请求体层面还会拒绝 ID 数量越界（<2 或 >12）
与重复 ID。

## 并发模型（两个进程共库）

* 每个请求一条短连接；写事务一律 `BEGIN IMMEDIATE` + `busy_timeout=30s`，两个进程的写入互斥但排队等待，不报 busy。
* **组线在事务之外**：先在一个 WAL 读快照上把批次规划完（含组线），再进入极短的写事务。
  因此 `pause_ms=2000` 的慢批次不会挡住另一进程的入库（演示第 5 步实测 < 1s 完成）。
* **提交前复核快照代号**（`store_meta.generation`）：规划期间若有别的进程提交过，
  本次规划整体作废、从新快照重算（响应里 `commit_attempts` 可见），
  绝不把基于旧坐标算出的线形写回库；并发新增 / 改引用的路段也在重算中被重新纳入，
  不会漏更新。
* **读一致性**：`/api/v1/state` 在一个延迟事务里完成全部读取，返回的测点原值与线形必定来自同一已提交状态。
* WAL 下读者不阻塞写者、写者不阻塞读者；`synchronous=FULL` 保证已提交数据断电安全。

## 本地运行与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
GIS_DB_PATH=/tmp/gis.db uvicorn app.main:app --port 8001

pytest            # 功能 + 双进程并发（测试会真的起两个 API 进程共库运行）
```

（无 docker 环境时也可直接验证整套并发行为：`tests/test_concurrency.py` 用
`tests/server_runner.py` 起两个独立进程，连接同一个 SQLite 文件。）

`tests/test_concurrency.py` 起两个真实 API 进程共库运行：跨进程一致性、暂停组线不持写锁、
并发改线无漏更新/旧坐标、`/state` 在持续写入下的快照一致性、核算接口的快照归属；
`tests/test_retry.py` 用可控方式制造"规划后另一进程提交"，断言旧规划被丢弃并重算；
`tests/test_overlap.py` 覆盖重合核算：共线/反向/不同拆分/三路覆盖只计一次/
单点相交不算/平行不算、缺点与不存在整次失败、40 顶点与 180° 经线限制、测地线长度闭合。

## 演示样例（要点）

`examples/run_demo.py`（compose profile `demo`）对两个进程逐步演示：

1. 批量入库测点 + 3 条路段，线形已持久化；
2. 删除被引用测点 → `geometry=null`、`missing_point_ids` 列明；
3. 补回该点 → 线形自动恢复；
4. 改由 API B 更正共享测点 → 两条引用它的路段同批生效；
5. **可控暂停**：A 提交一个 `pause_ms=2000` 的批次，B 在暂停期间完成测点更正 + 新建路段
   （用时 < 1s，未被写锁阻塞）；A 提交时发现代号已变，整批重算后 `commit_attempts=2`，
   重算结果把 B 新建的 RD-105 一并纳入（`affected_road_ids` 含 RD-103/104/105），
   两条路段与 PT-006 原值一致；
6. `fail=true` 批次写完后抛错 → 整体回滚，`/state` 与失败前逐字段一致。

本地直接跑（不用 compose）：

```bash
uvicorn app.main:app --port 8001 &  uvicorn app.main:app --port 8002 &
python -m examples.run_demo --base-a http://localhost:8001 --base-b http://localhost:8002
```

## 说明

* 缺测点用 `point_id` 不加外键约束是刻意设计：悬空引用是"可表示状态"，才能稳定报告缺失 ID
  并在补点后自动恢复；路段删除时其引用行由 `ON DELETE CASCADE` 清理。
* 单机 SQLite 的写入吞吐上限是设计边界：本方案保证**正确性**，写入串行化 + 组线在事务外
  使得暂停/慢组线不会拖住其他入库。若未来需要多机横向扩展，应将 `store_meta.generation`
  复核替换为外部分布式锁/版本号。
