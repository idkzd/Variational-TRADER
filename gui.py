#!/usr/bin/env python3
"""
Variational Omni Trading Bot — Modern GUI
Beautiful dark interface with config editor, live log, stats dashboard.
"""

from __future__ import annotations

import customtkinter as ctk
import logging
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

# ─── App-wide theme ─────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Colors
C_BG = "#0d1117"
C_CARD = "#161b22"
C_BORDER = "#30363d"
C_TEXT = "#e6edf3"
C_MUTED = "#7d8590"
C_ACCENT = "#58a6ff"
C_GREEN = "#3fb950"
C_RED = "#f85149"
C_YELLOW = "#d29922"
C_ORANGE = "#db6d28"
C_PURPLE = "#bc8cff"

CONFIG_PATH = Path(__file__).parent / "config.txt"
ENV_PATH = Path(__file__).parent / ".env"


# ════════════════════════════════════════════════════════════
#  Queue-based log handler → feeds GUI
# ════════════════════════════════════════════════════════════

class QueueLogHandler(logging.Handler):
    """Pushes log records into a queue for the GUI to consume."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.log_queue.put((record.levelno, msg))
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
#  Config field definitions
# ════════════════════════════════════════════════════════════

CONFIG_FIELDS = [
    # (key, label, default, type, tooltip)
    ("symbol", "Торговая пара", "BTC-USDC", "str", "Пара для торговли"),
    ("position_size_usdc", "Размер позиции ($)", "500.0", "float", "Сколько USD на каждый трейд"),
    ("leverage", "Плечо (x)", "49.0", "float", "Кредитное плечо 1-50"),
    ("tp_sl_distance_pct", "TP/SL дистанция", "0.0005", "float", "Расстояние TP/SL (0.0005 = 0.05%)"),
    ("market_spread_threshold", "Порог спреда (%)", "0.02", "float", "Маркет если спред ≤ порога"),
    ("min_balance_usdc", "Мин. баланс ($)", "5.0", "float", "Стоп если баланс ниже"),
    ("slippage_tolerance", "Проскальзывание", "0.005", "float", "Макс. slippage"),
    ("poll_interval", "Интервал опроса (с)", "1.0", "float", "Как часто проверяем цену"),
    ("order_fill_timeout", "Таймаут ордера (с)", "60.0", "float", "Макс. ожидание лимитки"),
    ("cycle_cooldown", "Пауза между циклами (с)", "3.0", "float", "Отдых между сделками"),
    ("max_consecutive_errors", "Макс. ошибок подряд", "10", "int", "Circuit breaker"),
]


def load_config_values() -> dict[str, str]:
    """Read config.txt into a dict."""
    vals = {}
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if "  #" in v:
                v = v[: v.index("  #")].strip()
            vals[k] = v
    return vals


def save_config_values(vals: dict[str, str]) -> None:
    """Write config.txt preserving nice formatting."""
    lines = [
        "# ══════════════════════════════════════════════════════════════",
        "#  Variational Omni Trading Bot — Configuration",
        "#  Сгенерировано через GUI",
        "# ══════════════════════════════════════════════════════════════",
        "",
    ]
    labels = {f[0]: f[1] for f in CONFIG_FIELDS}
    for key, val in vals.items():
        label = labels.get(key, key)
        lines.append(f"# {label}")
        lines.append(f"{key} = {val}")
        lines.append("")
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")


# ════════════════════════════════════════════════════════════
#  Stat Card Widget
# ════════════════════════════════════════════════════════════

class StatCard(ctk.CTkFrame):
    """Small card showing a single metric."""

    def __init__(self, master, title: str, value: str = "—", color: str = C_ACCENT, **kw):
        super().__init__(master, fg_color=C_CARD, corner_radius=12, border_width=1,
                         border_color=C_BORDER, **kw)

        self._title_lbl = ctk.CTkLabel(
            self, text=title, font=ctk.CTkFont(size=11, weight="normal"),
            text_color=C_MUTED, anchor="w",
        )
        self._title_lbl.pack(padx=14, pady=(10, 0), anchor="w")

        self._value_lbl = ctk.CTkLabel(
            self, text=value, font=ctk.CTkFont(family="JetBrains Mono", size=22, weight="bold"),
            text_color=color, anchor="w",
        )
        self._value_lbl.pack(padx=14, pady=(2, 12), anchor="w")

    def set(self, value: str, color: str | None = None):
        self._value_lbl.configure(text=value)
        if color:
            self._value_lbl.configure(text_color=color)


# ════════════════════════════════════════════════════════════
#  Main Application
# ════════════════════════════════════════════════════════════

class VariationalBotApp(ctk.CTk):
    """Main application window."""

    def __init__(self):
        super().__init__()

        self.title("⚡ Variational Omni Trading Bot")
        self.geometry("1100x750")
        self.minsize(900, 600)
        self.configure(fg_color=C_BG)

        # State
        self._bot_thread: threading.Thread | None = None
        self._bot_instance = None
        self._running = False
        self._paused = False
        self._log_queue: queue.Queue = queue.Queue()
        self._config_entries: dict[str, ctk.CTkEntry | ctk.CTkComboBox] = {}
        self._stats = {
            "cycles": 0, "wins": 0, "losses": 0, "pnl": 0.0,
            "volume": 0.0, "uptime_start": None,
        }

        self._build_ui()
        self._load_config_to_ui()
        self._poll_log_queue()

    # ─── UI Construction ────────────────────────────────────

    def _build_ui(self):
        # ── Top bar ──
        top = ctk.CTkFrame(self, fg_color=C_CARD, corner_radius=0, height=56)
        top.pack(fill="x", padx=0, pady=0)
        top.pack_propagate(False)

        logo_lbl = ctk.CTkLabel(
            top, text="⚡  Variational Bot",
            font=ctk.CTkFont(size=18, weight="bold"), text_color=C_ACCENT,
        )
        logo_lbl.pack(side="left", padx=20)

        self._status_lbl = ctk.CTkLabel(
            top, text="● ОСТАНОВЛЕН", font=ctk.CTkFont(size=13, weight="bold"),
            text_color=C_RED,
        )
        self._status_lbl.pack(side="right", padx=20)

        self._uptime_lbl = ctk.CTkLabel(
            top, text="", font=ctk.CTkFont(size=12), text_color=C_MUTED,
        )
        self._uptime_lbl.pack(side="right", padx=10)

        # ── Main content: left sidebar + right area ──
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=12, pady=12)

        # Sidebar (tabs)
        sidebar = ctk.CTkFrame(content, fg_color=C_CARD, width=200, corner_radius=12,
                               border_width=1, border_color=C_BORDER)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        sidebar.pack_propagate(False)

        nav_label = ctk.CTkLabel(sidebar, text="НАВИГАЦИЯ", font=ctk.CTkFont(size=10, weight="bold"),
                                 text_color=C_MUTED)
        nav_label.pack(padx=16, pady=(16, 8), anchor="w")

        self._tab_buttons: list[ctk.CTkButton] = []
        tabs = [
            ("📊  Дашборд", self._show_dashboard),
            ("⚙️  Настройки", self._show_settings),
            ("📜  Логи", self._show_logs),
        ]
        for text, cmd in tabs:
            btn = ctk.CTkButton(
                sidebar, text=text, fg_color="transparent", text_color=C_TEXT,
                hover_color="#21262d", anchor="w", height=40,
                font=ctk.CTkFont(size=13), corner_radius=8,
                command=cmd,
            )
            btn.pack(fill="x", padx=8, pady=2)
            self._tab_buttons.append(btn)

        # Separator
        sep = ctk.CTkFrame(sidebar, fg_color=C_BORDER, height=1)
        sep.pack(fill="x", padx=16, pady=12)

        # Control buttons in sidebar
        ctrl_label = ctk.CTkLabel(sidebar, text="УПРАВЛЕНИЕ", font=ctk.CTkFont(size=10, weight="bold"),
                                  text_color=C_MUTED)
        ctrl_label.pack(padx=16, pady=(4, 8), anchor="w")

        self._start_btn = ctk.CTkButton(
            sidebar, text="▶  Старт", fg_color=C_GREEN, hover_color="#2ea043",
            text_color="#ffffff", height=38, font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8, command=self._on_start,
        )
        self._start_btn.pack(fill="x", padx=8, pady=2)

        self._pause_btn = ctk.CTkButton(
            sidebar, text="⏸  Пауза", fg_color=C_YELLOW, hover_color="#bb8009",
            text_color="#ffffff", height=38, font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8, command=self._on_pause, state="disabled",
        )
        self._pause_btn.pack(fill="x", padx=8, pady=2)

        self._stop_btn = ctk.CTkButton(
            sidebar, text="⏹  Стоп", fg_color=C_RED, hover_color="#da3633",
            text_color="#ffffff", height=38, font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8, command=self._on_stop, state="disabled",
        )
        self._stop_btn.pack(fill="x", padx=8, pady=2)

        # Right panel (page container)
        self._page_container = ctk.CTkFrame(content, fg_color="transparent")
        self._page_container.pack(side="right", fill="both", expand=True)

        # Create pages
        self._dashboard_page = self._create_dashboard_page(self._page_container)
        self._settings_page = self._create_settings_page(self._page_container)
        self._logs_page = self._create_logs_page(self._page_container)

        self._pages = [self._dashboard_page, self._settings_page, self._logs_page]
        self._show_dashboard()

    def _select_tab(self, idx: int):
        for i, btn in enumerate(self._tab_buttons):
            if i == idx:
                btn.configure(fg_color="#21262d")
            else:
                btn.configure(fg_color="transparent")
        for page in self._pages:
            page.pack_forget()
        self._pages[idx].pack(fill="both", expand=True)

    def _show_dashboard(self):
        self._select_tab(0)

    def _show_settings(self):
        self._select_tab(1)

    def _show_logs(self):
        self._select_tab(2)

    # ─── Dashboard page ─────────────────────────────────────

    def _create_dashboard_page(self, parent) -> ctk.CTkFrame:
        page = ctk.CTkFrame(parent, fg_color="transparent")

        # Title
        title = ctk.CTkLabel(page, text="Дашборд", font=ctk.CTkFont(size=20, weight="bold"),
                             text_color=C_TEXT)
        title.pack(anchor="w", pady=(0, 12))

        # Stats row
        stats_row = ctk.CTkFrame(page, fg_color="transparent")
        stats_row.pack(fill="x", pady=(0, 12))

        self._card_cycles = StatCard(stats_row, "Циклы", "0", C_ACCENT)
        self._card_cycles.pack(side="left", fill="both", expand=True, padx=(0, 6))

        self._card_winrate = StatCard(stats_row, "Винрейт", "—", C_GREEN)
        self._card_winrate.pack(side="left", fill="both", expand=True, padx=6)

        self._card_pnl = StatCard(stats_row, "PnL ($)", "$0.00", C_TEXT)
        self._card_pnl.pack(side="left", fill="both", expand=True, padx=6)

        self._card_volume = StatCard(stats_row, "Объём ($)", "$0.00", C_PURPLE)
        self._card_volume.pack(side="left", fill="both", expand=True, padx=(6, 0))

        # Second stats row
        stats_row2 = ctk.CTkFrame(page, fg_color="transparent")
        stats_row2.pack(fill="x", pady=(0, 12))

        self._card_wins = StatCard(stats_row2, "Побед", "0", C_GREEN)
        self._card_wins.pack(side="left", fill="both", expand=True, padx=(0, 6))

        self._card_losses = StatCard(stats_row2, "Поражений", "0", C_RED)
        self._card_losses.pack(side="left", fill="both", expand=True, padx=6)

        self._card_balance = StatCard(stats_row2, "Баланс", "—", C_ACCENT)
        self._card_balance.pack(side="left", fill="both", expand=True, padx=6)

        self._card_uptime = StatCard(stats_row2, "Аптайм", "00:00:00", C_MUTED)
        self._card_uptime.pack(side="left", fill="both", expand=True, padx=(6, 0))

        # Recent trades table
        table_frame = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12,
                                   border_width=1, border_color=C_BORDER)
        table_frame.pack(fill="both", expand=True)

        table_title = ctk.CTkLabel(table_frame, text="  Последние сделки",
                                   font=ctk.CTkFont(size=14, weight="bold"),
                                   text_color=C_TEXT, anchor="w")
        table_title.pack(fill="x", padx=14, pady=(12, 4))

        # Header
        hdr = ctk.CTkFrame(table_frame, fg_color="#1c2128", corner_radius=6)
        hdr.pack(fill="x", padx=10, pady=(4, 0))
        for col, w in [("Время", 140), ("Пара", 90), ("Сторона", 80), ("Вход", 100),
                       ("Выход", 100), ("PnL", 90), ("Результат", 80)]:
            ctk.CTkLabel(hdr, text=col, width=w, font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=C_MUTED, anchor="w").pack(side="left", padx=4, pady=6)

        # Scrollable trade list
        self._trade_list = ctk.CTkScrollableFrame(
            table_frame, fg_color="transparent", corner_radius=0,
        )
        self._trade_list.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._trade_rows: list[ctk.CTkFrame] = []

        return page

    def _add_trade_row(self, ts: str, symbol: str, side: str, entry: str, exit_: str,
                       pnl: str, outcome: str):
        row = ctk.CTkFrame(self._trade_list, fg_color="transparent", height=32)
        row.pack(fill="x", pady=1)

        side_color = C_GREEN if side == "LONG" else C_RED
        outcome_color = C_GREEN if "TP" in outcome else C_RED
        pnl_color = C_GREEN if pnl.startswith("+") or (not pnl.startswith("-") and pnl != "$0.00") else C_RED

        for text, w, color in [
            (ts, 140, C_MUTED), (symbol, 90, C_TEXT), (side, 80, side_color),
            (entry, 100, C_TEXT), (exit_, 100, C_TEXT), (pnl, 90, pnl_color),
            (outcome, 80, outcome_color),
        ]:
            ctk.CTkLabel(row, text=text, width=w, font=ctk.CTkFont(family="JetBrains Mono", size=11),
                         text_color=color, anchor="w").pack(side="left", padx=4)

        self._trade_rows.append(row)
        # Keep last 50
        if len(self._trade_rows) > 50:
            old = self._trade_rows.pop(0)
            old.destroy()

    # ─── Settings page ──────────────────────────────────────

    def _create_settings_page(self, parent) -> ctk.CTkFrame:
        page = ctk.CTkFrame(parent, fg_color="transparent")

        title = ctk.CTkLabel(page, text="Настройки", font=ctk.CTkFont(size=20, weight="bold"),
                             text_color=C_TEXT)
        title.pack(anchor="w", pady=(0, 12))

        # Config card
        card = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12,
                            border_width=1, border_color=C_BORDER)
        card.pack(fill="both", expand=True)

        scroll = ctk.CTkScrollableFrame(card, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=6, pady=6)

        for key, label, default, typ, tooltip in CONFIG_FIELDS:
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=6)

            lbl = ctk.CTkLabel(row, text=label, width=220,
                               font=ctk.CTkFont(size=13), text_color=C_TEXT, anchor="w")
            lbl.pack(side="left")

            entry = ctk.CTkEntry(
                row, width=200, height=34, corner_radius=8,
                fg_color="#0d1117", border_color=C_BORDER, text_color=C_TEXT,
                font=ctk.CTkFont(family="JetBrains Mono", size=13),
                placeholder_text=default,
            )
            entry.pack(side="left", padx=(10, 0))

            hint = ctk.CTkLabel(row, text=tooltip, font=ctk.CTkFont(size=11),
                                text_color=C_MUTED, anchor="w")
            hint.pack(side="left", padx=(12, 0))

            self._config_entries[key] = entry

        # Save button
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(8, 16))

        save_btn = ctk.CTkButton(
            btn_row, text="💾  Сохранить настройки", fg_color=C_ACCENT,
            hover_color="#388bfd", text_color="#ffffff", height=40,
            font=ctk.CTkFont(size=14, weight="bold"), corner_radius=10,
            command=self._on_save_config,
        )
        save_btn.pack(side="left")

        self._save_status = ctk.CTkLabel(btn_row, text="", font=ctk.CTkFont(size=12),
                                         text_color=C_GREEN)
        self._save_status.pack(side="left", padx=16)

        reset_btn = ctk.CTkButton(
            btn_row, text="↺  Сбросить", fg_color="transparent", border_width=1,
            border_color=C_BORDER, hover_color="#21262d", text_color=C_MUTED, height=40,
            font=ctk.CTkFont(size=13), corner_radius=10,
            command=self._load_config_to_ui,
        )
        reset_btn.pack(side="left")

        return page

    # ─── Logs page ──────────────────────────────────────────

    def _create_logs_page(self, parent) -> ctk.CTkFrame:
        page = ctk.CTkFrame(parent, fg_color="transparent")

        top_row = ctk.CTkFrame(page, fg_color="transparent")
        top_row.pack(fill="x", pady=(0, 8))

        title = ctk.CTkLabel(top_row, text="Логи", font=ctk.CTkFont(size=20, weight="bold"),
                             text_color=C_TEXT)
        title.pack(side="left")

        clear_btn = ctk.CTkButton(
            top_row, text="🗑 Очистить", fg_color="transparent", border_width=1,
            border_color=C_BORDER, hover_color="#21262d", text_color=C_MUTED,
            width=120, height=32, font=ctk.CTkFont(size=12), corner_radius=8,
            command=self._clear_logs,
        )
        clear_btn.pack(side="right")

        self._autoscroll_var = ctk.BooleanVar(value=True)
        auto_cb = ctk.CTkCheckBox(
            top_row, text="Автоскролл", variable=self._autoscroll_var,
            font=ctk.CTkFont(size=12), text_color=C_MUTED, height=28,
            checkbox_width=18, checkbox_height=18,
        )
        auto_cb.pack(side="right", padx=12)

        # Log text area
        log_frame = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12,
                                 border_width=1, border_color=C_BORDER)
        log_frame.pack(fill="both", expand=True)

        self._log_text = ctk.CTkTextbox(
            log_frame, fg_color="#010409", text_color=C_TEXT,
            font=ctk.CTkFont(family="JetBrains Mono", size=12),
            corner_radius=10, wrap="word", state="disabled",
            scrollbar_button_color=C_BORDER,
        )
        self._log_text.pack(fill="both", expand=True, padx=6, pady=6)

        # Configure colored tags
        self._log_text._textbox.tag_configure("INFO", foreground=C_TEXT)
        self._log_text._textbox.tag_configure("DEBUG", foreground=C_MUTED)
        self._log_text._textbox.tag_configure("WARNING", foreground=C_YELLOW)
        self._log_text._textbox.tag_configure("ERROR", foreground=C_RED)
        self._log_text._textbox.tag_configure("CRITICAL", foreground=C_RED)

        return page

    # ─── Config I/O ─────────────────────────────────────────

    def _load_config_to_ui(self):
        vals = load_config_values()
        for key, label, default, typ, tooltip in CONFIG_FIELDS:
            entry = self._config_entries[key]
            entry.delete(0, "end")
            entry.insert(0, vals.get(key, default))

    def _on_save_config(self):
        vals = {}
        for key, label, default, typ, tooltip in CONFIG_FIELDS:
            vals[key] = self._config_entries[key].get().strip() or default
        save_config_values(vals)
        self._save_status.configure(text="✓ Сохранено!", text_color=C_GREEN)
        self.after(3000, lambda: self._save_status.configure(text=""))

    # ─── Bot controls ───────────────────────────────────────

    def _on_start(self):
        if self._running:
            return

        # Save config first
        self._on_save_config()

        self._running = True
        self._paused = False
        self._stats["uptime_start"] = time.time()
        self._update_status("РАБОТАЕТ", C_GREEN)
        self._start_btn.configure(state="disabled")
        self._pause_btn.configure(state="normal")
        self._stop_btn.configure(state="normal")

        # Disable config editing while running
        for entry in self._config_entries.values():
            entry.configure(state="disabled")

        self._bot_thread = threading.Thread(target=self._run_bot, daemon=True)
        self._bot_thread.start()
        self._update_uptime_loop()

    def _on_pause(self):
        if not self._running:
            return
        self._paused = not self._paused
        if self._paused:
            self._pause_btn.configure(text="▶  Продолжить", fg_color=C_GREEN, hover_color="#2ea043")
            self._update_status("ПАУЗА", C_YELLOW)
        else:
            self._pause_btn.configure(text="⏸  Пауза", fg_color=C_YELLOW, hover_color="#bb8009")
            self._update_status("РАБОТАЕТ", C_GREEN)

    def _on_stop(self):
        if not self._running:
            return
        self._running = False
        self._paused = False
        if self._bot_instance:
            self._bot_instance._running = False
        self._update_status("ОСТАНОВЛЕН", C_RED)
        self._start_btn.configure(state="normal")
        self._pause_btn.configure(state="disabled", text="⏸  Пауза",
                                  fg_color=C_YELLOW, hover_color="#bb8009")
        self._stop_btn.configure(state="disabled")

        for entry in self._config_entries.values():
            entry.configure(state="normal")

    def _update_status(self, text: str, color: str):
        self._status_lbl.configure(text=f"●  {text}", text_color=color)

    def _update_uptime_loop(self):
        if not self._running:
            return
        start = self._stats.get("uptime_start")
        if start:
            elapsed = int(time.time() - start)
            h, m = divmod(elapsed, 3600)
            m, s = divmod(m, 60)
            self._card_uptime.set(f"{h:02d}:{m:02d}:{s:02d}")
            self._uptime_lbl.configure(text=f"⏱ {h:02d}:{m:02d}:{s:02d}")
        self.after(1000, self._update_uptime_loop)

    # ─── Bot thread ─────────────────────────────────────────

    def _run_bot(self):
        """Run the bot in a background thread, feeding logs to the GUI."""
        try:
            # Re-import config to pick up saved values
            import importlib
            import config as cfg_mod
            importlib.reload(cfg_mod)
            from config import BotConfig

            bot_config = BotConfig()
            issues = bot_config.validate()
            if issues:
                for issue in issues:
                    self._log_queue.put((logging.ERROR, f"Config error: {issue}"))
                self._running = False
                self.after(0, self._on_stop)
                return

            # Setup logging with queue handler
            from logger import setup_logging
            bot_logger = setup_logging(bot_config.logging)

            # Add our queue handler
            q_handler = QueueLogHandler(self._log_queue)
            q_handler.setFormatter(logging.Formatter(
                "%(asctime)s │ %(levelname)-8s │ %(message)s", "%H:%M:%S"
            ))
            bot_logger.addHandler(q_handler)

            # Also capture trade records logger
            trade_logger = logging.getLogger("trade_records")
            trade_q = QueueLogHandler(self._log_queue)
            trade_q.setFormatter(logging.Formatter("%(message)s"))
            trade_logger.addHandler(trade_q)

            # Create bot
            from bot import TradingBot
            import bot as bot_module
            bot = TradingBot(bot_config)
            self._bot_instance = bot

            # Assign logger in bot.py module namespace (normally set inside bot.run())
            bot_module.logger = bot_logger
            bot._running = True

            bot_logger.info("=" * 50)
            bot_logger.info("  Variational Bot — Started from GUI")
            bot_logger.info("=" * 50)

            bot._initialise()

            while self._running:
                # Pause logic
                while self._paused and self._running:
                    time.sleep(0.5)

                if not self._running:
                    break

                try:
                    bot._run_one_cycle()
                    self._update_stats_from_risk(bot)
                except Exception as exc:
                    bot_logger.exception("Error in cycle: %s", exc)
                    if bot._risk_mgr and bot._risk_mgr.record_error():
                        bot_logger.critical("Circuit breaker tripped")
                        break
                    time.sleep(bot_config.trading.cycle_cooldown)

            bot._shutdown()

        except Exception as exc:
            self._log_queue.put((logging.CRITICAL, f"Bot crashed: {exc}"))
        finally:
            self._running = False
            self.after(0, self._on_stop)

    def _update_stats_from_risk(self, bot):
        """Pull stats from the risk manager and update dashboard cards."""
        rm = bot._risk_mgr
        if not rm:
            return

        cycles = rm._total_trades
        wins = rm._total_wins
        losses = rm._total_losses
        pnl = rm._cumulative_pnl

        self._stats["cycles"] = cycles
        self._stats["wins"] = wins
        self._stats["losses"] = losses
        self._stats["pnl"] = pnl

        # Schedule UI update on main thread
        self.after(0, self._refresh_dashboard_cards)

    def _refresh_dashboard_cards(self):
        s = self._stats
        self._card_cycles.set(str(s["cycles"]))
        self._card_wins.set(str(s["wins"]))
        self._card_losses.set(str(s["losses"]))

        pnl = s["pnl"]
        pnl_str = f"${pnl:+.4f}"
        self._card_pnl.set(pnl_str, C_GREEN if pnl >= 0 else C_RED)

        total = s["wins"] + s["losses"]
        if total > 0:
            wr = s["wins"] / total * 100
            self._card_winrate.set(f"{wr:.1f}%", C_GREEN if wr >= 50 else C_RED)

        # Volume is roughly cycles * position_size
        try:
            vol = s["cycles"] * float(self._config_entries["position_size_usdc"].get() or "0")
        except Exception:
            vol = 0
        self._card_volume.set(f"${vol:,.0f}", C_PURPLE)

    # ─── Log queue consumer ─────────────────────────────────

    def _poll_log_queue(self):
        """Drain the log queue and append to the text widget."""
        count = 0
        while count < 50:  # process up to 50 messages per tick
            try:
                level, msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            count += 1

            # Determine tag
            if level >= logging.CRITICAL:
                tag = "CRITICAL"
            elif level >= logging.ERROR:
                tag = "ERROR"
            elif level >= logging.WARNING:
                tag = "WARNING"
            elif level >= logging.DEBUG and level < logging.INFO:
                tag = "DEBUG"
            else:
                tag = "INFO"

            self._log_text.configure(state="normal")
            self._log_text._textbox.insert("end", msg + "\n", tag)
            self._log_text.configure(state="disabled")

            # Parse trade records for dashboard table
            if "CLOSED" in msg and "entry=" in msg:
                self._parse_trade_log(msg)

            if self._autoscroll_var.get():
                self._log_text.see("end")

        self.after(100, self._poll_log_queue)

    def _parse_trade_log(self, msg: str):
        """Try to extract trade info from log line for the trades table."""
        try:
            # Format: "=== CLOSED ===  TP_HIT  entry=$X  exit=$Y  PnL=$+Z  time=Ws"
            parts = msg.split("CLOSED")[1] if "CLOSED" in msg else ""
            outcome = "TP" if "TP_HIT" in parts else "SL"

            entry_str = ""
            exit_str = ""
            pnl_str = ""
            for token in parts.split():
                if token.startswith("entry=$"):
                    entry_str = token.split("=")[1]
                elif token.startswith("exit=$"):
                    exit_str = token.split("=")[1]
                elif token.startswith("PnL=$"):
                    pnl_str = token.split("=")[1]

            now = datetime.now().strftime("%H:%M:%S")
            symbol = self._config_entries.get("symbol")
            sym_text = symbol.get() if symbol else "BTC-USDC"

            # Determine side from context (rough — check PnL sign vs price move)
            side = "—"
            try:
                e = float(entry_str.replace("$", ""))
                x = float(exit_str.replace("$", ""))
                p = float(pnl_str.replace("$", "").replace("+", ""))
                if p > 0:
                    side = "LONG" if x > e else "SHORT"
                else:
                    side = "SHORT" if x > e else "LONG"
            except Exception:
                pass

            self._add_trade_row(now, sym_text, side, entry_str, exit_str, pnl_str, outcome)
        except Exception:
            pass

    def _clear_logs(self):
        self._log_text.configure(state="normal")
        self._log_text._textbox.delete("1.0", "end")
        self._log_text.configure(state="disabled")


# ════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════

def main():
    app = VariationalBotApp()
    app.mainloop()


if __name__ == "__main__":
    main()
