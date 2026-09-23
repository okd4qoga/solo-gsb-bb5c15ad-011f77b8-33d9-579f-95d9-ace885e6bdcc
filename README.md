# 多通道传感器故障规则 Web 应用

本地运行的多通道传感器故障规则工作台：导入 JSONL 遥测事件 → 定义带 AND/OR 与持续时长的故障规则 →
把每个通道的样本值展开为**半开时间区间** `[start, end)`，用**扫描线（sweep line）**合并条件真值，
产出每条规则的**故障区间**及**触发证据** → 在时间轴上创建区间标注 → 按时间范围筛选并导出包含
原始事件、故障区间、故障事件、规则与证据的 JSON。故障列表旁提供**故障事件**视图：不同规则产生、
时间区间重叠或首尾相接（含传递链）的故障归并为同一事件。

技术栈：Python 3.11 · FastAPI · SQLite（标准库 `sqlite3`）· 原生 JavaScript（无构建、无前端框架）。

---

## 一条命令启动（推荐 Docker Compose）

```bash
docker compose up --build
```

启动后访问：

- **地址 / 端口：http://localhost:8000**
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/health

数据保存在 Docker 卷 `sensor-data` 内的 SQLite 文件 `/data/sensor.db`，容器重启不丢失。
停止用 `docker compose down`；连数据卷一起删除用 `docker compose down -v`。

### 不用 Compose（仅 Docker）

```bash
docker build -t sensor-fault-rules .
docker run --rm -p 8000:8000 -v "$PWD/data:/data" sensor-fault-rules
# 打开 http://localhost:8000
```

### 不用 Docker（本机 Python 3.11+）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 打开 http://localhost:8000
```

本机运行时数据库默认位于仓库下 `data/sensor.db`（可用环境变量 `SENSOR_DB_PATH` 覆盖）。

---

## 快速体验

1. 打开 http://localhost:8000 ，点击顶部 **「载入示例」**，自动导入 `samples/telemetry.jsonl`
   （4 个通道、31 分钟、每分钟一个点）并创建 3 条示例规则。
2. 点击 **「评估规则」**，时间轴和故障列表会显示 4 个故障区间：
   - `pump-1 高温持续`：temp ≥ 80 且持续 ≥ 5 分钟 → `[00:06, 00:17)`；`00:20` 的单点尖峰被持续时长过滤；
   - `pump-1 高温且振动`（AND，2 分钟）→ `[00:12, 00:14)`；
   - `pump-2 振动大或压力低`（OR，200 秒）：vib>5 的 `[00:03,00:06)` 与 press<30 的
     `[00:06,00:09)` **在 00:06 边界相接，按连续处理**，合并为单个故障 `[00:03,00:09)`；
     另一个独立的压力低谷 `[00:10,00:14)` 为第二个故障。
3. 切到故障卡片右上角的 **「故障事件」** 标签：上述 4 个故障经传递归并为 **1 个事件**
   `[00:03, 00:17)`——pump-2 的 `[00:03,00:09)` 与 pump-1 高温的 `[00:06,00:17)` 首尾相接，
   后者又与 `[00:10,00:14)`、`[00:12,00:14)` 重叠。点击事件可展开成员规则、各自半开区间与原触发证据。
4. 在时间轴上**按住拖拽**框选一段时间，填写名称即可创建区间标注。
5. 点击故障色块或故障行，展开每个条件的触发样本证据；再点 **「导出 JSON」** 下载完整结果。

重新生成示例数据：`python3 samples/generate_sample.py`

---

## 数据模型与 JSONL 格式

每行一个 JSON 对象（JSONL），也接受 JSON 数组（`.json` 或直接粘贴）。默认字段：

```json
{"device": "pump-1", "metric": "temp", "ts": "2026-09-23T00:00:00Z", "value": 81.2}
```

- `device` 设备标识，`metric` 指标名（二者共同确定一个**通道**）。
- `ts` 时间戳：ISO-8601（推荐带 `Z`/时区，无时区按 UTC 处理）或 epoch（秒 / 毫秒，数字或数字字符串均可）。
- `value` 指标值：数字（非数字行会被跳过并计数）。
- 也支持**嵌套指标**对象，自动展开为每个键一条事件：
  `{"device":"pump-1","ts":"...","value":{"temp":81,"vib":2.1}}`。
- 字段名不同可在「字段映射」里自定义（如 `host/k/t/x`）。
- 同一 `(device, metric, ts)` 重复导入时**后值覆盖**。

## 故障规则

```
规则 = 条件列表 + 组合逻辑(AND|OR) + 持续时长(秒, ≥0)
条件 = 设备 + 指标 + 运算符(> >= < <= == !=) + 数值阈值
```

- 每个通道的样本值在相邻两个样本之间保持：样本 `(tᵢ, vᵢ)` 持有于半开区间 `[tᵢ, tᵢ₊₁)`；
  通道最后一个样本持有到**全数据集最大时间戳**（horizon）。
- 条件在其持有的值满足谓词时为真，得到一组 `[start,end)` 半开区间。
- **AND**：所有条件区间求交；某条件对应的通道完全没有数据时，AND 规则永不成立。
- **OR**：各条件区间求并。
- 扫描线只在“合并真值”真正跳变的边界切段，因此
  `[a,b)` 后紧跟 `[b,c)` 这种**边界相接不会产生零长度缝隙**，自动按一段连续区间处理。
- 长度 `end-start ≥ 持续时长` 的真值段才成为故障区间。
- **确认时刻** `confirmed_at = start + 持续时长`（即持续满指定时长的那一刻；时间轴上用 ▴ 标出）。
- 每条故障区间附带**逐条件证据**：该故障内命中的原始样本（event_id / 时间 / 值）与条件成立的子区间。

## 时间范围筛选语义

- 原始事件是时间点，筛选时**两端包含**（`start ≤ ts ≤ end`）。
- 故障区间 / 标注是 `[start,end)` 半开区间，与筛选窗口按**半开相交**返回（`iv.end > start AND iv.start < end`）。
- 筛选同时作用于时间轴、故障列表、故障事件列表、标注列表、原始事件表和导出。

## 故障事件归并语义

「故障事件」是**只读派生视图**，不改写、不删除 `fault_intervals` 中的任何原始故障结果：

- 两条故障（通常来自**不同规则**）的半开区间**重叠**或**首尾相接**（`[a,b)` 与 `[b,c)`）即属于同一事件；
  该关系按**传递闭包**归并——A 接 B、B 接 C，则 `{A,B,C}` 同属一个事件，即使 A、C 本身不相交。
  归并在全部故障上先完成，再套用时间筛选，因此跨窗口的传递链不会被截断。
- 事件区间 = 成员故障的**最早开始**至**最晚结束**，并列出每个成员的规则、各自 `[start,end)`
  区间、确认时刻和**原触发证据**（条件成立子区间与命中样本，逐字取自原故障）。
- 事件列表沿用当前时间范围：事件整体与筛选窗口半开相交即出现；仅在窗口外的成员仍会列出，
  以「窗口外」标签和淡化色块标出（`in_window: false`）。
- 同一规则自身相邻的故障区间在扫描线评估时已按相接合并，因此事件归并天然发生在不同规则之间。

## 导出 JSON

`GET /api/export?start=...&end=...&download=true`（浏览器「导出 JSON」按钮）结构：

```jsonc
{
  "exported_at": 1790000000.0,
  "time_range": {"start": null, "end": null, "note": "..."},
  "rules": [ /* 规则及其条件 */ ],
  "events": [ /* 范围内原始事件 */ ],
  "events_total_in_range": 124,
  "evidence_only_events": [ /* 作为证据、但落在事件筛选窗口外的样本，单独带出 */ ],
  "fault_intervals": [
    {
      "rule_id": 3, "rule_name": "...", "logic": "OR",
      "start": 1790121780.0, "end": 1790122140.0,
      "duration_required": 200.0, "confirmed_at": 1790121980.0,
      "evidence": [
        {
          "condition": {"device":"pump-2","metric":"vib","op":">","threshold":5},
          "held_intervals": [[1790121780.0, 1790121960.0]],
          "events": [{"event_id":15,"ts":1790121780.0,"value":6.4}]
        }
      ]
    }
  ],
  "fault_events": [
    {
      "id": "E1",
      "start": 1790121780.0, "end": 1790122820.0,
      "rule_count": 2, "fault_count": 2, "member_count_in_window": 2,
      "rule_ids": [2, 3], "rule_names": ["...", "..."],
      "members": [ /* 成员故障：fault_id/规则/区间 + 与 fault_intervals 同构的 evidence，另有 in_window 标志 */ ]
    }
  ],
  "annotations": [ /* 区间标注 */ ]
}
```

---

## HTTP API 摘要

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查 |
| `GET /api/meta` | 运算符、数据时间范围与计数 |
| `POST /api/import` | multipart 上传 JSONL（字段：`file`，可选 `device_field/metric_field/ts_field/value_field`） |
| `POST /api/import/text` | 直接提交 JSONL 文本（JSON body：`{text, ...字段映射}`） |
| `GET /api/channels` | 通道列表及值范围 |
| `GET /api/events?start&end&device&metric&limit&offset` | 原始事件（时间含端点，分页） |
| `GET/POST /api/rules`，`GET/PUT/DELETE /api/rules/{id}` | 规则增删改查 |
| `POST /api/evaluate` | 重新评估全部规则，重算故障区间与证据 |
| `GET /api/faults?start&end` | 故障区间（含证据，半开相交筛选） |
| `GET /api/fault-events?start&end` | 故障事件：跨规则重叠/首尾相接（传递）归并，含成员故障与原证据 |
| `GET/POST /api/annotations`，`DELETE /api/annotations/{id}` | 区间标注 |
| `GET /api/timeline?start&end&max_points` | 时间轴聚合数据（通道抽样、故障、标注） |
| `GET /api/export?start&end&download=true` | 导出完整 JSON（附件下载） |
| `POST /api/demo` | 载入内置示例数据与规则 |
| `POST /api/reset` | 清空全部数据 |

时间参数均可使用 ISO-8601 或 epoch 数字。完整交互模式见 http://localhost:8000/docs 。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SENSOR_DB_PATH` | 容器内 `/data/sensor.db`；本机运行时 `./data/sensor.db` | SQLite 数据库文件路径 |

服务监听固定为容器内 `0.0.0.0:8000`；宿主机端口在 `docker-compose.yml` 的 `ports` 中映射
（如改为 `18000:8000` 则访问 http://localhost:18000 ）。

## 项目结构

```
app/
  engine.py           # 半开区间、扫描线 AND/OR 合并、持续时长过滤、证据、区间聚类（事件归并）
  db.py               # SQLite 连接、建表、基础查询
  services.py         # JSONL 导入、规则评估、时间轴抽样、筛选、故障事件归并、导出
  main.py             # FastAPI 路由与静态页面
  static/             # 原生 JS 前端（index.html / app.js / styles.css）
samples/
  telemetry.jsonl     # 示例数据（generate_sample.py 可再生成）
tests/
  test_engine.py        # 引擎单元测试（pytest）
  test_fault_events.py  # 故障事件归并、传递链、窗口语义测试
Dockerfile / docker-compose.yml / requirements.txt
```

## 测试

```bash
pip install pytest
python3 -m pytest tests/ -q
```

测试覆盖：相接区间合并、真实缝隙不桥接、AND 相交（含同时刻边界）、OR 跨通道相接合并、
持续时长过滤单点尖峰、缺失通道、同时间戳后值覆盖，以及故障事件的重叠/相接/**传递链**归并、
事件外包区间、时间窗口成员标志与原故障不被修改等关键语义。
