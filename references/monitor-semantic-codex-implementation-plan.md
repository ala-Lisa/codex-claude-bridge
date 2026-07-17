# 监控页 Codex 语义降噪实施计划

**目标：** 用语义状态取代 Codex 内部 item 事件刷屏，并统一中文时长显示。

**架构：** `bridge.py` 继续完整保存原始脱敏 JSONL，只把结构化审查结论发布为监控事件；
`monitor.py` 聚合内部活动并生成中文时长；`monitor.html` 展示单一运行状态和最终交接内容。

## 全局约束

- 只修改本 Skill，不修改任何目标项目。
- 不调用真实 Claude、DeepSeek 或 Codex。
- 不提交、不推送、不安装依赖。
- 使用模拟进程和临时 Git 仓库验证。

### 任务 1：中文时长

**文件：** `scripts/monitor.py`、`assets/monitor.html`、`tests/test_monitor.py`

1. 先添加 59.9、60、3599.9、3600 秒边界失败测试。
2. 在 Python 端实现统一格式化，并写入顶层快照与事件。
3. 浏览器改用服务端显示值，删除 `+N秒` 直接拼接。

### 任务 2：Codex 内部事件聚合

**文件：** `scripts/monitor.py`、`scripts/bridge.py`、`tests/test_monitor.py`

1. 先添加大量 item 事件只产生一个审查开始事件的失败测试。
2. item 事件只更新计数与最近类型，不追加事件行。
3. 终端只输出审查开始和最终状态，不打印每个 item。

### 任务 3：结构化交接显示

**文件：** `scripts/bridge.py`、`assets/monitor.html`、`tests/test_bridge.py`、`tests/test_monitor.py`

1. 先添加 FAIL 指令恰好出现一次且与下一轮 Claude 指令一致的失败测试。
2. 发布 `codex_handoff`、`codex_passed` 语义事件；NEEDS_INPUT 沿用现有事件。
3. 浏览器为交接指令显示一个醒目的、可搜索的结果卡片。

### 任务 4：文档与完整验证

**文件：** `SKILL.md`、`references/protocol.md`

1. 记录监控页只展示语义事件、完整 JSONL 仍可审计。
2. 运行语法检查、`quick_validate.py`、普通与严格警告完整测试。
3. 检查危险参数、敏感信息和最终文件范围。
