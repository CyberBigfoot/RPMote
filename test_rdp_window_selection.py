import unittest

from app import rdp_launcher


class RDPWindowSelectionTests(unittest.TestCase):
    def test_choose_best_mstsc_main_window_ignores_credential_dialogs(self):
        candidates = [
            {
                "hwnd": 100,
                "title": "Windows Security",
                "class": "#32770",
                "visible": True,
                "area": 240_000,
            },
            {
                "hwnd": 200,
                "title": "Remote Desktop Connection",
                "class": "TscShellContainerClass",
                "visible": True,
                "area": 50_000,
            },
        ]

        self.assertEqual(rdp_launcher._choose_best_mstsc_main_window(candidates), 200)

    def test_choose_best_mstsc_main_window_returns_none_when_only_auth_dialog_visible(self):
        candidates = [
            {
                "hwnd": 100,
                "title": "Your credentials did not work",
                "class": "#32770",
                "visible": True,
                "area": 320_000,
            },
            {
                "hwnd": 101,
                "title": "Remote Desktop Connection",
                "class": "#32770",
                "visible": True,
                "area": 180_000,
            },
        ]

        self.assertIsNone(rdp_launcher._choose_best_mstsc_main_window(candidates))

    def test_a_hidden_desktop_container_is_never_adopted(self):
        # mstsc creates the container before it connects and shows it only
        # once the session is up. While a gateway session waits on a
        # credential prompt (drawn by another process) this is all mstsc
        # itself has — and adopting it put a blank panel in the tab.
        candidates = [
            {"hwnd": 300, "title": "Remote Desktop Connection",
             "class": "TscShellContainerClass", "visible": False, "area": 500_000},
            {"hwnd": 301, "title": "Remote Desktop Connection",
             "class": "TSC_POPUP_PARENT_WNDCLASS", "visible": True, "area": 0},
        ]
        self.assertIsNone(rdp_launcher._choose_best_mstsc_main_window(candidates))
        candidates[0]["visible"] = True
        self.assertEqual(rdp_launcher._choose_best_mstsc_main_window(candidates), 300)

    def test_a_window_owned_from_another_process_is_never_adopted(self):
        candidates = [
            {"hwnd": 400, "title": "Windows Security", "class": "Credential Dialog Xaml Host",
             "visible": True, "area": 3_000_000, "owned": True},
        ]
        self.assertIsNone(rdp_launcher._choose_best_mstsc_main_window(candidates))

    def test_rdp_auth_dialog_detection_includes_common_titles(self):
        self.assertTrue(rdp_launcher._is_probable_rdp_auth_dialog("Windows Security", "#32770"))
        self.assertTrue(rdp_launcher._is_probable_rdp_auth_dialog("Your credentials did not work", "#32770"))
        self.assertTrue(rdp_launcher._is_probable_rdp_auth_dialog("Remote Desktop Connection", "#32770"))
        self.assertFalse(
            rdp_launcher._is_probable_rdp_auth_dialog(
                "Remote Desktop Connection", "TscShellContainerClass"
            )
        )


if __name__ == "__main__":
    unittest.main()
