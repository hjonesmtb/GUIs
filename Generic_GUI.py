"""
TCP Client GUI
==============
Modular SCPI-style TCP client with live readback and command panels.

HOW TO CONFIGURE:
  - Edit READBACK_FIELDS: (label, word_index) — indices into the SFIFO? packet
  - Edit REGISTER_QUERIES: (label, query_string) — each queried independently
  - Edit COMMANDS: (label, command_template) — use {value} for the text-box arg
  - Adjust DEFAULT_IP / DEFAULT_PORT / PACKET_SIZE / POLL_INTERVAL_S below
"""

import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox


# ─────────────────────────────────────────────────────────────────────────────
# USER CONFIGURATION  ← edit these to match your device
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_IP      = "192.168.1.10"
DEFAULT_PORT    = 22040
PACKET_SIZE     = 4096   # recv buffer size (bytes)
POLL_INTERVAL_S = 0.2    # poll period in seconds (~5 Hz)
RECV_TIMEOUT_S  = 5.0    # per-query socket timeout

# Live readback — one SFIFO? per cycle, values indexed by word position
SFIFO_WD_QUERY = "SFIFO:WD?\r\n"
SFIFO_QUERY = "SFIFO? \r\n"
FIFO_WD_QUERY = "FIFO:WD?\r\n"
FIFO_QUERY = "FIFO?\r\n"
READBACK_FIELDS = [
    ("Val 1",  0),   # e.g. PID / alignment word
    ("Val 2",  3),   # e.g. avg_current
    ("Val 3", 4),   # e.g. peak_current
    ("Val 4",  5),
    ("Val 5", 10),
]

# Register panel — each queried separately, displayed as 16 bit-boxes
REGISTER_QUERIES = [
    ("Con Reg",    "CR?\r\n"),
    ("Status Reg", "SR?\r\n"),
]

# Command panel — write templates only. Use COMMAND_QUERIES for optional
# explicit readback queries when the device supports them.
COMMANDS = [
    ("PCA:GAIN", "{value}\r\n"),
    ("PCA:GAIN", "{value}\r\n"),
    ("PCA:GAIN", "{value}\r\n"),
    ("PCA:GAIN", "{value}\r\n"),
    ("PCA:GAIN", "{value}\r\n"),
]
COMMAND_QUERIES = [
    None,
    None,
    None,
    None,
    None,
]

# ─────────────────────────────────────────────────────────────────────────────
# COLOURS / STYLE
# ─────────────────────────────────────────────────────────────────────────────

BG          = "#1a1d23"
PANEL_BG    = "#22262f"
ACCENT      = "#00d4aa"
ACCENT2     = "#e05c5c"
TEXT        = "#dce3ed"
TEXT_DIM    = "#6b7585"
ENTRY_BG    = "#2c3140"
ENTRY_FG    = "#ffffff"
BORDER      = "#363c4e"
BIT_ON      = "#00d4aa"
BIT_OFF     = "#2c3140"
BIT_ON_TXT  = "#000000"
BIT_OFF_TXT = "#6b7585"
FONT_MONO   = ("Courier New", 10)
FONT_LABEL  = ("Segoe UI", 10)
FONT_TITLE  = ("Segoe UI", 11, "bold")
FONT_SMALL  = ("Segoe UI", 8)


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND I/O THREAD
# All socket work happens here. Results are pushed to result_queue.
# Commands arrive via cmd_queue and are sent before each poll cycle.
# ─────────────────────────────────────────────────────────────────────────────

class IOThread(threading.Thread):
    """
    Result queue message format  (always a tuple):
      ("status",  "connected" | "disconnected" | "error: <msg>")
      ("sfifo",   [word0, word1, ...] | None)
      ("reg",     index, raw_string | None)
      ("cmd_val", index, raw_string | None)
      ("cmd_ack", cmd_string)
      ("cmd_err", error_string)
    """

    def __init__(self, ip: str, port: int, command_templates: list, command_query_templates: list):
        super().__init__(daemon=True)
        self.ip   = ip
        self.port = port
        self.cmd_queue      = queue.Queue()
        self.result_queue   = queue.Queue()
        self._stop_evt      = threading.Event()
        self._command_queries = []
        for item in command_query_templates:
            if isinstance(item, tuple):
                _, qstr = item
            else:
                qstr = item
            self._command_queries.append(qstr)

    def stop(self):
        self._stop_evt.set()
        self.cmd_queue.put(None)   # unblock any waiting get()

    def _put(self, *args):
        self.result_queue.put(args)

    def _query(self, sock, cmd: str) -> str:
        sock.sendall(cmd.encode("ascii"))
        data = b""
        deadline = time.time() + RECV_TIMEOUT_S
        while time.time() < deadline:
            try:
                chunk = sock.recv(PACKET_SIZE)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if b"\r\n" in data or b"\n" in data:
                break
        return data.decode("ascii", errors="replace").strip()

    def _receive_line(self, sock, timeout: float = 0.1) -> str:
        """Read one response line if available without blocking long."""
        old_timeout = sock.gettimeout()
        try:
            sock.settimeout(timeout)
            data = b""
            while True:
                try:
                    chunk = sock.recv(PACKET_SIZE)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
                if b"\r\n" in data or b"\n" in data:
                    break
        finally:
            sock.settimeout(old_timeout)
        return data.decode("ascii", errors="replace").strip()

    def _query_fifo(self, sock):
        wd_raw = self._query(sock, FIFO_WD_QUERY)
        wd = int(wd_raw.splitlines()[0].strip(), 0)
        if wd < 16384:
            return None
        values = []
        for _ in range(8):
            raw = self._query(sock, FIFO_QUERY)
            values.extend(raw.split('\r\n')[0].split(','))
        return values

    def run(self):
        # ── connect ──────────────────────────────────────────────────────────
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(RECV_TIMEOUT_S)
            sock.connect((self.ip, self.port))
            self._put("status", "connected")
        except Exception as exc:
            self._put("status", f"error: {exc}")
            return

        # ── main loop ────────────────────────────────────────────────────────
        while not self._stop_evt.is_set():

            # Drain pending commands before each poll cycle
            while not self._stop_evt.is_set():
                try:
                    cmd = self.cmd_queue.get_nowait()
                    if cmd is None:
                        break
                    try:
                        if isinstance(cmd, tuple) and cmd[0] == "fifo":
                            values = self._query_fifo(sock)
                            self._put("fifo", values)
                        else:
                            sock.sendall(cmd.encode("ascii"))
                            # Drain any immediate write response so later poll queries
                            # see the expected query reply instead of stale command output.
                            self._receive_line(sock, timeout=0.25)
                            self._put("cmd_ack", cmd.strip())
                    except Exception as exc:
                        if isinstance(cmd, tuple) and cmd[0] == "fifo":
                            self._put("fifo_err", str(exc))
                        else:
                            self._put("cmd_err", str(exc))
                except queue.Empty:
                    break

            if self._stop_evt.is_set():
                break

            if self._stop_evt.is_set():
                break

            # SFIFO readback, only if the FIFO has more than 16 words available.
            try:
                wd_raw = self._query(sock, SFIFO_WD_QUERY)
                wd = int(wd_raw.split('\r\n')[0].strip(), 0)
                if wd >= 16:
                    raw = self._query(sock, SFIFO_QUERY)
                    self._put("sfifo", raw.split('\r\n')[0].split(','))
                else:
                    self._put("sfifo", None)
            except Exception:
                self._put("sfifo", None)

            if self._stop_evt.is_set():
                break

            # Register queries
            for idx, (label, qstr) in enumerate(REGISTER_QUERIES):
                try:
                    self._put("reg", idx, self._query(sock, qstr))
                except Exception:
                    self._put("reg", idx, None)
                if self._stop_evt.is_set():
                    break

            if self._stop_evt.is_set():
                break

            # Command value queries (current values)
            for idx, qstr in enumerate(self._command_queries):
                if qstr is None:
                    continue
                try:
                    self._put("cmd_val", idx, self._query(sock, qstr))
                except Exception:
                    self._put("cmd_val", idx, None)
                if self._stop_evt.is_set():
                    break

            # Wait for next cycle (interruptible by stop())
            self._stop_evt.wait(timeout=POLL_INTERVAL_S)

        try:
            sock.close()
        except Exception:
            pass
        self._put("status", "disconnected")


# ─────────────────────────────────────────────────────────────────────────────
# HELPER WIDGETS
# ─────────────────────────────────────────────────────────────────────────────

def styled_label(parent, text, font=FONT_LABEL, fg=TEXT, **kw) -> tk.Label:
    kw.setdefault("bg", parent.cget("bg"))
    return tk.Label(parent, text=text, font=font, fg=fg, **kw)


def styled_entry(parent, width=18, **kw) -> tk.Entry:
    return tk.Entry(
        parent,
        bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=ACCENT,
        relief="flat", font=FONT_MONO, width=width,
        highlightthickness=1, highlightbackground=BORDER,
        highlightcolor=ACCENT, **kw
    )


class BitRegisterWidget(tk.Frame):
    """16 coloured boxes representing a 16-bit value, MSB on the left."""

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=PANEL_BG, **kw)
        self._bits = []
        for col in range(16):
            lbl = tk.Label(
                self, text="0", width=2,
                bg=BIT_OFF, fg=BIT_OFF_TXT,
                font=FONT_SMALL, relief="flat", padx=1, pady=2
            )
            lbl.grid(row=0, column=col, padx=1)
            self._bits.append(lbl)

    def set_value(self, value: int):
        for col, lbl in enumerate(self._bits):
            bit = (value >> (15 - col)) & 1
            lbl.config(
                text=str(bit),
                bg=BIT_ON     if bit else BIT_OFF,
                fg=BIT_ON_TXT if bit else BIT_OFF_TXT,
            )

    def clear(self):
        for lbl in self._bits:
            lbl.config(text="?", bg=BIT_OFF, fg=BIT_OFF_TXT)


# ─────────────────────────────────────────────────────────────────────────────
# PANELS
# ─────────────────────────────────────────────────────────────────────────────

class ConnectionPanel(tk.Frame):
    def __init__(self, parent, on_connect, on_disconnect, **kw):
        super().__init__(parent, bg=BG, **kw)
        self._on_connect_cb    = on_connect
        self._on_disconnect_cb = on_disconnect
        self._connected        = False

        styled_label(self, "IP:", bg=BG).pack(side="left", padx=(0, 4))
        self.ip_var = tk.StringVar(value=DEFAULT_IP)
        styled_entry(self, width=16, textvariable=self.ip_var).pack(side="left", padx=(0, 10))

        styled_label(self, "Port:", bg=BG).pack(side="left", padx=(0, 4))
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        styled_entry(self, width=7, textvariable=self.port_var).pack(side="left", padx=(0, 14))

        self.btn = tk.Button(
            self, text="Connect", command=self._toggle,
            bg=ACCENT, fg="#000000", activebackground="#00b894",
            font=("Segoe UI", 10, "bold"), relief="flat",
            padx=14, pady=4, cursor="hand2"
        )
        self.btn.pack(side="left", padx=(0, 14))

        self.status_var = tk.StringVar(value="● Disconnected")
        self._status_lbl = tk.Label(
            self, textvariable=self.status_var,
            bg=BG, fg=ACCENT2, font=FONT_SMALL
        )
        self._status_lbl.pack(side="left")

        self._data_active = False
        self._data_led = tk.Label(
            self, text=" ", width=2, bg=BIT_OFF,
            bd=1, relief="solid"
        )
        self._data_led.pack(side="left", padx=(10, 0))

    def _set_status(self, text, colour):
        self.status_var.set(text)
        self._status_lbl.config(fg=colour)

    def toggle_data_activity(self):
        self._data_active = not self._data_active
        self._data_led.config(bg=BIT_ON if self._data_active else BIT_OFF)

    def _toggle(self):
        self.btn.config(state="disabled")
        if self._connected:
            self._on_disconnect_cb()
        else:
            ip   = self.ip_var.get().strip()
            port = int(self.port_var.get().strip())
            self._set_status("● Connecting…", TEXT_DIM)
            self._on_connect_cb(ip, port)

    def on_connected(self):
        self._connected = True
        self.btn.config(text="Disconnect", bg=ACCENT2, state="normal")
        self._set_status("● Connected", ACCENT)

    def on_disconnected(self):
        self._connected = False
        self.btn.config(text="Connect", bg=ACCENT, state="normal")
        self._set_status("● Disconnected", ACCENT2)

    def on_error(self, msg: str):
        self._connected = False
        self.btn.config(text="Connect", bg=ACCENT, state="normal")
        self._set_status(f"● {msg}", ACCENT2)


class ReadbackPanel(tk.LabelFrame):
    def __init__(self, parent, fields: list, **kw):
        super().__init__(
            parent, text=" Live Readback ", bg=PANEL_BG, fg=ACCENT,
            font=FONT_TITLE, bd=1, relief="groove", **kw
        )
        self._vars = []
        for i, (label, idx) in enumerate(fields):
            styled_label(self, f"{label}  [w{idx}]", bg=PANEL_BG).grid(
                row=i, column=0, sticky="e", padx=(10, 6), pady=5
            )
            var = tk.StringVar(value="--")
            tk.Entry(
                self, textvariable=var, state="readonly",
                readonlybackground=ENTRY_BG, fg=ACCENT,
                font=FONT_MONO, width=20, relief="flat",
                highlightthickness=1, highlightbackground=BORDER
            ).grid(row=i, column=1, padx=(0, 10), pady=5)
            self._vars.append(var)

    def update(self, words):
        if words is None:
            for v in self._vars:
                v.set("ERR")
            return
        for i, (_, idx) in enumerate(READBACK_FIELDS):
            try:
                self._vars[i].set(words[idx])
            except IndexError:
                self._vars[i].set("--")

    def clear(self):
        for v in self._vars:
            v.set("--")


def _to_signed_16(value: str) -> int:
    raw = int(value.strip(), 0) & 0xFFFF
    return raw - 0x10000 if raw & 0x8000 else raw


class RegisterPanel(tk.LabelFrame):
    def __init__(self, parent, queries: list, **kw):
        super().__init__(
            parent, text=" Registers ", bg=PANEL_BG, fg=ACCENT,
            font=FONT_TITLE, bd=1, relief="groove", **kw
        )
        self._bit_widgets = []
        for i, (label, _) in enumerate(queries):
            styled_label(self, label, bg=PANEL_BG, width=10, anchor="e").grid(
                row=i, column=0, padx=(10, 8), pady=8
            )
            bw = BitRegisterWidget(self)
            bw.grid(row=i, column=1, padx=(0, 10), pady=8, sticky="w")
            self._bit_widgets.append(bw)

        styled_label(
            self, "16 bit-boxes per register  (MSB left).  ~5 Hz.",
            fg=TEXT_DIM, font=FONT_SMALL, bg=PANEL_BG
        ).grid(row=len(queries), column=0, columnspan=2, padx=10, pady=(2, 10))

    def update(self, idx: int, raw):
        if raw is None:
            self._bit_widgets[idx].clear()
            return
        try:
            self._bit_widgets[idx].set_value(int(raw.splitlines()[0].strip()))
        except (ValueError, TypeError, IndexError):
            self._bit_widgets[idx].clear()

    def clear_all(self):
        for bw in self._bit_widgets:
            bw.clear()


class FIFOChartPanel(tk.LabelFrame):
    def __init__(self, parent, **kw):
        super().__init__(
            parent, text=" FIFO Chart ", bg=PANEL_BG, fg=ACCENT,
            font=FONT_TITLE, bd=1, relief="groove", **kw
        )
        self._values = []
        self._status_var = tk.StringVar(value="No FIFO data")
        self._canvas = tk.Canvas(
            self, bg=ENTRY_BG, highlightthickness=0,
            bd=0
        )
        self._canvas.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        self._status_label = tk.Label(
            self, textvariable=self._status_var,
            bg=PANEL_BG, fg=TEXT_DIM, font=FONT_SMALL,
            wraplength=380, justify="left"
        )
        self._status_label.pack(fill="x", padx=10, pady=(0, 10))
        self._canvas.bind("<Configure>", lambda ev: self._draw())

    def update(self, raw_values):
        if raw_values is None:
            self._values = []
            self._status_var.set("No FIFO data")
            self._draw()
            return
        values = []
        for raw in raw_values:
            try:
                values.append(_to_signed_16(raw))
            except Exception:
                pass
        self._values = values
        self._status_var.set(f"{len(values)} signed 16-bit values")
        self._draw()

    def clear(self):
        self._values = []
        self._status_var.set("No FIFO data")
        self._draw()

    def _draw(self):
        self._canvas.delete("all")
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        if width <= 10 or height <= 10:
            return
        self._canvas.create_rectangle(0, 0, width, height, fill=ENTRY_BG, outline="")
        if not self._values:
            self._canvas.create_text(
                width // 2, height // 2,
                text="No FIFO values to plot", fill=TEXT_DIM,
                font=FONT_SMALL
            )
            return
        min_val = min(self._values)
        max_val = max(self._values)
        if min_val == max_val:
            min_val -= 1
            max_val += 1
        x0, y0 = 40, 20
        x1, y1 = width - 10, height - 30
        self._canvas.create_rectangle(x0, y0, x1, y1, outline=TEXT_DIM)
        self._canvas.create_text(x0 + 4, y0 + 8, text=str(max_val), anchor="w", fill=TEXT_DIM, font=FONT_SMALL)
        self._canvas.create_text(x0 + 4, y1 - 8, text=str(min_val), anchor="w", fill=TEXT_DIM, font=FONT_SMALL)
        plot_width = max(1, x1 - x0)
        plot_height = max(1, y1 - y0)
        step = max(1, len(self._values) // plot_width)
        sampled = self._values[::step]
        if len(sampled) < 2:
            sampled = self._values
        coords = []
        for idx, value in enumerate(sampled):
            x = x0 + idx * (plot_width / (len(sampled) - 1)) if len(sampled) > 1 else x0
            y = y1 - ((value - min_val) / (max_val - min_val)) * plot_height
            coords.extend((x, y))
        self._canvas.create_line(*coords, fill=ACCENT, width=1.5, smooth=True)
        self._canvas.create_text(x1, y1 + 12, text=f"{len(self._values)} pts", anchor="e", fill=TEXT_DIM, font=FONT_SMALL)


class CommandPanel(tk.LabelFrame):
    def __init__(self, parent, commands: list, **kw):
        super().__init__(
            parent, text=" Commands ", bg=PANEL_BG, fg=ACCENT,
            font=FONT_TITLE, bd=1, relief="groove", **kw
        )
        self._cmd_queue = None
        self._current_vars = []
        self._vars = []
        self._labels = []

        styled_label(self, "", bg=PANEL_BG).grid(row=0, column=0, padx=(10, 6))
        styled_label(self, "Current", bg=PANEL_BG, width=18).grid(row=0, column=1, padx=(0, 8))
        styled_label(self, "New value", bg=PANEL_BG, width=18).grid(row=0, column=2, padx=(0, 8))

        for i, (label, template) in enumerate(commands, start=1):
            styled_label(self, label, bg=PANEL_BG, width=7, anchor="e").grid(
                row=i, column=0, padx=(10, 6), pady=6
            )
            current_var = tk.StringVar(value="--")
            tk.Entry(
                self, textvariable=current_var, state="readonly",
                readonlybackground=ENTRY_BG, fg=ACCENT,
                font=FONT_MONO, width=18, relief="flat",
                highlightthickness=1, highlightbackground=BORDER
            ).grid(row=i, column=1, padx=(0, 8), pady=6)
            self._current_vars.append(current_var)

            new_var = tk.StringVar()
            e = styled_entry(self, textvariable=new_var, width=18)
            e.grid(row=i, column=2, padx=(0, 8), pady=6)
            self._vars.append(new_var)
            self._labels.append(label)

            btn = tk.Button(
                self, text="✔", width=3,
                bg=ENTRY_BG, fg=ACCENT, activebackground=ACCENT,
                activeforeground="#000", font=FONT_LABEL,
                relief="flat", cursor="hand2",
                command=lambda l=label, t=template, v=new_var: self._send(l, t, v)
            )
            btn.grid(row=i, column=3, padx=(0, 10), pady=6)
            e.bind("<Return>", lambda ev, l=label, t=template, v=new_var: self._send(l, t, v))

        self._status_var = tk.StringVar(value="")
        tk.Label(
            self, textvariable=self._status_var,
            bg=PANEL_BG, fg=TEXT_DIM, font=FONT_SMALL
        ).grid(row=len(commands) + 1, column=0, columnspan=4, padx=10, pady=(2, 8))

    def set_queue(self, q):
        self._cmd_queue = q

    def update_current(self, idx: int, raw: str):
        if idx < 0 or idx >= len(self._current_vars):
            return
        if raw is None:
            self._current_vars[idx].set("ERR")
            return
        self._current_vars[idx].set(raw.split('\r\n')[0].strip())

    def clear_current_values(self):
        for v in self._current_vars:
            v.set("--")

    def _send(self, label: str, template: str, var: tk.StringVar):
        if self._cmd_queue is None:
            self._status_var.set("⚠  Not connected")
            return
        value = var.get().strip()
        if "{value}" in template:
            cmd = template.format(value=value)
            if cmd.strip() == value and label:
                # If the template is just a value placeholder, prepend the command label.
                cmd = f"{label} {value}\r\n"
        else:
            cmd = template
        self._cmd_queue.put(cmd)
        self._status_var.set(f"→  {cmd.strip()}")

    def on_ack(self, cmd: str):
        self._status_var.set(f"✔  {cmd}")

    def on_err(self, msg: str):
        self._status_var.set(f"✖  {msg}")


class CustomCommandPanel(tk.LabelFrame):
    def __init__(self, parent, **kw):
        super().__init__(
            parent, text=" Custom Command ", bg=PANEL_BG, fg=ACCENT,
            font=FONT_TITLE, bd=1, relief="groove", **kw
        )
        self._cmd_queue = None

        styled_label(self, "Command:", bg=PANEL_BG).grid(row=0, column=0, padx=(10, 6), pady=(10, 6), sticky="e")
        self._entry = styled_entry(self, width=30)
        self._entry.grid(row=0, column=1, padx=(0, 8), pady=(10, 6))

        self._send_btn = tk.Button(
            self, text="Send", width=6,
            bg=ACCENT, fg="#000000", activebackground="#00b894",
            font=FONT_LABEL, relief="flat", cursor="hand2",
            command=self._send
        )
        self._send_btn.grid(row=0, column=2, padx=(0, 10), pady=(10, 6))

        self._status_var = tk.StringVar(value="")
        tk.Label(
            self, textvariable=self._status_var,
            bg=PANEL_BG, fg=TEXT_DIM, font=FONT_SMALL
        ).grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 10))

    def set_queue(self, q):
        self._cmd_queue = q

    def _send(self):
        if self._cmd_queue is None:
            self._status_var.set("⚠  Not connected")
            return
        cmd = self._entry.get().strip()
        if not cmd:
            return
        if not cmd.endswith('\r\n'):
            cmd += '\r\n'
        self._cmd_queue.put(cmd)
        self._status_var.set(f"→  {cmd.strip()}")

    def on_ack(self, cmd: str):
        self._status_var.set(f"✔  {cmd}")

    def on_err(self, msg: str):
        self._status_var.set(f"✖  {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APPLICATION
# ─────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TCP Instrument Client")
        self.configure(bg=BG)
        self.resizable(True, True)
        self._io = None
        self._build_ui()
        self._poll_results()   # start draining result queue on main thread

    def _build_ui(self):
        top = tk.Frame(self, bg=BG, padx=16, pady=10)
        top.pack(fill="x")
        self.conn_panel = ConnectionPanel(
            top,
            on_connect=self._start_io,
            on_disconnect=self._stop_io,
        )
        self.conn_panel.pack(side="left")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        body = tk.Frame(self, bg=BG, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)
        body.rowconfigure(2, weight=0)

        self.readback_panel = ReadbackPanel(body, READBACK_FIELDS)
        self.readback_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))

        self.register_panel = RegisterPanel(body, REGISTER_QUERIES)
        self.register_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 12))

        self.command_panel = CommandPanel(body, COMMANDS)
        self.command_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))

        fifo_container = tk.LabelFrame(
            body, text=" FIFO Fetch ", bg=PANEL_BG, fg=ACCENT,
            font=FONT_TITLE, bd=1, relief="groove"
        )
        fifo_container.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))

        self.fifo_status_var = tk.StringVar(value="FIFO idle")
        self.fifo_button = tk.Button(
            fifo_container,
            text="Query FIFO",
            command=self._on_fifo_button,
            bg=ENTRY_BG, fg=ACCENT,
            activebackground=ACCENT, activeforeground="#000",
            font=FONT_LABEL, relief="flat", cursor="hand2",
            padx=10, pady=6
        )
        self.fifo_button.pack(padx=10, pady=(10, 8))

        tk.Label(
            fifo_container,
            textvariable=self.fifo_status_var,
            bg=PANEL_BG, fg=TEXT_DIM,
            font=FONT_SMALL, wraplength=260, justify="left"
        ).pack(fill="x", padx=10, pady=(0, 10))

        self.fifo_chart = FIFOChartPanel(fifo_container)
        self.fifo_chart.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.fifo_button.config(state="disabled")

        self.custom_panel = CustomCommandPanel(body)
        self.custom_panel.grid(row=2, column=0, columnspan=2, sticky="ew", padx=0, pady=(8, 0))

    def _start_io(self, ip: str, port: int):
        if self._io and self._io.is_alive():
            self._io.stop()
        self._io = IOThread(ip, port, COMMANDS, COMMAND_QUERIES)
        self._io.start()
        self.command_panel.set_queue(self._io.cmd_queue)
        self.custom_panel.set_queue(self._io.cmd_queue)

    def _on_fifo_button(self):
        if not self._io or not self._io.is_alive():
            self.fifo_status_var.set("Not connected")
            return
        self._io.cmd_queue.put(("fifo",))
        self.fifo_status_var.set("Querying FIFO…")

    def _stop_io(self):
        if self._io:
            self._io.stop()
            self._io = None
        self.command_panel.set_queue(None)
        self.custom_panel.set_queue(None)
        self.command_panel.clear_current_values()
        self.readback_panel.clear()
        self.register_panel.clear_all()
        self.fifo_button.config(state="disabled")
        self.fifo_status_var.set("FIFO idle")
        self.fifo_chart.clear()
        self.conn_panel.on_disconnected()

    def _poll_results(self):
        """Drain the result queue on the main thread at 20 Hz. Never blocks."""
        if self._io:
            try:
                while True:
                    msg = self._io.result_queue.get_nowait()
                    kind = msg[0]
                    if kind == "status":
                        val = msg[1]
                        if val == "connected":
                            self.conn_panel.on_connected()
                            self.command_panel.set_queue(self._io.cmd_queue)
                            self.custom_panel.set_queue(self._io.cmd_queue)
                            self.fifo_button.config(state="normal")
                        elif val == "disconnected":
                            self.conn_panel.on_disconnected()
                            self.command_panel.set_queue(None)
                            self.custom_panel.set_queue(None)
                            self.fifo_button.config(state="disabled")
                            self.readback_panel.clear()
                            self.register_panel.clear_all()
                        elif val.startswith("error:"):
                            self.conn_panel.on_error(val[6:].strip())
                            messagebox.showerror("Connection Error", val[6:].strip())
                    elif kind == "sfifo":
                        self.readback_panel.update(msg[1])
                        self.conn_panel.toggle_data_activity()
                    elif kind == "reg":
                        self.register_panel.update(msg[1], msg[2])
                    elif kind == "cmd_val":
                        self.command_panel.update_current(msg[1], msg[2])
                    elif kind == "fifo":
                        if msg[1] is None:
                            self.fifo_status_var.set("FIFO count != 16384, no fetch performed")
                            self.fifo_chart.clear()
                        else:
                            self.fifo_status_var.set(f"FIFO fetched {len(msg[1])} values")
                            self.fifo_chart.update(msg[1])
                    elif kind == "fifo_err":
                        self.fifo_status_var.set(f"FIFO error: {msg[1]}")

                    elif kind == "cmd_ack":
                        self.command_panel.on_ack(msg[1])
                        self.custom_panel.on_ack(msg[1])
                    elif kind == "cmd_err":
                        self.command_panel.on_err(msg[1])
                        self.custom_panel.on_err(msg[1])
            except queue.Empty:
                pass
        self.after(50, self._poll_results)

    def on_closing(self):
        if self._io:
            self._io.stop()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()