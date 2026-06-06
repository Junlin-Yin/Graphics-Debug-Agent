# Plan Template

## Draft Plan Review

请你对这套草案<plan.md>进行评审，重点关注以下要点：

- 功能点是否合理
- 是否完备地覆盖我提出的需求
- 技术方案是否具有可行性
- 有无前后矛盾或模糊不清的细节
- 是否与现有phase，特别是<active-phase>之前的spec兼容，会不会有冲突
- 重排期的方案是否合理
- 对于其中一些模棱两可的决策点，给出你的推荐方案和理由
- 判断当前草案是否可以冻结定版，并开始生成正式的phase文档

## Draft Plan Reflection

多个评审者评审了最新版的草案<plan.md>，汇集了所有评审者的批评意见，见 <review-results.md>，请你：

- 汇总所有批评意见，形成一个结构清晰的列表
- 逐项评估意见是否中肯，如果中肯，给出你的推荐解决方案
- 如果有需要我拍版的地方，请停下来问我，不要随意发挥

## Phase Documents Review

请你评审当前<active-phase>的文档集，结合参考project-contract.md和adr/*文档，主要关注以下要点：
1. 技术方案是否合理可行？
2. 技术细节有无模糊点、矛盾点
3. 是否兼容<active-phase>以前的各个phase，会不会有冲突？
4. 对于发现的问题，尽可能一次性全部详细列出, 不要只列出一部分, 并且给出你的推荐方案和理由
5. 如果有需要我拍版的地方，请停下来告诉我，不要随意发挥
6. 作出最终决定：是否可以冻结现有文档集，开始生成`implementation-plan.md`?
提示：
1. <active-phase>以前的各个phase都已经完成实现并通过验收，不用担心
2. 当前还没有生成`implementation-plan.md`，这是故意的：必须等其他文档迭代成熟后在做实现计划
3. 不要一次性完成全量审阅。请按文件逐个检查，每次最多读 2 个文件；每完成一组就输出 checkpoint：已检查内容、发现的问题、下一步。最后再给 freezing decision。

## Implementation Plan Generation

现在代码合同与技术文档已经迭代成熟，请生成<active-phase>的 implementation-plan.md

生成 `implementation-plan.md` 时，请将其视为“实现执行编排文档（execution planning document）”，而不是 roadmap、架构设计文档或简单 TODO 列表。

implementation-plan 的职责是将已有 contract/spec 文档，转换为：

- 可执行
- 可验证
- 可增量推进
- 可 review
- 可回滚
  的实现路径。

要求：

- 全局/公共 contract 的优先级高于 phase-specific spec。
- 不要重复 architecture.md 已经定义的架构设计。
- 不要机械复述 spec，除非其内容会影响实现顺序或依赖关系。

implementation-plan 必须包含：

1. 明确的：

   - Goals
   - Non-goals
   - Dependency Graph
   - Execution Stages
   - Verification Strategy
   - Migration / Rollback Strategy

2. 按“依赖顺序”组织实现，而不是按时间顺序或功能分类组织。

3. 每个阶段必须满足“增量安全（incrementally safe）”：

   - 仓库始终可编译
   - 测试始终可运行
   - 主流程始终可启动
   - 不允许长期存在半损坏状态

4. 每个阶段必须明确：

   - objective
   - deliverables
   - modified boundaries
   - invariants
   - verification steps
   - freeze/review checkpoint

5. 明确限制修改边界：

   - 允许修改哪些模块
   - 禁止修改哪些区域
   - 哪些兼容性必须保持
   - 哪些系统不变量不可破坏

6. 避免：

   - 巨型重写阶段
   - 模糊目标
   - 隐式依赖
   - scope creep
   - “实现某功能”式的大阶段描述

7. 优先采用：

   - abstraction-first migration
   - dual-path transition
   - incremental replacement
   - deterministic verification
   - small reviewable patches
   - rollback-safe evolution

8. 每个阶段必须产生“客观可验证”的完成证据，例如：

   - compile success
   - unit/integration tests
   - snapshot/golden tests
   - runtime validation
   - deterministic outputs
   - behavioral assertions

最终生成的 implementation-plan 应优化：

- deterministic agent execution
- 低架构漂移
- 低 context churn
- 小型可 review patch
- 长期可演进性
- contract-compliant implementation

格式参考：docs/phase-0/implementation-plan.md

## Implementation Plan Review

请审阅 docs/<active-phase>/implementation-plan.md，关注以下要点：

- 是否完整覆盖<active-phase>的specs, architecture, operations, scope, tests
- 是否可执行、可验证、可增量推进，不会出现反向依赖
- 最终判断：是否可按照该计划进入代码编写阶段？
- 不要一次性完成全量审阅。请按文件逐个检查，每次最多读 2 个文件；每完成一组就输出 checkpoint：已检查内容、发现的问题、下一步。最后再给 freezing decision。
