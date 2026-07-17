# Bridge 双向交接卡设计

## 目标

浏览器监控页必须完整显示每轮实际发生的两次模型交接：

1. `Claude / DeepSeek -> Codex`：Claude 本轮交给 Codex 审核的最终报告。
2. `Codex -> Claude / DeepSeek`：Codex 的结构化结论，以及 FAIL 时实际回传的修复指令。

监控页继续隐藏 Codex 隐藏推理、内部 item 事件、凭据和重复的 Claude 流式片段。

## 非目标

- 不显示 Codex 思维链或未经结构化的内部输出。
- 不把浏览器监控页改为控制面板。
- 不改变 PASS、FAIL、NEEDS_INPUT 的状态机语义。
- 不改变模型调用次数、验证门、轮次限制或会话复用规则。
- 不调用真实 Claude、DeepSeek 或 Codex 完成验证。

## 事件与数据流

### Claude 交接

`run_claude()` 返回的、实际插入 Codex 审核提示词的脱敏 `claude_result` 是权威交接正文。桥接器在进入验证阶段前发布一次专用监控事件：

```json
{
  "event": "claude_handoff",
  "attempt": 2,
  "message": "完整的脱敏 Claude 最终报告"
}
```

监控器不得再把同一轮 stream-json `result` 事件渲染成第二份交接卡。实时 `claude_text` 仍然原位合并显示，最终交接卡与实时输出用途不同，二者不互相替代。

### Codex 交接

Codex 结构化审核通过现有 schema 返回七个字段。桥接器在分支处理 PASS、FAIL 或 NEEDS_INPUT 前发布一次统一事件：

```json
{
  "event": "codex_decision",
  "attempt": 2,
  "review": 2,
  "status": "FAIL",
  "evidence": ["实际证据"],
  "remaining_issues": ["剩余问题"],
  "next_instructions": "实际回传 Claude 的完整修复指令",
  "question": "",
  "reason": "",
  "options": []
}
```

PASS、FAIL、NEEDS_INPUT 均使用同一事件结构。原有 `task_passed`、`codex_handoff` 和 `input_required` 继续承担持久状态或兼容用途，但不得在监控事件带中生成重复结论卡。

FAIL 卡内的 `next_instructions` 必须与下一轮传入 Claude 的文本逐字一致（脱敏后比较）。NEEDS_INPUT 卡必须显示问题、原因和选项。PASS 卡必须显示证据，并明确没有后续修复指令。

## 浏览器布局

事件带增加按实施轮次分组的“双向交接”区域。每轮最多两张主卡：

- 橙色标识：`Claude -> Codex · 本轮实施报告`
- 青色标识：`Codex -> Claude · 审核结论`

当前最高轮次的卡默认展开。更早轮次默认折叠为轮次、方向、状态和时间标题，可逐张展开。正文完整保留段落、列表与换行，不做字符截断；容器随正文增长，不使用内部滚动条隐藏内容。

Codex 卡按固定顺序呈现：

1. 状态。
2. 审核证据。
3. 剩余问题（为空时明确显示“无”）。
4. 给 Claude 的指令；PASS 显示“无后续修复指令”。
5. NEEDS_INPUT 的问题、原因和选项（仅该状态显示）。

所有正文通过 `textContent` 插入 DOM。卡片提供复制按钮，复制的是当前卡完整脱敏正文。现有全文搜索必须覆盖折叠卡内文本。

## 状态与历史

交接卡以 `(attempt, direction)` 作为稳定身份。SSE 快照重复到达时更新同一张卡，不重复追加。事件带仍受现有有界历史限制；被淘汰的最老事件不继续占用浏览器内存。

当收到新轮次时：

- 新轮次两张卡默认展开。
- 旧轮次自动折叠，但用户手动重新展开后，在该页面会话内保留选择。
- Codex 正在审查时只显示一个进行中状态，不显示内部 item start/completed 行。

## 安全与错误处理

- 所有新事件必须先经过现有 `sanitize_value()`。
- 事件和页面不得包含凭据命名环境变量的值、Bearer Token 或 API Key。
- 缺少可选数组时渲染为空列表；错误类型不得导致页面脚本崩溃。
- Claude 或 Codex 交接正文为空属于桥接协议错误，不能伪造“完整结论”。
- 完整脱敏 Claude/Codex JSONL 继续写入 `outputs/`，不改变审计文件格式。

## 验证要求

自动测试必须证明：

1. Claude 最终报告在监控快照中完整出现一次，并与 Codex 提示词中的报告一致。
2. Claude 流式片段继续合并为一行，最终 assistant/result 不造成重复交接卡。
3. PASS、FAIL、NEEDS_INPUT 的七字段结构完整进入 Codex 交接卡。
4. FAIL 指令与下一轮 Claude 收到的指令一致。
5. 100 组 Codex item start/completed 只更新计数，不增加事件行。
6. 当前轮默认展开、历史轮默认折叠，并可保留手动展开状态。
7. 长段落、列表、中文和换行不被截断。
8. HTML 注入文本只作为纯文本显示。
9. 终端、监控快照、SSE 和持久输出均不泄露测试凭据。
10. Python 语法检查、Skill quick validation、普通测试与 `-W error` 全部通过。

浏览器自动化不可用时，必须明确记录该项未验证，并至少执行 JavaScript 语法检查、HTTP/SSE 集成测试和人工浏览器验收。
