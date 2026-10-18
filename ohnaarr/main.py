"""Entry point: service loop with the web interface, plus the `check` / `explain` helpers."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from types import FrameType

from . import normalize, web
from .config import Config
from .service import Service
from .sonarr import SonarrClient, SonarrError

log = logging.getLogger("ohnaarr")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _install_signal_handlers(service: Service) -> None:
    def handler(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s received, shutting down", signum)
        service.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    once = cfg.run_once or args.once

    # Without a web interface there is nothing to configure things with, so an
    # incomplete config is fatal. With one, we start and wait for the setup form.
    if not cfg.configured and (once or not cfg.http_port):
        for problem in cfg.validate():
            log.error("config: %s", problem)
        log.error("set SONARR_URL and SONARR_API_KEY, or enable the web interface (HTTP_PORT)")
        return 2

    service = Service(cfg)
    _install_signal_handlers(service)

    server = None
    if cfg.http_port:
        try:
            server = web.serve(cfg.http_port, service)
        except OSError as exc:
            log.error("cannot bind the web interface to port %d: %s", cfg.http_port, exc)
            if not cfg.configured:
                service.close()
                return 2

    if not cfg.configured:
        log.warning("not configured yet - open http://<host>:%d to set it up", cfg.http_port)

    try:
        service.loop(once=once)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        service.close()
    return 0


def cmd_check(cfg: Config, _args: argparse.Namespace) -> int:
    """Connectivity and queue overview; no changes made."""
    problems = cfg.validate()
    if problems:
        for problem in problems:
            log.error("config: %s", problem)
        return 2
    with SonarrClient(
        cfg.base_url, cfg.api_key, timeout=cfg.request_timeout, verify=cfg.verify_tls
    ) as client:
        info = client.system_status()
        print(f"Sonarr {info.get('version')} @ {cfg.base_url}")
        queue = client.queue()
        print(f"queue: {len(queue)} item(s)")
        for item in queue:
            state = item.get("trackedDownloadState")
            status = item.get("trackedDownloadStatus")
            flag = "STUCK" if status in ("warning", "error") else "ok   "
            print(f"  [{flag}] {state:<14} {normalize.repair(str(item.get('title') or ''))}")
    return 0


def cmd_explain(cfg: Config, args: argparse.Namespace) -> int:
    """Show how a given release name would be interpreted."""
    if args.offline:
        cfg.api_key = ""  # skip the Sonarr lookups
    service = Service(cfg)
    try:
        print(json.dumps(service.explain(args.name), indent=2, ensure_ascii=False))
    finally:
        service.close()
    return 0


def cmd_settings(cfg: Config, _args: argparse.Namespace) -> int:
    """Print the effective configuration and where each value came from."""
    print(json.dumps({"values": cfg.snapshot(), "sources": cfg.sources}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ohnaarr",
        description="Automatic repair of stuck Sonarr imports (German umlauts / naming).",
    )
    parser.set_defaults(func=cmd_run, once=False)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="service loop with the web interface (default)")
    run.add_argument("--once", action="store_true", help="single pass, then exit")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="connectivity + queue overview")
    check.set_defaults(func=cmd_check)

    explain = sub.add_parser("explain", help="show how a release name is interpreted")
    explain.add_argument("name")
    explain.add_argument("--offline", action="store_true", help="skip Sonarr lookups")
    explain.set_defaults(func=cmd_explain)

    settings = sub.add_parser("settings", help="print the effective configuration")
    settings.set_defaults(func=cmd_settings)

    args = parser.parse_args(argv)
    cfg = Config.from_env()
    _configure_logging(cfg.log_level)
    try:
        return int(args.func(cfg, args))
    except SonarrError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
