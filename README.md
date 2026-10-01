# 地方产业人才适配

把企业岗位能力、培养目标、课程证据、毕业去向按授权范围汇合，产出**可追溯的适配结论与缺口**：
企业换版要求、学校改课或学生跨专业之后，旧结论仍按当时口径逐位复现；大批计算按能力拆单元、
可从检查点分批续跑；使用者看到的是结论的来源链，而不是一个孤立分数。

## 领域约定（domain/contract.json）

- 角色：产业园区、高校专业负责人、企业用工经理
- 状态：采集 → 映射 → 核验 → 发布 → 复算（复算为随时可发起的独立动作）
- 不变量：能力映射、授权裁剪、来源谱系、批次续跑

## 设计要点

| 不变量 | 实现 |
| --- | --- |
| 能力映射 | `engine.py` 以能力码为主键汇合：岗位要求（跨企业取最高、保留逐岗位出处）、培养目标、课程证据（取最高、留证据）、毕业去向；输出目标缺口/课程缺口/交付缺口与「适配/部分适配/缺口」结论 |
| 授权裁剪 | `authz.py`：园区全量；学校只见本专业数据、岗位条目只汇总不可见明细；企业只见本企业岗位与已发布运行。谱系保留链形、仅摘除越权载荷 |
| 来源谱系 | `lineage.py`：固定口径清单(manifest) → 运行(run) → 能力结论(conclusion) → 各数据源**精确版本**，节点带内容哈希 |
| 批次续跑 | 立项即冻结 manifest（每源钉死 kind/id/version/content_hash，哈希即 run_id）；逐能力写检查点，`resume` 跳过已完成、重试失败、可限批；`recompute` 从原始版本重算逐单元比对 |

确定性保证：全部哈希基于规范 JSON（键排序、无空白）的 SHA-256；计算不读墙上时钟，
`as_of` 由调用方显式给出；同一 manifest 重复立项幂等返回同一 run。
数据源只追加不改写——换版产生新版本，旧版本永久保留。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/talent_adapt/`：适配服务端
  - `canonical.py` 规范编码与内容哈希；`storage.py` SQLite 版本化存储与检查点
  - `validation.py` 登记校验；`engine.py` 口径冻结与确定性计算
  - `authz.py` 授权裁剪；`lineage.py` 来源谱系；`service.py` 五状态编排
  - `http_app.py` HTTP 适配层；`__main__.py` 进程入口
- `tools/check_contract.py`：契约命令行检查。
- `tests/`：契约回归、服务端生命周期与授权测试、HTTP 端到端测试。

零第三方运行时依赖（仅 Python 3.11 标准库，含 SQLite）。

## 运行

```bash
PYTHONPATH=src python3 -m talent_adapt --db data/talent.db --port 8080
```

## 验证

```bash
python3 -m unittest discover -s tests -v          # 16 个回归测试
python3 -m compileall -q src tools tests          # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## HTTP 接口

鉴权头（演示用；生产应替换为真实身份令牌）：

- `X-Actor-Role: park | school | enterprise`
- `X-Actor-Scope`：school 填专业代码、enterprise 填企业代码，park 留空

| 方法与路径 | 说明 |
| --- | --- |
| `POST /jobs/{id}` `/objectives/{id}` `/courses/{id}` `/outcomes/{id}` | 登记数据源，体含 `valid_from`、`payload`；同 id 再次登记自动升版本 |
| `GET /jobs` 等 | 列最新版本；越权条目返回 `redacted:true`、payload 为 null |
| `GET /jobs/{id}/v{n}` | 查指定历史版本 |
| `POST /runs` | 按 `{program_code, as_of}` 冻结口径立项，返回 `run_id`（=manifest 哈希前缀） |
| `POST /runs/{id}/resume` | 从检查点续跑，可传 `{"max_units": N}` 分批；全部完成转「待核验」 |
| `GET /runs` / `GET /runs/{id}` | 列运行（按角色过滤）/ 看进度、逐能力结论与汇总 |
| `POST /runs/{id}/verify` | 园区核验：不看旧结论，按清单从原始版本重算逐单元比对 |
| `POST /runs/{id}/publish` | 园区发布；发布后企业可见 |
| `POST /runs/{id}/recompute` | 复算：任何时候按当时口径重算并留痕，运行本身不变 |
| `GET /runs/{id}/lineage?ability=A01` | 该能力结论的来源链（越权载荷裁剪） |

快速示例：

```bash
curl -X POST localhost:8080/jobs/JOB-E001-J1 -H 'X-Actor-Role: park' \
  -H 'Content-Type: application/json' -d '{
    "valid_from":"2026-01-01",
    "payload":{"enterprise_id":"E001","job_code":"J1","job_name":"后端开发","requirements":[
      {"ability_code":"A01","ability_name":"编程能力","required_level":4}]}}'

curl -X POST localhost:8080/runs -H 'X-Actor-Role: park' \
  -H 'Content-Type: application/json' -d '{"program_code":"CS01","as_of":"2026-06-30"}'
```

## 结论语义

每个能力单元给出三类缺口：`target_gap`（要求−培养目标，目标缺位）、
`course_gap`（目标−课程证据，落地不足）、`delivery_gap`（要求−课程证据，总交付差距）。
结论只取决于交付差距：0 = 适配，1 = 部分适配，≥2 = 缺口，并附「培养目标未声明」
「课程证据未覆盖」等预警，直接回答“课程究竟缺哪项能力”。
