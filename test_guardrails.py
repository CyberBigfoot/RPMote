"""Knowing which boxes are production, and which commands deserve a pause."""

import unittest

from app.guardrails import (
    confirmation_for, dangers, describe_targets, is_production,
    looks_production, normalize_environment, normalize_production_tenants,
    production_reason,
)


class ProductionDetectionTests(unittest.TestCase):
    def test_an_explicit_choice_is_the_whole_answer(self):
        self.assertTrue(is_production({"name": "lab-01",
                                       "environment": "production"}))
        # ...including saying no on a box whose name says otherwise.
        self.assertFalse(is_production({"name": "acme-prod-sql",
                                        "environment": "development"}))

    def test_a_marked_tenant_covers_everything_under_it(self):
        conn = {"name": "web01", "tenant": "Acme Ltd"}
        self.assertFalse(is_production(conn))
        self.assertTrue(is_production(conn, ["Acme Ltd"]))

    def test_a_marked_tenant_is_matched_regardless_of_case(self):
        self.assertTrue(is_production({"tenant": "ACME LTD"}, ["acme ltd"]))

    def test_a_name_that_reads_as_production_is_enough(self):
        # An estate imported from ~/.ssh/config has nothing else to go on.
        for name in ("acme-prod-sql01", "PRD-DC1", "web (Production)",
                     "live-gateway"):
            self.assertTrue(is_production({"name": name}), name)

    def test_words_that_merely_contain_the_letters_do_not_count(self):
        for name in ("product-catalogue", "reproducible-build", "livestock-db",
                     "prodigy"):
            self.assertFalse(is_production({"name": name}), name)

    def test_a_plain_connection_is_not_production(self):
        self.assertFalse(is_production({"name": "web01", "tenant": "Acme"}))
        self.assertFalse(is_production({}))
        self.assertFalse(is_production(None))

    def test_the_reason_says_which_rule_fired(self):
        self.assertIn("connection's settings",
                      production_reason({"environment": "production"}))
        self.assertIn("production tenant",
                      production_reason({"tenant": "Acme"}, ["Acme"]))
        self.assertIn("tenant name", production_reason({"tenant": "Acme Prod"}))
        self.assertIn("connection name", production_reason({"name": "prod-01"}))
        self.assertEqual(production_reason({"name": "lab"}), "")

    def test_environments_are_normalised(self):
        self.assertEqual(normalize_environment("  Production "), "production")
        self.assertEqual(normalize_environment("prod"), "")
        self.assertEqual(normalize_environment(None), "")

    def test_a_saved_tenant_list_is_cleaned_up(self):
        self.assertEqual(
            normalize_production_tenants(["Acme", " Acme ", "", None, "Beta"]),
            ["Acme", "Beta"])
        self.assertEqual(normalize_production_tenants("Acme"), [])


class DangerousCommandTests(unittest.TestCase):
    def test_recursive_deletes_are_caught_however_they_are_spelt(self):
        for command in ("rm -rf /var/log", "rm -fr .", "rm -r -f build",
                        "sudo rm --recursive --force /opt/app"):
            self.assertTrue(dangers(command), command)

    def test_an_ordinary_delete_is_not_alarming(self):
        self.assertEqual(dangers("rm notes.txt"), [])
        self.assertEqual(dangers("rm -i notes.txt"), [])

    def test_the_usual_suspects_are_all_recognised(self):
        cases = {
            "shutdown -h now": "shuts the machine down",
            "sudo reboot": "restarts the machine",
            "mkfs.ext4 /dev/sdb1": "formats a filesystem",
            "dd if=/dev/zero of=/dev/sda bs=1M": "writes straight to a device",
            "DROP DATABASE customers;": "drops a database object",
            "kubectl delete ns prod": "deletes Kubernetes objects",
            "systemctl stop nginx": "stops a service",
            "chmod -R 777 /srv": "makes files world-writable",
            "Remove-Item C:\\data -Recurse -Force": "recursive delete (Remove-Item)",
            "iptables -F": "changes the firewall",
        }
        for command, expected in cases.items():
            self.assertIn(expected, dangers(command), command)

    def test_nothing_is_flagged_in_an_innocent_command(self):
        for command in ("ls -lah", "systemctl status nginx", "df -h",
                        "tail -f /var/log/syslog", "", None):
            self.assertEqual(dangers(command), [])

    def test_the_worst_thing_in_a_line_is_reported_first(self):
        found = dangers("rm -rf /tmp/x && systemctl stop nginx")
        self.assertEqual(found[0], "deletes recursively or without asking (rm)")
        self.assertIn("stops a service", found)

    def test_a_reason_is_only_given_once(self):
        found = dangers("rm -rf a; rm -rf b; rm -rf c")
        self.assertEqual(len(found), 1)


class TargetDescriptionTests(unittest.TestCase):
    def names(self, count):
        return [{"name": f"web{index:02d}"} for index in range(count)]

    def test_it_reads_like_a_sentence(self):
        self.assertEqual(describe_targets(self.names(1)), "web00")
        self.assertEqual(describe_targets(self.names(2)), "web00 and web01")
        self.assertEqual(describe_targets(self.names(3)),
                         "web00, web01 and web02")
        self.assertEqual(describe_targets(self.names(9)),
                         "web00, web01, web02 and 6 others")
        self.assertEqual(describe_targets([]), "nothing")

    def test_a_connection_with_no_name_falls_back_to_its_host(self):
        self.assertEqual(describe_targets([{"host": "10.0.0.5"}]), "10.0.0.5")


class ConfirmationTests(unittest.TestCase):
    LAB = {"name": "lab-01", "tenant": "Lab"}
    PROD = {"name": "acme-prod-db", "tenant": "Acme"}

    def test_a_harmless_command_on_one_lab_box_asks_nothing(self):
        self.assertIsNone(confirmation_for("ls -lah", [self.LAB]))

    def test_production_alone_is_worth_asking_about(self):
        confirmation = confirmation_for("ls -lah", [self.PROD])
        self.assertIsNotNone(confirmation)
        self.assertEqual(confirmation.severity, "production")
        self.assertIn("production", confirmation.body)

    def test_a_destructive_command_is_worth_asking_about_anywhere(self):
        confirmation = confirmation_for("rm -rf /var/log", [self.LAB])
        self.assertEqual(confirmation.severity, "dangerous")
        self.assertIn("deletes recursively", confirmation.body)

    def test_both_at_once_says_so(self):
        confirmation = confirmation_for("rm -rf /", [self.PROD])
        self.assertEqual(confirmation.severity, "both")

    def test_running_on_many_machines_is_itself_the_risk(self):
        confirmation = confirmation_for("ls", [self.LAB, dict(self.LAB)])
        self.assertEqual(confirmation.severity, "many")
        self.assertIn("2 machines", confirmation.body)

    def test_the_command_and_the_targets_are_both_in_the_question(self):
        confirmation = confirmation_for("uptime", [self.PROD])
        self.assertIn("uptime", confirmation.body)
        self.assertIn("acme-prod-db", confirmation.body)
        self.assertEqual(confirmation.title, "Run on production?")

    def test_the_threshold_for_many_can_be_raised(self):
        many = [self.LAB, dict(self.LAB), dict(self.LAB)]
        self.assertIsNone(confirmation_for("ls", many, always_confirm_many=8))


if __name__ == "__main__":
    unittest.main()
