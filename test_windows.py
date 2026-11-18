import ctypes
import time

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

def check_windows():
    title_buf = ctypes.create_unicode_buffer(512)
    cls_buf = ctypes.create_unicode_buffer(512)
    proc_id = ctypes.c_ulong()
    
    def _enum_top(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        user32.GetWindowTextW(hwnd, title_buf, 512)
        title = title_buf.value.lower()
        if "remote desktop connection" in title or "security warning" in title:
            user32.GetClassNameW(hwnd, cls_buf, 512)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            print(f"FOUND WINDOW: Title='{title_buf.value}', Class='{cls_buf.value}', PID={proc_id.value}")
        return True

    print("Scanning windows...")
    user32.EnumWindows(WNDENUMPROC(_enum_top), 0)

if __name__ == "__main__":
    for _ in range(5):
        check_windows()
        time.sleep(1)

