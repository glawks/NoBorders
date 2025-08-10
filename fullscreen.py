# Full corrected fullscreen.py — drop-in replacement
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import psutil
import win32gui
import win32process
import win32con
import win32api
import pystray
from PIL import Image, ImageDraw
import ctypes
import sys
import os
import time

# --- Admin check and relaunch if not admin ---
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def run_as_admin():
    params = " ".join(f'"{x}"' for x in sys.argv)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1)
    sys.exit()

if not is_admin():
    run_as_admin()

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_CAPTION = WS_BORDER | WS_DLGFRAME
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000

# Globals for tracking
window_style_cache = {}  # hwnd -> (style, exstyle, rect)
last_fullscreen_by_pid = {}  # pid -> (process_name, monitor_rect_or_None)
lock = threading.Lock()  # protect shared globals between threads

class WindowInfo:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.pid = None
        self.process_name = None
        self.title = None
        self.get_process_info()

    def get_process_info(self):
        try:
            self.title = win32gui.GetWindowText(self.hwnd)
        except Exception:
            self.title = ""
        try:
            _, self.pid = win32process.GetWindowThreadProcessId(self.hwnd)
            try:
                p = psutil.Process(self.pid)
                self.process_name = p.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.process_name = "Unknown"
        except Exception:
            self.pid = None
            self.process_name = "Unknown"

    def __str__(self):
        title = self.title if self.title else "<No Title>"
        return f"{self.process_name} - {title}"

def enum_windows():
    """
    Safely enumerate visible, titled windows. Exceptions within callback
    are caught so one bad window won't abort enumeration.
    """
    windows = []

    def callback(hwnd, extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title or title.isspace():
                return True

            wi = WindowInfo(hwnd)
            # Exclude windows without process info
            if wi.pid is None:
                return True
            if wi.process_name is None or wi.process_name == "":
                wi.process_name = "Unknown"
            windows.append(wi)
        except Exception:
            # Ignore problematic windows
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        # Defensive: shouldn't happen often
        pass
    return windows

def create_image():
    width = 64
    height = 64
    image = Image.new("RGB", (width, height), "blue")
    dc = ImageDraw.Draw(image)
    dc.rectangle((8, 8, width - 8, height - 8), fill="white")
    return image

def set_borderless_fullscreen(hwnd, monitor_rect=None):
    """
    Remove borders and resize to monitor_rect (or primary monitor if None).
    Also caches the original window style/rect and stores pid mapping for auto-reapply.
    Thread-safe wrt shared globals.
    """
    global window_style_cache, last_fullscreen_by_pid
    with lock:
        # cache original style if not cached (use hwnd as key)
        if hwnd not in window_style_cache:
            try:
                style = win32gui.GetWindowLong(hwnd, GWL_STYLE)
                exstyle = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
                rect = win32gui.GetWindowRect(hwnd)
                window_style_cache[hwnd] = (style, exstyle, rect)
            except Exception:
                # if we fail to read style, abort
                raise

        # store PID mapping for auto-reapply
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = None
        if pid:
            try:
                pname = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pname = "Unknown"
            # store the monitor rectangle we used so we can reapply consistently
            last_fullscreen_by_pid[pid] = (pname, monitor_rect)

        # Remove window borders and caption styles
        try:
            new_style = win32gui.GetWindowLong(hwnd, GWL_STYLE)
            new_exstyle = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
        except Exception:
            raise

        new_style &= ~(WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
        new_exstyle &= ~(WS_EX_DLGMODALFRAME | WS_EX_CLIENTEDGE | WS_EX_STATICEDGE)

        win32gui.SetWindowLong(hwnd, GWL_STYLE, new_style)
        win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, new_exstyle)

        if monitor_rect:
            left, top, right, bottom = monitor_rect
            width = right - left
            height = bottom - top
        else:
            width = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            height = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            left = 0
            top = 0

        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOP,
            left,
            top,
            width,
            height,
            win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW,
        )

def revert_to_windowed(hwnd):
    """
    Restore cached style and geometry for hwnd. Also remove tracking entries.
    Thread-safe.
    """
    global window_style_cache, last_fullscreen_by_pid
    with lock:
        if hwnd not in window_style_cache:
            # Nothing cached for this hwnd
            return
        style, exstyle, rect = window_style_cache.pop(hwnd, (None, None, None))

        try:
            if style is not None:
                win32gui.SetWindowLong(hwnd, GWL_STYLE, style)
            if exstyle is not None:
                win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, exstyle)
        except Exception as e:
            # if the window is gone or invalid, just remove cache and return
            print(f"Failed to set styles back for hwnd {hwnd}: {e}")

        if rect:
            left, top, right, bottom = rect
            width = right - left
            height = bottom - top
            try:
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOP,
                    left,
                    top,
                    width,
                    height,
                    win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW,
                )
            except Exception:
                pass

        # Remove any last_fullscreen_by_pid entry for this process (best effort)
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid and pid in last_fullscreen_by_pid:
                del last_fullscreen_by_pid[pid]
        except Exception:
            pass

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NoBorders")
        if is_admin():
            self.title(self.title() + " [Admin]")
        self.geometry("900x520")
        self.minsize(600, 400)

        # Set window icon (iconbitmap accepts .ico files on Windows)
        ico_path = os.path.join(os.path.dirname(sys.argv[0]), "NoBorders.ico")
        try:
            if os.path.isfile(ico_path):
                self.iconbitmap(ico_path)
            else:
                print(f"Icon file not found: {ico_path}")
        except Exception as e:
            print(f"Failed to set window icon: {e}")

        # Load tray icon image using PIL without resizing to keep quality
        try:
            if os.path.isfile(ico_path):
                self.tray_icon_image = Image.open(ico_path)
            else:
                print(f"Icon file not found for tray icon: {ico_path}")
                self.tray_icon_image = create_image()
        except Exception as e:
            print(f"Failed to load tray icon image: {e}")
            self.tray_icon_image = create_image()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.windowed_windows = []
        self.fullscreen_windows = []
        self.fullscreen_hwnds = set()

        self.selected_hwnd = None
        self.selected_list = None

        self.close_to_tray_enabled = tk.BooleanVar(value=False)
        self.tray_icon = None
        self.tray_thread = None

        self.is_minimized = False

        self.create_widgets()
        self.create_menu()

        self.bind("<Unmap>", self.on_minimize)
        self.bind("<Map>", self.on_restore)

        self.listbox_windowed.bind("<Button-3>", self.on_right_click_windowed)
        self.listbox_fullscreen.bind("<Button-3>", self.on_right_click_fullscreen)

        self.loop = asyncio.new_event_loop()
        self.async_thread = threading.Thread(target=self.start_async_loop, daemon=True)
        self.async_thread.start()

        self.after(1000, self.update_windows_periodically)

    def create_widgets(self):
        mainframe = ttk.Frame(self)
        mainframe.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_frame = ttk.Frame(mainframe)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Label(left_frame, text="Windowed Programs", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.listbox_windowed = tk.Listbox(left_frame, font=("Segoe UI", 10))
        self.listbox_windowed.pack(fill=tk.BOTH, expand=True)
        self.listbox_windowed.bind("<<ListboxSelect>>", self.on_windowed_select)
        self.listbox_windowed.config(exportselection=False)

        right_frame = ttk.Frame(mainframe)
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

        ttk.Label(right_frame, text="Fullscreen Borderless Programs", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.listbox_fullscreen = tk.Listbox(right_frame, font=("Segoe UI", 10))
        self.listbox_fullscreen.pack(fill=tk.BOTH, expand=True)
        self.listbox_fullscreen.bind("<<ListboxSelect>>", self.on_fullscreen_select)
        self.listbox_fullscreen.config(exportselection=False)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=10)

        self.fullscreen_btn = ttk.Button(btn_frame, text="Make Fullscreen Borderless", command=self.make_fullscreen, state=tk.DISABLED)
        self.fullscreen_btn.grid(row=0, column=0, padx=5)

        self.revert_btn = ttk.Button(btn_frame, text="Revert to Windowed", command=self.revert_windowed, state=tk.DISABLED)
        self.revert_btn.grid(row=0, column=1, padx=5)

        self.refresh_btn = ttk.Button(btn_frame, text="Refresh Now", command=self.refresh_windows)
        self.refresh_btn.grid(row=0, column=2, padx=5)

        self.statuslabel = ttk.Label(self, text="Status: Ready")
        self.statuslabel.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

    def create_menu(self):
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        options_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Options", menu=options_menu)
        options_menu.add_checkbutton(label="Close to Tray", variable=self.close_to_tray_enabled, onvalue=True, offvalue=False)
        options_menu.add_separator()
        options_menu.add_command(label="Exit", command=self.exit_app)

    def on_windowed_select(self, event):
        self.listbox_fullscreen.selection_clear(0, tk.END)
        sel = self.listbox_windowed.curselection()
        if sel:
            idx = sel[0]
            if idx < 0 or idx >= len(self.windowed_windows):
                self.selected_hwnd = None
                self.selected_list = None
                self.fullscreen_btn.config(state=tk.DISABLED)
                self.revert_btn.config(state=tk.DISABLED)
                self.statuslabel.config(text="Status: Ready")
                return
            win = self.windowed_windows[idx]
            self.selected_hwnd = win.hwnd
            self.selected_list = "windowed"
            self.fullscreen_btn.config(state=tk.NORMAL)
            self.revert_btn.config(state=tk.DISABLED)
            self.statuslabel.config(text=f"Selected (Windowed): {str(win)}")
        else:
            self.selected_hwnd = None
            self.selected_list = None
            self.fullscreen_btn.config(state=tk.DISABLED)
            self.revert_btn.config(state=tk.DISABLED)
            self.statuslabel.config(text="Status: Ready")

    def on_fullscreen_select(self, event):
        self.listbox_windowed.selection_clear(0, tk.END)
        sel = self.listbox_fullscreen.curselection()
        if sel:
            idx = sel[0]
            if idx < 0 or idx >= len(self.fullscreen_windows):
                self.selected_hwnd = None
                self.selected_list = None
                self.fullscreen_btn.config(state=tk.DISABLED)
                self.revert_btn.config(state=tk.DISABLED)
                self.statuslabel.config(text="Status: Ready")
                return
            win = self.fullscreen_windows[idx]
            self.selected_hwnd = win.hwnd
            self.selected_list = "fullscreen"
            self.fullscreen_btn.config(state=tk.DISABLED)
            self.revert_btn.config(state=tk.NORMAL)
            self.statuslabel.config(text=f"Selected (Fullscreen): {str(win)}")
        else:
            self.selected_hwnd = None
            self.selected_list = None
            self.fullscreen_btn.config(state=tk.DISABLED)
            self.revert_btn.config(state=tk.DISABLED)
            self.statuslabel.config(text="Status: Ready")

    def make_fullscreen(self):
        if self.selected_hwnd and self.selected_list == "windowed":
            try:
                set_borderless_fullscreen(self.selected_hwnd)
                self.move_window_to_fullscreen(self.selected_hwnd)
                self.statuslabel.config(text="Made window borderless fullscreen.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to set fullscreen: {e}")

    def revert_windowed(self):
        if self.selected_hwnd and self.selected_list == "fullscreen":
            try:
                revert_to_windowed(self.selected_hwnd)
                self.move_window_to_windowed(self.selected_hwnd)
                self.statuslabel.config(text="Reverted window to windowed mode.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to revert window: {e}")

    def move_window_to_fullscreen(self, hwnd):
        win_to_move = None
        for i, win in enumerate(self.windowed_windows):
            if win.hwnd == hwnd:
                win_to_move = self.windowed_windows.pop(i)
                self.fullscreen_windows.append(win_to_move)
                self.fullscreen_hwnds.add(hwnd)
                break
        if win_to_move is None:
            return
        self.refresh_listboxes()
        idx = self.fullscreen_windows.index(win_to_move)
        self.listbox_fullscreen.selection_clear(0, tk.END)
        self.listbox_fullscreen.selection_set(idx)
        self.listbox_fullscreen.see(idx)
        self.on_fullscreen_select(None)

    def move_window_to_windowed(self, hwnd):
        win_to_move = None
        for i, win in enumerate(self.fullscreen_windows):
            if win.hwnd == hwnd:
                win_to_move = self.fullscreen_windows.pop(i)
                self.windowed_windows.append(win_to_move)
                if hwnd in self.fullscreen_hwnds:
                    self.fullscreen_hwnds.remove(hwnd)
                break
        if win_to_move is None:
            return
        self.refresh_listboxes()
        idx = self.windowed_windows.index(win_to_move)
        self.listbox_windowed.selection_clear(0, tk.END)
        self.listbox_windowed.selection_set(idx)
        self.listbox_windowed.see(idx)
        self.on_windowed_select(None)

    def refresh_listboxes(self):
        self.listbox_windowed.delete(0, tk.END)
        for w in self.windowed_windows:
            self.listbox_windowed.insert(tk.END, str(w))

        self.listbox_fullscreen.delete(0, tk.END)
        for w in self.fullscreen_windows:
            self.listbox_fullscreen.insert(tk.END, str(w))

    def refresh_windows(self):
        asyncio.run_coroutine_threadsafe(self.async_enum_windows(), self.loop)

    async def async_enum_windows(self):
        windows = enum_windows()
        self.after(0, self.update_windows_lists, windows)

    def update_windows_lists(self, windows):
        """
        Update internal lists. Also:
         - auto-reapply borderless fullscreen to newly created HWNDs that belong
           to a PID we previously set fullscreen for (matching process name),
         - clean up stale hwnds from fullscreen tracking and cache.
        """
        global last_fullscreen_by_pid, window_style_cache

        prev_selected_hwnd = self.selected_hwnd
        prev_selected_list = self.selected_list

        new_windowed = []
        new_fullscreen = []
        new_hwnds = set()

        # Build a map pid -> list of WindowInfo for quick lookup
        pid_to_windows = {}
        for w in windows:
            pid_to_windows.setdefault(w.pid, []).append(w)

        # Attempt auto-reapply for pids in last_fullscreen_by_pid
        with lock:
            pids_to_check = list(last_fullscreen_by_pid.keys())
        for pid in pids_to_check:
            try:
                if pid in pid_to_windows:
                    for w in pid_to_windows[pid]:
                        with lock:
                            expected_name, monitor_rect = last_fullscreen_by_pid.get(pid, (None, None))
                        if expected_name and expected_name == w.process_name:
                            # if this HWND isn't already tracked as fullscreen, apply
                            if w.hwnd not in self.fullscreen_hwnds and w.hwnd not in window_style_cache:
                                try:
                                    set_borderless_fullscreen(w.hwnd, monitor_rect)
                                    with lock:
                                        self.fullscreen_hwnds.add(w.hwnd)
                                except Exception:
                                    pass
            except Exception:
                # ignore issues per-pid
                pass

        # Now classify windows based on currently tracked fullscreen_hwnds
        for w in windows:
            with lock:
                is_tracked_fullscreen = (w.hwnd in self.fullscreen_hwnds and w.hwnd in window_style_cache)
            if is_tracked_fullscreen:
                new_fullscreen.append(w)
                new_hwnds.add(w.hwnd)
            else:
                new_windowed.append(w)

        # Cleanup: remove closed windows from fullscreen tracking and cache
        with lock:
            closed_hwnds = set(self.fullscreen_hwnds) - new_hwnds
            if closed_hwnds:
                for hwnd in closed_hwnds:
                    if hwnd in window_style_cache:
                        try:
                            del window_style_cache[hwnd]
                        except KeyError:
                            pass
                self.fullscreen_hwnds.difference_update(closed_hwnds)
            # Additionally, remove last_fullscreen_by_pid for processes that no longer exist
            pids_to_remove = []
            for pid in list(last_fullscreen_by_pid.keys()):
                if not psutil.pid_exists(pid):
                    pids_to_remove.append(pid)
            for pid in pids_to_remove:
                try:
                    del last_fullscreen_by_pid[pid]
                except KeyError:
                    pass

        self.windowed_windows = new_windowed
        self.fullscreen_windows = new_fullscreen

        self.refresh_listboxes()

        # Restore previous selection if still valid
        if prev_selected_hwnd is not None and prev_selected_list is not None:
            found = False
            if prev_selected_list == "windowed":
                for idx, win in enumerate(self.windowed_windows):
                    if win.hwnd == prev_selected_hwnd:
                        self.listbox_windowed.selection_set(idx)
                        self.listbox_windowed.see(idx)
                        self.selected_hwnd = prev_selected_hwnd
                        self.selected_list = "windowed"
                        self.fullscreen_btn.config(state=tk.NORMAL)
                        self.revert_btn.config(state=tk.DISABLED)
                        self.statuslabel.config(text=f"Selected (Windowed): {str(win)}")
                        found = True
                        break
            elif prev_selected_list == "fullscreen":
                for idx, win in enumerate(self.fullscreen_windows):
                    if win.hwnd == prev_selected_hwnd:
                        self.listbox_fullscreen.selection_set(idx)
                        self.listbox_fullscreen.see(idx)
                        self.selected_hwnd = prev_selected_hwnd
                        self.selected_list = "fullscreen"
                        self.fullscreen_btn.config(state=tk.DISABLED)
                        self.revert_btn.config(state=tk.NORMAL)
                        self.statuslabel.config(text=f"Selected (Fullscreen): {str(win)}")
                        found = True
                        break
            if not found:
                # If previous HWND is gone, reset selection
                self.selected_hwnd = None
                self.selected_list = None
                self.fullscreen_btn.config(state=tk.DISABLED)
                self.revert_btn.config(state=tk.DISABLED)
                self.statuslabel.config(
                    text=f"Status: {len(windows)} windows found — {len(new_windowed)} windowed, {len(new_fullscreen)} fullscreen borderless"
                )
        else:
            self.selected_hwnd = None
            self.selected_list = None
            self.fullscreen_btn.config(state=tk.DISABLED)
            self.revert_btn.config(state=tk.DISABLED)
            self.statuslabel.config(
                text=f"Status: {len(windows)} windows found — {len(new_windowed)} windowed, {len(new_fullscreen)} fullscreen borderless"
            )

    def update_windows_periodically(self):
        if not self.is_minimized:
            self.refresh_windows()
        delay = 3000 if not self.is_minimized else 10000
        self.after(delay, self.update_windows_periodically)

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def show_window(self, icon=None, item=None):
        if icon:
            icon.visible = False
        self.deiconify()
        self.after(0, self.lift)
        self.after(0, self.focus_force)
        self.tray_icon = None

    def quit_app(self, icon=None, item=None):
        if icon:
            icon.visible = False
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.destroy()

    def on_close(self):
        if self.close_to_tray_enabled.get():
            self.withdraw()
            if self.tray_icon is None:
                menu = pystray.Menu(
                    pystray.MenuItem("Restore", self.show_window),
                    pystray.MenuItem("Exit", self.quit_app),
                )
                try:
                    self.tray_icon = pystray.Icon(
                        "NoBordersTrayIcon", self.tray_icon_image, "NoBorders", menu
                    )
                    self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
                    self.tray_thread.start()
                except Exception:
                    pass
            self.statuslabel.config(text="Status: Minimized to tray")
        else:
            self.exit_app()

    def exit_app(self):
        if self.tray_icon:
            try:
                self.tray_icon.visible = False
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.destroy()

    def on_minimize(self, event):
        if self.state() == "iconic":
            self.is_minimized = True
            self.statuslabel.config(text="Status: Minimized - paused updates")

    def on_restore(self, event):
        if self.is_minimized:
            self.is_minimized = False
            self.statuslabel.config(text="Status: Restored - resuming updates")
            self.after(100, self.update_windows_periodically)

    def on_right_click_windowed(self, event):
        index = self.listbox_windowed.nearest(event.y)
        if index < 0 or index >= len(self.windowed_windows):
            return
        self.listbox_windowed.selection_clear(0, tk.END)
        self.listbox_windowed.selection_set(index)
        self.on_windowed_select(None)

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Make Borderless", command=self.context_make_borderless)

        submenu = tk.Menu(menu, tearoff=0)
        monitors = self.get_monitors()
        for device_name, rect in monitors:
            friendly_name = device_name.replace("\\\\.\\", "")
            submenu.add_command(label=friendly_name, command=lambda r=rect: self.context_make_borderless_on_monitor(r))
        menu.add_cascade(label="Make borderless on:", menu=submenu)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def context_make_borderless(self):
        if self.selected_hwnd and self.selected_list == "windowed":
            try:
                set_borderless_fullscreen(self.selected_hwnd)
                self.move_window_to_fullscreen(self.selected_hwnd)
                self.statuslabel.config(text="Made window borderless fullscreen (default monitor).")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to set fullscreen: {e}")

    def context_make_borderless_on_monitor(self, monitor_rect):
        if self.selected_hwnd and self.selected_list == "windowed":
            try:
                set_borderless_fullscreen(self.selected_hwnd, monitor_rect=monitor_rect)
                self.move_window_to_fullscreen(self.selected_hwnd)
                self.statuslabel.config(text="Made window borderless fullscreen on specified monitor.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to set fullscreen on monitor: {e}")

    def on_right_click_fullscreen(self, event):
        index = self.listbox_fullscreen.nearest(event.y)
        if index < 0 or index >= len(self.fullscreen_windows):
            return
        self.listbox_fullscreen.selection_clear(0, tk.END)
        self.listbox_fullscreen.selection_set(index)
        self.on_fullscreen_select(None)

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Revert to Normal", command=self.context_revert_to_normal)

        submenu = tk.Menu(menu, tearoff=0)
        monitors = self.get_monitors()
        for device_name, rect in monitors:
            friendly_name = device_name.replace("\\\\.\\", "")
            submenu.add_command(label=friendly_name, command=lambda r=rect: self.context_change_display(r))
        menu.add_cascade(label="Change Display Used:", menu=submenu)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def context_revert_to_normal(self):
        if self.selected_hwnd and self.selected_list == "fullscreen":
            try:
                revert_to_windowed(self.selected_hwnd)
                self.move_window_to_windowed(self.selected_hwnd)
                self.statuslabel.config(text="Reverted window to windowed mode.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to revert window: {e}")

    def context_change_display(self, monitor_rect):
        if self.selected_hwnd and self.selected_list == "fullscreen":
            try:
                left, top, right, bottom = monitor_rect
                width = right - left
                height = bottom - top
                hwnd = self.selected_hwnd
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOP,
                    left,
                    top,
                    width,
                    height,
                    win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW,
                )
                self.statuslabel.config(text=f"Moved window to display {monitor_rect}.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to change display: {e}")

    def get_monitors(self):
        monitors = []
        try:
            monitor_info = win32api.EnumDisplayMonitors()
            for hMonitor, hdcMonitor, rect in monitor_info:
                info = win32api.GetMonitorInfo(hMonitor)
                device = info.get("Device", "Unknown")
                rc = info.get("Monitor", (0, 0, 0, 0))
                monitors.append((device, rc))
        except Exception:
            # Fallback to primary screen size if enumeration fails
            primary = ("\\\\.\\DISPLAY1", (0, 0, win32api.GetSystemMetrics(win32con.SM_CXSCREEN), win32api.GetSystemMetrics(win32con.SM_CYSCREEN)))
            monitors.append(primary)
        return monitors

if __name__ == "__main__":
    app = App()
    app.mainloop()
