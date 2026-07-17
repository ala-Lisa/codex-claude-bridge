# Codex 额度节流实施计划

> **执行要求：** 本计划由当前 Codex 会话按任务顺序直接实施。禁止调用真实 Claude、DeepSeek 或 Codex；禁止提交、推送或修改 Git 历史。

**目标：** 为桥接器增加已批准命令的独立验证闸门，使明显失败直接返回 DeepSeek，仅把绿色候选交给 Codex，并减少后续 Codex 提示的重复上下文。

**架构：** Claude 流结束后进入 `VERIFYING`。桥接器以参数数组运行验证清单；非零退出返回同一 Claude 会话，全部为零才调用 Codex。Claude 实施尝试和 Codex 审查分别计数；后续 Codex 审查复用会话并发送增量证据。

**技术栈：** Python 标准库、`subprocess.Popen`、JSON、SHA-256、现有 SSE 监控页、`unittest` 模拟可执行文件。

## 全局约束

- 仅修改设计规范列出的 Skill 文件。
- 验证命令必须使用 `shell=False` 和参数数组。
- 所有模型和命令输出必须在显示或保存前脱敏。
- Claude/Codex 测试必须使用模拟可执行文件。
- Claude 文本仍实时推送；本地状态默认每 5 秒采样；图表每 10 秒记录。
- `PASS` 只能来自 Codex；桥接器验证成功只代表 `VERIFICATION_PASSED`。
- 不新增依赖，不使用 `bypassPermissions`。

---

### 任务 1：验证清单与运行上限

**文件：**
- 修改：`scripts/bridge.py`
- 测试：`tests/test_bridge.py`

**接口：**
- 新增：`load_verification_manifest(path: str) -> tuple[str, tuple[tuple[str, ...], ...]]`
- 新增参数：`--verification-file`、`--verification-timeout`、`--max-implementation-attempts`、`--max-codex-reviews`
- 兼容参数：`--max-rounds` 作为 `--max-codex-reviews` 别名
- 新状态字段：`implementation_attempts`、`codex_reviews`、`max_implementation_attempts`、`max_codex_reviews`、`verification_manifest_sha256`、`verification_commands`

- [ ] **步骤 1：先写清单格式、敏感内容、冲突参数和恢复摘要冲突测试**

测试必须断言非法输入在模拟 Claude/Codex 计数文件出现前失败，并覆盖大小、UTF-8、JSON 结构、`bool` 版本、命令数量、参数类型、NUL、换行、Shell 控制参数和凭据形态。

- [ ] **步骤 2：运行定向测试并确认 RED**

```bash
python3 -m unittest tests.test_bridge.VerificationManifestTests -v
```

预期：新增接口或参数尚不存在导致失败。

- [ ] **步骤 3：实现最小清单解析与计数器迁移**

核心返回类型固定为不可变 tuple：

```python
def load_verification_manifest(path_value: str) -> tuple[str, tuple[tuple[str, ...], ...]]:
    """返回文件 SHA-256 和已验证命令参数数组。"""
```

首次运行保存命令快照；恢复时未传文件使用快照，传入文件则比较摘要。旧状态把 `max_rounds` 原值迁移为 `max_codex_reviews`。

- [ ] **步骤 4：运行定向测试并确认 GREEN**

```bash
python3 -m unittest tests.test_bridge.VerificationManifestTests -v
```

预期：全部通过。

---

### 任务 2：独立验证命令执行器

**文件：**
- 修改：`scripts/bridge.py`
- 测试：`tests/test_bridge.py`

**接口：**
- 新增：`Bridge.run_verification(attempt_no: int) -> tuple[bool, str]`
- 输出：`.codex-bridge/outputs/attempt-NN-verification.json`

- [ ] **步骤 1：先写非零退出、超时、启动失败、双管道大输出和脱敏测试**

测试区分：已启动命令的非零退出或超时返回可修复失败；命令无法启动、管道错误或持久化错误产生受控 `BridgeError`。

- [ ] **步骤 2：运行定向测试并确认 RED**

```bash
python3 -m unittest tests.test_bridge.VerificationRunnerTests -v
```

- [ ] **步骤 3：实现并发排空和有限输出**

实现必须使用：

```python
subprocess.Popen(
    list(argv),
    cwd=self.repo,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    shell=False,
    text=True,
    encoding="utf-8",
    errors="replace",
)
```

stdout/stderr 用两个线程排空，每个脱敏流最多保留 20,000 个字符；结果文件只保存脱敏值。

- [ ] **步骤 4：运行定向测试并确认 GREEN**

```bash
python3 -m unittest tests.test_bridge.VerificationRunnerTests -v
```

---

### 任务 3：自动验证—修复—审查状态机

**文件：**
- 修改：`scripts/bridge.py`
- 测试：`tests/test_bridge.py`

**接口：**
- `Bridge.loop()` 分别递增实施尝试和 Codex 审查计数。
- `Bridge.run_codex(...)` 仅在验证闸门成功后调用。
- 状态新增 `VERIFYING`，保留 `AWAITING_INPUT`、`PASS` 和 `STOPPED`。

- [ ] **步骤 1：先写完整模拟循环测试**

必须覆盖：

```text
验证 FAIL → Claude 修复 → 验证 PASS → Codex PASS
Codex FAIL → Claude 修复 → 验证 PASS → Codex PASS
持续验证 FAIL → 达实施上限，Codex 0 次
持续 Codex FAIL → 达审查上限，不多调用子进程
NEEDS_INPUT → 不再次调用 Claude
```

- [ ] **步骤 2：运行定向测试并确认 RED**

```bash
python3 -m unittest tests.test_bridge.VerificationLoopTests -v
```

- [ ] **步骤 3：实现状态机并保留同一会话**

验证失败指令只包含命令索引、退出码、超时标志和有限脱敏输出。Codex FAIL 使用结构化 `next_instructions`。两种修复均通过原 `claude_session_id` 的 `--resume` 继续。

- [ ] **步骤 4：运行定向测试并确认 GREEN**

```bash
python3 -m unittest tests.test_bridge.VerificationLoopTests -v
```

---

### 任务 4：分级 Codex 提示与证据压缩

**文件：**
- 修改：`scripts/bridge.py`
- 测试：`tests/test_bridge.py`

**接口：**
- 第一次审查：完整任务、计划、验证证据、状态、统计、最多 20,000 字符 diff。
- 后续审查：上次问题、最新报告、验证证据、状态、统计、变更路径；不含任务、计划和 diff 正文。

- [ ] **步骤 1：先写首轮和后续提示内容精确断言**

同时断言后续 Codex 参数包含原 `codex_session_id`，缺少会话 ID 时受控停止。

- [ ] **步骤 2：运行定向测试并确认 RED**

```bash
python3 -m unittest tests.test_bridge.CodexPromptTierTests -v
```

- [ ] **步骤 3：拆分完整证据和增量证据生成函数**

Codex 指令必须增加：一次性报告本轮发现的全部范围内阻塞问题，但不得加入推测性范围。

- [ ] **步骤 4：运行定向测试并确认 GREEN**

```bash
python3 -m unittest tests.test_bridge.CodexPromptTierTests -v
```

---

### 任务 5：监控页和文档

**文件：**
- 修改：`scripts/monitor.py`
- 修改：`assets/monitor.html`
- 修改：`SKILL.md`
- 修改：`references/protocol.md`
- 测试：`tests/test_monitor.py`

**接口：**
- `MonitorSampler(..., interval_seconds=5.0)`
- HUD 分别显示实施尝试和 Codex 审查计数。
- 图表 `setInterval(sampleTelemetry, 10000)` 保持不变。

- [ ] **步骤 1：先写默认 5 秒、实时事件和双计数标签测试**
- [ ] **步骤 2：运行定向测试并确认 RED**

```bash
python3 -m unittest tests.test_monitor -v
```

- [ ] **步骤 3：修改最小监控状态、标签和中文说明**
- [ ] **步骤 4：运行定向测试并确认 GREEN**

```bash
python3 -m unittest tests.test_monitor -v
```

---

### 任务 6：完整离线验收

**文件：**
- 检查全部允许修改文件

- [ ] **步骤 1：运行 Python 语法检查**

```bash
python3 -m py_compile scripts/bridge.py scripts/monitor.py
```

- [ ] **步骤 2：运行 Skill 验证器**

```bash
python3 /home/a8/.codex/skills/.system/skill-creator/scripts/quick_validate.py /home/a8/.codex/skills/codex-claude-bridge
```

- [ ] **步骤 3：运行全部离线测试两次**

```bash
python3 -m unittest discover -s tests -v
python3 -W error -m unittest discover -s tests -v
```

- [ ] **步骤 4：检查禁止项和修改范围**

```bash
rg -n "bypassPermissions|dangerously-bypass|shell=True" .
find . -maxdepth 3 -type f -newermt '2026-07-17 00:00:00' -print
```

预期：无新增依赖、无真实模型调用、无范围外文件修改、无提交或推送。
