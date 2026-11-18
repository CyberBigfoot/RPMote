import ctypes
import time
import psutil

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
            pid = proc_id.value
            pname = "unknown"
            try:
                pname = psutil.Process(pid).name()
            except:
                pass
            print(f"FOUND WINDOW: Title='{title_buf.value}', Class='{cls_buf.value}', PID={pid}, EXE={pname}")
            
            # Print children
            def _enum_child(child, _lp):
                user32.GetWindowTextW(child, title_buf, 512)
                user32.GetClassNameW(child, cls_buf, 512)
                print(f"  CHILD: Title='{title_buf.value}', Class='{cls_buf.value}'")
                return True
            user32.EnumChildWindows(hwnd, WNDENUMPROC(_enum_child), 0)

        return True

    print("Scanning windows...")
    user32.EnumWindows(WNDENUMPROC(_enum_top), 0)

if __name__ == "__main__":
    check_windows()
