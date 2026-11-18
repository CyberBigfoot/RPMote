"""Restart tests: what gets sent, where it goes, and what refuses to run.

Nothing here actually reboots anything — the transports are stubbed.  What is
tested is the part that decides *which* machine receives *which* command, since
getting that wrong means rebooting the wrong customer's server.
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

_TMP = Path(tempfile.mkdtemp(prefix="rpmote_restart_test_"))
# All-or-nothing: redirecting only some paths would leave the rest pointed at
# the real ~/.rpmote and let these tests clobber a live vault.
vault_mod.use_vault_directory(_TMP)

from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from app import restart as restart_mod
from app.host_keys import fingerprint
from app.restart import (
    COMMAND_PRESETS, DEFAULT_COMMANDS, DEFAULT_DIRECT_COMMAND, REBOOT_METHODS,
    RestartError, RestartNotConfigured, RestartPlan, RestartResult,
    RestartRunner, can_restart_directly, direct_plan, needs_sudo,
    normalize_config, normalize_config_map, prepare_sudo, prune_configs,
    render_command, resolve_plan, run_restart,
)

_APP = QApplication.instance() or QApplication([])

# Host keys for the verification tests. Generated once — 2048-bit RSA is slow.
import paramiko as _paramiko_for_keys
_UNKNOWN_KEY = _paramiko_for_keys.RSAKey.generate(2048)
_EXPECTED_KEY = _paramiko_for_keys.RSAKey.generate(2048)


def pump(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    _APP.processEvents()
    return bool(predicate())


CONNECTIONS = [
    {"id": "dc1", "name": "DC01", "host": "10.0.0.10", "port": 3389,
     "protocol": "rdp", "tenant": "Contoso", "username": "admin"},
    {"id": "mgmt", "name": "MGMT01", "host": "10.0.0.5", "port": 22,
     "protocol": "ssh", "tenant": "Contoso", "username": "svc",
     "password": "hunter2"},
    {"id": "fab1", "name": "FAB-APP", "host": "10.1.0.20", "port": 22,
     "protocol": "ssh", "tenant": "Fabrikam"},
]


def servers(**overrides) -> dict:
    base = {"Contoso": {"conn_id": "mgmt", "transport": "ssh",
                        "command": "restart {host}", "enabled": True}}
    base.update(overrides)
    return base


class ConfigNormalisationTests(unittest.TestCase):

    def test_a_full_record_survives_intact(self):
        cfg = normalize_config({"conn_id": "mgmt", "transport": "powershell",
                                "command": "do the thing", "enabled": False})
        self.assertEqual(cfg["conn_id"], "mgmt")
        self.assertEqual(cfg["transport"], "powershell")
        self.assertEqual(cfg["command"], "do the thing")
        self.assertFalse(cfg["enabled"])

    def test_a_record_without_a_server_is_useless_and_dropped(self):
        self.assertIsNone(normalize_config({"transport": "ssh"}))
        self.assertIsNone(normalize_config({"conn_id": "   "}))
        self.assertIsNone(normalize_config(None))
        self.assertIsNone(normalize_config("nonsense"))

    def test_an_unknown_transport_falls_back_to_ssh(self):
        cfg = normalize_config({"conn_id": "mgmt", "transport": "carrier pigeon"})
        self.assertEqual(cfg["transport"], "ssh")

    def test_a_blank_command_gets_the_transport_default(self):
        cfg = normalize_config({"conn_id": "mgmt", "transport": "powershell",
                                "command": "   "})
        self.assertEqual(cfg["command"], DEFAULT_COMMANDS["powershell"])

    def test_a_map_drops_only_the_bad_entries(self):
        got = normalize_config_map({
            "Contoso": {"conn_id": "mgmt"},
            "Broken": {"no": "server"},
            "": {"conn_id": "mgmt"},
        })
        self.assertEqual(set(got), {"Contoso"})


class CommandRenderingTests(unittest.TestCase):

    target = {"host": "SRV01", "name": "File Server", "port": 3389,
              "username": "admin", "tenant": "Contoso"}
    server = {"host": "MGMT01", "name": "Management"}

    def test_placeholders_are_filled_from_both_ends(self):
        got = render_command("reboot {host} from {server} for {tenant}",
                             self.target, self.server)
        self.assertEqual(got, "reboot SRV01 from MGMT01 for Contoso")

    def test_a_powershell_script_block_survives_untouched(self):
        """Literal replacement, not str.format — braces are PowerShell syntax
        here, not formatting."""
        template = DEFAULT_COMMANDS["powershell"]
        got = render_command(template, self.target, self.server)
        self.assertIn("ScriptBlock { Restart-Computer", got)
        self.assertNotIn("{{", got)
        self.assertNotIn("{host}", got)

    def test_every_preset_renders_without_leftover_placeholders(self):
        for label, _transport, template in COMMAND_PRESETS:
            got = render_command(template, self.target, self.server)
            self.assertNotIn("{host}", got, label)
            self.assertNotIn("{server}", got, label)
            self.assertTrue(got, label)

    def test_a_hostname_carrying_a_second_command_is_refused(self):
        for evil in ("SRV01; rm -rf /", "SRV01 && shutdown", "SRV01`whoami`",
                     "SRV01 | tee", "SRV01\nshutdown", "$(id)"):
            with self.assertRaises(RestartError, msg=evil):
                render_command("reboot {host}", {"host": evil}, self.server)

    def test_an_unused_placeholder_is_not_vetted(self):
        """An apostrophe in a tenant name must not break a command that never
        mentions the tenant."""
        got = render_command("reboot {host}",
                             {"host": "SRV01", "tenant": "O'Brien Ltd"},
                             self.server)
        self.assertEqual(got, "reboot SRV01")

    def test_the_placeholder_it_does_use_is_still_vetted(self):
        with self.assertRaises(RestartError):
            render_command("echo {tenant}; reboot",
                           {"host": "SRV01", "tenant": "O'Brien"}, self.server)

    def test_missing_fields_render_as_empty_rather_than_crashing(self):
        got = render_command("reboot {host} as {user}", {"host": "SRV01"}, {})
        self.assertEqual(got, "reboot SRV01 as")


class PlanResolutionTests(unittest.TestCase):

    def test_a_tenants_server_is_used_by_default(self):
        plan = resolve_plan(CONNECTIONS[0], CONNECTIONS, servers(), {})
        self.assertEqual(plan.server_name, "MGMT01")
        self.assertEqual(plan.command, "restart 10.0.0.10")
        self.assertEqual(plan.scope, "tenant")

    def test_a_connection_override_beats_its_tenant(self):
        overrides = {"dc1": {"conn_id": "fab1", "transport": "ssh",
                             "command": "special {host}", "enabled": True}}
        plan = resolve_plan(CONNECTIONS[0], CONNECTIONS, servers(), overrides)
        self.assertEqual(plan.server_name, "FAB-APP")
        self.assertEqual(plan.command, "special 10.0.0.10")
        self.assertEqual(plan.scope, "connection")

    def test_a_disabled_override_falls_back_to_the_tenant(self):
        overrides = {"dc1": {"conn_id": "fab1", "transport": "ssh",
                             "command": "x", "enabled": False}}
        plan = resolve_plan(CONNECTIONS[0], CONNECTIONS, servers(), overrides)
        self.assertEqual(plan.server_name, "MGMT01")

    def test_an_unconfigured_ssh_box_falls_back_to_a_direct_restart(self):
        """Nothing configured, but RPMote can already open a shell on it — so
        asking the user to nominate a restart server would be ceremony."""
        plan = resolve_plan(CONNECTIONS[2], CONNECTIONS, servers(), {})
        self.assertTrue(plan.direct)
        self.assertIs(plan.server, plan.target)

    def test_an_unconfigured_tenant_with_no_direct_route_says_so(self):
        rdp_only = {"id": "rdp1", "name": "KIOSK", "host": "10.2.0.9",
                    "protocol": "rdp", "tenant": "Northwind"}
        with self.assertRaises(RestartNotConfigured) as caught:
            resolve_plan(rdp_only, CONNECTIONS + [rdp_only], servers(), {})
        self.assertIn("Northwind", str(caught.exception))

    def test_the_direct_fallback_can_be_switched_off(self):
        with self.assertRaises(RestartNotConfigured):
            resolve_plan(CONNECTIONS[2], CONNECTIONS, servers(), {},
                         allow_direct=False)

    def test_a_configured_server_still_wins_over_the_direct_route(self):
        """Configuring one was deliberate; do not quietly ignore it."""
        plan = resolve_plan(CONNECTIONS[1], CONNECTIONS, servers(), {})
        self.assertFalse(plan.direct)
        self.assertEqual(plan.scope, "tenant")

    def test_a_disabled_tenant_server_refuses_rather_than_guessing(self):
        cfg = servers()
        cfg["Contoso"]["enabled"] = False
        with self.assertRaises(RestartNotConfigured):
            resolve_plan(CONNECTIONS[0], CONNECTIONS, cfg, {})

    def test_a_server_that_has_been_deleted_is_reported_clearly(self):
        cfg = servers()
        cfg["Contoso"]["conn_id"] = "gone"
        with self.assertRaises(RestartNotConfigured) as caught:
            resolve_plan(CONNECTIONS[0], CONNECTIONS, cfg, {},
                         allow_direct=False)
        self.assertIn("no longer exists", str(caught.exception))

    def test_an_unsafe_hostname_stops_the_plan_before_it_can_run(self):
        evil = dict(CONNECTIONS[0], host="10.0.0.10; shutdown -h now")
        with self.assertRaises(RestartError):
            resolve_plan(evil, CONNECTIONS + [evil], servers(), {})

    def test_no_connection_at_all_is_handled(self):
        with self.assertRaises(RestartNotConfigured):
            resolve_plan(None, CONNECTIONS, servers(), {})


class PruningTests(unittest.TestCase):

    def test_config_for_a_deleted_tenant_is_dropped(self):
        cfg = servers(Ghost={"conn_id": "mgmt", "transport": "ssh",
                             "command": "x", "enabled": True})
        kept, _ = prune_configs(cfg, {}, CONNECTIONS)
        self.assertEqual(set(kept), {"Contoso"})

    def test_config_pointing_at_a_deleted_server_is_dropped(self):
        cfg = servers()
        cfg["Contoso"]["conn_id"] = "gone"
        kept, _ = prune_configs(cfg, {}, CONNECTIONS)
        self.assertEqual(kept, {})

    def test_an_override_for_a_deleted_connection_is_dropped(self):
        overrides = {"vanished": {"conn_id": "mgmt", "transport": "ssh",
                                  "command": "x", "enabled": True}}
        _, kept = prune_configs(servers(), overrides, CONNECTIONS)
        self.assertEqual(kept, {})

    def test_an_empty_connection_list_is_not_treated_as_a_deletion(self):
        """A vault that has not finished loading must not wipe the user's
        setup."""
        kept_s, kept_o = prune_configs(servers(), {"dc1": normalize_config(
            {"conn_id": "mgmt"})}, [])
        self.assertEqual(set(kept_s), {"Contoso"})
        self.assertEqual(set(kept_o), {"dc1"})


class ExecutionTests(unittest.TestCase):

    def make_plan(self, transport="ssh") -> RestartPlan:
        return RestartPlan(CONNECTIONS[0], CONNECTIONS[1], transport,
                           "restart 10.0.0.10", "tenant")

    def test_a_zero_exit_code_is_success(self):
        self.addCleanup(setattr, restart_mod, "_run_over_ssh",
                        restart_mod._run_over_ssh)
        restart_mod._run_over_ssh = lambda plan, timeout: RestartResult(
            True, "sent", "ok", "", 0, plan.command)
        result = run_restart(self.make_plan())
        self.assertTrue(result.ok)

    def test_a_transport_that_raises_becomes_a_failed_result_not_a_crash(self):
        self.addCleanup(setattr, restart_mod, "_run_over_ssh",
                        restart_mod._run_over_ssh)

        def boom(plan, timeout):
            raise RuntimeError("network on fire")

        restart_mod._run_over_ssh = boom
        result = run_restart(self.make_plan())
        self.assertFalse(result.ok)
        self.assertIn("network on fire", result.detail)

    def test_the_transport_choice_is_honoured(self):
        seen = []
        for name in ("_run_over_ssh", "_run_over_powershell"):
            self.addCleanup(setattr, restart_mod, name, getattr(restart_mod, name))
        restart_mod._run_over_ssh = lambda p, t: seen.append("ssh") or RestartResult(True)
        restart_mod._run_over_powershell = lambda p, t: seen.append("ps") or RestartResult(True)

        run_restart(self.make_plan("ssh"))
        run_restart(self.make_plan("powershell"))
        self.assertEqual(seen, ["ssh", "ps"])

    def test_a_failure_summary_carries_the_diagnosis(self):
        result = RestartResult(False, "rejected", "", "Access is denied.", 5,
                               "shutdown /r")
        summary = result.summary()
        self.assertIn("rejected", summary)
        self.assertIn("exit code 5", summary)
        self.assertIn("Access is denied", summary)


class RunnerTests(unittest.TestCase):

    def test_a_restart_runs_off_the_calling_thread_and_reports_back(self):
        self.addCleanup(setattr, restart_mod, "_run_over_ssh",
                        restart_mod._run_over_ssh)
        restart_mod._run_over_ssh = lambda plan, timeout: RestartResult(
            True, "sent", "", "", 0, plan.command)

        runner = RestartRunner()
        self.addCleanup(runner.shutdown)
        done = []
        runner.finished.connect(lambda plan, result: done.append((plan, result)))
        plan = RestartPlan(CONNECTIONS[0], CONNECTIONS[1], "ssh", "restart", "tenant")
        runner.submit(plan)

        self.assertTrue(pump(lambda: bool(done)), "the runner never reported back")
        self.assertTrue(done[0][1].ok)
        self.assertIs(done[0][0], plan)

    def test_a_failing_restart_still_reports_rather_than_hanging(self):
        self.addCleanup(setattr, restart_mod, "_run_over_ssh",
                        restart_mod._run_over_ssh)
        restart_mod._run_over_ssh = lambda plan, timeout: RestartResult(
            False, "no route to host", command=plan.command)

        runner = RestartRunner()
        self.addCleanup(runner.shutdown)
        done = []
        runner.finished.connect(lambda plan, result: done.append(result))
        runner.submit(RestartPlan(CONNECTIONS[0], CONNECTIONS[1], "ssh",
                                  "restart", "tenant"))
        self.assertTrue(pump(lambda: bool(done)))
        self.assertFalse(done[0].ok)


class DialogTests(unittest.TestCase):

    def test_confirming_a_restart_shows_target_route_and_command(self):
        from app.restart_dialogs import RestartConfirmDialog

        plan = RestartPlan(CONNECTIONS[0], CONNECTIONS[1], "ssh",
                           "shutdown /r /m \\\\10.0.0.10", "tenant")
        dialog = RestartConfirmDialog(plan)
        self.addCleanup(dialog.deleteLater)
        text = " ".join(w.text()
                        for w in dialog.findChildren(type(dialog.btn_restart)))
        labels = " ".join(lbl.text() for lbl in dialog.findChildren(QLabel))
        self.assertIn("DC01", labels)
        self.assertIn("MGMT01", labels)
        self.assertIn("10.0.0.10", labels)
        self.assertIn("Restart", text)

    def test_enter_does_not_fire_the_restart_button(self):
        """The safe option is the default; rebooting must be a deliberate
        click."""
        from app.restart_dialogs import RestartConfirmDialog

        plan = RestartPlan(CONNECTIONS[0], CONNECTIONS[1], "ssh", "x", "tenant")
        dialog = RestartConfirmDialog(plan)
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.btn_restart.isDefault())
        self.assertFalse(dialog.btn_restart.autoDefault())

    def test_the_setup_dialog_previews_the_real_command(self):
        from app.restart_dialogs import RestartServerDialog

        dialog = RestartServerDialog("tenant", "Contoso", "Contoso", CONNECTIONS,
                                     servers()["Contoso"], preferred_tenant="Contoso")
        self.addCleanup(dialog.deleteLater)
        self.assertIn("10.0.0.10", dialog.preview.text())

    def test_the_setup_dialog_refuses_an_empty_command(self):
        from app.restart_dialogs import RestartServerDialog

        warned = []
        self.addCleanup(setattr, QMessageBox, "warning", QMessageBox.warning)
        QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))

        dialog = RestartServerDialog("tenant", "Contoso", "Contoso", CONNECTIONS,
                                     servers()["Contoso"])
        self.addCleanup(dialog.deleteLater)
        dialog.command_edit.setPlainText("   ")
        dialog._save()
        self.assertIsNone(dialog.result_config)
        self.assertTrue(warned)

    def test_the_setup_dialog_saves_what_was_chosen(self):
        from app.restart_dialogs import RestartServerDialog

        dialog = RestartServerDialog("tenant", "Contoso", "Contoso", CONNECTIONS,
                                     None, preferred_tenant="Contoso")
        self.addCleanup(dialog.deleteLater)
        index = dialog.server_combo.findData("mgmt")
        dialog.server_combo.setCurrentIndex(index)
        dialog.command_edit.setPlainText("reboot {host}")
        dialog._save()
        self.assertEqual(dialog.result_config["conn_id"], "mgmt")
        self.assertEqual(dialog.result_config["command"], "reboot {host}")
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_the_tenants_own_machines_are_offered_first(self):
        from app.restart_dialogs import RestartServerDialog

        dialog = RestartServerDialog("tenant", "Fabrikam", "Fabrikam", CONNECTIONS,
                                     None, preferred_tenant="Fabrikam")
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.server_combo.itemData(0), "fab1")

    def test_removing_a_setup_is_confirmed_first(self):
        from app.restart_dialogs import RestartServerDialog

        self.addCleanup(setattr, QMessageBox, "question", QMessageBox.question)
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.No)

        dialog = RestartServerDialog("tenant", "Contoso", "Contoso", CONNECTIONS,
                                     servers()["Contoso"])
        self.addCleanup(dialog.deleteLater)
        dialog._remove()
        self.assertFalse(dialog.removed, "declining must not remove anything")

    def test_the_manager_lists_configured_and_unconfigured_tenants_alike(self):
        from app.restart_dialogs import RestartManagerDialog

        dialog = RestartManagerDialog(servers(), {}, CONNECTIONS)
        self.addCleanup(dialog.deleteLater)
        rows = [dialog.list.item(i).text() for i in range(dialog.list.count())]
        joined = "\n".join(rows)
        self.assertIn("Contoso", joined)
        self.assertIn("MGMT01", joined)
        self.assertIn("Fabrikam", joined)
        self.assertIn("not configured", joined)


class MainWindowRestartTests(unittest.TestCase):
    """The window owns the vault, so it owns keeping the config honest."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def setUp(self):
        # Re-assert our own directory: another test module imported later in
        # the same process would otherwise still own the module-level globals.
        vault_mod.use_vault_directory(_TMP)
        for f in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                  vault_mod.HASH_FILE, vault_mod.KEY_FILE):
            if f.exists():
                f.unlink()

        from app.vault import Vault

        self.vault = Vault()
        self.vault.setup("restart-test-pw")
        for conn in CONNECTIONS:
            self.vault.add_connection({k: v for k, v in conn.items() if k != "id"})
        self.conns = self.vault.connections
        self.by_name = {c["name"]: c for c in self.conns}

        from app.main_window import MainWindow

        self.win = MainWindow(self.vault)
        self.addCleanup(self.win.deleteLater)
        self.addCleanup(self.win.close)

    def configure(self, tenant="Contoso", server_name="MGMT01"):
        self.vault.update_settings({"restart_servers": {tenant: {
            "conn_id": str(self.by_name[server_name]["id"]),
            "transport": "ssh", "command": "restart {host}", "enabled": True}}})

    def test_config_survives_an_ordinary_refresh(self):
        self.configure()
        self.win._refresh_sidebar()
        self.assertIn("Contoso", self.vault.settings.get("restart_servers", {}))

    def test_deleting_the_restart_server_clears_the_setup(self):
        """Otherwise a right-click keeps offering to restart through a machine
        that was deleted months ago."""
        self.configure()
        self.vault.delete_connection(str(self.by_name["MGMT01"]["id"]))
        self.win._refresh_sidebar()
        self.assertEqual(self.vault.settings.get("restart_servers", {}), {})

    def test_deleting_a_whole_tenant_clears_its_setup(self):
        self.configure()
        for conn in list(self.conns):
            if conn["tenant"] == "Contoso":
                self.vault.delete_connection(str(conn["id"]))
        self.win._refresh_sidebar()
        self.assertEqual(self.vault.settings.get("restart_servers", {}), {})

    def test_an_override_for_a_deleted_connection_is_cleared(self):
        self.configure()
        doomed = str(self.by_name["DC01"]["id"])
        self.vault.update_settings({"restart_overrides": {doomed: {
            "conn_id": str(self.by_name["MGMT01"]["id"]),
            "transport": "ssh", "command": "x", "enabled": True}}})
        self.vault.delete_connection(doomed)
        self.win._refresh_sidebar()
        self.assertEqual(self.vault.settings.get("restart_overrides", {}), {})

    def test_a_one_off_check_reveals_the_dots_without_subscribing(self):
        self.assertFalse(self.win.reachability.enabled)
        self.win._check_reachability_now([str(self.by_name["DC01"]["id"])])
        self.addCleanup(self.win.reachability.shutdown)
        self.assertTrue(self.win.dashboard.is_ping_enabled,
                        "a result you cannot see is not a result")
        self.assertFalse(self.win.reachability.enabled,
                         "one check must not turn on continuous probing")

    def test_probing_preferences_round_trip_through_the_vault(self):
        self.win._apply_ping_settings({"enabled": True, "interval": 120,
                                       "timeout": 4.0, "icmp_fallback": False})
        self.addCleanup(self.win.reachability.shutdown)
        saved = self.vault.settings.get("ping_settings", {})
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["interval"], 120)
        self.assertFalse(saved["icmp_fallback"])

        from app.main_window import MainWindow

        fresh = MainWindow(self.vault)
        self.addCleanup(fresh.deleteLater)
        self.addCleanup(fresh.close)
        self.assertTrue(fresh.reachability.enabled)
        self.assertEqual(fresh.reachability.settings["interval"], 120)
        self.assertTrue(fresh.dashboard.is_ping_enabled)


class SudoPreparationTests(unittest.TestCase):
    """The bug behind "sudo: A terminal is required to authenticate"."""

    def test_a_plain_sudo_command_is_told_to_read_stdin(self):
        self.assertEqual(prepare_sudo("sudo systemctl reboot"),
                         "sudo -S -p '' systemctl reboot")

    def test_an_already_prepared_command_is_left_alone(self):
        for done in ("sudo -S -p '' reboot", "sudo -n reboot"):
            self.assertEqual(prepare_sudo(done), done)

    def test_sudo_later_in_a_pipeline_is_prepared_too(self):
        self.assertEqual(prepare_sudo("echo x | sudo tee /etc/y"),
                         "echo x | sudo -S -p '' tee /etc/y")

    def test_a_quoted_shell_fallback_survives_intact(self):
        prepared = prepare_sudo(DEFAULT_DIRECT_COMMAND)
        self.assertTrue(prepared.startswith("sudo -S -p '' sh -c"))
        self.assertIn("systemctl reboot || shutdown -r now", prepared)

    def test_commands_without_sudo_are_untouched(self):
        for plain in ("shutdown -r now", "shutdown /r /t 0 /f", "reboot"):
            self.assertFalse(needs_sudo(plain))
            self.assertEqual(prepare_sudo(plain), plain)

    def test_a_word_merely_containing_sudo_is_not_mistaken_for_it(self):
        self.assertFalse(needs_sudo("/usr/local/bin/pseudonym --go"))
        self.assertFalse(needs_sudo("echo sudoku"))

    def test_every_reboot_method_is_classified_correctly(self):
        for label, template in REBOOT_METHODS:
            expected = template.strip().startswith("sudo")
            self.assertEqual(needs_sudo(template), expected, label)


class DirectPlanTests(unittest.TestCase):

    def test_an_ssh_connection_can_be_restarted_with_no_setup(self):
        plan = direct_plan(CONNECTIONS[1])
        self.assertTrue(plan.direct)
        self.assertEqual(plan.scope, "direct")
        self.assertEqual(plan.transport, "ssh")
        self.assertIs(plan.server, plan.target)
        self.assertTrue(plan.needs_sudo)

    def test_the_default_method_survives_not_knowing_the_os(self):
        """The MacMini case: systemctl does not exist there, so the command has
        to fall through to shutdown rather than simply failing."""
        plan = direct_plan(CONNECTIONS[1])
        self.assertIn("systemctl reboot", plan.command)
        self.assertIn("shutdown -r now", plan.command)
        self.assertIn("||", plan.command)

    def test_an_rdp_connection_explains_why_it_cannot(self):
        with self.assertRaises(RestartNotConfigured) as caught:
            direct_plan(CONNECTIONS[0])
        message = str(caught.exception)
        self.assertIn("DC01", message)
        self.assertIn("restart server", message)

    def test_a_connection_with_no_host_is_refused(self):
        with self.assertRaises(RestartNotConfigured):
            direct_plan({"id": "x", "name": "Nowhere", "protocol": "ssh",
                         "host": "   "})

    def test_a_chosen_method_overrides_the_default(self):
        plan = direct_plan(CONNECTIONS[1], "sudo shutdown -r now")
        self.assertEqual(plan.command, "sudo shutdown -r now")

    def test_a_root_method_needs_no_password(self):
        plan = direct_plan(CONNECTIONS[1], "shutdown -r now")
        self.assertFalse(plan.needs_sudo)

    def test_probeable_protocols_are_the_ones_we_can_shell_into(self):
        self.assertTrue(can_restart_directly({"protocol": "ssh", "host": "h"}))
        self.assertTrue(can_restart_directly({"protocol": "sftp", "host": "h"}))
        for no in ("rdp", "ftp", "azure_ps", "synology", ""):
            self.assertFalse(can_restart_directly({"protocol": no, "host": "h"}), no)


# ── A paramiko stand-in, so nothing reboots during a test run ──────

class _FakeChannel:
    def __init__(self, exit_status):
        self._exit_status = exit_status
        self.shutdown_write_called = False

    def recv_exit_status(self):
        if isinstance(self._exit_status, Exception):
            raise self._exit_status
        return self._exit_status

    def shutdown_write(self):
        self.shutdown_write_called = True


class _FakeStdin:
    def __init__(self, channel):
        self.channel = channel
        self.written = []

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass


class _FakeStream:
    def __init__(self, text, channel=None):
        self._text = text
        self.channel = channel

    def read(self):
        return self._text.encode("utf-8")


class _Recorder:
    def __init__(self):
        self.connect_kwargs = None
        self.command = None
        self.get_pty = None
        self.stdin = None
        self.closed = False
        self.policy = None


class _FakeHostKeys:
    """The slice of paramiko.HostKeys that prepare_client uses."""

    def __init__(self):
        self.entries = {}

    def add(self, name, keytype, key):
        self.entries.setdefault(name, {})[keytype] = key

    def lookup(self, name):
        return self.entries.get(name)


def fake_paramiko(stdout="", stderr="", exit_status=0,
                  unknown_host_key=None, bad_host_key=None):
    """Install a paramiko stand-in; returns the recorder of what happened.

    ``unknown_host_key`` makes the fake call the installed missing-host-key
    policy the way real paramiko does, and ``bad_host_key`` raises the real
    BadHostKeyException — so the host key paths are exercised for real rather
    than mocked past.
    """
    import types

    from paramiko import BadHostKeyException as _RealBadHostKey

    recorder = _Recorder()

    class FakeClient:
        def __init__(self):
            self._policy = None
            self._host_keys = _FakeHostKeys()

        def get_host_keys(self):
            return self._host_keys

        def set_missing_host_key_policy(self, policy):
            self._policy = policy
            recorder.policy = policy

        def connect(self, **kwargs):
            recorder.connect_kwargs = kwargs
            if bad_host_key is not None:
                server_key, expected_key = bad_host_key
                raise _RealBadHostKey(kwargs.get("hostname", ""),
                                      server_key, expected_key)
            if unknown_host_key is not None and self._policy is not None:
                self._policy.missing_host_key(
                    self, kwargs.get("hostname", ""), unknown_host_key)

        def exec_command(self, command, timeout=None, get_pty=False):
            recorder.command = command
            recorder.get_pty = get_pty
            channel = _FakeChannel(exit_status)
            recorder.stdin = _FakeStdin(channel)
            return (recorder.stdin,
                    _FakeStream(stdout, channel),
                    _FakeStream(stderr, channel))

        def close(self):
            recorder.closed = True

    module = types.ModuleType("paramiko")
    module.SSHClient = FakeClient
    module.AutoAddPolicy = lambda: object()
    module.SSHException = Exception
    return module, recorder


class SshExecutionTests(unittest.TestCase):

    def install(self, **kwargs):
        import sys
        import tempfile

        import app.vault as vault_module
        from app import host_keys

        # Keep the known-host list out of the developer's real ~/.rpmote, and
        # make every run start from "no host has been verified yet".
        tmp = tempfile.TemporaryDirectory()
        old_dir = vault_module.VAULT_DIR
        vault_module.use_vault_directory(tmp.name)
        host_keys.store().invalidate()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(host_keys.store().invalidate)
        self.addCleanup(vault_module.use_vault_directory, old_dir)

        module, recorder = fake_paramiko(**kwargs)
        real = sys.modules.get("paramiko")
        sys.modules["paramiko"] = module
        self.addCleanup(
            lambda: sys.modules.__setitem__("paramiko", real) if real
            else sys.modules.pop("paramiko", None))
        return recorder

    def ssh_conn(self, **overrides):
        conn = {"id": "mac", "name": "MacMini", "host": "10.0.0.44", "port": 22,
                "protocol": "ssh", "tenant": "Home", "username": "grant",
                "password": "login-pw"}
        conn.update(overrides)
        return conn

    def test_a_sudo_command_gets_the_terminal_it_asked_for(self):
        """The original failure: no PTY, so sudo had nowhere to authenticate."""
        recorder = self.install(exit_status=0)
        plan = direct_plan(self.ssh_conn(), "sudo systemctl reboot", "sudo-pw")
        result = run_restart(plan)
        self.assertTrue(recorder.get_pty, "sudo needs a PTY")
        self.assertIn("sudo -S -p ''", recorder.command)
        self.assertTrue(result.ok, result.summary())

    def test_the_password_goes_in_on_stdin_and_stdin_is_then_closed(self):
        """Closing stdin is what stops a wrong password hanging until timeout."""
        recorder = self.install(exit_status=0)
        run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "sudo-pw"))
        self.assertEqual(recorder.stdin.written, ["sudo-pw\n"])
        self.assertTrue(recorder.stdin.channel.shutdown_write_called)

    def test_a_command_without_sudo_gets_no_pty_and_no_password(self):
        recorder = self.install(exit_status=0)
        run_restart(direct_plan(self.ssh_conn(), "shutdown -r now"))
        self.assertFalse(recorder.get_pty)
        self.assertEqual(recorder.stdin.written, [])

    def test_the_login_password_is_used_when_no_sudo_password_is_given(self):
        """On most boxes the account's own password is what sudo wants."""
        recorder = self.install(exit_status=0)
        run_restart(direct_plan(self.ssh_conn(), "sudo reboot"))
        self.assertEqual(recorder.stdin.written, ["login-pw\n"])

    def test_an_unknown_host_is_refused_before_the_command_runs(self):
        """A reboot has no UI to prompt from, so it must not trust blind."""
        recorder = self.install(unknown_host_key=_UNKNOWN_KEY)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "pw"))
        self.assertFalse(result.ok)
        self.assertIn("not a known host", result.summary())
        self.assertIsNone(recorder.command, "nothing may run on an unverified host")

    def test_an_unknown_host_message_carries_the_fingerprint_to_check(self):
        self.install(unknown_host_key=_UNKNOWN_KEY)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "pw"))
        self.assertIn(fingerprint(_UNKNOWN_KEY), result.summary())

    def test_a_changed_host_key_is_reported_as_a_change_not_a_generic_error(self):
        recorder = self.install(bad_host_key=(_UNKNOWN_KEY, _EXPECTED_KEY))
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "pw"))
        self.assertFalse(result.ok)
        self.assertIn("CHANGED", result.summary())
        self.assertIsNone(recorder.command)

    def test_a_refused_host_never_sees_the_sudo_password(self):
        recorder = self.install(unknown_host_key=_UNKNOWN_KEY)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "hunter2"))
        self.assertNotIn("hunter2", result.summary())
        self.assertIsNone(recorder.stdin)

    def test_the_password_never_comes_back_in_the_output(self):
        """With a PTY the remote tty echoes what we type, so it would otherwise
        appear verbatim in the failure dialog."""
        self.install(stdout="[sudo] password: hunter2\nSorry, try again.",
                     exit_status=1)
        result = run_restart(
            direct_plan(self.ssh_conn(), "sudo reboot", "hunter2"))
        self.assertNotIn("hunter2", result.stdout)
        self.assertNotIn("hunter2", result.summary())
        self.assertIn("••••", result.stdout)

    def test_a_rejected_password_is_named_as_such(self):
        self.install(stdout="Sorry, try again.", exit_status=1)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "wrong"))
        self.assertFalse(result.ok)
        self.assertIn("password", result.detail.lower())

    def test_the_original_terminal_error_is_reported_as_a_password_problem(self):
        self.install(stdout="sudo: A terminal is required to authenticate",
                     exit_status=1)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "pw"))
        self.assertFalse(result.ok)
        self.assertIn("password", result.detail.lower())

    def test_a_user_missing_from_sudoers_gets_a_different_diagnosis(self):
        """Telling someone their password is wrong when the real problem is
        permissions sends them off fixing the wrong thing."""
        self.install(stdout="grant is not in the sudoers file.", exit_status=1)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "pw"))
        self.assertFalse(result.ok)
        self.assertIn("sudo", result.detail.lower())
        self.assertNotIn("not accepted", result.detail)

    def test_a_dropped_connection_reads_as_the_machine_going_down(self):
        """A reboot kills sshd before it can report an exit status; calling that
        a failure would be wrong every single time."""
        self.install(exit_status=-1)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "pw"))
        self.assertTrue(result.ok)
        self.assertIn("reboot", result.detail.lower())

    def test_an_exception_reading_the_exit_status_is_also_a_reboot(self):
        self.install(exit_status=EOFError("channel closed"))
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "pw"))
        self.assertTrue(result.ok)

    def test_a_real_nonzero_failure_is_still_a_failure(self):
        self.install(stdout="command not found: systemctl", exit_status=127)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo systemctl reboot",
                                         "pw"))
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 127)

    def test_a_dropped_connection_after_a_bad_password_is_not_success(self):
        """Auth failure is checked before the dropped-connection benefit of the
        doubt, or a wrong password would look like a successful reboot."""
        self.install(stdout="Sorry, try again.", exit_status=-1)
        result = run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "wrong"))
        self.assertFalse(result.ok)

    def test_the_connection_is_always_closed(self):
        recorder = self.install(exit_status=0)
        run_restart(direct_plan(self.ssh_conn(), "sudo reboot", "pw"))
        self.assertTrue(recorder.closed)


class ConfirmDialogSudoTests(unittest.TestCase):

    def conn(self, **overrides):
        base = {"id": "mac", "name": "MacMini", "host": "10.0.0.44", "port": 22,
                "protocol": "ssh", "tenant": "Home", "username": "grant",
                "password": "login-pw"}
        base.update(overrides)
        return base

    def dialog(self, plan, saved=""):
        from app.restart_dialogs import RestartConfirmDialog

        dlg = RestartConfirmDialog(plan, saved_password=saved)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_a_sudo_restart_asks_for_a_password(self):
        dlg = self.dialog(direct_plan(self.conn(), "sudo reboot"))
        self.assertTrue(dlg.sudo_box.isVisibleTo(dlg))

    def test_a_root_restart_does_not_ask(self):
        dlg = self.dialog(direct_plan(self.conn(), "shutdown -r now"))
        self.assertFalse(dlg.sudo_box.isVisibleTo(dlg))

    def test_switching_method_shows_and_hides_the_password_row(self):
        dlg = self.dialog(direct_plan(self.conn(), "shutdown -r now"))
        self.assertFalse(dlg.sudo_box.isVisibleTo(dlg))
        dlg.command_edit.setPlainText("sudo reboot")
        self.assertTrue(dlg.sudo_box.isVisibleTo(dlg))

    def test_the_saved_password_is_offered_and_used(self):
        dlg = self.dialog(direct_plan(self.conn(), "sudo reboot"), saved="login-pw")
        self.assertTrue(dlg.chk_saved.isChecked())
        dlg._accept()
        self.assertEqual(dlg.sudo_password, "login-pw")
        self.assertEqual(dlg.plan.sudo_password, "login-pw")

    def test_a_typed_password_wins_when_saved_is_unticked(self):
        dlg = self.dialog(direct_plan(self.conn(), "sudo reboot"), saved="login-pw")
        dlg.chk_saved.setChecked(False)
        dlg.pw_input.setText("different-pw")
        dlg._accept()
        self.assertEqual(dlg.sudo_password, "different-pw")

    def test_the_password_field_is_masked(self):
        from PyQt6.QtWidgets import QLineEdit

        dlg = self.dialog(direct_plan(self.conn(), "sudo reboot"))
        self.assertEqual(dlg.pw_input.echoMode(), QLineEdit.EchoMode.Password)

    def test_an_empty_password_is_refused_rather_than_sent(self):
        warned = []
        self.addCleanup(setattr, QMessageBox, "warning", QMessageBox.warning)
        QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))

        dlg = self.dialog(direct_plan(self.conn(password=""), "sudo reboot"))
        dlg.chk_saved.setChecked(False)
        dlg._accept()
        self.assertTrue(warned)
        self.assertEqual(dlg.result(), 0, "the dialog must stay open")

    def test_enter_in_the_password_field_does_not_fire_the_restart(self):
        """Cancel is the default button; a password field that accepted on
        Enter would quietly undo that protection."""
        dlg = self.dialog(direct_plan(self.conn(), "sudo reboot"), saved="pw")
        received = []
        dlg.accepted.connect(lambda: received.append(True))
        dlg.pw_input.returnPressed.emit()
        self.assertEqual(received, [])
        self.assertFalse(dlg.btn_restart.isDefault())

    def test_remembering_is_opt_in(self):
        dlg = self.dialog(direct_plan(self.conn(), "sudo reboot"), saved="login-pw")
        self.assertFalse(dlg.chk_remember.isChecked())
        dlg._accept()
        self.assertFalse(dlg.remember)

    def test_choosing_a_method_rewrites_the_command(self):
        dlg = self.dialog(direct_plan(self.conn()))
        index = next(i for i, (_l, t) in enumerate(REBOOT_METHODS)
                     if t == "sudo shutdown -r now")
        dlg.method_combo.setCurrentIndex(index)
        self.assertEqual(dlg.command_edit.toPlainText(), "sudo shutdown -r now")

    def test_a_direct_restart_says_it_needs_no_middleman(self):
        dlg = self.dialog(direct_plan(self.conn()))
        labels = " ".join(lbl.text() for lbl in dlg.findChildren(QLabel))
        self.assertIn("the machine itself", labels)

    def test_an_edited_command_reaches_the_plan(self):
        dlg = self.dialog(direct_plan(self.conn()), saved="login-pw")
        dlg.command_edit.setPlainText("sudo /sbin/reboot")
        dlg._accept()
        self.assertEqual(dlg.plan.command, "sudo /sbin/reboot")

    def test_a_routed_restart_keeps_its_command_read_only(self):
        """Editing a tenant's configured command from the confirm dialog would
        silently diverge from what the setup screen shows."""
        plan = RestartPlan(CONNECTIONS[0], CONNECTIONS[1], "ssh",
                           "restart 10.0.0.10", "tenant")
        dlg = self.dialog(plan)
        self.assertTrue(dlg.command_edit.isReadOnly())
        self.assertIsNone(dlg.method_combo)


if __name__ == "__main__":
    unittest.main()
