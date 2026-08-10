import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import intent
import llm


class SchedulingIntentValidationTests(unittest.TestCase):
    def test_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            intent.SchedulingIntent.model_validate({"action": "search", "made_up": True})

    def test_rejects_invalid_action(self):
        with self.assertRaises(ValidationError):
            intent.SchedulingIntent.model_validate({"action": "guess"})

    def test_rejects_invalid_clock(self):
        with self.assertRaises(ValidationError):
            intent.SchedulingIntent.model_validate({
                "time_kind": "preferred", "time_start_local": "25:00",
            })

    def test_rejects_missing_clock_for_preferred_time(self):
        with self.assertRaises(ValidationError):
            intent.SchedulingIntent.model_validate({"time_kind": "preferred"})

    def test_rejects_multiple_day_references(self):
        with self.assertRaises(ValidationError):
            intent.SchedulingIntent.model_validate({"weekday": "monday", "day_offset": 1})

    def test_accepts_window_query(self):
        patch_value = intent.SchedulingIntent.model_validate({
            "action": "windows", "day_offset": 1,
            "time_kind": "daypart", "time_start_local": "09:00",
            "time_end_local": "12:00",
        })
        self.assertEqual(patch_value.action, "windows")


class ContextReducerTests(unittest.TestCase):
    def setUp(self):
        self.prior = {
            "weekday": "tuesday",
            "time_kind": "preferred",
            "time_start_local": "07:00",
            "duration_minutes": 30,
            "purpose": "coffee",
            "title": "Coffee",
        }

    def test_date_only_followup_preserves_other_constraints(self):
        result = intent.merge_intent(
            self.prior, intent.SchedulingIntent(weekday="friday"))
        self.assertEqual(result["weekday"], "friday")
        self.assertEqual(result["time_start_local"], "07:00")
        self.assertEqual(result["purpose"], "coffee")

    def test_new_purpose_can_clear_inherited_time_and_duration(self):
        result = intent.merge_intent(self.prior, intent.SchedulingIntent(
            purpose="lunch",
        ))
        self.assertEqual(result["purpose"], "lunch")
        self.assertNotIn("time_kind", result)
        self.assertNotIn("time_start_local", result)
        self.assertNotIn("duration_minutes", result)
        self.assertNotIn("title", result)

    def test_new_day_replaces_old_day_reference(self):
        result = intent.merge_intent(
            {"explicit_date": "2026-08-10"}, intent.SchedulingIntent(day_offset=1))
        self.assertEqual(result["day_offset"], 1)
        self.assertIsNone(result["explicit_date"])

    def test_relative_shift_is_transient(self):
        result = intent.merge_intent(self.prior, intent.SchedulingIntent(
            relative_shift="later",
        ))
        self.assertEqual(result["_relative_shift"], "later")

    def test_later_uses_standard_after_window(self):
        context = dict(self.prior)
        context["current_proposal"] = {
            "start": "2026-08-11T07:00:00-07:00",
            "end": "2026-08-11T07:30:00-07:00",
        }
        context["_relative_shift"] = "later"
        plan = intent.resolve_plan(
            context, "America/Los_Angeles",
            datetime(2026, 8, 10, 8, tzinfo=ZoneInfo("America/Los_Angeles")),
        )
        self.assertEqual((plan.window_start.hour, plan.window_start.minute), (7, 30))
        self.assertEqual((plan.day_lo_min, plan.day_hi_min), (420, 720))
        self.assertIsNone(plan.preferred_start)

    def test_lunch_defaults_are_resolved_in_python(self):
        context = intent.merge_intent(self.prior, intent.SchedulingIntent(
            purpose="lunch",
        ))
        plan = intent.resolve_plan(
            context, "America/Los_Angeles",
            datetime(2026, 8, 10, 8, tzinfo=ZoneInfo("America/Los_Angeles")),
        )
        self.assertEqual(plan.duration_minutes, 60)
        self.assertEqual((plan.day_lo_min, plan.day_hi_min), (690, 840))
        self.assertEqual(plan.title, "Lunch")

    def test_broad_windows_intent_clears_old_clock_but_keeps_date(self):
        result = intent.merge_intent(self.prior, intent.SchedulingIntent(
            action="windows", day_offset=1,
        ))
        self.assertEqual(result["day_offset"], 1)
        self.assertNotIn("time_start_local", result)
        self.assertNotIn("purpose", result)
        self.assertNotIn("duration_minutes", result)

    def test_unspecified_relax_hours_is_preserved(self):
        result = intent.merge_intent(
            {"day_offset": 1, "relax_hours": True},
            intent.SchedulingIntent(action="search", day_offset=1, relax_hours=True))
        self.assertTrue(result["relax_hours"])


class ParserBoundaryTests(unittest.TestCase):
    def test_inferred_lunch_daypart_is_removed_when_user_gave_no_time(self):
        normalized = llm._normalize_extraction({
            "purpose": "lunch", "time_kind": "daypart",
            "time_start_local": "12:00", "time_end_local": "17:00",
        }, "when is best for lunch?")
        self.assertIsNone(normalized["time_kind"])
        self.assertIsNone(normalized["time_start_local"])

    def test_explicit_daypart_is_kept(self):
        normalized = llm._normalize_extraction({
            "purpose": "lunch", "time_kind": "daypart",
            "time_start_local": "09:00", "time_end_local": "12:00",
        }, "lunch tomorrow morning")
        self.assertEqual(normalized["time_kind"], "daypart")

    def test_relative_shift_does_not_require_an_invented_clock(self):
        normalized = llm._normalize_extraction({
            "relative_shift": "later", "time_kind": "after",
            "time_start_local": None,
        }, "anything later?")
        parsed = intent.SchedulingIntent.model_validate(normalized)
        self.assertEqual(parsed.relative_shift, "later")
        self.assertIsNone(parsed.time_kind)
        self.assertEqual(parsed.action, "search")

    def test_conflicting_day_references_are_canonicalized(self):
        normalized = llm._normalize_extraction({
            "weekday": "tuesday", "day_offset": 1,
            "explicit_date": "2026-08-11",
        }, "tomorrow")
        parsed = intent.SchedulingIntent.model_validate(normalized)
        self.assertEqual(parsed.day_offset, 1)
        self.assertIsNone(parsed.weekday)
        self.assertIsNone(parsed.explicit_date)

    def test_enum_values_are_normalized_to_lowercase(self):
        normalized = llm._normalize_extraction({
            "action": "Windows", "weekday": "Friday", "time_kind": "Any",
        }, "available windows Friday")
        parsed = intent.SchedulingIntent.model_validate(normalized)
        self.assertEqual(parsed.action, "windows")
        self.assertEqual(parsed.weekday, "friday")
        self.assertIsNone(parsed.time_kind)

    @patch.object(llm, "ai_enabled", return_value=True)
    @patch.object(llm, "_chat_json")
    def test_valid_model_output_becomes_typed_intent(self, chat_json, _enabled):
        chat_json.return_value = {"action": "search", "day_offset": 1}
        parsed = llm.interpret_scheduling_intent(
            "tomorrow", "America/Los_Angeles")
        self.assertIsInstance(parsed, intent.SchedulingIntent)
        self.assertEqual(parsed.day_offset, 1)

    @patch.object(llm, "ai_enabled", return_value=True)
    @patch.object(llm, "_chat_json")
    def test_invalid_model_output_stops_at_boundary(self, chat_json, _enabled):
        chat_json.return_value = {"action": "search", "time_kind": "preferred"}
        parsed = llm.interpret_scheduling_intent(
            "at some time", "America/Los_Angeles")
        self.assertIsNone(parsed)

    @patch.object(llm, "ai_enabled", return_value=True)
    @patch.object(llm, "_chat_json")
    def test_only_current_message_is_sent_to_model(self, chat_json, _enabled):
        chat_json.return_value = {"action": "book"}
        llm.interpret_scheduling_intent(
            "that works", "America/Los_Angeles",
            has_proposal=True,
        )
        user_prompt = chat_json.call_args.args[1]
        self.assertNotIn("Recent conversation", user_prompt)
        self.assertEqual(user_prompt, "Current message: that works")


if __name__ == "__main__":
    unittest.main()
