from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.control import (
    DEFAULT_CODEX_SELECTION,
    CodexControlSelection,
    CodexModelOption,
    ControlConfigError,
    ControlRequestError,
    ReviewControl,
    browser_codex_model_catalog,
    load_codex_model_catalog,
)


class CodexModelCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-control-test-")
        self.root = Path(self.temp.name)
        self.cache = self.root / "models_cache.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, models: list[object]) -> None:
        self.cache.write_text(
            json.dumps({"models": models}),
            encoding="utf-8",
        )

    @staticmethod
    def model(
        slug: str,
        display_name: str,
        efforts: tuple[str, ...],
        *,
        visibility: str = "list",
    ) -> dict[str, object]:
        return {
            "slug": slug,
            "display_name": display_name,
            "visibility": visibility,
            "supported_reasoning_levels": [
                {"effort": effort, "description": "not exposed"}
                for effort in efforts
            ],
            "base_instructions": "must not be returned",
        }

    def test_catalog_keeps_only_visible_models_and_supported_efforts(self) -> None:
        self.write([
            self.model(
                "gpt-5.6-sol",
                "GPT-5.6-Sol",
                ("low", "medium", "high", "xhigh", "max", "ultra"),
            ),
            self.model(
                "gpt-5.6-terra",
                "GPT-5.6-Terra",
                ("low", "medium", "high"),
            ),
            self.model(
                "codex-auto-review",
                "Codex Auto Review",
                ("high",),
                visibility="hide",
            ),
        ])

        catalog = load_codex_model_catalog(self.cache)

        self.assertIsInstance(catalog, tuple)
        self.assertEqual(
            [item.slug for item in catalog],
            ["gpt-5.6-sol", "gpt-5.6-terra"],
        )
        self.assertEqual(catalog[0].display_name, "GPT-5.6-Sol")
        self.assertEqual(
            catalog[0].reasoning_efforts,
            ("low", "medium", "high", "xhigh", "max"),
        )
        self.assertNotIn("ultra", catalog[0].reasoning_efforts)
        self.assertNotIn("base_instructions", repr(catalog))

    def test_default_selection_is_sol_with_maximum_reasoning(self) -> None:
        self.assertEqual(DEFAULT_CODEX_SELECTION.model, "gpt-5.6-sol")
        self.assertEqual(DEFAULT_CODEX_SELECTION.effort, "max")

    def test_browser_catalog_is_limited_to_ordered_5_6_family(self) -> None:
        catalog = (
            CodexModelOption("gpt-5.5", "GPT-5.5", ("high",)),
            CodexModelOption(
                "gpt-5.6-luna",
                "GPT-5.6-Luna",
                ("low", "medium", "high", "xhigh", "max"),
            ),
            CodexModelOption(
                "gpt-5.6-sol",
                "GPT-5.6-Sol",
                ("low", "medium", "high", "xhigh", "max"),
            ),
            CodexModelOption("gpt-5.4", "GPT-5.4", ("high",)),
            CodexModelOption(
                "gpt-5.6-terra",
                "GPT-5.6-Terra",
                ("low", "medium", "high", "xhigh", "max"),
            ),
        )

        result = browser_codex_model_catalog(catalog)

        self.assertEqual(
            [item.slug for item in result],
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        )
        self.assertEqual(
            [item.reasoning_efforts for item in result],
            [("max", "xhigh", "high")] * 3,
        )

    def test_catalog_rejects_duplicate_visible_slugs(self) -> None:
        self.write([
            self.model("gpt-5.6-sol", "Sol", ("max",)),
            self.model("gpt-5.6-sol", "Sol again", ("max",)),
        ])

        with self.assertRaises(ControlConfigError) as caught:
            load_codex_model_catalog(self.cache)

        self.assertEqual(str(caught.exception), "Codex 模型目录无效")

    def test_catalog_rejects_invalid_structure_without_leaking_values(self) -> None:
        self.write([{
            "slug": "SECRET_MODEL",
            "display_name": "Secret",
            "visibility": "list",
            "supported_reasoning_levels": "max",
        }])

        with self.assertRaises(ControlConfigError) as caught:
            load_codex_model_catalog(self.cache)

        self.assertEqual(str(caught.exception), "Codex 模型目录无效")
        self.assertNotIn("SECRET_MODEL", str(caught.exception))

    def test_catalog_rejects_missing_file_with_fixed_error(self) -> None:
        missing = self.root / "SECRET_PATH" / "models.json"

        with self.assertRaises(ControlConfigError) as caught:
            load_codex_model_catalog(missing)

        self.assertEqual(str(caught.exception), "无法读取 Codex 模型目录")
        self.assertNotIn("SECRET_PATH", str(caught.exception))


class ReviewControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = (
            CodexModelOption(
                "gpt-5.6-sol",
                "GPT-5.6 Sol",
                ("low", "high", "max"),
            ),
            CodexModelOption(
                "gpt-5.6-terra",
                "GPT-5.6 Terra",
                ("medium", "high"),
            ),
        )
        self.control = ReviewControl(
            self.catalog,
            DEFAULT_CODEX_SELECTION,
            token="bridge-test-token",
        )

    def test_bootstrap_exposes_catalog_and_token_but_public_state_does_not(
        self,
    ) -> None:
        bootstrap = self.control.bootstrap()
        public = self.control.public_snapshot()

        self.assertEqual(bootstrap["token"], "bridge-test-token")
        self.assertEqual(bootstrap["models"][0]["slug"], "gpt-5.6-sol")
        self.assertEqual(bootstrap["control"], public)
        self.assertNotIn("token", public)
        self.assertNotIn("bridge-test-token", repr(public))

    def test_idle_selection_is_queued_for_the_next_review(self) -> None:
        result = self.control.request_selection("gpt-5.6-terra", "high")

        self.assertEqual(result["mode"], "next_review")
        self.assertEqual(
            self.control.selection_for_next_review(),
            CodexControlSelection("gpt-5.6-terra", "high"),
        )
        action = self.control.take_action()
        self.assertIsNone(action)

    def test_resumed_legacy_effective_selection_can_stay_until_changed(
        self,
    ) -> None:
        control = ReviewControl(
            self.catalog,
            CodexControlSelection("gpt-legacy-review", "high"),
            token="legacy-token",
            allow_legacy_effective=True,
        )

        self.assertEqual(
            control.selection_for_next_review(),
            CodexControlSelection("gpt-legacy-review", "high"),
        )
        result = control.request_selection("gpt-5.6-sol", "max")
        self.assertEqual(result["mode"], "next_review")
        self.assertEqual(
            control.selection_for_next_review(),
            CodexControlSelection("gpt-5.6-sol", "max"),
        )

    def test_active_review_allows_one_immediate_restart(self) -> None:
        self.control.mark_review_started(3)

        result = self.control.request_selection("gpt-5.6-sol", "high")
        action = self.control.take_action()

        self.assertEqual(result["mode"], "restart")
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, "restart")
        self.assertEqual(action.review_no, 3)
        self.assertEqual(
            action.selection,
            CodexControlSelection("gpt-5.6-sol", "high"),
        )
        with self.assertRaises(ControlRequestError) as caught:
            self.control.request_selection("gpt-5.6-sol", "max")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(str(caught.exception), "本轮审核已重启过一次")

    def test_new_logical_review_resets_the_restart_allowance(self) -> None:
        self.control.mark_review_started(1)
        self.control.request_selection("gpt-5.6-sol", "high")
        self.control.take_action()
        self.control.mark_review_started(2)

        result = self.control.request_selection("gpt-5.6-sol", "max")

        self.assertEqual(result["mode"], "restart")

    def test_stop_is_idempotent_and_takes_precedence_over_restart(self) -> None:
        self.control.mark_review_started(1)
        self.control.request_selection("gpt-5.6-sol", "high")

        first = self.control.request_stop()
        second = self.control.request_stop()
        action = self.control.take_action()

        self.assertEqual(first["status"], "stop_requested")
        self.assertEqual(second, first)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, "stop")
        self.assertIsNone(action.selection)
        with self.assertRaises(ControlRequestError) as caught:
            self.control.request_selection("gpt-5.6-sol", "max")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(str(caught.exception), "任务正在停止")

    def test_invalid_model_and_effort_are_fixed_errors(self) -> None:
        for model, effort in (
            ("SECRET_MODEL", "high"),
            ("gpt-5.6-sol", "SECRET_EFFORT"),
            (None, "high"),
        ):
            with self.subTest(model=model, effort=effort):
                with self.assertRaises(ControlRequestError) as caught:
                    self.control.request_selection(model, effort)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(str(caught.exception), "Codex 模型配置无效")
                self.assertNotIn("SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
