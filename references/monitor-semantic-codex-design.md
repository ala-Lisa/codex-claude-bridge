# 监控页 Codex 语义降噪设计

## 目标

保持 Claude 实时输出和完整脱敏审计日志，同时把监控页与终端中的 Codex
内部事件压缩为一个运行状态和一个结构化结论，避免
`command_execution started/completed` 反复占满事件流。

## 行为

- Codex 审查开始时显示一个“Codex 正在审查”状态。
- `item.started`、`item.completed` 只更新内部活动计数，不新增事件行，也不在终端打印。
- Codex 隐藏推理永不显示；审查未完成时只显示状态。
- `FAIL` 只显示一次经过脱敏的 `next_instructions`，内容与交给同一 Claude/DeepSeek
  会话的修复指令一致。
- `PASS` 只显示一次“审核通过”。
- `NEEDS_INPUT` 保持黄色问题、原因和选项提示。
- 完整脱敏 Codex JSONL 继续保存到 `.codex-bridge/outputs/`，不删减审计证据。

## 时间格式

监控页使用自然中文时长：`8.4秒`、`2分15.6秒`、`1小时2分3.4秒`。
事件保留一位小数，总运行时间显示整数秒。格式化由 Python 端统一生成，浏览器只展示，
避免多个 JavaScript 位置产生不同格式。

## 非目标

- 不显示 Codex 隐藏推理。
- 不删除或压缩落盘 JSONL。
- 不改变模型、权限、锁、会话复用、验证闸门或自动循环。
- 不增加网页控制或回答提交能力。

## 验收

- 100 个 Codex item 事件不产生 200 条事件行。
- FAIL 修复指令在网页事件流恰好出现一次，并与下一轮 Claude 指令一致。
- PASS、FAIL、NEEDS_INPUT 状态仍可区分。
- 59.9、60、3599.9、3600 秒边界格式正确。
- 模拟测试保留完整 Codex JSONL，且无凭据泄漏。
