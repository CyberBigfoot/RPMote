"""Tests for per-connection RDP local resource redirection.

The first time a device is opened the user is asked which of this PC's
resources the session may use; the answer is stored on the connection record
and reused for every later session without prompting again.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app import rdp_launcher
from app.rdp_launcher import (
    RDP_RESOURCE_KEY,
    has_saved_rdp_resources,
    normalize_rdp_resources,
    rdp_resource_lines,
)


class NormalizeResourcesTests(unittest.TestCase):
    def test_defaults_are_clipboard_and_audio_only(self):
        # Drives, printers and smart cards stay off until asked for.
        self.assertEqual(
            normalize_rdp_resources(None),
            {
                "clipboard": True,
                "printers": False,
                "drives": False,
                "audio": True,
                "smartcards": False,
            },
        )

    def test_partial_record_keeps_stored_values_and_fills_the_rest(self):
        result = normalize_rdp_resources({"printers": True, "clipboard": False})
        self.assertTrue(result["printers"])
        self.assertFalse(result["clipboard"])
        self.assertTrue(result["audio"])  # untouched key falls back to default

    def test_junk_values_are_coerced_and_extras_dropped(self):
        result = normalize_rdp_resources({"drives": 1, "audio": 0, "bogus": True})
        self.assertIs(result["drives"], True)
        self.assertIs(result["audio"], False)
        self.assertNotIn("bogus", result)

    def test_non_dict_input_falls_back_to_defaults(self):
        for junk in ("", [], 0, "clipboard"):
            self.assertEqual(normalize_rdp_resources(junk), normalize_rdp_resources(None))


class HasSavedResourcesTests(unittest.TestCase):
    def test_absent_until_the_user_answers(self):
        self.assertFalse(has_saved_rdp_resources({"host": "srv01"}))

    def test_present_once_stored(self):
        self.assertTrue(has_saved_rdp_resources({RDP_RESOURCE_KEY: {"clipboard": True}}))

    def test_an_empty_answer_still_counts_as_answered(self):
        # Everything unchecked is a real choice and must not re-prompt.
        self.assertTrue(has_saved_rdp_resources({RDP_RESOURCE_KEY: {}}))


class ResourceLinesTests(unittest.TestCase):
    def test_enabled_resources_render_as_rdp_directives(self):
        lines = rdp_resource_lines(
            {
                "clipboard": True,
                "printers": True,
                "drives": True,
                "audio": True,
                "smartcards": True,
            }
        )
        self.assertIn("redirectclipboard:i:1", lines)
        self.assertIn("redirectprinters:i:1", lines)
        self.assertIn("redirectdrives:i:1", lines)
        self.assertIn("redirectsmartcards:i:1", lines)
        self.assertIn("audiomode:i:0", lines)
        # mstsc needs the device list alongside the drive redirect flag.
        self.assertIn("drivestoredirect:s:*", lines)

    def test_disabled_resources_are_written_out_explicitly(self):
        lines = rdp_resource_lines(
            {
                "clipboard": False,
                "printers": False,
                "drives": False,
                "audio": False,
                "smartcards": False,
            }
        )
        self.assertIn("redirectclipboard:i:0", lines)
        self.assertIn("redirectprinters:i:0", lines)
        self.assertIn("redirectdrives:i:0", lines)
        self.assertIn("redirectsmartcards:i:0", lines)
        self.assertIn("audiomode:i:2", lines)  # 2 = do not play
        self.assertNotIn("drivestoredirect:s:*", lines)

    def test_every_line_is_a_valid_rdp_directive(self):
        for line in rdp_resource_lines(None):
            self.assertRegex(line, r"^[a-z ]+:[is]:.+$")


class ResolveLocalResourcesTests(unittest.TestCase):
    """The prompt fires once per device, then never again."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.prompts = []
        self.saved = []

        chosen = {
            "clipboard": True,
            "printers": True,
            "drives": False,
            "audio": False,
            "smartcards": False,
        }

        class _FakeDialog:
            def __init__(_self, connection_info, resources, parent=None):
                self.prompts.append(connection_info.get("name"))

            def exec(_self):
                return self.dialog_result

            def values(_self):
                return dict(chosen)

        self.chosen = chosen
        self.dialog_result = rdp_launcher.QDialog.DialogCode.Accepted
        self._real_dialog = rdp_launcher.RDPLocalResourcesDialog
        rdp_launcher.RDPLocalResourcesDialog = _FakeDialog
        self.addCleanup(
            setattr, rdp_launcher, "RDPLocalResourcesDialog", self._real_dialog
        )

    def _resolve(self, conn, force_prompt=False):
        return rdp_launcher._resolve_local_resources(
            None, conn, self.saved.append, force_prompt=force_prompt
        )

    def test_first_use_prompts_and_stores_the_answer(self):
        conn = {"name": "DC01"}

        result = self._resolve(conn)

        self.assertEqual(self.prompts, ["DC01"])
        self.assertEqual(result, self.chosen)
        self.assertEqual(conn[RDP_RESOURCE_KEY], self.chosen)
        self.assertEqual(self.saved, [self.chosen])

    def test_later_connects_reuse_the_answer_without_prompting(self):
        conn = {"name": "DC01"}
        self._resolve(conn)
        self.prompts.clear()
        self.saved.clear()

        result = self._resolve(conn)

        self.assertEqual(self.prompts, [])
        self.assertEqual(result, self.chosen)
        self.assertEqual(self.saved, [])

    def test_editing_forces_the_prompt_again(self):
        conn = {"name": "DC01", RDP_RESOURCE_KEY: {"clipboard": False}}

        result = self._resolve(conn, force_prompt=True)

        self.assertEqual(self.prompts, ["DC01"])
        self.assertEqual(result, self.chosen)
        self.assertEqual(self.saved, [self.chosen])

    def test_cancelling_returns_none_and_stores_nothing(self):
        self.dialog_result = rdp_launcher.QDialog.DialogCode.Rejected
        conn = {"name": "DC01"}

        self.assertIsNone(self._resolve(conn))
        self.assertNotIn(RDP_RESOURCE_KEY, conn)
        self.assertEqual(self.saved, [])

    def test_stored_answer_survives_a_json_round_trip(self):
        # The vault serializes connections, so booleans come back as JSON types.
        import json

        conn = {"name": "DC01"}
        self._resolve(conn)
        reloaded = json.loads(json.dumps(conn))
        self.prompts.clear()

        result = self._resolve(reloaded)

        self.assertEqual(self.prompts, [])
        self.assertEqual(result, self.chosen)


if __name__ == "__main__":
    unittest.main()
