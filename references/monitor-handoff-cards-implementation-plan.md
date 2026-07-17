# Bridge 双向交接卡 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在监控页完整显示每轮 `Claude -> Codex` 最终报告和 `Codex -> Claude` 结构化审核结论，同时保持事件聚合、脱敏和单页历史折叠。

**Architecture:** 桥接器在两个真实交接边界发布权威的 `claude_handoff` 与 `codex_decision` 事件；MonitorState 保存脱敏结构化负载并抑制旧分支事件的重复行；浏览器把两类事件渲染为按轮次分组的可折叠主卡。原始模型 JSONL、状态机和模型调用协议不变。

**Tech Stack:** Python 3 标准库、`unittest`、原生 HTML/CSS/JavaScript、HTTP Server-Sent Events。

## Global Constraints

- 仅修改 `/home/a8/.codex/skills/codex-claude-bridge/` 内文件。
- 不调用真实 Claude、DeepSeek 或 Codex；所有集成测试使用模拟可执行文件和临时 Git 仓库。
- 不安装依赖，不使用 `bypassPermissions`，不降低锁、脏工作区、轮次、验证门或脱敏保护。
- 不显示 Codex 隐藏推理或内部 item 事件。
- 不提交、不推送、不修改 Git 历史。
- 当前轮交接卡默认展开；历史轮默认折叠但可手动展开；正文不截断。

---

### Task 1: 发布权威双向交接事件

**Files:**
- Modify: `tests/test_bridge.py`
- Modify: `scripts/bridge.py`

**Interfaces:**
- Consumes: `run_claude(...) -> str` 与 `run_codex(...) -> dict[str, Any]`。
- Produces: `claude_handoff` 和 `codex_decision` 两类脱敏 bridge events。

- [ ] **Step 1: 写失败测试，证明 Claude 报告与 Codex 结论完整发布**

在现有 FAIL -> PASS 模拟集成测试中解析 `.codex-bridge/events.jsonl`，断言每次实施恰有一个 `claude_handoff`，其 `message` 与对应 `round-NN-codex.md` 中 `Claude's report` 或 `Latest Claude correction report` 段落一致；每次审核恰有一个 `codex_decision`，并精确包含：

```python
{
    "status": "FAIL",
    "evidence": ["..."],
    "remaining_issues": ["..."],
    "next_instructions": "fix it",
    "question": "",
    "reason": "",
    "options": [],
}
```

同时断言 FAIL 的 `next_instructions` 与下一次模拟 Claude stdin 中的实际修复指令相同。

- [ ] **Step 2: 运行定向测试并确认 RED**

Run:

```bash
python3 -m unittest -v tests.test_bridge.BridgeTests.test_streams_live_redacts_and_preserves_fail_pass_flow
```

Expected: FAIL，因为当前没有 `claude_handoff` 或统一 `codex_decision` 事件。

- [ ] **Step 3: 在真实交接边界发布事件**

在 `loop()` 中：

```python
claude_result = self.run_claude(instruction, attempt_no)
self.event(
    "claude_handoff",
    message=claude_result,
    attempt=attempt_no,
)
```

在 `run_codex()` 返回且 `last_review` 保存之后、PASS/FAIL/NEEDS_INPUT 分支之前：

```python
self.event(
    "codex_decision",
    message=f"Codex {review['status']}",
    attempt=attempt_no,
    review=review_no,
    status=review["status"],
    evidence=review["evidence"],
    remaining_issues=review["remaining_issues"],
    next_instructions=review["next_instructions"],
    question=review["question"],
    reason=review["reason"],
    options=review["options"],
)
```

所有字段继续由 `event()` 的现有 sanitizer 处理。不得把 Codex 原始 JSONL 或 reasoning 复制进事件。

- [ ] **Step 4: 运行定向测试并确认 GREEN**

Run 同 Step 2。Expected: PASS。

---

### Task 2: MonitorState 去重并保存结构化结论

**Files:**
- Modify: `tests/test_monitor.py`
- Modify: `scripts/monitor.py`

**Interfaces:**
- Consumes: Task 1 的 `claude_handoff`、`codex_decision` 和旧的 `task_passed`、`codex_handoff`、`input_required`。
- Produces: 一份 Claude 交接事件和一份 Codex 决策事件；旧状态事件仍更新状态但不生成重复 feed 行。

- [ ] **Step 1: 写失败测试覆盖完整内容、三种状态和去重**

新增测试依次发布：

```python
state.publish("claude_handoff", {
    "message": "第一段\n\n- 完整列表",
    "attempt": 2,
})
state.publish("codex_decision", {
    "message": "Codex FAIL",
    "attempt": 2,
    "review": 1,
    "status": "FAIL",
    "evidence": ["证据一"],
    "remaining_issues": ["问题一"],
    "next_instructions": "完整修复指令",
    "question": "",
    "reason": "",
    "options": [],
})
```

断言换行与列表完整保留、结构化字段位于 `details`、同轮每方向只有一项。再分别覆盖 PASS 与 NEEDS_INPUT。发布兼容事件后断言 feed 数量不增加，但 `awaiting_input` 仍正确设置。

修改现有 `result` 测试，断言 stream-json result 只更新 telemetry，不再生成第二张 `claude_result` 交接卡。

- [ ] **Step 2: 运行 MonitorState 定向测试并确认 RED**

Run:

```bash
python3 -m unittest -v \
  tests.test_monitor.MonitorStateTests.test_handoff_events_preserve_full_structured_content \
  tests.test_monitor.MonitorStateTests.test_legacy_outcome_events_do_not_duplicate_handoff_cards
```

Expected: FAIL，因为旧分支事件仍追加且 stream result 仍生成 `claude_result` 行。

- [ ] **Step 3: 最小实现去重规则**

在 `publish()` 中让 `task_passed`、`codex_handoff` 和 `input_required` 完成原有状态更新后设置 `append_event = False`。`claude_handoff` 与 `codex_decision` 使用通用安全 details 保留全部字段。

在 `consume_claude()` 的 `event_type == "result"` 分支保留 usage、cost、speed、context 更新，但删除 `_append_event_locked("claude_result", ...)`，因为 Task 1 的 bridge event 是实际交给 Codex 的权威文本。

- [ ] **Step 4: 运行 MonitorState 测试并确认 GREEN**

Run Task 2 Step 2 命令，再运行：

```bash
python3 -m unittest -v tests.test_monitor.MonitorStateTests tests.test_monitor.MonitorSamplerTests
```

Expected: 全部 PASS。

---

### Task 3: 渲染当前轮展开、历史轮折叠的完整交接卡

**Files:**
- Modify: `assets/monitor.html`
- Modify: `tests/test_monitor.py`

**Interfaces:**
- Consumes: snapshot `events` 中 `claude_handoff` 与 `codex_decision` 的 `message/details`。
- Produces: 原生 `<details class="handoff-card">` 交接卡、复制按钮、结构化 Codex 字段列表。

- [ ] **Step 1: 写失败的页面契约测试**

扩展 `test_page_is_loopback_only_self_contained_and_hardened`，断言页面含：

```text
handoff-card
Claude → Codex
Codex → Claude
审核证据
剩余问题
给 Claude 的指令
复制完整内容
```

并断言脚本仍使用 `textContent`，不使用 `innerHTML`。新增 Node 语法检查，把 `<script>` 内容传给 `node --check -`；若 `node` 不可用则明确跳过并在最终报告列为未验证，不安装依赖。

- [ ] **Step 2: 运行页面契约测试并确认 RED**

Run:

```bash
python3 -m unittest -v tests.test_monitor.LiveMonitorServerTests.test_page_is_loopback_only_self_contained_and_hardened
```

Expected: FAIL，因为交接卡结构和文案尚不存在。

- [ ] **Step 3: 实现交接卡 DOM 与样式**

在现有 feed 内对两类事件使用专用渲染路径：

```javascript
const isHandoff = kind => kind === 'claude_handoff' || kind === 'codex_decision';
```

卡片必须使用 DOM API 创建 `details/summary/section/ul/li/pre/button`；正文节点设置 `textContent` 和 `white-space: pre-wrap`。`codex_decision` 固定呈现状态、证据、剩余问题、指令以及 NEEDS_INPUT 字段。空问题列表显示“无”，PASS 指令显示“无后续修复指令”。

维护：

```javascript
const handoffOpenState = new Map();
```

最高 `attempt` 默认 `open = true`，较旧 attempt 默认 false；用户触发 `toggle` 后把选择存入 map，后续 SSE render 不覆盖。复制按钮拼接卡中完整可见语义文本后调用 `navigator.clipboard.writeText()`。

不得设置正文 `max-height`、`overflow: hidden` 或字符截断。搜索继续以 `row.textContent` 匹配，因此覆盖折叠卡内部文本。

- [ ] **Step 4: 运行页面测试和 JavaScript 语法检查并确认 GREEN**

Run:

```bash
python3 -m unittest -v tests.test_monitor.LiveMonitorServerTests
python3 - <<'PY' | node --check -
from pathlib import Path
text = Path('assets/monitor.html').read_text(encoding='utf-8')
start = text.index('<script>') + len('<script>')
end = text.index('</script>', start)
print(text[start:end])
PY
```

Expected: 测试 PASS，Node 退出码 0。

---

### Task 4: 协议文档与完整离线验收

**Files:**
- Modify: `SKILL.md`
- Modify: `references/protocol.md`
- Test: `tests/test_bridge.py`
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: Tasks 1-3 已验证行为。
- Produces: 与实现一致的操作说明和最终验证证据。

- [ ] **Step 1: 更新文档**

在 `SKILL.md` 与 `references/protocol.md` 明确：监控页显示完整脱敏双向交接；当前轮展开、历史轮折叠；Codex 内部 reasoning/item 仍不显示；完整 JSONL 继续保留。

- [ ] **Step 2: 执行语法和 Skill 校验**

Run:

```bash
python3 -m py_compile scripts/bridge.py scripts/monitor.py tests/test_bridge.py tests/test_monitor.py
python3 /home/a8/.codex/skills/.system/skill-creator/scripts/quick_validate.py /home/a8/.codex/skills/codex-claude-bridge
```

Expected: 退出码 0；输出 `Skill is valid!`。

- [ ] **Step 3: 执行完整普通与严格测试**

Run:

```bash
python3 -m unittest discover -s tests -q
python3 -W error -m unittest discover -s tests -q
```

Expected: 两次均全部 PASS，无 warning。

- [ ] **Step 4: 启动模拟窗口验收**

使用临时脚本和模拟事件启动 `LiveMonitor`，不得调用真实模型。通过 HTTP/SSE 检查：页面 200、连续 revision、完整 Claude/Codex 交接正文、旧 item 事件未进入 feed、敏感测试文本已脱敏。打开 loopback 浏览器窗口供用户人工确认。

- [ ] **Step 5: 检查范围**

Run:

```bash
find /home/a8/.codex/skills/codex-claude-bridge -type f -not -path '*/__pycache__/*' -printf '%P\n' | sort
git -C /home/a8/objects/Mitochondrial_Database status --short
```

Expected: 只有计划内 Skill 文件发生变化；数据库项目原有未提交工作保持不变。报告浏览器自动化未执行的原因（若仍缺少 Playwright）。
