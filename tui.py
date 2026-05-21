"""
NP4M Textual TUI.

Single-screen dashboard that talks to a locally-running NP4M Flask
process over HTTP. Whatever mode the process is in, the TUI:
- shows the live mode in the header,
- offers an `m` keybinding (or "Switch mode" button) to flip MASTER <-> PROBE,
- renders a mode-appropriate panel underneath (probes table in master mode,
  health + recent activity in probe mode),
- polls the relevant endpoints every few seconds so the user can leave
  it open during a test run.

Run it after starting the Flask app:

    python app.py        # in one terminal
    python tui.py        # in another

By default it talks to http://127.0.0.1:5000. Override via NP4M_URL.
"""

from __future__ import annotations

import os

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "tui.py requires `requests`. Run: python -m pip install -r requirements.txt"
    ) from exc

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.reactive import reactive
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Label,
        Log,
        Static,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "tui.py requires `textual`. Run: python -m pip install textual"
    ) from exc


NP4M_URL = os.environ.get("NP4M_URL", "http://127.0.0.1:5000").rstrip("/")
REQ_TIMEOUT = 5.0


def _get(path: str) -> dict | None:
    try:
        r = requests.get(NP4M_URL + path, timeout=REQ_TIMEOUT)
        if r.status_code >= 400:
            return None
        return r.json()
    except requests.RequestException:
        return None


def _post(path: str, body: dict | None = None) -> dict | None:
    try:
        r = requests.post(NP4M_URL + path, json=body or {}, timeout=REQ_TIMEOUT)
        if r.status_code >= 400:
            return None
        return r.json()
    except requests.RequestException:
        return None


class ModePanel(Static):
    """Top-of-screen banner that shows the current mode + connection URL."""

    mode: reactive[str] = reactive("unknown")
    build: reactive[str] = reactive("?")
    error: reactive[str] = reactive("")

    def render(self) -> str:
        if self.error:
            return f"[bold red]NP4M @ {NP4M_URL}: {self.error}[/]"
        if self.mode == "master":
            color = "deep_sky_blue1"
        elif self.mode == "probe":
            color = "dark_orange"
        else:
            color = "white"
        return (
            f"NP4M [bold {color}]MODE: {self.mode.upper()}[/] "
            f"build {self.build}    @ {NP4M_URL}\n"
            f"press [bold]m[/] to switch mode  ·  [bold]r[/] to refresh  ·  [bold]q[/] to quit"
        )


class Np4mTui(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    ModePanel {
        height: 3;
        padding: 0 1;
        background: $boost;
        border: tall $accent;
    }
    #body {
        height: 1fr;
    }
    DataTable {
        height: 1fr;
    }
    Log {
        height: 1fr;
        background: black;
    }
    #footer-actions {
        height: 3;
        padding: 0 1;
    }
    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("m", "flip_mode", "Switch mode"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._mode_panel = ModePanel()
        self._title_label = Label("Loading...", id="title")
        self._table = DataTable(zebra_stripes=True, cursor_type="row")
        self._log = Log(highlight=True)
        self._current_mode: str = "unknown"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield self._mode_panel
        yield self._title_label
        with Vertical(id="body"):
            yield self._table
            yield self._log
        with Horizontal(id="footer-actions"):
            yield Button("Switch mode (m)", id="btn-mode")
            yield Button("Refresh (r)", id="btn-refresh", variant="primary")
            yield Button("Quit (q)", id="btn-quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "NP4M"
        self.sub_title = NP4M_URL
        self.refresh_state()
        # Poll the server every 4s so the TUI reflects a remote mode-flip
        # (e.g. someone clicked the pill in the Web UI) without manual reload.
        self.set_interval(4.0, self.refresh_state)

    # ------------------------------------------------------------------ actions

    def action_refresh(self) -> None:
        self.refresh_state()

    def action_flip_mode(self) -> None:
        next_mode = "probe" if self._current_mode == "master" else "master"
        result = _post("/api/mode", {"mode": next_mode})
        if result and result.get("mode"):
            self._log.write_line(f"[mode] switched to {result['mode'].upper()}")
            self.refresh_state()
        else:
            self._log.write_line(f"[mode] flip to {next_mode} failed")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-mode":
            self.action_flip_mode()
        elif event.button.id == "btn-refresh":
            self.action_refresh()
        elif event.button.id == "btn-quit":
            self.exit()

    # ------------------------------------------------------------------ render

    def refresh_state(self) -> None:
        info = _get("/api/version")
        if info is None:
            self._mode_panel.error = "unreachable"
            self._mode_panel.mode = "unknown"
            self._mode_panel.build = "?"
            self._title_label.update("Could not reach NP4M.")
            return
        self._mode_panel.error = ""
        mode = (info.get("mode") or "master").lower()
        self._current_mode = mode
        self._mode_panel.mode = mode
        self._mode_panel.build = f"v{info.get('version', '?')} build {info.get('build', '?')}"

        if mode == "master":
            self._render_master()
        else:
            self._render_probe()

    def _render_master(self) -> None:
        self._title_label.update("[b]Master mode[/] — registered probes")
        probes = _get("/api/probes")
        self._table.clear(columns=True)
        self._table.add_columns("Name", "Mgmt", "Health", "Test iface", "VM UUID", "NIC ext id")
        if not probes or not probes.get("probes"):
            self._table.add_row("(no probes)", "—", "—", "—", "—", "—")
            return
        for p in probes["probes"]:
            scheme = "https" if p.get("use_https") else "http"
            mgmt = f"{scheme}://{p.get('mgmt_host')}:{p.get('mgmt_port')}"
            h = p.get("health") or {}
            health = "OK" if h.get("reachable") else f"down ({h.get('error', '?')})"
            test_iface = h.get("test_iface") or "—"
            vm_uuid = (p.get("probe_vm_uuid") or "—")[:18]
            nic_id = (p.get("test_nic_ext_id") or "—")[:18]
            self._table.add_row(
                str(p.get("name") or "?"),
                mgmt,
                health,
                test_iface,
                vm_uuid,
                nic_id,
            )

    def _render_probe(self) -> None:
        self._title_label.update("[b]Probe mode[/] — local health + recent activity")
        health = _get("/probe/health")
        self._table.clear(columns=True)
        self._table.add_columns("Property", "Value")
        if not health:
            self._table.add_row("status", "unreachable")
            return
        rows = [
            ("hostname", str(health.get("hostname", "?"))),
            ("mgmt_iface", f"{health.get('mgmt_iface') or '?'} ({health.get('mgmt_ip') or '—'})"),
            ("test_iface", f"{health.get('test_iface') or '?'} ({health.get('test_ip') or '—'})"),
            ("auth_required", "yes" if health.get("auth_required") else "no (open)"),
            ("interfaces", ", ".join(
                f"{it.get('name')}={it.get('ipv4') or '—'}"
                for it in (health.get("interfaces") or [])
            )),
        ]
        for k, v in rows:
            self._table.add_row(k, v)

        logs_payload = _get("/probe/logs?limit=50")
        if logs_payload and logs_payload.get("logs"):
            # Clear and re-append: textual.Log doesn't have a deduplicated
            # tail mode, so just rewrite the whole pane.
            self._log.clear()
            for entry in logs_payload["logs"][-50:]:
                level = entry.get("level", "info").upper()
                msg = entry.get("msg", "")
                self._log.write_line(f"[{level}] {msg}")


def main() -> None:
    Np4mTui().run()


if __name__ == "__main__":
    main()
