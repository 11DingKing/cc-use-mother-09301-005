# 地方产业人才适配

本项目维护地方产业人才适配的领域约定、角色边界与样例数据，并提供从零搭建的服务端：将**企业岗位能力、培养目标、课程证据、毕业去向**按授权范围汇合，产出可追溯的适配结论与能力缺口。

当前契约覆盖产业园区、高校专业负责人、企业用工经理，并明确四条关键约束：

| 不变量 | 实现 |
| --- | --- |
| 能力映射 | `engine`：岗位要求 vs 课程证据×覆盖度、毕业去向，输出逐项缺口与加权适配度 |
| 授权裁剪 | `authz`：按主体授权的 `(资源类型, 业务键)` 汇合，未授权既不计算也不进谱系 |
| 来源谱系 | 每条结论携带逐资源**版本+内容哈希**的来源链，可解析回完整来源内容 |
| 批次续跑 | 计算单元落检查点，只重跑 `pending/error`，中断后续跑结论逐字节一致 |

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/talentfit/`：服务端核心。
  - `canonical.py`：规范化 JSON 与 SHA-256 内容哈希（结论身份）。
  - `store.py`：只增不改的 SQLite 版本库（资源版本、授权、结论与谱系、批次检查点）。
  - `authz.py`：授权裁剪。
  - `engine.py`：能力映射纯函数（算法版本号 `ability-max-level/v1`，改规则须换号）。
  - `pipeline.py`：口径快照 → 授权裁剪 → 专业归组 → 计算落库；批次执行/续跑。
  - `service.py`：服务门面，含按当时口径复算校验（`reproduce`）。
  - `httpapi.py`：零第三方依赖的 HTTP 适配层。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性与四不变量/变更场景回归测试。

## 口径复现（企业换版 / 学校改课 / 学生跨专业）

- 资源按 `(类型, 业务键)` 累积**不可变版本**，每版携带生效日；旧结论的谱系钉死所用版本。
- 评估指定口径日 `as_of`，只取 `effective_from <= as_of` 的最新版本。
- 换版/改课只产生新版本：旧结论仍取 v1 来源，`reproduce` 重算哈希与原哈希逐字节一致。
- 毕业去向按 `program_id` 归组，跨专业学生的证据不会流入其他专业的结论。
- 培养目标是"目标声明"而非达成证据，单列 `objective_level`；供给只取课程证据与毕业去向，避免用目标掩盖课程缺口。

## HTTP 接口

```bash
PYTHONPATH=src python3 -m talentfit.httpapi talentfit.db   # 监听 127.0.0.1:8080

POST /resources/<kind>/<key>?effective_from=YYYY-MM-DD   # 写入版本
POST /grants                      # {"subject","scope_kind","scope_key"}
POST /evaluate?as_of&subject&job_key                    # 单次评估 → 结论哈希
POST /batches                     # {"batch_id","as_of","subject","job_keys"}
POST /batches/<id>/run            # 执行 / 从检查点续跑
GET  /batches/<id>                # 批次与各条目状态
GET  /results/<hash>              # 结论 + 来源链（含完整来源内容）
POST /results/<hash>/reproduce    # 按当时口径复算并比对哈希
```

`kind` 取值：`job_requirement`、`program_objective`、`course_evidence`、`graduate_outcome`。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
