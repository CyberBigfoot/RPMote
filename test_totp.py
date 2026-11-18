"""Tests for the one-time codes a NAS asks for as a second factor.

The generator is checked against RFC 6238's own published vectors — a TOTP
implementation that is subtly wrong produces codes that look perfectly
plausible and are rejected, so "it returned six digits" proves nothing.
"""

import base64
import json
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

# WebEngine reads argv[0] to locate QtWebEngineProcess, and shares GL contexts
# with the rest of the app — both have to be settled before any tab is built.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
_APP = QApplication.instance() or QApplication(sys.argv[:1] or ["rpmote-tests"])

from app import totp
from app.web_browser import _ensure_webengine

#: RFC 6238 Appendix B: the ASCII string "12345678901234567890", repeated to
#: the length each algorithm's test uses, as base32.
_RFC_SHA1 = base64.b32encode(b"12345678901234567890").decode()
_RFC_SHA256 = base64.b32encode(b"12345678901234567890123456789012").decode()
_RFC_SHA512 = base64.b32encode(
    b"1234567890123456789012345678901234567890123456789012345678901234").decode()


def _rfc_uri(secret: str, algorithm: str) -> str:
    return f"otpauth://totp/rfc?secret={secret}&digits=8&algorithm={algorithm}"


class RFC6238VectorTests(unittest.TestCase):
    """Appendix B, in full."""

    def test_sha1_vectors(self):
        for at, expected in ((59, "94287082"),
                             (1111111109, "07081804"),
                             (1111111111, "14050471"),
                             (1234567890, "89005924"),
                             (2000000000, "69279037"),
                             (20000000000, "65353130")):
            with self.subTest(at=at):
                self.assertEqual(
                    totp.generate(_rfc_uri(_RFC_SHA1, "SHA1"), at=at), expected)

    def test_sha256_vectors(self):
        for at, expected in ((59, "46119246"),
                             (1111111109, "68084774"),
                             (1234567890, "91819424"),
                             (20000000000, "77737706")):
            with self.subTest(at=at):
                self.assertEqual(
                    totp.generate(_rfc_uri(_RFC_SHA256, "SHA256"), at=at), expected)

    def test_sha512_vectors(self):
        for at, expected in ((59, "90693936"),
                             (1111111109, "25091201"),
                             (1234567890, "93441116"),
                             (20000000000, "47863826")):
            with self.subTest(at=at):
                self.assertEqual(
                    totp.generate(_rfc_uri(_RFC_SHA512, "SHA512"), at=at), expected)

    def test_the_default_six_digit_code_is_the_tail_of_the_eight_digit_one(self):
        long_code = totp.generate(_rfc_uri(_RFC_SHA1, "SHA1"), at=59)
        short_code = totp.generate(_RFC_SHA1, at=59)
        self.assertEqual(short_code, long_code[-6:])


class SecretParsingTests(unittest.TestCase):
    def test_a_bare_secret_gets_the_usual_parameters(self):
        parsed = totp.parse_secret("JBSWY3DPEHPK3PXP")
        self.assertEqual(parsed["secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(parsed["digits"], 6)
        self.assertEqual(parsed["period"], 30)
        self.assertEqual(parsed["algorithm"], "SHA1")

    def test_display_formatting_is_stripped(self):
        # This is how a NAS shows it, and how it gets pasted.
        for shown in ("jbsw y3dp ehpk 3pxp", "JBSW-Y3DP-EHPK-3PXP",
                      "  JBSWY3DPEHPK3PXP  ", "jbswy3dpehpk3pxp"):
            with self.subTest(shown=shown):
                self.assertEqual(
                    totp.parse_secret(shown)["secret"], "JBSWY3DPEHPK3PXP")

    def test_padding_is_dropped_rather_than_trusted(self):
        self.assertEqual(totp.parse_secret("JBSWY3DP====")["secret"], "JBSWY3DP")

    def test_the_same_secret_padded_or_not_makes_the_same_code(self):
        self.assertEqual(totp.generate("JBSWY3DPEHPK3PXP", at=1234567890),
                         totp.generate("jbsw y3dp ehpk 3pxp=", at=1234567890))

    def test_an_otpauth_link_carries_its_own_parameters(self):
        parsed = totp.parse_secret(
            "otpauth://totp/NAS:admin?secret=JBSWY3DPEHPK3PXP&issuer=NAS"
            "&digits=8&period=60&algorithm=SHA256")
        self.assertEqual(parsed["secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(parsed["digits"], 8)
        self.assertEqual(parsed["period"], 60)
        self.assertEqual(parsed["algorithm"], "SHA256")
        self.assertEqual(parsed["issuer"], "NAS")
        self.assertEqual(parsed["label"], "NAS:admin")

    def test_an_issuer_is_taken_from_the_label_when_it_is_not_given(self):
        parsed = totp.parse_secret(
            "otpauth://totp/Synology:admin?secret=JBSWY3DPEHPK3PXP")
        self.assertEqual(parsed["issuer"], "Synology")

    def test_a_percent_encoded_label_is_decoded(self):
        parsed = totp.parse_secret(
            "otpauth://totp/Acme%20NAS:admin?secret=JBSWY3DPEHPK3PXP")
        self.assertEqual(parsed["label"], "Acme NAS:admin")

    def test_a_link_with_spaces_in_the_secret_still_works(self):
        parsed = totp.parse_secret(
            "otpauth://totp/x?secret=jbsw%20y3dp%20ehpk%203pxp")
        self.assertEqual(parsed["secret"], "JBSWY3DPEHPK3PXP")


class BadSecretTests(unittest.TestCase):
    """Every rejection has to say what is wrong with it."""

    def _refuses(self, text) -> str:
        with self.assertRaises(totp.TOTPError) as caught:
            totp.parse_secret(text)
        return str(caught.exception)

    def test_nothing_at_all(self):
        for empty in ("", "   ", None):
            self.assertIn("No secret", self._refuses(empty))

    def test_characters_outside_base32(self):
        message = self._refuses("JBSWY3DP!EHPK")
        self.assertIn("base32", message)
        self.assertIn("'!'", message)

    def test_the_digits_0_1_and_8_are_not_base32(self):
        # The classic paste error: a secret read off a screen with an O for a
        # zero. Better to say so than to hand back codes that never work.
        self.assertIn("base32", self._refuses("JBSW01"))

    def test_a_length_that_is_not_whole_bytes(self):
        # 3 and 6 characters are not whole base32 groups; a secret that lost
        # characters on the way in is caught here rather than at the NAS.
        for truncated in ("JBS", "JBSWY3", "JBSWY3DPEHPK3PXPJBS"):
            with self.subTest(truncated=truncated):
                self.assertIn("base32", self._refuses(truncated))

    def test_a_counter_based_link_is_not_time_based(self):
        message = self._refuses(
            "otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP&counter=1")
        self.assertIn("time-based", message)

    def test_a_link_with_no_secret(self):
        self.assertIn("secret=", self._refuses("otpauth://totp/x?issuer=NAS"))

    def test_an_algorithm_nothing_implements(self):
        message = self._refuses(
            "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&algorithm=MD5")
        self.assertIn("MD5", message)

    def test_a_digit_count_outside_what_totp_allows(self):
        self.assertIn(
            "6", self._refuses("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&digits=4"))

    def test_a_period_that_is_not_a_number(self):
        self.assertIn(
            "number",
            self._refuses("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&period=soon"))


class CountdownTests(unittest.TestCase):
    def test_the_code_holds_for_the_whole_period(self):
        parsed = totp.parse_secret(_RFC_SHA1)
        first = totp.generate_parsed(parsed, at=1234567890)
        self.assertEqual(totp.generate_parsed(parsed, at=1234567890 + 29), first)
        self.assertNotEqual(totp.generate_parsed(parsed, at=1234567890 + 30), first)

    def test_the_countdown_runs_down_to_the_boundary(self):
        parsed = totp.parse_secret(_RFC_SHA1)
        self.assertEqual(totp.seconds_remaining(parsed, at=1234567890), 30)
        self.assertEqual(totp.seconds_remaining(parsed, at=1234567890 + 10), 20)
        # Never zero: a "0s left" badge reads as expired when it is not.
        self.assertEqual(totp.seconds_remaining(parsed, at=1234567890 + 29.5), 1)

    def test_a_custom_period_is_honoured_by_the_countdown(self):
        parsed = totp.parse_secret(
            "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&period=60")
        self.assertEqual(totp.seconds_remaining(parsed, at=1234567890), 30)

    def test_current_code_returns_both_halves_of_the_badge(self):
        code, seconds = totp.current_code(_RFC_SHA1, at=59)
        self.assertEqual(code, "287082")
        self.assertEqual(seconds, 1)


class ConnectionRecordTests(unittest.TestCase):
    def test_a_connection_without_a_secret_has_no_second_factor(self):
        for conn in ({}, {"totp_secret": ""}, {"totp_secret": "   "}, None, "nas"):
            self.assertFalse(totp.has_totp(conn))
            self.assertIsNone(totp.code_for_connection(conn))

    def test_a_connection_with_a_secret_produces_a_code(self):
        conn = {"name": "NAS1", "totp_secret": _RFC_SHA1}
        self.assertTrue(totp.has_totp(conn))
        self.assertEqual(totp.code_for_connection(conn, at=59), ("287082", 1))

    def test_a_secret_that_has_gone_bad_leaves_the_box_empty_rather_than_raising(self):
        # A broken second factor must not stop the console opening at all.
        conn = {"name": "NAS1", "totp_secret": "JBSWY3DP<hand-edited>"}
        self.assertTrue(totp.has_totp(conn))
        self.assertIsNone(totp.code_for_connection(conn))

    def test_the_secret_survives_a_json_round_trip(self):
        conn = {"name": "NAS1", "totp_secret": "jbsw y3dp ehpk 3pxp"}
        reloaded = json.loads(json.dumps(conn))
        self.assertEqual(totp.code_for_connection(reloaded, at=59),
                         totp.code_for_connection(conn, at=59))


class ConnectionDialogTOTPTests(unittest.TestCase):
    """The 2FA block in the connection dialog."""

    def _nas_dialog(self, connection=None):
        from app.connection_manager import ConnectionDialog

        dialog = ConnectionDialog(connection=connection)
        if connection is None:
            dialog.protocol.setCurrentText("NAS")
        return dialog

    def test_the_block_is_only_offered_for_nas(self):
        dialog = self._nas_dialog()
        self.assertTrue(dialog.totp_group.isVisibleTo(dialog))
        dialog.protocol.setCurrentText("RDP")
        self.assertFalse(dialog.totp_group.isVisibleTo(dialog))

    def test_the_preview_ticks_only_while_nas_is_selected(self):
        dialog = self._nas_dialog()
        self.assertTrue(dialog._totp_timer.isActive())
        dialog.protocol.setCurrentText("SSH")
        self.assertFalse(dialog._totp_timer.isActive())

    def test_the_preview_shows_a_code_for_a_good_secret(self):
        dialog = self._nas_dialog()
        dialog.totp_secret.setText("jbsw y3dp ehpk 3pxp")
        self.assertRegex(dialog.totp_preview.text(), r"^\d{3} \d{3}\s+·\s+\d+s left$")

    def test_the_preview_says_why_a_bad_secret_is_bad(self):
        dialog = self._nas_dialog()
        dialog.totp_secret.setText("nope!")
        self.assertIn("base32", dialog.totp_preview.text())

    def test_an_empty_secret_previews_as_nothing(self):
        dialog = self._nas_dialog()
        dialog.totp_secret.setText("")
        self.assertEqual(dialog.totp_preview.text(), "—")

    def test_saving_a_good_secret_keeps_it_as_typed(self):
        dialog = self._nas_dialog()
        dialog.name_input.setText("NAS1")
        dialog.host_input.setText("nas.contoso.com")
        dialog.totp_secret.setText("jbsw y3dp ehpk 3pxp")
        dialog._save()
        self.assertEqual(dialog.get_result()["totp_secret"], "jbsw y3dp ehpk 3pxp")

    def test_saving_a_bad_secret_is_refused_rather_than_stored(self):
        dialog = self._nas_dialog()
        dialog.name_input.setText("NAS1")
        dialog.host_input.setText("nas.contoso.com")
        dialog.totp_secret.setText("nope!")

        warned = []
        from app import connection_manager

        original = connection_manager.QMessageBox.warning
        connection_manager.QMessageBox.warning = staticmethod(
            lambda parent, title, text: warned.append(title))
        self.addCleanup(
            setattr, connection_manager.QMessageBox, "warning", original)

        dialog._save()
        self.assertEqual(warned, ["Two-Step Verification"])
        self.assertEqual(dialog.get_result(), {})

    def test_editing_shows_the_saved_secret_back(self):
        dialog = self._nas_dialog(connection={
            "name": "NAS1", "protocol": "synology", "host": "nas.contoso.com",
            "port": 5001, "totp_secret": "JBSWY3DPEHPK3PXP",
        })
        self.assertEqual(dialog.totp_secret.text(), "JBSWY3DPEHPK3PXP")

    def test_a_connection_saved_before_2fa_existed_opens_without_one(self):
        dialog = self._nas_dialog(connection={
            "name": "NAS1", "protocol": "synology", "host": "nas.contoso.com",
            "port": 5001, "username": "admin", "password": "s3cret",
        })
        self.assertEqual(dialog.totp_secret.text(), "")


@unittest.skipUnless(_ensure_webengine(),
                     "PyQt6-WebEngine is not installed in this environment")
class BrowserTabTOTPTests(unittest.TestCase):
    """What the NAS tab does with a stored secret.

    Only the parts that do not need a page to be loaded: the injected script
    and the button's own text.
    """

    def _tab(self, **extra):
        from app.web_browser import WebBrowserTab

        conn = {"name": "NAS1", "protocol": "synology", "host": "nas.contoso.com",
                "port": 5001, "username": "admin", "password": "s3cret"}
        conn.update(extra)
        tab = WebBrowserTab(conn, _skip_navigate=True)
        self.addCleanup(tab.cleanup)
        return tab

    def test_the_code_button_appears_only_when_a_secret_is_stored(self):
        self.assertFalse(hasattr(self._tab(), "btn_totp"))
        self.assertTrue(hasattr(self._tab(totp_secret=_RFC_SHA1), "btn_totp"))

    def test_the_button_shows_the_live_code_and_its_countdown(self):
        tab = self._tab(totp_secret=_RFC_SHA1)
        tab._refresh_totp_button()
        self.assertRegex(tab.btn_totp.text(), r"^\d{3} \d{3} · +\d+s$")
        self.assertTrue(tab.btn_totp.isEnabled())

    def test_a_broken_secret_disables_the_button_and_says_so(self):
        tab = self._tab(totp_secret="nope!")
        tab._refresh_totp_button()
        self.assertFalse(tab.btn_totp.isEnabled())
        self.assertIn("unreadable", tab.btn_totp.text())

    def test_the_countdown_stops_when_the_tab_is_closed(self):
        tab = self._tab(totp_secret=_RFC_SHA1)
        timer = tab._totp_timer
        self.assertTrue(timer.isActive())
        tab.cleanup()
        self.assertFalse(timer.isActive())

    def test_the_injected_script_carries_the_code_it_was_given(self):
        tab = self._tab(totp_secret=_RFC_SHA1)
        script = tab._build_saved_login_injector_js(user="", pw="", code="123456")
        self.assertIn('const CODE = "123456"', script)
        self.assertIn('const USER = ""', script)
        self.assertIn('const PASS = ""', script)

    def test_the_script_defaults_to_the_connections_own_credentials(self):
        tab = self._tab(totp_secret=_RFC_SHA1)
        script = tab._build_saved_login_injector_js()
        self.assertIn('const USER = "admin"', script)
        self.assertIn('const PASS = "s3cret"', script)

    def test_a_quote_in_a_password_cannot_break_out_of_the_script(self):
        tab = self._tab(password='he said "run";alert(1)//')
        script = tab._build_saved_login_injector_js()
        self.assertIn(json.dumps('he said "run";alert(1)//'), script)

    def test_the_script_never_submits_the_code_by_itself(self):
        # A wrong code counts against the account's lockout.
        tab = self._tab(totp_secret=_RFC_SHA1)
        script = tab._build_saved_login_injector_js(user="", pw="", code="123456")
        self.assertIn("clickNextIfNeeded(u, p, roots)", script)
        self.assertNotIn("clickNextIfNeeded(u, p, o", script)

    def test_copying_the_login_includes_the_current_code(self):
        tab = self._tab(totp_secret=_RFC_SHA1)
        tab._copy_saved_login()
        clipboard = QApplication.clipboard().text()
        self.assertIn("Username: admin", clipboard)
        self.assertRegex(clipboard, r"Code: \d{6}  \(valid \d+s\)")

    def test_copying_the_login_without_a_secret_mentions_no_code(self):
        tab = self._tab()
        tab._copy_saved_login()
        self.assertNotIn("Code:", QApplication.clipboard().text())

    # The password must never be typed into the 2FA box. On DSM's
    # verification screen that box is the only visible text input, so a
    # password picked on visibility alone landed there — and with no secret
    # stored, stayed there.
    def test_the_password_needs_a_real_password_box(self):
        script = self._tab()._build_saved_login_injector_js()
        self.assertIn("pickBest(inputs, scorePass, 3)", script)
        score_pass = script.split("const scorePass = ")[1].split("const pickBest")[0]
        self.assertIn("has(text, otpKw)) score -= 30", score_pass)

    def test_checkboxes_are_never_text_fields(self):
        script = self._tab()._build_saved_login_injector_js()
        self.assertIn("notText.includes(t)", script)
        self.assertIn("'checkbox', 'radio'", script)

    def test_the_script_reports_a_code_box_it_could_not_fill(self):
        script = self._tab()._build_saved_login_injector_js(user="admin", pw="pw", code="")
        self.assertIn("needsCode", script)
        self.assertIn("return { user: u, pass: p, code: o, needsCode: needsCode };", script)

    def test_a_code_box_with_no_secret_saved_is_explained(self):
        tab = self._tab()
        tab.url_bar.setText("https://nas.contoso.com:5001/")
        tab._on_saved_login_filled({"user": False, "pass": False, "code": False,
                                    "needsCode": True})
        self.assertIn("none is saved", tab.url_bar.text())
        self.assertIn("add the secret", tab.url_bar.text())

    def test_nothing_is_said_when_the_page_did_not_want_a_code(self):
        tab = self._tab()
        tab.url_bar.setText("https://nas.contoso.com:5001/")
        tab._on_saved_login_filled({"user": True, "pass": True, "code": False,
                                    "needsCode": False})
        self.assertEqual(tab.url_bar.text(), "https://nas.contoso.com:5001/")

    def test_nothing_is_said_when_a_secret_is_stored(self):
        # A stored secret means the code was offered; if the box still went
        # unfilled the cause is elsewhere and this hint would mislead.
        tab = self._tab(totp_secret=_RFC_SHA1)
        tab.url_bar.setText("https://nas.contoso.com:5001/")
        tab._on_saved_login_filled({"needsCode": True})
        self.assertEqual(tab.url_bar.text(), "https://nas.contoso.com:5001/")

    def test_a_result_that_is_not_a_dict_is_ignored(self):
        tab = self._tab()
        tab.url_bar.setText("https://nas.contoso.com:5001/")
        for odd in (None, False, "", 0, []):
            tab._on_saved_login_filled(odd)
        self.assertEqual(tab.url_bar.text(), "https://nas.contoso.com:5001/")


if __name__ == "__main__":
    unittest.main()
