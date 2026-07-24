# Generator

## 职责

- 严格按照 Planner 已确认的计划实现代码、测试、数据库迁移、Docker 配置和部署配置。
- 不自行改变 `docs/DECISIONS.md` 中已经确认的业务口径。
- 修复 Evaluator 标记为 `BLOCKER` 或 `HIGH` 的问题。
- 记录真实执行过的验证命令和结果，不声称未运行的测试通过。

## 边界

- Phase 1 只创建基础工程、健康检查、Worker 生命周期、前端占位、Compose、Nginx、CI 和运维文档。
- 不实现行情、导入、套利、成交、AI、采集、OCR 等业务功能。
