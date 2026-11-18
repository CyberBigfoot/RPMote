"""Snippets that fill themselves in, and a search that finds them."""

import unittest

from app.snippets import (
    PLACEHOLDER_RE, SnippetError, fuzzy_match, is_parameterized,
    missing_placeholders, placeholders_in, rank, render, session_values,
)

CONN = {"name": "acme-web01", "host": "10.0.0.5", "port": 22,
        "username": "admin", "tenant": "Acme Ltd"}


class PlaceholderTests(unittest.TestCase):
    def test_placeholders_are_found_in_order_without_repeats(self):
        self.assertEqual(
            placeholders_in("ssh {user}@{host} 'tail {file}' # {host}"),
            ["user", "host", "file"])

    def test_shell_and_powershell_braces_are_left_alone(self):
        # ${VAR}, ${1} and an awk program all have to survive untouched.
        for text in ("echo ${HOME}", "awk '{print $1}'", "echo ${1:-x}",
                     "Get-Item | % { $_.Name }"):
            self.assertEqual(placeholders_in(text), [], text)

    def test_a_placeholder_must_start_with_a_letter(self):
        self.assertEqual(PLACEHOLDER_RE.findall("{9lives} {ok_1}"), ["ok_1"])

    def test_a_snippet_with_nothing_to_fill_in_says_so(self):
        self.assertFalse(is_parameterized("df -h"))
        self.assertTrue(is_parameterized("df -h {mount}"))


class RenderingTests(unittest.TestCase):
    def test_the_session_fills_in_what_it_knows(self):
        values = session_values(CONN, cwd="/srv/app")
        self.assertEqual(
            render("ssh {user}@{host} # {tenant} in {cwd}", values),
            "ssh admin@10.0.0.5 # Acme Ltd in /srv/app")

    def test_a_missing_value_is_reported_rather_than_guessed(self):
        values = session_values(CONN)
        self.assertEqual(missing_placeholders("systemctl status {service}",
                                              values),
                         ["service"])
        self.assertEqual(missing_placeholders("uptime on {host}", values), [])

    def test_the_shell_survives_substitution(self):
        values = dict(session_values(CONN), pattern="x")
        self.assertEqual(
            render("awk '{print $1}' /var/log/{pattern}.log", values),
            "awk '{print $1}' /var/log/x.log")

    def test_a_hostname_cannot_smuggle_in_a_second_command(self):
        values = session_values({"host": "10.0.0.5; rm -rf /"})
        with self.assertRaises(SnippetError):
            render("ping -c1 {host}", values)

    def test_only_the_placeholders_used_are_vetted(self):
        # An apostrophe in a tenant name must not break a command that never
        # mentions the tenant.
        values = session_values({"host": "10.0.0.5", "tenant": "O'Brien Ltd"})
        self.assertEqual(render("ping -c1 {host}", values), "ping -c1 10.0.0.5")
        with self.assertRaises(SnippetError):
            render("echo {tenant}", values)

    def test_what_the_user_typed_is_taken_at_its_word(self):
        # They are writing a command line; second-guessing their quoting
        # would only teach them to work around the prompt.
        rendered = render("grep {pattern} /var/log/syslog",
                          {"pattern": "'error|warn'"}, trusted=["pattern"])
        self.assertEqual(rendered, "grep 'error|warn' /var/log/syslog")

    def test_an_unknown_placeholder_is_left_where_it_is(self):
        self.assertEqual(render("echo {nope}", {"host": "x"}), "echo {nope}")

    def test_a_shell_variable_of_the_same_name_keeps_its_dollar(self):
        self.assertEqual(render("echo ${host} vs {host}", {"host": "web01"}),
                         "echo ${host} vs web01")

    def test_an_empty_template_renders_to_nothing(self):
        self.assertEqual(render("", {}), "")
        self.assertEqual(render(None, None), "")


class FuzzyMatchTests(unittest.TestCase):
    def test_letters_have_to_be_there_in_order(self):
        self.assertIsNotNone(fuzzy_match("dfh", "df -h"))
        self.assertIsNone(fuzzy_match("xyz", "df -h"))
        self.assertIsNone(fuzzy_match("hd", "df -h"))

    def test_matching_is_case_insensitive(self):
        self.assertIsNotNone(fuzzy_match("NGINX", "restart nginx"))

    def test_a_word_start_beats_a_letter_in_the_middle(self):
        start = fuzzy_match("rn", "restart nginx")[0]
        middle = fuzzy_match("rn", "current running")[0]
        self.assertGreater(start, middle)

    def test_an_exact_prefix_wins(self):
        exact = fuzzy_match("disk", "disk usage")[0]
        scattered = fuzzy_match("disk", "docker images since kickoff")[0]
        self.assertGreater(exact, scattered)

    def test_the_positions_come_back_for_highlighting(self):
        _score, positions = fuzzy_match("dfh", "df -h")
        self.assertEqual(positions, [0, 1, 4])

    def test_an_empty_query_matches_everything(self):
        self.assertEqual(fuzzy_match("", "anything"), (0, []))


class RankingTests(unittest.TestCase):
    ITEMS = [
        {"name": "Disk usage", "command": "du -sh .", "description": "sizes"},
        {"name": "Restart nginx", "command": "systemctl restart nginx",
         "description": "bounce the web server"},
        {"name": "Tail syslog", "command": "tail -f /var/log/syslog",
         "description": "follow the system log"},
    ]

    def test_the_obvious_match_comes_first(self):
        results = rank("nginx", self.ITEMS)
        self.assertEqual(results[0][0]["name"], "Restart nginx")

    def test_a_command_can_be_found_by_what_it_runs(self):
        # Nothing in this snippet's name looks like "syslog"'s command.
        results = rank("tail -f", self.ITEMS)
        self.assertEqual(results[0][0]["name"], "Tail syslog")

    def test_things_that_do_not_match_are_left_out(self):
        self.assertEqual(rank("zzzz", self.ITEMS), [])

    def test_no_query_keeps_the_users_own_order(self):
        results = rank("", self.ITEMS)
        self.assertEqual([item["name"] for item, _score, _pos in results],
                         [item["name"] for item in self.ITEMS])

    def test_a_name_match_outranks_a_description_match(self):
        items = [{"name": "Something else", "description": "restart nginx"},
                 {"name": "Restart nginx", "description": "something else"}]
        results = rank("restart nginx", items)
        self.assertEqual(results[0][0]["name"], "Restart nginx")


if __name__ == "__main__":
    unittest.main()
