"""Tests for per-connection RD Gateway settings.

A machine published through an RD Gateway needs three things said about it —
the gateway's address, whether to try the machine directly first, and whether
one set of credentials answers both challenges — and they have to survive the
trip through the connection record into the .rdp file mstsc is handed.
"""

import os
import socket
import threading
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app import windows_credentials
from app.rdp_gateway import (
    account_has_domain,
    describe_gateway_route,
    explain_mstsc_message,
    gateway_credential_targets,
    is_gateway_message,
    seed_gateway_credentials,
    split_account,
)
from app.rdp_launcher import (
    EmbeddedRDPWidget,
    describe_disconnect,
    rdp_client_outcome,
    DIALOG_CREDENTIALS,
    DIALOG_MESSAGE,
    DIALOG_PROGRESS,
    DIALOG_SECURITY_WARNING,
    RDP_GATEWAY_DEFAULT_PORT,
    split_gateway_address,
    RDP_GATEWAY_BYPASS_KEY,
    RDP_GATEWAY_HOST_KEY,
    RDP_GATEWAY_PASSWORD_KEY,
    RDP_GATEWAY_SHARE_CREDS_KEY,
    RDP_GATEWAY_USERNAME_KEY,
    classify_mstsc_dialog,
    dialog_message_text,
    gateway_is_enabled,
    gateway_login,
    normalize_gateway_host,
    normalize_rdp_gateway,
    rdp_gateway_lines,
)


class _MemoryCredentials(unittest.TestCase):
    """Any test that can reach Credential Manager gets an in-memory one.

    ``conftest.py`` does this for a pytest run; this does it for a plain
    ``unittest`` run, and either way the developer's real store is never
    written to.
    """

    def setUp(self):
        super().setUp()
        self.store = windows_credentials.MemoryCredentialStore()
        previous = windows_credentials.use_credential_store(self.store)
        self.addCleanup(windows_credentials.use_credential_store, previous)


class NormalizeGatewayHostTests(unittest.TestCase):
    def test_a_bare_hostname_is_left_alone(self):
        self.assertEqual(normalize_gateway_host("gw.contoso.com"), "gw.contoso.com")

    def test_a_port_is_kept(self):
        self.assertEqual(normalize_gateway_host("gw.contoso.com:8443"),
                         "gw.contoso.com:8443")

    def test_a_pasted_url_is_reduced_to_the_address(self):
        # Gateway addresses get copied out of browser bars and runbooks.
        self.assertEqual(
            normalize_gateway_host("https://gw.contoso.com/remoteDesktopGateway"),
            "gw.contoso.com")

    def test_surrounding_whitespace_goes(self):
        self.assertEqual(normalize_gateway_host("  gw.contoso.com \n"),
                         "gw.contoso.com")

    def test_an_ipv6_literal_keeps_its_brackets(self):
        self.assertEqual(normalize_gateway_host("[2001:db8::1]:443"),
                         "[2001:db8::1]:443")

    def test_nothing_in_nothing_out(self):
        for junk in ("", "   ", None):
            self.assertEqual(normalize_gateway_host(junk), "")


class SplitGatewayAddressTests(unittest.TestCase):
    """Turning the stored address into something a socket can be opened on."""

    def test_no_port_means_the_gateway_default(self):
        self.assertEqual(split_gateway_address("gw.contoso.com"),
                         ("gw.contoso.com", RDP_GATEWAY_DEFAULT_PORT))
        self.assertEqual(RDP_GATEWAY_DEFAULT_PORT, 443)

    def test_an_explicit_port_wins(self):
        self.assertEqual(split_gateway_address("gw.contoso.com:8443"),
                         ("gw.contoso.com", 8443))

    def test_a_pasted_url_is_split_too(self):
        self.assertEqual(split_gateway_address("https://gw.contoso.com/rpc"),
                         ("gw.contoso.com", 443))

    def test_an_ipv6_literal_loses_the_brackets_socket_cannot_take(self):
        # mstsc wants them, socket.create_connection does not.
        self.assertEqual(split_gateway_address("[2001:db8::1]:8443"),
                         ("2001:db8::1", 8443))
        self.assertEqual(split_gateway_address("[2001:db8::1]"),
                         ("2001:db8::1", 443))

    def test_a_nonsense_port_falls_back_rather_than_raising(self):
        # The address is still worth trying; refusing to test it is not help.
        for junk in ("gw:notaport", "gw:0", "gw:99999", "gw:-1"):
            with self.subTest(junk=junk):
                self.assertEqual(split_gateway_address(junk), ("gw", 443))

    def test_no_address_at_all(self):
        self.assertEqual(split_gateway_address(""), ("", 443))


class NetworkErrorWordingTests(unittest.TestCase):
    """The failures a test actually hits, in words rather than Winsock codes."""

    def setUp(self):
        from app.connection_manager import describe_network_error

        self.describe = describe_network_error

    def test_a_name_that_does_not_resolve_says_so(self):
        # [Errno 11001] getaddrinfo failed is what Windows gives, and it names
        # neither the host nor anything to do about it.
        message = self.describe(
            socket.gaierror(11001, "getaddrinfo failed"), "dc01.internal", 3389)
        self.assertIn("dc01.internal", message)
        self.assertIn("did not resolve", message)
        self.assertNotIn("11001", message)

    def test_a_timeout_distinguishes_itself_from_a_bad_name(self):
        message = self.describe(socket.timeout(), "10.0.0.5", 3389)
        self.assertIn("10.0.0.5:3389", message)
        self.assertIn("did not answer", message)

    def test_a_refused_connection_says_nothing_is_listening(self):
        message = self.describe(ConnectionRefusedError(), "10.0.0.5", 3389)
        self.assertIn("nothing is listening", message)

    def test_anything_else_is_passed_through_unchanged(self):
        message = self.describe(RuntimeError("something odd"), "10.0.0.5", 3389)
        self.assertEqual(message, "something odd")


class NormalizeGatewayTests(unittest.TestCase):
    def test_a_connection_without_a_gateway_has_none(self):
        gateway = normalize_rdp_gateway({"host": "10.0.0.5", "protocol": "rdp"})
        self.assertEqual(gateway["host"], "")
        self.assertFalse(gateway_is_enabled(gateway))

    def test_the_connections_own_fields_are_never_read_as_the_gateways(self):
        # The whole point of the prefix: reading a bare "host" off a
        # connection record would aim the gateway at the machine behind it.
        gateway = normalize_rdp_gateway({
            "host": "10.0.0.5",
            "username": "CONTOSO\\admin",
            "password": "s3cret",
        })
        self.assertEqual(gateway["host"], "")
        self.assertEqual(gateway["username"], "")
        self.assertEqual(gateway["password"], "")

    def test_stored_fields_are_read_and_cleaned(self):
        gateway = normalize_rdp_gateway({
            RDP_GATEWAY_HOST_KEY: " https://gw.contoso.com/ ",
            RDP_GATEWAY_BYPASS_KEY: False,
            RDP_GATEWAY_SHARE_CREDS_KEY: False,
            RDP_GATEWAY_USERNAME_KEY: "  CONTOSO\\gwuser ",
            RDP_GATEWAY_PASSWORD_KEY: "gwpass",
        })
        self.assertEqual(gateway, {
            "host": "gw.contoso.com",
            "bypass_local": False,
            "use_for_remote": False,
            "username": "CONTOSO\\gwuser",
            "password": "gwpass",
        })
        self.assertTrue(gateway_is_enabled(gateway))

    def test_both_boxes_default_to_ticked_once_a_gateway_is_named(self):
        # Matches what mstsc offers: bypass on, one credential prompt.
        gateway = normalize_rdp_gateway({RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertTrue(gateway["bypass_local"])
        self.assertTrue(gateway["use_for_remote"])

    def test_junk_values_are_coerced(self):
        gateway = normalize_rdp_gateway({
            RDP_GATEWAY_HOST_KEY: "gw",
            RDP_GATEWAY_BYPASS_KEY: 0,
            RDP_GATEWAY_SHARE_CREDS_KEY: 1,
            RDP_GATEWAY_USERNAME_KEY: None,
        })
        self.assertIs(gateway["bypass_local"], False)
        self.assertIs(gateway["use_for_remote"], True)
        self.assertEqual(gateway["username"], "")

    def test_non_dict_input_falls_back_to_no_gateway(self):
        for junk in ("", [], 0, "gw.contoso.com"):
            self.assertEqual(normalize_rdp_gateway(junk)["host"], "")

    def test_normalizing_an_already_normalized_gateway_changes_nothing(self):
        # It gets passed along between the launch paths, so it has to be
        # idempotent or the second pass would empty it.
        once = normalize_rdp_gateway({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_USERNAME_KEY: "gwuser",
        })
        self.assertEqual(normalize_rdp_gateway(once), once)

    def test_it_survives_a_json_round_trip(self):
        import json

        conn = {
            "name": "DC01",
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_BYPASS_KEY: True,
            RDP_GATEWAY_SHARE_CREDS_KEY: False,
        }
        reloaded = json.loads(json.dumps(conn))
        self.assertEqual(normalize_rdp_gateway(reloaded),
                         normalize_rdp_gateway(conn))


class GatewayLineTests(unittest.TestCase):
    def test_no_gateway_writes_no_gateway_directives(self):
        # A connection that does not use one has to be written exactly as it
        # was before this feature existed, so a gateway pushed by policy is
        # still the one that applies.
        lines = rdp_gateway_lines({})
        self.assertEqual(lines, ["promptcredentialonce:i:0"])

    def test_the_address_is_written_out(self):
        lines = rdp_gateway_lines({RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertIn("gatewayhostname:s:gw.contoso.com", lines)

    def test_bypassing_local_addresses_tries_the_machine_first(self):
        # 2 = direct first, gateway as the fallback.
        lines = rdp_gateway_lines({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_BYPASS_KEY: True,
        })
        self.assertIn("gatewayusagemethod:i:2", lines)

    def test_not_bypassing_always_goes_through_the_gateway(self):
        lines = rdp_gateway_lines({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_BYPASS_KEY: False,
        })
        self.assertIn("gatewayusagemethod:i:1", lines)

    def test_sharing_credentials_asks_once(self):
        lines = rdp_gateway_lines({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_SHARE_CREDS_KEY: True,
        })
        self.assertIn("promptcredentialonce:i:1", lines)

    def test_not_sharing_credentials_asks_twice(self):
        lines = rdp_gateway_lines({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_SHARE_CREDS_KEY: False,
        })
        self.assertIn("promptcredentialonce:i:0", lines)

    def test_promptcredentialonce_is_written_exactly_once(self):
        # Two disagreeing copies in one file is the failure this guards.
        for gateway in ({}, {RDP_GATEWAY_HOST_KEY: "gw.contoso.com"}):
            directives = [l.split(":")[0] for l in rdp_gateway_lines(gateway)]
            self.assertEqual(directives.count("promptcredentialonce"), 1)

    def test_an_explicit_gateway_login_is_named_in_the_file(self):
        lines = rdp_gateway_lines({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_USERNAME_KEY: "CONTOSO\\gwuser",
        })
        self.assertIn("gatewayusername:s:CONTOSO\\gwuser", lines)

    def test_no_gateway_login_leaves_the_username_out(self):
        lines = rdp_gateway_lines({RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertFalse([l for l in lines if l.startswith("gatewayusername")])

    def test_the_settings_in_the_file_win_over_a_stored_profile(self):
        lines = rdp_gateway_lines({RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertIn("gatewayprofileusagemethod:i:1", lines)

    def test_every_line_is_a_valid_rdp_directive(self):
        lines = rdp_gateway_lines({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_USERNAME_KEY: "gwuser",
        })
        for line in lines:
            self.assertRegex(line, r"^[a-z ]+:[is]:.+$")


class GatewayLoginTests(unittest.TestCase):
    """Which credentials the gateway is handed."""

    def test_an_explicit_gateway_login_wins(self):
        gateway = normalize_rdp_gateway({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_USERNAME_KEY: "gwuser",
            RDP_GATEWAY_PASSWORD_KEY: "gwpass",
        })
        self.assertEqual(gateway_login(gateway, "admin", "s3cret"),
                         ("gwuser", "gwpass"))

    def test_sharing_credentials_hands_over_the_connections_own(self):
        gateway = normalize_rdp_gateway({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_SHARE_CREDS_KEY: True,
        })
        self.assertEqual(gateway_login(gateway, "admin", "s3cret"),
                         ("admin", "s3cret"))

    def test_not_sharing_and_nothing_stored_means_windows_will_ask(self):
        gateway = normalize_rdp_gateway({
            RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
            RDP_GATEWAY_SHARE_CREDS_KEY: False,
        })
        self.assertEqual(gateway_login(gateway, "admin", "s3cret"), ("", ""))


class GeneratedRDPFileTests(_MemoryCredentials):
    """What mstsc is actually handed, end to end.

    The helpers above are only worth anything if their output reaches the
    file, and the file is launched from three different places.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        from app import rdp_launcher

        self.rdp_launcher = rdp_launcher
        self.launched = []
        # The launcher releases a session's logins once its mstsc exits, so
        # the fake has to look alive until the test is over.
        exited = threading.Event()
        self.addCleanup(exited.set)

        class _FakeProc:
            pid = 4242

            def poll(_self):
                return None

            def terminate(_self):
                pass

            def wait(_self):
                exited.wait(10)

        def _fake_popen(cmd, *args, **kwargs):
            self.launched.append(list(cmd))
            return _FakeProc()

        original = rdp_launcher.subprocess.Popen
        rdp_launcher.subprocess.Popen = _fake_popen
        self.addCleanup(setattr, rdp_launcher.subprocess, "Popen", original)

        original_dismiss = rdp_launcher._auto_dismiss_rdp_security_warning
        rdp_launcher._auto_dismiss_rdp_security_warning = lambda pid: None
        self.addCleanup(
            setattr, rdp_launcher, "_auto_dismiss_rdp_security_warning",
            original_dismiss)

    def _connection(self, **extra):
        conn = {
            "name": "DC01",
            "protocol": "rdp",
            "host": "10.0.0.5",
            "port": 3389,
            "username": "CONTOSO\\admin",
            "password": "s3cret",
            # Pre-answered so the first-run local resources prompt stays shut.
            "rdp_resources": {"clipboard": True, "printers": False,
                              "drives": False, "audio": True,
                              "smartcards": False},
        }
        conn.update(extra)
        return conn

    @property
    def cmdkey_targets(self) -> list[str]:
        """Where the logins went — the name is what the tests always called it."""
        return self.store.targets

    def _read_rdp(self, path: str) -> list[str]:
        # mstsc's own files are UTF-16 with a BOM; ours are written the same way.
        with open(path, "r", encoding="utf-16") as handle:
            return [l for l in handle.read().splitlines() if l]

    def _launch(self, **extra) -> list[str]:
        launcher = self.rdp_launcher.RDPLauncher(
            self._connection(**extra), embedded_mode=False)
        self.addCleanup(launcher.deleteLater)
        launcher._launch_rdp()
        self.assertTrue(self.launched, "mstsc was never launched")
        return self._read_rdp(self.launched[0][1])

    def _launch_embedded(self, **extra) -> list[str]:
        """The hosted-mstsc path, which is what actually runs on most machines."""
        widget = self.rdp_launcher.EmbeddedRDPWidget(self._connection(**extra))
        self.addCleanup(widget.cleanup)
        self.addCleanup(widget.deleteLater)
        widget._attach_timer.stop()
        self.launched.clear()
        widget._launch_fallback_mstsc(1280, 800, reconnecting=False)
        self.assertTrue(self.launched, "mstsc was never launched")
        return self._read_rdp(self.launched[0][1])

    def _open_external_window(self, **extra) -> list[str]:
        widget = self.rdp_launcher.EmbeddedRDPWidget(self._connection(**extra))
        self.addCleanup(widget.cleanup)
        self.addCleanup(widget.deleteLater)
        widget._attach_timer.stop()
        self.launched.clear()
        widget.btn_cad.click()
        self.assertTrue(self.launched, "external mstsc was never launched")
        return self._read_rdp(self.launched[-1][1])

    def test_a_connection_with_no_gateway_gets_no_gateway_directives(self):
        lines = self._launch()
        self.assertFalse([l for l in lines if l.startswith("gateway")])
        self.assertIn("promptcredentialonce:i:0", lines)

    def test_the_gateway_reaches_the_file(self):
        lines = self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertIn("gatewayhostname:s:gw.contoso.com", lines)
        self.assertIn("gatewayusagemethod:i:2", lines)
        self.assertIn("promptcredentialonce:i:1", lines)
        # The machine itself is still the machine, not the gateway.
        self.assertIn("full address:s:10.0.0.5:3389", lines)

    def test_promptcredentialonce_is_never_written_twice(self):
        # It used to be hard-coded next to the other credential directives;
        # two disagreeing copies in one file is the regression to catch.
        for extra in ({}, {RDP_GATEWAY_HOST_KEY: "gw.contoso.com"}):
            with self.subTest(extra=extra):
                self.launched.clear()
                lines = self._launch(**extra)
                self.assertEqual(
                    len([l for l in lines
                         if l.startswith("promptcredentialonce")]), 1)

    def test_sharing_credentials_seeds_the_gateway_with_the_connections_own(self):
        self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
                        RDP_GATEWAY_SHARE_CREDS_KEY: True})
        self.assertIn("TERMSRV/10.0.0.5", self.cmdkey_targets)
        self.assertIn("gw.contoso.com", self.cmdkey_targets)
        # The same account answers both challenges — and the password
        # reaches the store as typed, not as cmdkey would have mangled it.
        self.assertEqual(self.store.saved["gw.contoso.com"], ("CONTOSO\\admin", "s3cret"))
        self.assertEqual(self.store.saved["TERMSRV/10.0.0.5"], ("CONTOSO\\admin", "s3cret"))

    def test_a_separate_gateway_login_is_seeded_instead(self):
        self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
                        RDP_GATEWAY_SHARE_CREDS_KEY: False,
                        RDP_GATEWAY_USERNAME_KEY: "CONTOSO\\gwuser",
                        RDP_GATEWAY_PASSWORD_KEY: "gwpass"})
        self.assertEqual(self.store.saved["gw.contoso.com"], ("CONTOSO\\gwuser", "gwpass"))
        self.assertEqual(self.store.saved["TERMSRV/10.0.0.5"], ("CONTOSO\\admin", "s3cret"))

    def test_nothing_is_seeded_when_windows_should_ask(self):
        self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
                        RDP_GATEWAY_SHARE_CREDS_KEY: False})
        self.assertNotIn("gw.contoso.com", self.cmdkey_targets)

    def test_the_gateway_login_is_a_domain_credential_under_the_bare_address(self):
        # Established against mstsc on Windows 11: a generic credential, or
        # anything under TERMSRV/<gateway>, leaves the "Enter your
        # credentials" prompt up; a Domain Password one under the bare
        # address makes it go away.
        self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertEqual(self.store.kinds["gw.contoso.com"],
                         windows_credentials.CRED_TYPE_DOMAIN_PASSWORD)
        self.assertEqual(self.store.kinds["TERMSRV/10.0.0.5"],
                         windows_credentials.CRED_TYPE_GENERIC)
        self.assertNotIn("TERMSRV/gw.contoso.com", self.cmdkey_targets)

    def test_a_gateway_typed_with_a_port_is_stored_both_ways(self):
        self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com:8443"})
        for target in ("gw.contoso.com:8443", "gw.contoso.com"):
            self.assertIn(target, self.cmdkey_targets)

    def test_the_prompt_is_suppressed_only_when_the_desktop_login_was_stored(self):
        lines = self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertIn("prompt for credentials:i:0", lines)

        self.launched.clear()
        lines = self._launch(password="", **{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertIn("prompt for credentials:i:1", lines)

    def test_a_refused_credential_write_does_not_stop_the_launch(self):
        # Credential Manager can say no (policy, a locked profile). mstsc
        # then asks — which is the fallback, not a failure.
        windows_credentials.use_credential_store(
            windows_credentials.MemoryCredentialStore(fail_targets={"gw.contoso.com"}))
        lines = self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertIn("gatewayhostname:s:gw.contoso.com", lines)
        self.assertTrue(self.launched)

    # ── the same route in, whichever window the desktop lands in ────────

    def test_external_multimon_uses_fixed_resolution_and_network_detection(self):
        lines = self._launch()
        self.assertIn("use multimon:i:1", lines)
        self.assertIn("dynamic resolution:i:0", lines)
        self.assertIn("networkautodetect:i:1", lines)
        self.assertIn("bandwidthautodetect:i:1", lines)
        self.assertIn("bitmapcachepersistenable:i:0", lines)

    def _reach_lines(self, lines: list[str]) -> set[str]:
        """The directives about reaching the machine, display ones set aside."""
        keys = ("full address", "username", "prompt for credentials", "gateway",
                "promptcredentialonce", "authentication level", "enablecredsspsupport",
                "negotiate security layer", "redirect", "audiomode", "drivestoredirect")
        return {l for l in lines if l.split(":")[0].startswith(keys)}

    def test_the_embedded_session_is_handed_the_same_gateway(self):
        gateway = {RDP_GATEWAY_HOST_KEY: "gw.contoso.com", RDP_GATEWAY_BYPASS_KEY: False}
        external = self._reach_lines(self._launch(**gateway))
        embedded = self._reach_lines(self._launch_embedded(**gateway))
        self.assertEqual(external, embedded)
        self.assertIn("gatewayusagemethod:i:1", embedded)
        self.assertIn("gw.contoso.com", self.cmdkey_targets)

    def test_the_external_handoff_is_handed_the_same_gateway(self):
        gateway = {RDP_GATEWAY_HOST_KEY: "gw.contoso.com"}
        external = self._reach_lines(self._launch(**gateway))
        aux = self._reach_lines(self._open_external_window(**gateway))
        self.assertEqual(external, aux)

    def test_failed_external_launch_keeps_the_embedded_connection(self):
        widget = self.rdp_launcher.EmbeddedRDPWidget(self._connection())
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.cleanup)
        widget._launch_fallback_mstsc(1280, 800, reconnecting=False)
        embedded = widget._rdp_proc
        widget._process_health_timer.start()
        closed = mock.Mock()
        widget.session_closed.connect(closed)

        def fail_launch(cmd):
            self.launched.append(list(cmd))
            raise OSError("process could not start")

        with mock.patch.object(self.rdp_launcher.subprocess, "Popen", fail_launch):
            widget.btn_cad.click()

        self.assertIs(widget._rdp_proc, embedded)
        self.assertTrue(widget._process_health_timer.isActive())
        self.assertFalse(widget._user_disconnected)
        self.assertIn("Could not open", widget.status.text())
        self.assertFalse(os.path.exists(self.launched[-1][1]))
        self.assertTrue(widget._credentials.desktop_ready)
        closed.assert_not_called()

    def test_the_embedded_session_still_sets_its_own_display(self):
        lines = self._launch_embedded(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertIn("desktopwidth:i:1280", lines)
        self.assertIn("desktopheight:i:800", lines)
        self.assertIn("screen mode id:i:1", lines)

    def test_every_launch_path_writes_one_directive_per_key(self):
        # Two copies of a key in one file is how the paths used to disagree
        # with themselves; mstsc takes the last one, silently.
        for lines in (self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"}),
                      self._launch_embedded(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"}),
                      self._open_external_window(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})):
            keys = [l.split(":")[0] for l in lines]
            duplicates = {k for k in keys if keys.count(k) > 1}
            self.assertEqual(duplicates, set(), lines)

    def test_the_local_resource_answer_still_reaches_the_file(self):
        # The gateway block is appended alongside it; neither may displace
        # the other.
        lines = self._launch(**{RDP_GATEWAY_HOST_KEY: "gw.contoso.com"})
        self.assertIn("redirectclipboard:i:1", lines)
        self.assertIn("audiomode:i:0", lines)


class GatewayCredentialTargetTests(_MemoryCredentials):
    """Where a gateway login is put so that mstsc finds it."""

    def test_the_bare_address_comes_first_because_that_is_what_mstsc_writes(self):
        self.assertEqual(gateway_credential_targets("gw.contoso.com")[0], "gw.contoso.com")

    def test_a_port_is_stored_with_and_without(self):
        self.assertEqual(gateway_credential_targets("gw.contoso.com:8443"),
                         ["gw.contoso.com:8443", "gw.contoso.com"])

    def test_termsrv_is_not_a_gateway_target(self):
        # mstsc does not consult it for the gateway hop.
        for target in gateway_credential_targets("gw.contoso.com:8443"):
            self.assertFalse(target.startswith("TERMSRV/"), target)

    def test_an_ipv6_gateway_keeps_its_brackets_for_mstsc(self):
        targets = gateway_credential_targets("[2001:db8::1]:443")
        self.assertIn("[2001:db8::1]", targets)

    def test_no_gateway_means_nowhere(self):
        self.assertEqual(gateway_credential_targets(""), [])

    def test_seeding_writes_the_shared_login_under_each_target(self):
        report = seed_gateway_credentials(
            {RDP_GATEWAY_HOST_KEY: "gw.contoso.com"}, "CONTOSO\\admin", "s3cret")
        self.assertTrue(report.ok)
        self.assertEqual(report.targets_written, ["gw.contoso.com"])
        self.assertEqual(self.store.saved["gw.contoso.com"], ("CONTOSO\\admin", "s3cret"))
        self.assertEqual(self.store.kinds["gw.contoso.com"],
                         windows_credentials.CRED_TYPE_DOMAIN_PASSWORD)

    def test_seeding_reports_a_refusal_rather_than_hiding_it(self):
        windows_credentials.use_credential_store(
            windows_credentials.MemoryCredentialStore(fail_targets={"gw.contoso.com"}))
        report = seed_gateway_credentials(
            {RDP_GATEWAY_HOST_KEY: "gw.contoso.com"}, "CONTOSO\\admin", "s3cret")
        self.assertFalse(report.ok)
        self.assertTrue(report.attempted)
        self.assertIn("refused", report.failure)

    def test_nothing_to_store_is_not_a_failure(self):
        report = seed_gateway_credentials(
            {RDP_GATEWAY_HOST_KEY: "gw.contoso.com", RDP_GATEWAY_SHARE_CREDS_KEY: False},
            "CONTOSO\\admin", "s3cret")
        self.assertFalse(report.attempted)
        self.assertEqual(self.store.targets, [])

    def test_a_password_that_will_not_fit_is_refused_before_windows_sees_it(self):
        write = windows_credentials.store_generic_credential("gw", "user", "x" * 3000)
        self.assertFalse(write.ok)
        self.assertIn("too long", write.reason)


class AccountFormTests(unittest.TestCase):
    """Gateways authenticate over NTLM, where the domain is not optional."""

    def test_down_level_and_upn_forms_are_recognised(self):
        self.assertEqual(split_account("CONTOSO\\admin"), ("CONTOSO", "admin"))
        self.assertEqual(split_account("admin@contoso.com"), ("contoso.com", "admin"))
        self.assertTrue(account_has_domain("CONTOSO\\admin"))
        self.assertTrue(account_has_domain("admin@contoso.com"))

    def test_a_bare_name_has_no_domain(self):
        self.assertEqual(split_account("admin"), ("", "admin"))
        self.assertFalse(account_has_domain("admin"))
        self.assertFalse(account_has_domain(""))


class MstscDialogClassificationTests(unittest.TestCase):
    """Telling mstsc's dialogs apart, from window trees seen on Windows 11.

    They are all titled "Remote Desktop Connection" and all class #32770;
    what is inside them is the only difference, and the difference between
    "waiting for a password" and "the gateway refused the certificate".
    """

    PROGRESS = [("Static", "Connecting to:"), ("Static", "dc01.contoso.local"),
                ("Static", "Securing remote connection..."),
                ("msctls_progress32", ""), ("Button", "Cancel")]
    GATEWAY_CERT_ERROR = [
        ("Static", "Your computer can't connect to the remote computer because "
                   "the Remote Desktop Gateway server's certificate is not trusted. "
                   "Contact your network administrator for assistance."),
        ("Button", "OK"), ("Button", "Help")]
    PUBLISHER_WARNING = [
        ("Static", "The publisher of this remote connection can't be identified. "
                   "Do you want to connect anyway?"),
        ("Button", "Clipboard"), ("Button", "Co&nnect"), ("Button", "Cancel")]
    CERT_WARNING = [
        ("Static", "The identity of the remote computer cannot be verified. "
                   "Do you want to connect anyway?"),
        ("Button", "&Yes"), ("Button", "&No"), ("Button", "View certificate...")]

    def test_the_progress_box_is_not_a_credential_prompt(self):
        self.assertEqual(
            classify_mstsc_dialog("Remote Desktop Connection", "#32770", self.PROGRESS),
            DIALOG_PROGRESS)

    def test_an_error_box_is_a_message(self):
        self.assertEqual(
            classify_mstsc_dialog("Remote Desktop Connection", "#32770", self.GATEWAY_CERT_ERROR),
            DIALOG_MESSAGE)

    def test_the_publisher_warning_is_a_security_warning(self):
        self.assertEqual(
            classify_mstsc_dialog("Remote Desktop Connection", "#32770", self.PUBLISHER_WARNING),
            DIALOG_SECURITY_WARNING)
        self.assertEqual(
            classify_mstsc_dialog("Remote Desktop Connection", "#32770", self.CERT_WARNING),
            DIALOG_SECURITY_WARNING)

    def test_the_xaml_credential_prompt_is_recognised_by_class(self):
        self.assertEqual(
            classify_mstsc_dialog("Windows Security", "Credential Dialog Xaml Host", []),
            DIALOG_CREDENTIALS)
        self.assertEqual(classify_mstsc_dialog("Windows Security", "#32770", []),
                         DIALOG_CREDENTIALS)

    def test_the_desktop_window_is_not_a_dialog(self):
        self.assertIsNone(
            classify_mstsc_dialog("dc01 - Remote Desktop Connection", "TscShellContainerClass"))

    def test_without_a_control_tree_a_bare_dialog_is_still_a_dialog(self):
        # Title-only callers only need to know "do not adopt this".
        self.assertIsNotNone(classify_mstsc_dialog("Remote Desktop Connection", "#32770"))

    def test_the_message_text_is_read_off_the_dialog_without_its_buttons(self):
        text = dialog_message_text(self.GATEWAY_CERT_ERROR)
        self.assertIn("certificate is not trusted", text)
        self.assertNotIn("OK", text.split())
        self.assertNotIn("Help", text)


class MstscMessageGuidanceTests(unittest.TestCase):
    """mstsc's words, turned into the thing to check."""

    def test_an_untrusted_gateway_certificate_says_to_import_it(self):
        guidance = explain_mstsc_message(
            "Your computer can't connect to the remote computer because the "
            "Remote Desktop Gateway server's certificate is not trusted.")
        self.assertIn("Trusted Root", guidance)

    def test_a_name_mismatch_says_to_use_the_certificate_name(self):
        guidance = explain_mstsc_message(
            "the Remote Desktop Gateway server address requested and the "
            "certificate subject name do not match")
        self.assertIn("name on the certificate", guidance)

    def test_an_unreachable_gateway_says_to_check_443(self):
        guidance = explain_mstsc_message(
            "Your computer can't connect to the remote computer because the "
            "Remote Desktop Gateway server is temporarily unavailable.")
        self.assertIn("443", guidance)

    def test_a_policy_refusal_names_the_policy(self):
        self.assertIn("RD CAP", explain_mstsc_message(
            "You are not authorized to connect through the Remote Desktop Gateway. "
            "The connection authorization policy does not allow it."))
        self.assertIn("RD RAP", explain_mstsc_message(
            "The Remote Desktop Gateway server's resource authorization policy "
            "does not allow access to this computer."))

    def test_a_rejected_login_suggests_the_domain_form(self):
        guidance = explain_mstsc_message("Your credentials did not work")
        self.assertIn("DOMAIN\\user", guidance)

    def test_an_unknown_message_comes_back_tidied_not_lost(self):
        self.assertEqual(explain_mstsc_message("  Something   odd\n happened "),
                         "Something odd happened")
        self.assertEqual(explain_mstsc_message(""), "")

    def test_gateway_messages_are_blamed_on_the_gateway(self):
        self.assertTrue(is_gateway_message("The Remote Desktop Gateway server is unavailable"))
        self.assertFalse(is_gateway_message("The remote computer requires NLA"))


class DisconnectReasonTests(unittest.TestCase):
    """How a connection ended, as Windows' own RDP client logged it."""

    #: As mstsc pid 25900 logged it on 2026-10-14: signed in, then reason 2308.
    EVENTS = [
        "<Event><System><EventID>1027</EventID></System><EventData>"
        "<Data Name='DomainName'>ECS</Data><Data Name='SessionId'>3</Data></EventData></Event>",
        "<Event><System><EventID>1026</EventID></System><EventData>"
        "<Data Name='Name'>Disconnect Reason</Data><Data Name='Value'>2308</Data>"
        "<Data Name='CustomLevel'>Info</Data></EventData></Event>",
    ]

    def fake_log(self, events, queries):
        batches = [list(events), []]
        return types.SimpleNamespace(
            EvtQueryChannelPath=1, EvtRenderEventXml=1,
            EvtQuery=lambda channel, flags, query: queries.append((channel, query)) or "handle",
            EvtNext=lambda handle, count: batches.pop(0),
            EvtRender=lambda event, flags: event)

    def test_a_signed_in_session_cut_by_the_network_says_so(self):
        queries = []
        with mock.patch.dict("sys.modules", {"win32evtlog": self.fake_log(self.EVENTS, queries)}):
            self.assertEqual(rdp_client_outcome(25900, 1_760_466_359.9), (True, 2308))
        channel, query = queries[0]
        self.assertEqual(channel, "Microsoft-Windows-TerminalServices-RDPClient/Operational")
        self.assertIn("Execution[@ProcessID=25900]", query)
        self.assertIn("TimeCreated[@SystemTime>=", query)
        text = describe_disconnect(True, 2308, "10.100.50.4")
        self.assertIn("Signed in to 10.100.50.4", text)
        self.assertIn("reason 2308", text)
        self.assertIn("VPN", text)

    def test_a_connection_that_never_got_in_says_it_could_not_connect(self):
        self.assertEqual(describe_disconnect(False, 516, "srv"),
                         "Could not connect to srv: nothing accepted the connection (Windows reason 516).")

    def test_a_person_ending_the_session_needs_no_explanation(self):
        for reason in (None, 0, 1, 2, 3):
            self.assertEqual(describe_disconnect(True, reason, "srv"), "")

    def test_an_unreadable_log_is_not_an_error(self):
        broken = types.SimpleNamespace(EvtQueryChannelPath=1,
                                       EvtQuery=mock.Mock(side_effect=OSError("access denied")))
        with mock.patch.dict("sys.modules", {"win32evtlog": broken}):
            self.assertEqual(rdp_client_outcome(1, 0), (False, None))

    def test_the_log_is_read_once_per_launch(self):
        launcher = types.SimpleNamespace(connection_info={"host": "10.100.50.4"}, _rdp_launch=(25900, 1.0))
        with mock.patch("app.rdp_launcher.rdp_client_outcome", return_value=(True, 2308)) as outcome:
            first = EmbeddedRDPWidget._how_it_ended(launcher)
            again = EmbeddedRDPWidget._how_it_ended(launcher)
        self.assertEqual(first, again)
        self.assertIn("reason 2308", first)
        outcome.assert_called_once_with(25900, 1.0)


class GatewayRouteDescriptionTests(unittest.TestCase):
    def test_no_gateway_is_a_plain_connect(self):
        self.assertEqual(describe_gateway_route({}, "dc01"), "Connecting to dc01...")

    def test_the_route_says_whether_direct_is_tried_first(self):
        self.assertIn("direct first, then RD Gateway gw.contoso.com",
                      describe_gateway_route({RDP_GATEWAY_HOST_KEY: "gw.contoso.com"}, "dc01"))
        self.assertIn("always through RD Gateway gw.contoso.com",
                      describe_gateway_route({RDP_GATEWAY_HOST_KEY: "gw.contoso.com",
                                              RDP_GATEWAY_BYPASS_KEY: False}, "dc01"))


class ConnectionDialogGatewayTests(unittest.TestCase):
    """The gateway block in the connection dialog."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _rdp_dialog(self, connection=None):
        from app.connection_manager import ConnectionDialog

        dialog = ConnectionDialog(connection=connection)
        if connection is None:
            dialog.protocol.setCurrentText("RDP")
        return dialog

    def test_the_block_is_only_offered_for_rdp(self):
        dialog = self._rdp_dialog()
        self.assertTrue(dialog.rdp_gateway_group.isVisibleTo(dialog))
        dialog.protocol.setCurrentText("SSH")
        self.assertFalse(dialog.rdp_gateway_group.isVisibleTo(dialog))

    def test_the_options_stay_disabled_until_there_is_a_gateway(self):
        dialog = self._rdp_dialog()
        self.assertFalse(dialog.gateway_bypass_local.isEnabled())
        dialog.gateway_host.setText("gw.contoso.com")
        self.assertTrue(dialog.gateway_bypass_local.isEnabled())
        self.assertTrue(dialog.gateway_use_for_remote.isEnabled())

    def test_saving_stores_the_gateway_under_its_own_keys(self):
        dialog = self._rdp_dialog()
        dialog.name_input.setText("DC01")
        dialog.host_input.setText("10.0.0.5")
        dialog.username_input.setText("CONTOSO\\admin")
        dialog.password_input.setText("s3cret")
        dialog.gateway_host.setText("https://gw.contoso.com/rpc")
        dialog.gateway_bypass_local.setChecked(False)
        dialog.gateway_use_for_remote.setChecked(True)
        dialog._save()

        saved = dialog.get_result()
        self.assertEqual(saved[RDP_GATEWAY_HOST_KEY], "gw.contoso.com")
        self.assertIs(saved[RDP_GATEWAY_BYPASS_KEY], False)
        self.assertIs(saved[RDP_GATEWAY_SHARE_CREDS_KEY], True)
        # The machine's own address is untouched by any of it.
        self.assertEqual(saved["host"], "10.0.0.5")

    def test_editing_shows_back_what_was_saved(self):
        dialog = self._rdp_dialog()
        dialog.name_input.setText("DC01")
        dialog.host_input.setText("10.0.0.5")
        dialog.gateway_host.setText("gw.contoso.com")
        dialog.gateway_bypass_local.setChecked(False)
        dialog.gateway_use_for_remote.setChecked(False)
        dialog.gateway_username.setText("CONTOSO\\gwuser")
        dialog._save()

        reopened = self._rdp_dialog(connection=dialog.get_result())
        self.assertEqual(reopened.gateway_host.text(), "gw.contoso.com")
        self.assertFalse(reopened.gateway_bypass_local.isChecked())
        self.assertFalse(reopened.gateway_use_for_remote.isChecked())
        self.assertEqual(reopened.gateway_username.text(), "CONTOSO\\gwuser")

    def test_a_connection_saved_before_gateways_existed_opens_without_one(self):
        legacy = {"name": "DC01", "protocol": "rdp", "host": "10.0.0.5",
                  "username": "admin", "password": "s3cret", "port": 3389}
        dialog = self._rdp_dialog(connection=legacy)
        self.assertEqual(dialog.gateway_host.text(), "")
        self.assertEqual(dialog.gateway_username.text(), "")

    def test_the_hint_says_where_the_credentials_come_from(self):
        dialog = self._rdp_dialog()
        dialog.gateway_host.setText("gw.contoso.com")

        dialog.gateway_use_for_remote.setChecked(True)
        self.assertIn("both", dialog.gateway_hint.text())

        dialog.gateway_use_for_remote.setChecked(False)
        self.assertIn("separately", dialog.gateway_hint.text())

        dialog.gateway_username.setText("gwuser")
        self.assertIn("gateway login below", dialog.gateway_hint.text())

    def test_the_hint_asks_for_a_domain_when_the_gateway_login_has_none(self):
        # NTLM at the gateway needs one; the first sign otherwise is "your
        # credentials did not work" after the session has already failed.
        dialog = self._rdp_dialog()
        dialog.gateway_host.setText("gw.contoso.com")
        dialog.gateway_use_for_remote.setChecked(True)

        dialog.username_input.setText("admin")
        self.assertIn("DOMAIN\\admin", dialog.gateway_hint.text())

        dialog.username_input.setText("CONTOSO\\admin")
        self.assertNotIn("Gateways need the domain", dialog.gateway_hint.text())

        # A separate gateway login is judged on its own.
        dialog.gateway_username.setText("gwuser")
        self.assertIn("DOMAIN\\gwuser", dialog.gateway_hint.text())
        dialog.gateway_username.setText("gwuser@contoso.com")
        self.assertNotIn("Gateways need the domain", dialog.gateway_hint.text())


class GatewayConnectionTestTests(unittest.TestCase):
    """Pressing Test on a connection that goes through a gateway.

    Everything here runs on loopback — a listening port for "reachable" and a
    closed one for "not" — so no test depends on DNS or on anything outside
    the machine running it.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _listening_port(self) -> int:
        """A port that accepts and immediately closes, like a gateway would."""
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        self.addCleanup(server.close)

        def serve():
            while True:
                try:
                    connection, _ = server.accept()
                    connection.close()
                except OSError:
                    return

        threading.Thread(target=serve, daemon=True).start()
        return server.getsockname()[1]

    def _closed_port(self) -> int:
        """A port nothing is listening on, so connecting is refused at once."""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def _dialog(self, host, port, gateway, bypass=True):
        from app.connection_manager import ConnectionDialog

        dialog = ConnectionDialog()
        dialog.protocol.setCurrentText("RDP")
        dialog.host_input.setText(host)
        dialog.port_input.setValue(port)
        dialog.gateway_host.setText(gateway)
        dialog.gateway_bypass_local.setChecked(bypass)
        # Called straight rather than through the worker thread the button
        # starts, so the assertion sees a settled result.
        dialog._do_test()
        return dialog

    def test_a_desktop_that_only_answers_through_the_gateway_still_passes(self):
        # The reported failure: testing an RDP connection behind a gateway
        # probed the desktop directly and reported the resolver's error.
        gateway_port = self._listening_port()
        dialog = self._dialog(
            "127.0.0.1", self._closed_port(), f"127.0.0.1:{gateway_port}")

        status = dialog.test_status.text()
        self.assertTrue(status.startswith("✓"), status)
        self.assertIn("RD Gateway", status)
        self.assertIn("only answers through it", status)
        self.assertIn("normal answer", dialog.test_status.toolTip())

    def test_an_unreachable_gateway_fails_and_names_the_gateway(self):
        dialog = self._dialog(
            "127.0.0.1", 3389, f"127.0.0.1:{self._closed_port()}")

        status = dialog.test_status.text()
        self.assertTrue(status.startswith("✗"), status)
        self.assertIn("RD Gateway", status)
        self.assertIn("hop that has to work", dialog.test_status.toolTip())

    def test_a_desktop_reachable_both_ways_says_which_route_wins(self):
        port = self._listening_port()
        dialog = self._dialog("127.0.0.1", port, f"127.0.0.1:{port}", bypass=True)

        self.assertIn("also answers directly", dialog.test_status.text())
        self.assertIn("bypass it", dialog.test_status.text())

    def test_with_bypass_off_the_gateway_is_always_the_route(self):
        port = self._listening_port()
        dialog = self._dialog("127.0.0.1", port, f"127.0.0.1:{port}", bypass=False)

        self.assertIn("also answers directly", dialog.test_status.text())
        self.assertNotIn("bypass it", dialog.test_status.text())
        self.assertIn("Bypass is off", dialog.test_status.toolTip())

    def test_a_gateway_on_the_default_port_needs_no_port_typed(self):
        # Nothing is listening on 443 here, so this fails — but it must fail
        # as a gateway on 443, proving the default was applied.
        dialog = self._dialog("127.0.0.1", 3389, "127.0.0.1")
        self.assertIn("127.0.0.1:443", dialog.test_status.toolTip())

    def test_the_gateway_is_read_from_the_form_before_it_is_saved(self):
        gateway_port = self._listening_port()
        dialog = self._dialog(
            "127.0.0.1", self._closed_port(), f"127.0.0.1:{gateway_port}")
        # Nothing was saved; the test still used what was on screen.
        self.assertEqual(dialog.get_result(), {})
        self.assertTrue(dialog.test_status.text().startswith("✓"))

    def test_a_connection_with_no_gateway_takes_the_original_path(self):
        dialog = self._dialog("127.0.0.1", self._closed_port(), "")

        status = dialog.test_status.text()
        self.assertTrue(status.startswith("✗"), status)
        self.assertNotIn("RD Gateway", status)
        self.assertIn("refused the connection", status)
        # The label is clipped to fit; the hover text carries the whole thing.
        self.assertIn("nothing is listening", dialog.test_status.toolTip())

    def test_a_gateway_on_a_non_rdp_connection_is_ignored(self):
        # The block is hidden for SSH, but a protocol switched after typing
        # one must not reroute the test.
        from app.connection_manager import ConnectionDialog

        dialog = ConnectionDialog()
        dialog.protocol.setCurrentText("RDP")
        dialog.gateway_host.setText(f"127.0.0.1:{self._listening_port()}")
        dialog.protocol.setCurrentText("SSH")
        dialog.host_input.setText("127.0.0.1")
        dialog.port_input.setValue(self._closed_port())
        dialog._do_test()

        self.assertNotIn("RD Gateway", dialog.test_status.text())


if __name__ == "__main__":
    unittest.main()
