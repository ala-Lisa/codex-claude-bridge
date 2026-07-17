"""Validated local control data for the Codex-Claude bridge monitor."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import secrets
import threading
from typing import Any, NamedTuple


PUBLIC_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
BROWSER_CODEX_MODEL_SLUGS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
BROWSER_REASONING_EFFORTS = ("max", "xhigh", "high")


class ControlConfigError(RuntimeError):
    """The local Codex model catalog cannot be used safely."""


class ControlRequestError(RuntimeError):
    """A browser control request cannot be applied safely."""

    def __init__(self, reason: str, status_code: int) -> None:
        super().__init__(reason)
        self.status_code = status_code


class CodexModelOption(NamedTuple):
    slug: str
    display_name: str
    reasoning_efforts: tuple[str, ...]


class CodexControlSelection(NamedTuple):
    model: str
    effort: str


class ControlAction(NamedTuple):
    kind: str
    selection: CodexControlSelection | None
    review_no: int | None


DEFAULT_CODEX_SELECTION = CodexControlSelection(
    model="gpt-5.6-sol",
    effort="max",
)


def load_codex_model_catalog(path: Path) -> tuple[CodexModelOption, ...]:
    """Read and freeze the public subset of a local Codex model cache."""
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlConfigError("无法读取 Codex 模型目录") from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("models"), list
    ):
        raise ControlConfigError("Codex 模型目录无效")

    result: list[CodexModelOption] = []
    seen: set[str] = set()
    allowed = frozenset(PUBLIC_REASONING_EFFORTS)
    for item in payload["models"]:
        if not isinstance(item, dict):
            raise ControlConfigError("Codex 模型目录无效")
        visibility = item.get("visibility")
        if not isinstance(visibility, str):
            raise ControlConfigError("Codex 模型目录无效")
        if visibility != "list":
            continue
        slug = item.get("slug")
        display_name = item.get("display_name")
        levels = item.get("supported_reasoning_levels")
        if (
            not isinstance(slug, str)
            or not slug.strip()
            or slug != slug.strip()
            or not isinstance(display_name, str)
            or not display_name.strip()
            or not isinstance(levels, list)
            or not levels
            or slug in seen
        ):
            raise ControlConfigError("Codex 模型目录无效")
        efforts: list[str] = []
        for level in levels:
            if not isinstance(level, dict):
                raise ControlConfigError("Codex 模型目录无效")
            effort = level.get("effort")
            if not isinstance(effort, str) or not effort.strip():
                raise ControlConfigError("Codex 模型目录无效")
            if effort in allowed and effort not in efforts:
                efforts.append(effort)
        if not efforts:
            raise ControlConfigError("Codex 模型目录无效")
        seen.add(slug)
        result.append(CodexModelOption(
            slug=slug,
            display_name=display_name,
            reasoning_efforts=tuple(efforts),
        ))
    if not result:
        raise ControlConfigError("Codex 模型目录无效")
    return tuple(result)


def browser_codex_model_catalog(
    catalog: tuple[CodexModelOption, ...],
) -> tuple[CodexModelOption, ...]:
    """Return the fixed, ordered 5.6-family browser control choices."""
    by_slug = {item.slug: item for item in catalog}
    result: list[CodexModelOption] = []
    for slug in BROWSER_CODEX_MODEL_SLUGS:
        item = by_slug.get(slug)
        if item is None:
            continue
        efforts = tuple(
            effort for effort in BROWSER_REASONING_EFFORTS
            if effort in item.reasoning_efforts
        )
        if efforts:
            result.append(CodexModelOption(
                slug=item.slug,
                display_name=item.display_name,
                reasoning_efforts=efforts,
            ))
    if not result:
        raise ControlConfigError("Codex 浏览器模型目录无效")
    return tuple(result)


class ReviewControl:
    """Own browser-requested review configuration and stop state."""

    def __init__(
        self,
        catalog: tuple[CodexModelOption, ...],
        effective: CodexControlSelection,
        *,
        token: str | None = None,
        allow_legacy_effective: bool = False,
    ) -> None:
        if not isinstance(catalog, tuple) or not catalog:
            raise ControlConfigError("Codex 模型目录无效")
        self._catalog = catalog
        self._models = {item.slug: item for item in catalog}
        if allow_legacy_effective:
            if (
                not isinstance(effective.model, str)
                or not effective.model.strip()
                or effective.model != effective.model.strip()
                or not isinstance(effective.effort, str)
                or not effective.effort.strip()
                or effective.effort != effective.effort.strip()
            ):
                raise ControlConfigError("Codex 模型配置无效")
        else:
            self._validate_selection(effective.model, effective.effort)
        if token is not None and (not isinstance(token, str) or not token):
            raise ControlConfigError("控制令牌无效")
        self._token = token or secrets.token_urlsafe(32)
        self._condition = threading.Condition()
        self._effective = effective
        self._pending: CodexControlSelection | None = None
        self._phase = "IDLE"
        self._active_review_no: int | None = None
        self._restart_used_review_no: int | None = None
        self._stop_requested = False
        self._stopped = False
        self._action: ControlAction | None = None

    @property
    def token(self) -> str:
        return self._token

    def _validate_selection(
        self,
        model: Any,
        effort: Any,
    ) -> CodexControlSelection:
        if not isinstance(model, str) or not isinstance(effort, str):
            raise ControlRequestError("Codex 模型配置无效", 400)
        option = self._models.get(model)
        if option is None or effort not in option.reasoning_efforts:
            raise ControlRequestError("Codex 模型配置无效", 400)
        return CodexControlSelection(model, effort)

    @staticmethod
    def _selection_dict(
        selection: CodexControlSelection | None,
    ) -> dict[str, str] | None:
        if selection is None:
            return None
        return {"model": selection.model, "effort": selection.effort}

    def public_snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "effective": self._selection_dict(self._effective),
                "pending": self._selection_dict(self._pending),
                "phase": self._phase,
                "active_review_no": self._active_review_no,
                "restart_used": (
                    self._active_review_no is not None
                    and self._restart_used_review_no == self._active_review_no
                ),
                "stop_requested": self._stop_requested,
                "stopped": self._stopped,
            }

    def bootstrap(self) -> dict[str, Any]:
        return {
            "token": self._token,
            "models": [
                {
                    "slug": item.slug,
                    "display_name": item.display_name,
                    "reasoning_efforts": list(item.reasoning_efforts),
                }
                for item in self._catalog
            ],
            "control": self.public_snapshot(),
        }

    def set_phase(self, phase: str) -> None:
        if not isinstance(phase, str) or not phase:
            raise ValueError("phase must be a non-empty string")
        with self._condition:
            self._phase = phase
            if phase != "CODEX_REVIEWING":
                self._active_review_no = None
            self._condition.notify_all()

    def mark_review_started(self, review_no: int) -> None:
        if (
            not isinstance(review_no, int)
            or isinstance(review_no, bool)
            or review_no <= 0
        ):
            raise ValueError("review_no must be a positive integer")
        with self._condition:
            if self._active_review_no != review_no:
                self._restart_used_review_no = None
            self._active_review_no = review_no
            self._phase = "CODEX_REVIEWING"
            self._condition.notify_all()

    def mark_review_restart_used(self, review_no: int) -> None:
        if (
            not isinstance(review_no, int)
            or isinstance(review_no, bool)
            or review_no <= 0
        ):
            raise ValueError("review_no must be a positive integer")
        with self._condition:
            if self._active_review_no != review_no:
                raise ValueError("review is not active")
            self._restart_used_review_no = review_no
            self._condition.notify_all()

    def mark_effective(self, selection: CodexControlSelection) -> None:
        validated = (
            selection
            if selection == self._effective
            else self._validate_selection(selection.model, selection.effort)
        )
        with self._condition:
            self._effective = validated
            if self._pending == validated:
                self._pending = None
            self._condition.notify_all()

    def selection_for_next_review(self) -> CodexControlSelection:
        with self._condition:
            return self._pending or self._effective

    def request_selection(
        self,
        model: Any,
        effort: Any,
        *,
        persist: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        selection = self._validate_selection(model, effort)
        with self._condition:
            if self._stop_requested:
                raise ControlRequestError("任务正在停止", 409)
            active = (
                self._phase == "CODEX_REVIEWING"
                and self._active_review_no is not None
            )
            if active:
                if self._restart_used_review_no == self._active_review_no:
                    raise ControlRequestError("本轮审核已重启过一次", 409)
                if selection == self._effective and self._pending is None:
                    return {
                        "mode": "unchanged",
                        "control": self.public_snapshot(),
                    }
                previous_pending = self._pending
                previous_restart = self._restart_used_review_no
                self._pending = selection
                self._restart_used_review_no = self._active_review_no
                mode = "restart"
            else:
                previous_pending = self._pending
                previous_restart = self._restart_used_review_no
                self._pending = selection
                mode = "next_review"
            snapshot = self.public_snapshot()
            persisted = persist is None
            try:
                if persist is not None:
                    persist(snapshot)
                    persisted = True
            finally:
                if not persisted:
                    self._pending = previous_pending
                    self._restart_used_review_no = previous_restart
            if active:
                self._action = ControlAction(
                    "restart", selection, self._active_review_no)
            self._condition.notify_all()
            return {"mode": mode, "control": snapshot}

    def request_stop(
        self,
        *,
        persist: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        with self._condition:
            if self._stop_requested:
                return {
                    "status": "stop_requested",
                    "control": self.public_snapshot(),
                }
            previous_phase = self._phase
            self._stop_requested = True
            self._phase = "USER_STOPPING"
            snapshot = self.public_snapshot()
            persisted = persist is None
            try:
                if persist is not None:
                    persist(snapshot)
                    persisted = True
            finally:
                if not persisted:
                    self._stop_requested = False
                    self._phase = previous_phase
            self._action = ControlAction("stop", None, self._active_review_no)
            self._condition.notify_all()
            return {"status": "stop_requested", "control": snapshot}

    def take_action(self, timeout: float = 0.0) -> ControlAction | None:
        with self._condition:
            if self._action is None and timeout > 0:
                self._condition.wait_for(
                    lambda: self._action is not None,
                    timeout=timeout,
                )
            action = self._action
            self._action = None
            return action

    def mark_stopped(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._stopped = True
            self._phase = "USER_STOPPED"
            self._action = None
            self._condition.notify_all()
