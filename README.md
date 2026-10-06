# 原始创新研究组合审议服务

本项目在科技创新协作基础能力（科研机构、操作者、角色权限、请求幂等、SQLite 事务与哈希串联审计）之上，构建一套完整的**原始创新研究组合审议服务**，供国家实验室战略办公室汇总下一轮基础研究建议时使用。

它把相互依赖的科学问题、可证伪主张、前置发现、研究路线、资源需求、申请团队关联和证据版本组织成**可追踪的依赖图**，并保证：

- **评审快照冻结**：征集截止形成快照后，补交材料不能静默改写评审依据；
- **利益冲突回避**：评审人与申请团队的合作关系（含同属团队）强制回避，事后披露会自动翻转既有分配，并重新核对独立评审人数；
- **资源原子占用**：有限预算与稀缺设施只能被一个获批组合原子占用，竞争审批不会产生两个生效版本；
- **决定全程留痕**：撤回、路线合并、条件立项、复议均以追加方式保留原决定及其适用依据（含冻结快照、评分、证据版本、未公开先导数据标注）；
- **会签可恢复**：多签会签状态持久化，服务重启后继续尚未结束的会签；
- **主管可解释、可比较**：通过 API 解释一项建议为何进入探索/验证/候补/拒绝状态，并比较组合调整对前沿覆盖与关键依赖的影响。

## 目录

- `src/science_strategy_foundation/`
  - `service.py`：基础服务（主体、角色、幂等、事务、审计）；
  - `review.py`：研究组合审议领域服务（依赖图、快照、回避、决定留痕、组合原子占用、会签）；
  - `storage.py`：SQLite 建表与短事务（含部分唯一索引保证唯一生效版本与单一占用）；
  - `models.py` / `errors.py` / `domain.py` / `clock.py` / `audit.py`：数据对象、业务异常、类别、时钟、哈希链；
  - `api.py`：仅依赖标准库的 HTTP/JSON 边界；
  - `acceptance.py`：离线端到端验收。
- `tests/`：存储事务、基础服务、审议领域、HTTP 路由与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
```

验收在临时 SQLite 数据库中走完整条链路：基础登记 → 团队/设施/证据/问题依赖图 → 轮次与建议 → 截止快照（补交被拒）→ 利益冲突回避与独立评审人数复核 → 评分与决定 → 组合会签 → **关闭并重启服务后续签生效** → 竞争占用被拒、显式取代原子生效 → 撤回留痕、解释与组合比较。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database review.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`（同时返回审计链校验结果）。写入接口通过 `X-Actor-Id` 标识操作者，所有写操作需携带幂等键 `request_id`；服务重启后 SQLite 中的业务状态、会签进度与审计历史继续保留。

## 领域模型

| 对象 | 说明 |
| --- | --- |
| 团队 `teams` / 成员 | 申请团队与成员 actor；成员评审本团队建议时同样构成利益冲突 |
| 合作关系 `reviewer_collaborations` | 评审人与团队的合作披露；事后登记会把既有在途分配自动标记为 `conflicted` |
| 证据版本 `evidence_versions` | 先导数据按 `(evidence_id, version)` 不可变版本保存，区分是否已公开 |
| 科学问题 `questions` | 同义术语归并到唯一问题（术语全局唯一），问题间依赖图禁止成环 |
| 设施 `facilities` | 稀缺资源，生效组合对其互斥占用 |
| 轮次 `rounds` / 资源池 | 一轮征集含截止时间、预算总额、最低独立评审人数；`open → closed` |
| 建议 `proposals` | 可证伪判据、预算、可选设施、证据版本引用；截止前修订产生新版本 |
| 路线 `routes` | 研究路线，可整体并入目标建议并记录来源 |
| 快照 `review_snapshots` | 截止时对建议版本与内容哈希求基线摘要，冻结为评审依据 |
| 评审分配/评分 | `assigned / recused / conflicted` 三态；决定前校验独立评审人数 |
| 决定 `decisions` | 追加式、每建议递增序号，记录快照、评分、证据、条件、被取代决定 |
| 组合 `portfolios` | `draft → countersigning → effective / superseded / rejected`；部分唯一索引保证每轮至多一个 `effective` |
| 资源占用 `resource_allocations` | 生效时在单事务内插入；预算、设施、建议三个维度的部分唯一索引保证互斥 |
| 会签 `countersign_*` | 多人有序签署，任一否决即驳回；状态持久化可跨重启恢复 |

## 主要 HTTP 接口

写入均为 `POST` 且需 `request_id`；查询为 `GET`。

```
POST /teams                       登记申请团队（含成员）
POST /collaborations              披露评审人-团队合作关系
POST /facilities                  登记稀缺设施
POST /evidence                    登记先导证据首版本
POST /evidence/versions           追加证据新版本
POST /questions                   登记科学问题（terms 归并同义术语，depends_on 建依赖边）
POST /rounds                      开启征集轮次（预算总额、最低独立评审人数）
POST /proposals                   提交/截止前修订建议（可证伪判据、预算、设施、证据版本引用）
POST /routes                      为建议登记研究路线
POST /rounds/{id}/close           截止并冻结评审快照
POST /proposals/{id}/reviewers    分配评审人（命中利益冲突返回 403）
POST /proposals/{id}/recusals     评审人主动回避
POST /proposals/{id}/scores       独立评审人评分
POST /proposals/{id}/decisions    作出 explore/validate/waitlist/reject/conditional 决定
POST /proposals/{id}/reconsiderations  复议（保留原决定与依据）
POST /proposals/{id}/withdraw     撤回（保留原决定）
POST /proposals/{id}/merge        把该建议的路线并入目标建议
POST /portfolios                  提出获批组合（可带 supersedes_portfolio_id 声明取代）
POST /portfolios/{id}/countersign/start  发起多签会签
POST /portfolios/{id}/countersign        某位会签人同意/否决（可跨重启继续）

GET  /dependency-graph?round_id=…        科学问题依赖图与术语归并
GET  /rounds/{id}/snapshot               冻结快照内容与基线哈希
GET  /proposals/{id}/explanation         解释当前状态的成因与依据
GET  /proposals/{id}/decisions           追加式决定历史
GET  /proposals/{id}/assignments         评审分配与回避状态
GET  /portfolios/{id}                    组合状态与条目
GET  /portfolios/{id}/countersign        会签进度（重启后续签依据）
GET  /portfolio-comparison?a=…&b=…       比较两组合的前沿覆盖、关键依赖与预算变化
```

业务错误以稳定的 `error` 代码返回，例如 `submission_closed`（截止后补交）、`review_quorum`（独立评审人数不足）、`resource_contention`（预算/设施原子占用冲突或出现第二生效版本）、`workflow_state`（状态不允许）、`permission_denied`（利益冲突或角色不足）。

## 关键一致性保证

- **快照不可静默改写**：截止后提交/修订直接返回 `submission_closed`；证据新版本不会改变建议中钉住的版本引用；所有决定的 `basis` 都记录 `snapshot_id`、`baseline_hash` 与当时的 `payload_hash`。
- **回避后人数复核**：作出决定时统计 `assigned` 评审数，低于轮次 `min_reviewers` 返回 `review_quorum`，需重新分配补足。
- **原子占用与唯一生效**：会签全部通过时，在单个 `BEGIN IMMEDIATE` 事务内完成「释放被显式取代的旧组合 → 预算/设施/建议三重冲突校验 → 写入占用 → 置为生效」。竞争组合若未声明取代目标则被拒绝并整体回滚；数据库层的部分唯一索引进一步保证每轮至多一个 `effective` 组合、每个设施与每条建议至多一个未释放占用。
- **决定追加不覆盖**：撤回、合并、复议都新增一条带递增序号与 `supersedes` 指针的决定，原记录与适用依据不被修改。
- **会签持久化**：每位签署人状态独立落库；重启后通过 `GET /portfolios/{id}/countersign` 读取待签人并继续，重复的同 `request_id` 请求返回原始幂等回执。
