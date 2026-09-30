"""
App dieu khien den qua cong COM, hien thi trang thai 5 vi tri san pham,
tu dong do va ket noi Arduino (khong can chon cong COM tay), tu dong phat video (co am thanh) tu thu muc clips/.

CHI CHAY TREN WINDOWS. Can cai dat truoc khi dung:
    1. Cai VLC Media Player (ban 64-bit) tu videolan.org
    2. python3 -m pip install pyserial python-vlc

Chay app:
    python3 arduino_control_app.py

Thu muc clips/ dat CUNG CHO voi file .py (hoac file .exe sau khi build), chua cac file:
    bg.mp4   -> clip nen (khong san pham nao bi lay)
    sp1.mp4  -> clip san pham 1
    sp2.mp4  -> clip san pham 2
    sp3.mp4  -> clip san pham 3
    sp4.mp4  -> clip san pham 4
    sp5.mp4  -> clip san pham 5
"""

import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports
import threading
import queue
import time
import sys
import os

try:
    import vlc
except ImportError:
    vlc = None

BAUD_RATE = 9600
NUM_PRODUCTS = 5
AUTO_CONNECT_INTERVAL_MS = 3000  # Cu 3 giay thu do lai Arduino neu chua ket noi

# Tu khoa nhan dien Arduino/mach USB-Serial pho bien, dung de tu dong tim dung cong
ARDUINO_KEYWORDS = [
    "arduino", "ch340", "wchusbserial", "usbserial", "usbmodem",
    "usb-serial", "usb serial", "2341", "1a86", "2a03"
]


def _base_dir():
    """Thu muc chua file .py hoac .exe (de tim thu muc clips/ ben canh no)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CLIP_DIR = os.path.join(_base_dir(), "clips")


def find_arduino_port():
    """Quet cac cong COM dang cam, tra ve cong co khop tu khoa Arduino/CH340... dau tien tim thay."""
    for p in serial.tools.list_ports.comports():
        text = f"{p.description} {p.hwid}".lower()
        for kw in ARDUINO_KEYWORDS:
            if kw in text:
                return p.device
    return None


class SerialApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Dieu khien Arduino qua COM")
        self.root.geometry("480x420")

        self.ser = None
        self.read_thread = None
        self.running = False
        self.msg_queue = queue.Queue()

        # Trang thai tung vi tri san pham: True = da lay ra, False = con hang
        self.product_removed_state = [False] * NUM_PRODUCTS
        self.removal_order = []

        # Duong dan clip (nap tu thu muc clips/)
        self.product_video_paths = [None] * NUM_PRODUCTS
        self.background_video_path = None
        self.current_playing = None
        self.last_switch_time = 0.0        # Thoi diem lan cuoi thuc su doi clip
        self.switch_cooldown = 0.4         # Khoang cach toi thieu (giay) giua 2 lan doi clip
        self._pending_switch_scheduled = False

        self.any_removed_active = False
        self.removal_start_time = None

        self.vlc_instance = None
        self.media_player = None
        self.list_player = None
        if vlc is not None:
            self.vlc_instance = vlc.Instance()
            self.media_player = self.vlc_instance.media_player_new()
            self.list_player = self.vlc_instance.media_list_player_new()
            self.list_player.set_media_player(self.media_player)

        self.control_visible = False

        self._build_ui()
        self._load_clips_from_folder()
        self._poll_queue()
        self._update_timer()
        self._embed_video_player()
        self._auto_connect_tick()  # Bat dau vong tu dong do & ket noi Arduino

        self.root.attributes("-fullscreen", True)
        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        self.root.bind("<F1>", lambda e: self._toggle_control_panel())

    def _build_ui(self):
        # --- Lop nen: video phu kin toan bo cua so ---
        self.video_frame = tk.Frame(self.root, bg="black")
        self.video_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        # --- Lop noi: khung dieu khien, an mac dinh, hien khi nhan F1 ---
        self.control_panel = tk.Frame(self.root, bg="#1e1e1e")

        # --- Khung trang thai ket noi (tu dong, khong can chon tay) ---
        frame_top = ttk.Frame(self.control_panel, padding=10)
        frame_top.pack(fill="x")

        self.status_label = ttk.Label(frame_top, text="Dang do Arduino...", foreground="orange")
        self.status_label.pack(side="left", padx=5)

        ttk.Button(frame_top, text="Do lai ngay", command=self._auto_connect_tick_manual).pack(
            side="left", padx=5)

        # --- Khung hien thi trang thai cam bien ---
        frame_status = ttk.LabelFrame(self.control_panel, text="Trang thai cam bien", padding=10)
        frame_status.pack(fill="x", padx=10, pady=5)

        self.product_labels = []
        for i in range(NUM_PRODUCTS):
            lbl = ttk.Label(frame_status, text=f"San pham {i + 1}: --", font=("Arial", 11))
            lbl.pack(anchor="w")
            self.product_labels.append(lbl)

        # --- Khung dong ho dem giay ---
        frame_timer = ttk.LabelFrame(self.control_panel, text="Thoi gian co san pham dang bi lay ra", padding=10)
        frame_timer.pack(fill="x", padx=10, pady=5)

        self.timer_label = ttk.Label(frame_timer, text="0.0 giay", font=("Arial", 28, "bold"))
        self.timer_label.pack(anchor="center", pady=5)

        # --- Khung trang thai clip (doc tu thu muc clips/) ---
        frame_clip = ttk.LabelFrame(self.control_panel, text=f"Clip video (thu muc: {CLIP_DIR})", padding=10)
        frame_clip.pack(fill="both", expand=True, padx=10, pady=5)

        ttk.Button(frame_clip, text="Nap lai clip tu thu muc", command=self._load_clips_from_folder).pack(
            fill="x", pady=(0, 5))

        self.clip_status_text = tk.Text(frame_clip, height=7, state="disabled", font=("Arial", 9))
        self.clip_status_text.pack(fill="both", expand=True)

        # --- Khung log du lieu tho ---
        frame_log = ttk.LabelFrame(self.control_panel, text="Du lieu nhan duoc", padding=10)
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = tk.Text(frame_log, height=6, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _toggle_control_panel(self):
        if self.control_visible:
            self.control_panel.place_forget()
        else:
            self.control_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.control_panel.lift()
        self.control_visible = not self.control_visible

    def _embed_video_player(self):
        if self.media_player is None:
            return
        self.root.update_idletasks()
        handle = self.video_frame.winfo_id()
        self.media_player.set_hwnd(handle)

    # ===== NAP CLIP TU THU MUC CO DINH =====
    def _load_clips_from_folder(self):
        os.makedirs(CLIP_DIR, exist_ok=True)  # Tu tao thu muc neu chua co, de nguoi dung biet cho bo file vao

        bg_path = os.path.join(CLIP_DIR, "bg.mp4")
        self.background_video_path = bg_path if os.path.exists(bg_path) else None

        for i in range(NUM_PRODUCTS):
            p = os.path.join(CLIP_DIR, f"sp{i + 1}.mp4")
            self.product_video_paths[i] = p if os.path.exists(p) else None

        # Neu dang khong co san pham nao bi lay va vua nap lai clip nen -> phat lai cho chac
        if not self.removal_order and self.background_video_path:
            self.current_playing = None  # Ep phat lai
            self._play_video("background")

        self._update_clip_status_label()

    def _update_clip_status_label(self):
        lines = [f"Nen (bg.mp4): {'OK' if self.background_video_path else 'THIEU FILE'}"]
        for i in range(NUM_PRODUCTS):
            ok = self.product_video_paths[i] is not None
            lines.append(f"SP{i + 1} (sp{i + 1}.mp4): {'OK' if ok else 'THIEU FILE'}")

        self.clip_status_text.config(state="normal")
        self.clip_status_text.delete("1.0", "end")
        self.clip_status_text.insert("1.0", "\n".join(lines))
        self.clip_status_text.config(state="disabled")

    # ===== TU DONG DO VA KET NOI ARDUINO =====
    def _auto_connect_tick(self):
        if not (self.ser and self.ser.is_open):
            port = find_arduino_port()
            if port:
                self._connect_to(port)
            else:
                self.status_label.config(text="Khong tim thay Arduino (dang do...)", foreground="orange")
        self.root.after(AUTO_CONNECT_INTERVAL_MS, self._auto_connect_tick)

    def _auto_connect_tick_manual(self):
        # Cho phep nguoi dung bam "Do lai ngay" thay vi cho 3 giay
        if self.ser and self.ser.is_open:
            self._disconnect()
        port = find_arduino_port()
        if port:
            self._connect_to(port)
        else:
            self.status_label.config(text="Khong tim thay Arduino", foreground="red")

    def _connect_to(self, port: str):
        try:
            self.ser = serial.Serial(port, BAUD_RATE, timeout=1)
            time.sleep(2)  # Cho Arduino reset sau khi mo cong serial
            self.running = True
            self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.read_thread.start()
            self.status_label.config(text=f"Da ket noi ({port})", foreground="green")
        except serial.SerialException:
            self.status_label.config(text=f"Loi mo cong {port}, se thu lai...", foreground="red")
            self.ser = None

    def _disconnect(self):
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.ser = None
        self.status_label.config(text="Dang do Arduino...", foreground="orange")

    def _read_loop(self):
        while self.running and self.ser and self.ser.is_open:
            try:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line:
                    self.msg_queue.put(line)
            except serial.SerialException:
                # Mat ket noi (rut day, tat may...) -> danh dau ngat, vong auto-connect se tu tim lai
                self.running = False
                self.root.after(0, self._disconnect)
                break

    def _poll_queue(self):
        while not self.msg_queue.empty():
            line = self.msg_queue.get()
            self._append_log(line)
            self._parse_line(line)
        self.root.after(100, self._poll_queue)

    def _append_log(self, line: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _parse_line(self, line: str):
        if "PROD:" not in line:
            return
        try:
            parts = dict(item.split(":") for item in line.split(","))
            prod = parts.get("PROD")

            if prod is not None and len(prod) == NUM_PRODUCTS:
                for i in range(NUM_PRODUCTS):
                    is_removed = prod[i] == '1'
                    old_state = self.product_removed_state[i]

                    self.product_labels[i].config(
                        text=f"San pham {i + 1}: {'DA LAY RA' if is_removed else 'Con hang'}",
                        foreground="red" if is_removed else "black")

                    if is_removed and not old_state:
                        if i not in self.removal_order:
                            self.removal_order.append(i)
                    elif not is_removed and old_state:
                        if i in self.removal_order:
                            self.removal_order.remove(i)

                    self.product_removed_state[i] = is_removed

                if self.removal_order:
                    target_idx = self.removal_order[-1]  # San pham vua nhac ra gan nhat, con dang ngoai
                    self._request_video_switch(("product", target_idx))
                else:
                    self._request_video_switch("background")

                any_removed = bool(self.removal_order)
                if any_removed and not self.any_removed_active:
                    self.any_removed_active = True
                    self.removal_start_time = time.time()
                elif not any_removed and self.any_removed_active:
                    self.any_removed_active = False
                    self.removal_start_time = None
                    self.timer_label.config(text="0.0 giay")
        except (ValueError, KeyError, IndexError):
            pass

    def _update_timer(self):
        if self.any_removed_active and self.removal_start_time is not None:
            elapsed = time.time() - self.removal_start_time
            self.timer_label.config(text=f"{elapsed:.1f} giay")
        self.root.after(100, self._update_timer)

    def _request_video_switch(self, which):
        """Goi thay cho _play_video truc tiep, de gioi han toc do doi clip toi da.
        Neu vua doi clip qua gan day, hoan lai va tu kiem tra lai sau, tranh spam
        lenh vao VLC lien tuc gay dung/treo app khi cam bien thay doi qua nhanh."""
        now = time.time()
        elapsed = now - self.last_switch_time
        if elapsed >= self.switch_cooldown:
            self._play_video(which)
            self.last_switch_time = now
        elif not self._pending_switch_scheduled:
            self._pending_switch_scheduled = True
            remaining_ms = int((self.switch_cooldown - elapsed) * 1000) + 20
            self.root.after(remaining_ms, self._process_pending_switch)

    def _process_pending_switch(self):
        self._pending_switch_scheduled = False
        # Doc lai trang thai MOI NHAT tai thoi diem nay (khong dung "which" cu da loi thoi)
        if self.removal_order:
            target = ("product", self.removal_order[-1])
        else:
            target = "background"
        self._request_video_switch(target)

    def _play_video(self, which):
        if self.vlc_instance is None or self.list_player is None:
            return
        if which == self.current_playing:
            return

        if which == "background":
            path = self.background_video_path
        else:
            _, idx = which
            path = self.product_video_paths[idx]

        if not path:
            return  # Chua co file clip nay trong thu muc, giu nguyen video dang phat

        self.list_player.stop()
        media_list = self.vlc_instance.media_list_new([path])
        self.list_player.set_media_list(media_list)
        self.list_player.set_playback_mode(vlc.PlaybackMode.loop)
        self.list_player.play()
        self.current_playing = which

    def on_close(self):
        if self.list_player is not None:
            self.list_player.stop()
        self._disconnect()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = SerialApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()