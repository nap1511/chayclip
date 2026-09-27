"""
App Windows dieu khien den qua cong COM va hien thi du lieu cam bien tu Arduino.

Cai dat thu vien can thiet (chi can lam 1 lan):
    pip install pyserial

Chay app:
    python arduino_control_app.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
import serial.tools.list_ports
import threading
import queue
import time

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

BAUD_RATE = 9600


class SerialApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Dieu khien Arduino qua COM")
        self.root.geometry("480x420")

        self.ser = None
        self.read_thread = None
        self.running = False
        self.msg_queue = queue.Queue()

        # Trang thai bo dem giay khi co vat can
        self.obstacle_active = False
        self.obstacle_start_time = None

        # Duong dan clip video cho 2 truong hop
        self.obstacle_video_path = None
        self.no_obstacle_video_path = None
        self.current_playing = None  # "obstacle" | "no_obstacle" | None

        self.video_capture = None   # cv2.VideoCapture dang mo
        self.video_delay = 33       # ms giua cac khung hinh (~30fps mac dinh)

        self.control_visible = False  # Giao dien dieu khien mac dinh an di

        self._build_ui()
        self._refresh_ports()
        self._poll_queue()
        self._update_timer()
        self._update_video_frame()

        # Chay full man hinh (macOS/Windows). Nhan ESC de thoat fullscreen.
        self.root.attributes("-fullscreen", True)
        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))

        # Phim tat F1 de hien/an giao dien dieu khien
        self.root.bind("<F1>", lambda e: self._toggle_control_panel())

    def _build_ui(self):
        # --- Lop nen: video phu kin toan bo cua so ---
        self.video_label = tk.Label(self.root, bg="black")
        self.video_label.place(relx=0, rely=0, relwidth=1, relheight=1)

        # --- Lop noi: khung dieu khien, an mac dinh, hien khi nhan F1 ---
        self.control_panel = tk.Frame(self.root, bg="#1e1e1e")

        # --- Khung chon cong COM ---
        frame_top = ttk.Frame(self.control_panel, padding=10)
        frame_top.pack(fill="x")

        ttk.Label(frame_top, text="Cong COM:").pack(side="left")
        self.port_combo = ttk.Combobox(frame_top, width=15, state="readonly")
        self.port_combo.pack(side="left", padx=5)

        ttk.Button(frame_top, text="Lam moi", command=self._refresh_ports).pack(side="left", padx=5)

        self.connect_btn = ttk.Button(frame_top, text="Ket noi", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=5)

        self.status_label = ttk.Label(frame_top, text="Chua ket noi", foreground="red")
        self.status_label.pack(side="left", padx=10)

        # --- Khung dieu khien den ---
        frame_led = ttk.LabelFrame(self.control_panel, text="Dieu khien den (chan A0 - dung chung voi canh bao)", padding=10)
        frame_led.pack(fill="x", padx=10, pady=10)

        ttk.Button(frame_led, text="BAT DEN", command=lambda: self._send_command('1')).pack(
            side="left", expand=True, fill="x", padx=5)
        ttk.Button(frame_led, text="TAT DEN", command=lambda: self._send_command('0')).pack(
            side="left", expand=True, fill="x", padx=5)

        # --- Khung hien thi trang thai cam bien ---
        frame_status = ttk.LabelFrame(self.control_panel, text="Trang thai cam bien", padding=10)
        frame_status.pack(fill="x", padx=10, pady=5)

        self.tilt_label = ttk.Label(frame_status, text="Nghieng: --", font=("Arial", 11))
        self.tilt_label.pack(anchor="w")

        self.obstacle_label = ttk.Label(frame_status, text="Vat can: --", font=("Arial", 11))
        self.obstacle_label.pack(anchor="w")

        self.alert_label = ttk.Label(frame_status, text="Den A0: --", font=("Arial", 12, "bold"))
        self.alert_label.pack(anchor="w", pady=5)

        self.manual_label = ttk.Label(frame_status, text="Dieu khien tay: --", font=("Arial", 10))
        self.manual_label.pack(anchor="w")

        # --- Khung dong ho dem giay vat can ---
        frame_timer = ttk.LabelFrame(self.control_panel, text="Thoi gian co vat can lien tuc", padding=10)
        frame_timer.pack(fill="x", padx=10, pady=5)

        self.timer_label = ttk.Label(frame_timer, text="0.0 giay", font=("Arial", 28, "bold"))
        self.timer_label.pack(anchor="center", pady=5)

        # --- Khung chon clip video ---
        frame_video_select = ttk.LabelFrame(self.control_panel, text="Chon clip video", padding=10)
        frame_video_select.pack(fill="x", padx=10, pady=5)

        ttk.Button(frame_video_select, text="Chon clip KHI CO vat can",
                   command=lambda: self._choose_video("obstacle")).pack(
            side="left", expand=True, fill="x", padx=5)
        ttk.Button(frame_video_select, text="Chon clip KHI KHONG CO vat can",
                   command=lambda: self._choose_video("no_obstacle")).pack(
            side="left", expand=True, fill="x", padx=5)

        self.video_path_label = ttk.Label(self.control_panel, text="Chua chon clip nao.", font=("Arial", 9))
        self.video_path_label.pack(fill="x", padx=10)

        # --- Khung log du lieu tho ---
        frame_log = ttk.LabelFrame(self.control_panel, text="Du lieu nhan duoc", padding=10)
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = tk.Text(frame_log, height=8, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _toggle_control_panel(self):
        if self.control_visible:
            self.control_panel.place_forget()
        else:
            self.control_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.control_panel.lift()
        self.control_visible = not self.control_visible

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports:
            self.port_combo.current(0)

    def _toggle_connect(self):
        if self.ser and self.ser.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_combo.get()
        if not port:
            messagebox.showwarning("Chua chon cong", "Vui long chon cong COM truoc.")
            return
        try:
            self.ser = serial.Serial(port, BAUD_RATE, timeout=1)
            time.sleep(2)  # Cho Arduino reset sau khi mo cong serial
            self.running = True
            self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.read_thread.start()

            self.status_label.config(text=f"Da ket noi ({port})", foreground="green")
            self.connect_btn.config(text="Ngat ket noi")
        except serial.SerialException as e:
            messagebox.showerror("Loi ket noi", f"Khong the mo cong {port}:\n{e}")

    def _disconnect(self):
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.status_label.config(text="Chua ket noi", foreground="red")
        self.connect_btn.config(text="Ket noi")

    def _send_command(self, cmd: str):
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(cmd.encode())
            except serial.SerialException as e:
                messagebox.showerror("Loi gui lenh", str(e))
        else:
            messagebox.showwarning("Chua ket noi", "Hay ket noi cong COM truoc khi dieu khien.")

    def _read_loop(self):
        while self.running and self.ser and self.ser.is_open:
            try:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line:
                    self.msg_queue.put(line)
            except serial.SerialException:
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
        # Dinh dang mong doi: TILT:1,OBSTACLE:0,ALERT:1
        if "TILT:" not in line:
            return
        try:
            parts = dict(item.split(":") for item in line.split(","))
            tilt = parts.get("TILT")
            obstacle = parts.get("OBSTACLE")
            alert = parts.get("ALERT")
            manual = parts.get("MANUAL")

            if tilt is not None:
                self.tilt_label.config(
                    text=f"Nghieng: {'CO' if tilt == '1' else 'Khong'}")
            if obstacle is not None:
                is_obstacle = obstacle == '1'
                self.obstacle_label.config(
                    text=f"Vat can: {'CO' if is_obstacle else 'Khong'}")

                if is_obstacle and not self.obstacle_active:
                    # Vua moi phat hien vat can -> bat dau dem tu 0
                    self.obstacle_active = True
                    self.obstacle_start_time = time.time()
                    self._play_video("obstacle")
                elif not is_obstacle and self.obstacle_active:
                    # Het vat can -> dung dem, tro ve 0
                    self.obstacle_active = False
                    self.obstacle_start_time = None
                    self.timer_label.config(text="0.0 giay")
                    self._play_video("no_obstacle")
            if alert is not None:
                is_alert = alert == '1'
                self.alert_label.config(
                    text=f"Den A0: {'BAT' if is_alert else 'TAT'}",
                    foreground="red" if is_alert else "green")
            if manual is not None:
                self.manual_label.config(
                    text=f"Dieu khien tay: {'BAT' if manual == '1' else 'TAT'}")
        except (ValueError, KeyError):
            pass  # Bo qua dong du lieu khong dung dinh dang

    def _update_timer(self):
        if self.obstacle_active and self.obstacle_start_time is not None:
            elapsed = time.time() - self.obstacle_start_time
            self.timer_label.config(text=f"{elapsed:.1f} giay")
        self.root.after(100, self._update_timer)

    def _update_video_frame(self):
        if cv2 is not None and Image is not None and self.video_capture is not None:
            ret, frame = self.video_capture.read()
            if not ret:
                # Het video -> quay lai frame dau de lap lai
                self.video_capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = self.video_capture.read()
            if ret:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                w = self.video_label.winfo_width() or self.root.winfo_screenwidth()
                h = self.video_label.winfo_height() or self.root.winfo_screenheight()
                img = Image.fromarray(frame)
                img.thumbnail((max(w, 1), max(h, 1)))
                imgtk = ImageTk.PhotoImage(image=img)
                self.video_label.imgtk = imgtk  # giu tham chieu, tranh bi thu gom rac
                self.video_label.config(image=imgtk)
        self.root.after(self.video_delay, self._update_video_frame)

    def _choose_video(self, which: str):
        if cv2 is None or Image is None:
            messagebox.showerror(
                "Thieu thu vien",
                "Chay lenh sau roi khoi dong lai app:\n"
                "python3 -m pip install opencv-python pillow")
            return
        path = filedialog.askopenfilename(
            title="Chon file video",
            filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv"), ("Tat ca file", "*.*")]
        )
        if not path:
            return
        if which == "obstacle":
            self.obstacle_video_path = path
            if self.obstacle_active:
                self._play_video("obstacle")
        else:
            self.no_obstacle_video_path = path
            if not self.obstacle_active:
                self._play_video("no_obstacle")
        self._update_video_path_label()

    def _update_video_path_label(self):
        obs = self.obstacle_video_path or "(chua chon)"
        no_obs = self.no_obstacle_video_path or "(chua chon)"
        self.video_path_label.config(
            text=f"Vat can: {obs}   |   Khong vat can: {no_obs}")

    def _play_video(self, which: str):
        if cv2 is None:
            return
        path = self.obstacle_video_path if which == "obstacle" else self.no_obstacle_video_path
        if not path:
            return  # Chua chon clip cho truong hop nay
        if self.current_playing == which:
            return  # Dang phat dung clip nay roi, khong lam gi them
        if self.video_capture is not None:
            self.video_capture.release()
        self.video_capture = cv2.VideoCapture(path)
        fps = self.video_capture.get(cv2.CAP_PROP_FPS)
        self.video_delay = int(1000 / fps) if fps and fps > 0 else 33
        self.current_playing = which

    def on_close(self):
        if self.video_capture is not None:
            self.video_capture.release()
        self._disconnect()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = SerialApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()