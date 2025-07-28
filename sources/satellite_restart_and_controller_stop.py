#!/usr/bin/env python3

from __future__ import print_function

import argparse
import atexit
import getpass
import os
import sys

import XenAPI

SCRIPT_NAME = "satellite_restart_and_controller_stop"
SERVICE_PLUGIN = "service.py"


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
        return cls._instances[cls]


class HostRecs(metaclass=SingletonMeta):
    hosts_recs = None

    def get_host(self, session, ref):
        if self.hosts_recs is None:
            self.hosts_recs = session.xenapi.host.get_all_records()
        return self.hosts_recs[ref]


class PBDRecs(metaclass=SingletonMeta):
    pbd_recs = None

    def get_pbd(self, session, ref):
        if self.pbd_recs is None:
            self.pbd_recs = session.xenapi.PBD.get_all_records()
        return self.pbd_recs[ref]


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def get_password():
    return os.getenv("XENAPI_PASSWORD") or (
        getpass.getpass("XenAPI password: ") if sys.stdin.isatty()
        else sys.stdin.readline().strip()
    )


def prompt(interactive, fn, message, default, *args, **kwargs):
    if not interactive:
        return fn(*args, **kwargs)

    if default is None:
        yn = " [y/n] "
    elif default in ("y", "yes"):
        yn = " [Y/n] "
    elif default in ("n", "no"):
        yn = " [y/N] "
    else:
        raise ValueError("invalid default answer: '%s'" % default)

    while True:
        print("{} {}".format(message, yn), end="")
        choice = input().lower()
        if choice == "" and default is not None:
            choice = default.lower()
        if choice in ("y", "yes"):
            return fn(*args, **kwargs)
        elif choice in ("n", "no"):
            return None
        else:
            print("Please respond with 'yes' or 'no' (or 'y' or 'n').")


def call_service_action(
    session, host_ref, action, service, dry_run=False
):
    ret = None
    print("{} {}".format(action, service))
    if dry_run:
        return ret
    try:
        ret = session.xenapi.host.call_plugin(
            host_ref, SERVICE_PLUGIN, "{}_service".format(action), {"service": service}
        )
    except XenAPI.Failure as e:
        if "XENAPI_MISSING_PLUGIN" in e.details:
            eprint("[ERROR] service plugin missing. you must {} {} manually".format(
                action, service
            ))
        else:
            raise e
    return ret


class HostInfo:
    LINSTOR_PLUGIN = "linstor-manager"

    def __init__(self, session, ref):
        self.ref = ref

        host_rec = HostRecs().get_host(session, ref)
        self.hostname = host_rec["hostname"]
        self.uuid = host_rec["uuid"]
        self.has_controller_running = (
            session.xenapi.host.call_plugin(
                ref, self.LINSTOR_PLUGIN, "hasControllerRunning", {}
            ) == "True"
        )

    def __repr__(self):
        return "Host: [{}] {} ({})".format(
            "C" if self.has_controller_running else "S", self.hostname, self.uuid
        )

    def stop_controller(self, session, dry_run, interactive, default="y"):
        prompt(
            interactive,
            call_service_action,
            "Do you want to stop linstor-controller on this host?",
            default,
            session, self.ref, "stop", "linstor-controller", dry_run
        )

    def restart_satellite(self, session, dry_run, interactive, default="y"):
        prompt(
            interactive,
            call_service_action,
            "Do you want to restart linstor-satellite on this host?",
            default,
            session, self.ref, "try_restart", "linstor-satellite", dry_run
        )

    def stop_drbd(self, session, dry_run, interactive, default="y"):
        prompt(
            interactive,
            call_service_action,
            "Do you want to stop drbd-reactor?",
            default,
            session, self.ref, "stop", "drbd-reactor", dry_run
        )

    def start_drbd(self, session, dry_run, interactive, default="y"):
        prompt(
            interactive,
            call_service_action,
            "Do you want to start drbd-reactor?",
            default,
            session, self.ref, "start", "drbd-reactor", dry_run
        )


def main(
    interactive,
    dry_run,
    ssl,
    uri,
    stop_controller,
    restart_satellites,
    stop_drbd,
    start_drbd,
):
    session_factory = (
        (lambda: XenAPI.xapi_local()) if uri == "local"
        else lambda: XenAPI.Session(uri, ignore_ssl=not ssl)
    )

    try:
        session = session_factory()
        session.xenapi.login_with_password(
            "root", "" if uri == "local" else get_password(), "", SCRIPT_NAME
        )
        atexit.register(session.xenapi.session.logout)
        hosts = set()
        for _sr_ref, sr_rec in session.xenapi.SR.get_all_records_where('field "type" = "linstor"').items():
            print("SR: {} ({})".format(sr_rec["name_label"], sr_rec["uuid"],))
            for pbd_ref in sr_rec["PBDs"]:
                host_ref = PBDRecs().get_pbd(session, pbd_ref)["host"]
                hosts.add(HostInfo(session, host_ref))
        hosts = sorted(hosts, key=lambda host: host.uuid)
        if stop_drbd:
            for host in hosts:
                print(host)
                host.stop_drbd(session, dry_run, interactive)
        for host in hosts:
            print(host)
            if host.has_controller_running and (stop_controller or interactive):
                host.stop_controller(
                    session, dry_run, interactive,
                    default=("y" if stop_controller else "n"),
                )
            if restart_satellites or interactive:
                host.restart_satellite(
                    session, dry_run, interactive,
                    default=("y" if restart_satellites else "n"),
                )
        if start_drbd:
            for host in hosts:
                print(host)
                host.start_drbd(session, dry_run, interactive)
    except FileNotFoundError as e:
        if uri == "local":
            eprint("[ERROR] could not find a running XenAPI on this host")
            return e
        raise e
    except ConnectionRefusedError as e:
        eprint("[ERROR] could not connect to XenAPI host ({}): {}".format(uri, e))
        return e
    except XenAPI.Failure as e:
        eprint("[ERROR] XenAPI: {}".format(e))
        raise e
    except Exception as e:
        eprint("[ERROR]: {}".format(e))
        raise e


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", type=str, default="local")
    parser.add_argument("--ssl", action="store_true", default=False)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--interactive", action="store_true", default=False)
    parser.add_argument("--stop-drbd", action="store_true", default=False)
    parser.add_argument("--start-drbd", action="store_true", default=False)
    parser.add_argument("--no-stop-controller", dest="stop_controller", action="store_false", default=True)
    parser.add_argument("--no-restart-satellites", dest="restart_satellites", action="store_false", default=True)
    args = parser.parse_args()
    sys.exit(main(
        interactive=args.interactive,
        dry_run=args.dry_run,
        ssl=args.ssl,
        uri=args.uri,
        stop_controller=args.stop_controller,
        stop_drbd=args.stop_drbd,
        start_drbd=args.start_drbd,
        restart_satellites=args.restart_satellites
    ))
