"""Service installation helpers for systemd and launchd."""

from __future__ import annotations

import getpass
import textwrap
from pathlib import Path


def generate_systemd_unit(daemon_path: str, user: str) -> str:
    """Generate systemd service unit file content.

    Args:
        daemon_path: Absolute path to the bog-agents-daemon executable.
        user: The system user to run the service as.

    Returns:
        A string containing the full .service file content.
    """
    return textwrap.dedent(f"""\
        [Unit]
        Description=Bog Agents Daemon — ambient agent service
        Documentation=https://bogware.com/docs/daemon
        After=network.target

        [Service]
        Type=simple
        User={user}
        ExecStart={daemon_path}
        Restart=on-failure
        RestartSec=10
        StandardOutput=journal
        StandardError=journal
        SyslogIdentifier=bog-agents-daemon
        Environment=HOME={Path.home()}

        [Install]
        WantedBy=default.target
    """)


def generate_launchd_plist(
    daemon_path: str,
    label: str = "com.bogware.bog-agents-daemon",
) -> str:
    """Generate a launchd plist file for macOS.

    Args:
        daemon_path: Absolute path to the bog-agents-daemon executable.
        label: The launchd service label (reverse-DNS style).

    Returns:
        A string containing the full .plist XML content.
    """
    log_dir = Path.home() / ".bog-agents" / "daemon"
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
            "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{label}</string>

            <key>ProgramArguments</key>
            <array>
                <string>{daemon_path}</string>
            </array>

            <key>RunAtLoad</key>
            <true/>

            <key>KeepAlive</key>
            <true/>

            <key>StandardOutPath</key>
            <string>{log_dir}/daemon.stdout.log</string>

            <key>StandardErrorPath</key>
            <string>{log_dir}/daemon.stderr.log</string>

            <key>EnvironmentVariables</key>
            <dict>
                <key>HOME</key>
                <string>{Path.home()}</string>
            </dict>
        </dict>
        </plist>
    """)


def install_systemd(daemon_path: str) -> str:
    """Install the daemon as a systemd user service.

    Writes the service unit to `~/.config/systemd/user/bog-agents-daemon.service`
    and returns enable/start instructions.

    Args:
        daemon_path: Absolute path to the bog-agents-daemon executable.

    Returns:
        A multi-line string with instructions to enable and start the service.
    """
    user = getpass.getuser()
    unit_content = generate_systemd_unit(daemon_path, user)

    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "bog-agents-daemon.service"
    service_file.write_text(unit_content, encoding="utf-8")

    return textwrap.dedent(f"""\
        Service unit written to: {service_file}

        Enable and start with:
          systemctl --user daemon-reload
          systemctl --user enable bog-agents-daemon
          systemctl --user start bog-agents-daemon

        Check status with:
          systemctl --user status bog-agents-daemon

        View logs with:
          journalctl --user -u bog-agents-daemon -f
    """)


def install_launchd(
    daemon_path: str,
    label: str = "com.bogware.bog-agents-daemon",
) -> str:
    """Install the daemon as a launchd user agent on macOS.

    Writes the plist to `~/Library/LaunchAgents/<label>.plist` and returns
    load instructions.

    Args:
        daemon_path: Absolute path to the bog-agents-daemon executable.
        label: The launchd service label.

    Returns:
        A multi-line string with instructions to load the agent.
    """
    plist_content = generate_launchd_plist(daemon_path, label)

    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_file = agents_dir / f"{label}.plist"
    plist_file.write_text(plist_content, encoding="utf-8")

    return textwrap.dedent(f"""\
        LaunchAgent plist written to: {plist_file}

        Load and start with:
          launchctl load {plist_file}

        Check status with:
          launchctl list {label}

        Stop with:
          launchctl unload {plist_file}

        The daemon will start automatically at login.
    """)
