#!/usr/bin/env python3
"""
Moonlight Task Manager
----------------------
A lightweight terminal (curses) task manager for Moonlight OS.

Features:
  - Live process list (PID, user, CPU%, MEM%, status, name) - like `top`/`htop`
  - Sort by CPU, memory, PID, or name
  - Kill / terminate selected process
  - System error log viewer (journalctl -p err, falls back to /var/log/syslog)
  - Simple tabbed interface: [F1] Processes  [F2] Logs  [F3] Overview

Requirements:
  - Python 3.8+
  - psutil  (pip install psutil --break-system-packages)
  - curses  (built into Python on Linux)
  - Optional: journalctl (systemd) for the log tab; falls back to /var/log/syslog

Usage:
  python3 moonlight-taskmanager.py
  (or make executable: chmod +x moonlight-taskmanager.py && ./moonlight-taskmanager.py)

Run as root (sudo) to see/kill processes owned by other users and to read
protected log files.
"""

import curses
import curses.textpad
import os
import signal
import subprocess
import time
from datetime import datetime

try:
    import psutil
except ImportError:
    print("This app requires the 'psutil' package.")
    print("Install it with:  pip install psutil --break-system-packages")
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Data gathering
# --------------------------------------------------------------------------- #

SORT_KEYS = ["cpu", "mem", "pid", "name"]


def get_processes(sort_key="cpu"):
    """Return a list of dicts describing every running process."""
    procs = []
    for p in psutil.process_iter(
        ["pid", "name", "username", "status", "cpu_percent", "memory_percent"]
    ):
        try:
            info = p.info
            procs.append(
                {
                    "pid": info["pid"],
                    "name": info["name"] or "?",
                    "user": (info["username"] or "?")[:12],
                    "status": info["status"] or "?",
                    "cpu": info["cpu_percent"] or 0.0,
                    "mem": info["memory_percent"] or 0.0,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    reverse = sort_key in ("cpu", "mem")
    if sort_key == "name":
        procs.sort(key=lambda x: x["name"].lower())
    else:
        procs.sort(key=lambda x: x[sort_key], reverse=reverse)
    return procs


def get_system_overview():
    """Return a dict of high level system stats."""
    cpu_percent = psutil.cpu_percent(interval=0.0)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    boot = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot

    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append((part.mountpoint, usage.percent, usage.used, usage.total))
        except PermissionError:
            continue

    return {
        "cpu_percent": cpu_percent,
        "cpu_count": psutil.cpu_count(),
        "mem_total": vm.total,
        "mem_used": vm.used,
        "mem_percent": vm.percent,
        "swap_total": swap.total,
        "swap_used": swap.used,
        "swap_percent": swap.percent,
        "load": load,
        "uptime": uptime,
        "disks": disks,
        "process_count": len(psutil.pids()),
    }


def get_error_logs(max_lines=200):
    """
    Try journalctl first (systemd systems), fall back to /var/log/syslog,
    then /var/log/messages. Returns a list of plain text lines (most recent last).
    """
    # Try journalctl for priority <= err (0-3: emerg, alert, crit, err)
    try:
        result = subprocess.run(
            ["journalctl", "-p", "err", "-n", str(max_lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: plain log files
    for path in ("/var/log/syslog", "/var/log/messages"):
        if os.path.isfile(path):
            try:
                with open(path, "r", errors="replace") as f:
                    lines = f.readlines()
                return [l.rstrip("\n") for l in lines[-max_lines:]]
            except PermissionError:
                return [f"Permission denied reading {path}. Try running as root."]

    return ["No log source available (journalctl not found, no syslog file present)."]


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def human_timedelta(td):
    total = int(td.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

TAB_PROCESSES = 0
TAB_LOGS = 1
TAB_OVERVIEW = 2
TAB_NAMES = ["Processes (F1)", "Error Logs (F2)", "Overview (F3)"]


class TaskManagerApp:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.tab = TAB_PROCESSES
        self.sort_key = "cpu"
        self.selected = 0
        self.scroll = 0
        self.log_scroll = 0
        self.processes = []
        self.logs = []
        self.overview = {}
        self.status_msg = ""
        self.status_msg_time = 0
        self.refresh_interval = 2.0
        self.last_refresh = 0
        self.running = True

        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)     # headers
        curses.init_pair(2, curses.COLOR_GREEN, -1)    # ok / low usage
        curses.init_pair(3, curses.COLOR_YELLOW, -1)   # medium usage
        curses.init_pair(4, curses.COLOR_RED, -1)      # high usage / errors
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selection
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)  # title bar

    def set_status(self, msg):
        self.status_msg = msg
        self.status_msg_time = time.time()

    def refresh_data(self, force=False):
        now = time.time()
        if force or (now - self.last_refresh) >= self.refresh_interval:
            self.processes = get_processes(self.sort_key)
            self.overview = get_system_overview()
            if self.tab == TAB_LOGS:
                self.logs = get_error_logs()
            self.last_refresh = now

    # ---------------------------------------------------------------- #
    # Drawing helpers
    # ---------------------------------------------------------------- #

    def draw_title_bar(self, width):
        title = " Moonlight Task Manager "
        clock = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bar = title.ljust(width - len(clock) - 1) + clock + " "
        self.stdscr.addstr(0, 0, bar[:width], curses.color_pair(6) | curses.A_BOLD)

    def draw_tabs(self, width):
        x = 0
        for i, name in enumerate(TAB_NAMES):
            attr = curses.color_pair(5) | curses.A_BOLD if i == self.tab else curses.color_pair(1)
            label = f" {name} "
            self.stdscr.addstr(1, x, label, attr)
            x += len(label) + 1

    def draw_footer(self, width, height):
        if self.tab == TAB_PROCESSES:
            help_txt = " ↑/↓ select  k=kill  1-4=sort(cpu/mem/pid/name)  r=refresh  q=quit "
        elif self.tab == TAB_LOGS:
            help_txt = " ↑/↓ scroll  r=refresh  q=quit "
        else:
            help_txt = " r=refresh  q=quit "

        if self.status_msg and (time.time() - self.status_msg_time) < 3:
            help_txt = f" {self.status_msg} "
            attr = curses.color_pair(4) | curses.A_BOLD
        else:
            attr = curses.color_pair(1)

        self.stdscr.addstr(height - 1, 0, help_txt.ljust(width)[: width - 1], attr)

    def usage_color(self, pct):
        if pct >= 75:
            return curses.color_pair(4) | curses.A_BOLD
        if pct >= 40:
            return curses.color_pair(3)
        return curses.color_pair(2)

    # ---------------------------------------------------------------- #
    # Tab: Processes
    # ---------------------------------------------------------------- #

    def draw_processes(self, width, height):
        top = 3
        header = f"{'PID':>7} {'USER':<12} {'CPU%':>6} {'MEM%':>6} {'STATUS':<10} NAME"
        self.stdscr.addstr(top, 0, header[: width - 1], curses.color_pair(1) | curses.A_UNDERLINE)

        list_height = height - top - 2
        if self.selected >= len(self.processes):
            self.selected = max(0, len(self.processes) - 1)
        if self.selected < self.scroll:
            self.scroll = self.selected
        if self.selected >= self.scroll + list_height:
            self.scroll = self.selected - list_height + 1

        visible = self.processes[self.scroll : self.scroll + list_height]
        for row, proc in enumerate(visible):
            y = top + 1 + row
            idx = self.scroll + row
            line = (
                f"{proc['pid']:>7} {proc['user']:<12} "
                f"{proc['cpu']:>6.1f} {proc['mem']:>6.1f} "
                f"{proc['status']:<10} {proc['name']}"
            )
            line = line[: width - 1]

            if idx == self.selected:
                self.stdscr.addstr(y, 0, line.ljust(width - 1), curses.color_pair(5))
            else:
                attr = curses.color_pair(0)
                if proc["cpu"] >= 75 or proc["mem"] >= 75:
                    attr = curses.color_pair(4)
                elif proc["cpu"] >= 40 or proc["mem"] >= 40:
                    attr = curses.color_pair(3)
                self.stdscr.addstr(y, 0, line, attr)

        summary_y = height - 2
        summary = (
            f" {len(self.processes)} processes  |  sort: {self.sort_key}  "
            f"|  CPU {self.overview.get('cpu_percent', 0):.1f}%  "
            f"MEM {self.overview.get('mem_percent', 0):.1f}% "
        )
        self.stdscr.addstr(summary_y, 0, summary[: width - 1], curses.color_pair(1))

    def kill_selected(self):
        if not self.processes:
            return
        proc = self.processes[self.selected]
        pid = proc["pid"]
        name = proc["name"]
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.3)
            if psutil.pid_exists(pid):
                os.kill(pid, signal.SIGKILL)
            self.set_status(f"Terminated {name} (PID {pid})")
        except ProcessLookupError:
            self.set_status(f"Process {pid} already gone")
        except PermissionError:
            self.set_status(f"Permission denied killing {name} (PID {pid}) - try running as root")
        self.refresh_data(force=True)

    # ---------------------------------------------------------------- #
    # Tab: Logs
    # ---------------------------------------------------------------- #

    def draw_logs(self, width, height):
        top = 3
        self.stdscr.addstr(
            top,
            0,
            " System error log (journalctl -p err, or syslog fallback) ".ljust(width - 1),
            curses.color_pair(1) | curses.A_UNDERLINE,
        )
        list_height = height - top - 2

        max_scroll = max(0, len(self.logs) - list_height)
        self.log_scroll = min(self.log_scroll, max_scroll)
        self.log_scroll = max(self.log_scroll, 0)

        visible = self.logs[self.log_scroll : self.log_scroll + list_height]
        for row, line in enumerate(visible):
            y = top + 1 + row
            attr = curses.color_pair(0)
            low = line.lower()
            if any(k in low for k in ("error", "fail", "critical", "panic", "denied")):
                attr = curses.color_pair(4)
            elif "warn" in low:
                attr = curses.color_pair(3)
            self.stdscr.addstr(y, 0, line[: width - 1], attr)

        summary_y = height - 2
        summary = f" {len(self.logs)} lines  |  showing {self.log_scroll+1}-{min(self.log_scroll+list_height, len(self.logs))} "
        self.stdscr.addstr(summary_y, 0, summary[: width - 1], curses.color_pair(1))

    # ---------------------------------------------------------------- #
    # Tab: Overview
    # ---------------------------------------------------------------- #

    def draw_overview(self, width, height):
        top = 3
        o = self.overview
        y = top

        def line(text, attr=curses.color_pair(0)):
            nonlocal y
            if y < height - 1:
                self.stdscr.addstr(y, 2, text[: width - 3], attr)
                y += 1

        line("System Overview", curses.color_pair(1) | curses.A_BOLD)
        y += 1
        line(f"Uptime:          {human_timedelta(o.get('uptime', 0)) if o else 'n/a'}")
        line(f"Processes:       {o.get('process_count', 0)}")
        load = o.get("load", (0, 0, 0))
        line(f"Load average:    {load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}")
        y += 1

        cpu_pct = o.get("cpu_percent", 0)
        line(f"CPU usage:       {cpu_pct:.1f}%  ({o.get('cpu_count', '?')} cores)", self.usage_color(cpu_pct))

        mem_pct = o.get("mem_percent", 0)
        mem_used = human_bytes(o.get("mem_used", 0))
        mem_total = human_bytes(o.get("mem_total", 0))
        line(f"Memory usage:    {mem_pct:.1f}%  ({mem_used} / {mem_total})", self.usage_color(mem_pct))

        swap_pct = o.get("swap_percent", 0)
        swap_used = human_bytes(o.get("swap_used", 0))
        swap_total = human_bytes(o.get("swap_total", 0))
        line(f"Swap usage:      {swap_pct:.1f}%  ({swap_used} / {swap_total})", self.usage_color(swap_pct))

        y += 1
        line("Disks:", curses.color_pair(1) | curses.A_BOLD)
        for mount, pct, used, total in o.get("disks", []):
            line(f"  {mount:<20} {pct:>5.1f}%  ({human_bytes(used)} / {human_bytes(total)})", self.usage_color(pct))

    # ---------------------------------------------------------------- #
    # Main loop
    # ---------------------------------------------------------------- #

    def handle_key(self, key):
        if key in (ord("q"), ord("Q")):
            self.running = False
        elif key == curses.KEY_F1:
            self.tab = TAB_PROCESSES
        elif key == curses.KEY_F2:
            self.tab = TAB_LOGS
            self.refresh_data(force=True)
        elif key == curses.KEY_F3:
            self.tab = TAB_OVERVIEW
        elif key in (ord("r"), ord("R")):
            self.refresh_data(force=True)
            self.set_status("Refreshed")
        elif self.tab == TAB_PROCESSES:
            if key == curses.KEY_UP:
                self.selected = max(0, self.selected - 1)
            elif key == curses.KEY_DOWN:
                self.selected = min(len(self.processes) - 1, self.selected + 1)
            elif key in (ord("k"), ord("K")):
                self.kill_selected()
            elif key == ord("1"):
                self.sort_key = "cpu"
                self.refresh_data(force=True)
            elif key == ord("2"):
                self.sort_key = "mem"
                self.refresh_data(force=True)
            elif key == ord("3"):
                self.sort_key = "pid"
                self.refresh_data(force=True)
            elif key == ord("4"):
                self.sort_key = "name"
                self.refresh_data(force=True)
        elif self.tab == TAB_LOGS:
            if key == curses.KEY_UP:
                self.log_scroll = max(0, self.log_scroll - 1)
            elif key == curses.KEY_DOWN:
                self.log_scroll += 1
            elif key == curses.KEY_PPAGE:
                self.log_scroll = max(0, self.log_scroll - 10)
            elif key == curses.KEY_NPAGE:
                self.log_scroll += 10

    def run(self):
        self.refresh_data(force=True)
        self.logs = get_error_logs()

        while self.running:
            height, width = self.stdscr.getmaxyx()
            self.refresh_data()

            self.stdscr.erase()
            try:
                self.draw_title_bar(width)
                self.draw_tabs(width)

                if self.tab == TAB_PROCESSES:
                    self.draw_processes(width, height)
                elif self.tab == TAB_LOGS:
                    self.draw_logs(width, height)
                else:
                    self.draw_overview(width, height)

                self.draw_footer(width, height)
            except curses.error:
                # Terminal too small for a frame - ignore this redraw
                pass

            self.stdscr.refresh()

            try:
                key = self.stdscr.getch()
            except curses.error:
                key = -1

            if key != -1:
                self.handle_key(key)


def main(stdscr):
    app = TaskManagerApp(stdscr)
    app.run()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
