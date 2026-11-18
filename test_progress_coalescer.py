import unittest

from app.progress_coalescer import TerminalProgressCoalescer


class TerminalProgressCoalescerTests(unittest.TestCase):
    def test_compacts_buildkit_rows(self):
        coalescer = TerminalProgressCoalescer()
        output = coalescer.feed(
            "#1 [internal] load build definition from Dockerfile\n"
            "#1 DONE 0.1s\n"
            "#2 [1/3] FROM python:3.12\n"
        )

        self.assertIn("Docker", output)
        self.assertIn("steps", output)
        self.assertIn("\x1b[2K", output)
        self.assertNotIn("#1 [internal]", output)

    def test_preserves_regular_output(self):
        coalescer = TerminalProgressCoalescer()
        output = coalescer.feed("hello\nworld\n")

        self.assertEqual(output, "hello\nworld\n")

    def test_finishes_before_prompt(self):
        coalescer = TerminalProgressCoalescer()
        output = coalescer.feed("#1 [internal] load metadata\nuser@host:~$ ")

        self.assertIn("Docker", output)
        self.assertIn("\nuser@host:~$ ", output)

    def test_finishes_before_regular_summary_line(self):
        coalescer = TerminalProgressCoalescer()
        output = coalescer.feed("Get:1 http://example.test repo InRelease\nFetched 12 MB in 2s\n")

        self.assertIn("Update", output)
        self.assertIn("\nFetched 12 MB in 2s\n", output)

    def test_preserves_docker_compose_layout_while_removing_sgr_color(self):
        coalescer = TerminalProgressCoalescer()
        output = coalescer.feed(
            "\x1b[42m[+] Running 1/2\x1b[0m\n"
            "\x1b[1A\x1b[2K\x1b[42m Container jellyfin Stopping 0.1s\x1b[0m\n"
        )

        self.assertIn("[+] Running 1/2\n", output)
        self.assertIn("\x1b[1A\x1b[2K", output)
        self.assertIn("Container jellyfin Stopping 0.1s\n", output)
        self.assertNotIn("\x1b[42m", output)

    def test_docker_compose_does_not_join_following_prompt(self):
        coalescer = TerminalProgressCoalescer()
        output = coalescer.feed(
            "\x1b[42m[+] down 9/9\x1b[0m\n"
            "root@grant:~# "
        )

        self.assertIn("[+] down 9/9\nroot@grant:~# ", output)
        self.assertNotIn("Docker Compose:", output)

    def test_docker_compose_preserves_colored_following_prompt(self):
        coalescer = TerminalProgressCoalescer()
        output = coalescer.feed(
            "\x1b[42m[+] down 9/9\x1b[0m\n"
            "\x1b[1;38;5;141mroot@grant \x1b[38;5;15m| "
            "\x1b[1;38;5;226m/home/grant/AAR-Stack "
            "\x1b[1;38;5;141m17:30:53\x1b[0m\n"
            "\x1b[1;38;5;82m❯ \x1b[0m"
        )

        self.assertIn("[+] down 9/9\n", output)
        self.assertNotIn("\x1b[42m", output)
        self.assertIn("\x1b[1;38;5;141mroot@grant", output)
        self.assertIn("\x1b[1;38;5;82m❯", output)

    def test_tty_compose_stream_passes_through_split_chunks(self):
        coalescer = TerminalProgressCoalescer()
        # First chunk carries cursor movement -> recognized as a TTY compose
        # redraw, which we pass through for the screen buffer to emulate.
        coalescer.feed("\x1b[1A\x1b[2K\x1b[42m[+] Running 1/2\x1b[0m\n")

        # A split chunk that would otherwise be coalesced is passed through
        # verbatim while the TTY compose stream is active.
        output2 = coalescer.feed("Starting 0.5s\r\n")
        self.assertEqual(output2, "Starting 0.5s\r\n")

        # A prompt turns compose mode off.
        output3 = coalescer.feed("root@grant:~# ")
        self.assertEqual(output3, "root@grant:~# ")

        # A progress line after the prompt coalesces normally.
        output4 = coalescer.feed("Downloading 15%\r\n")
        self.assertIn("Progress [", output4)

    def test_plain_compose_frames_collapse_to_one_line(self):
        # Docker Compose "plain" progress (no cursor control) prints one line
        # per refresh. The ticking timer must update a single line in place
        # rather than scrolling a fresh row every 100ms.
        coalescer = TerminalProgressCoalescer()
        out = ""
        for t in (8.0, 8.1, 8.2, 8.3):
            out += coalescer.feed(
                f"\x1b[42m[+] down 0/1 FlickVault Stopping {t:.1f}s\x1b[0m\r\n"
            )
        self.assertEqual(out.count("\x1b[2K"), 4)   # four in-place rewrites
        self.assertNotIn("\n", out)                 # never scrolls a new row
        self.assertNotIn("\x1b[42m", out)           # noisy green bg stripped

    def test_plain_compose_finalizes_and_keeps_result_lines(self):
        coalescer = TerminalProgressCoalescer()
        coalescer.feed("\x1b[42m[+] down 0/1 FlickVault Stopping 8.0s\x1b[0m\r\n")
        coalescer.feed("\x1b[42m[+] down 0/1 FlickVault Stopping 8.1s\x1b[0m\r\n")
        out = coalescer.feed("\x1b[32m ✔ Container FlickVault Removed 10.3s\x1b[0m\r\n")
        # The completed result line survives as permanent output, colour stripped.
        self.assertIn("Container FlickVault Removed", out)
        self.assertNotIn("\x1b[42m", out)
        self.assertNotIn("\x1b[32m", out)

    def test_completed_plain_compose_line_passes_through(self):
        # A one-off completed header ("[+] down 9/9") is not a rolling frame,
        # so it stays as-is (background colour stripped) and the following
        # prompt is not joined to it.
        coalescer = TerminalProgressCoalescer()
        out = coalescer.feed("\x1b[42m[+] down 9/9\x1b[0m\nroot@grant:~# ")
        self.assertIn("[+] down 9/9\nroot@grant:~# ", out)
        self.assertNotIn("\x1b[42m", out)

    def test_detects_compose_stop_and_remove_streams(self):
        coalescer = TerminalProgressCoalescer()
        self.assertTrue(coalescer._looks_like_docker_compose_stream("[+] Stopping 2/2\n"))
        self.assertTrue(coalescer._looks_like_docker_compose_stream("[+] Removing 3/3\n"))
        self.assertTrue(coalescer._looks_like_docker_compose_stream(" Container web Stopped 0.3s\n"))

    def test_crlf_progress_steps_collapse_to_single_line(self):
        coalescer = TerminalProgressCoalescer()
        out = coalescer.feed(
            "Get:1 http://a.test jammy InRelease\r\n"
            "Get:2 http://b.test jammy-updates InRelease\r\n"
            "Get:3 http://c.test jammy-security InRelease\r\n"
        )
        # All three steps update the same line in place: three erase-line
        # rewrites and no embedded newline scrolling a fresh row per step.
        self.assertEqual(out.count("\x1b[2K"), 3)
        self.assertNotIn("\n", out)

    def test_crlf_progress_finalizes_before_regular_line(self):
        coalescer = TerminalProgressCoalescer()
        out = coalescer.feed(
            "Get:1 http://a.test jammy InRelease\r\n"
            "Fetched 12 MB in 2s (6 MB/s)\r\n"
        )
        self.assertIn("Update", out)
        self.assertIn("Fetched 12 MB in 2s", out)


if __name__ == "__main__":
    unittest.main()
