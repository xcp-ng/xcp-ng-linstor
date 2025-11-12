import argparse
import json
import logging
import os
import shlex
import subprocess
from functools import lru_cache

from typing import Any, Dict, Iterator, List, Optional, Set, Union

SCRIPT_NAME = "drbd_resource_repair"
DRBD_STATUS = "drbdsetup status all --json"
DRBD_VERIFY = "drbdadm verify {resource}:{peer}/0"
DRBD_WAIT_SYNC = "drbdadm wait-sync {resource}"
DRBD_INVALIDATE = "drbdadm invalidate {resource}:{peer}/0 --reset-bitmap=no"
DRBD_INVALIDATE_REMOTE = (
    "drbdadm invalidate-remote {resource}:{peer}/0 --reset-bitmap=no"
)


def run_command(cmd: str, ignore_dry_run: bool = False, remote_host: str = "") -> str:
    if remote_host:
        cmd = f"ssh root@{remote_host} -C {cmd}"
    logger.debug(cmd)
    if DRY_RUN and not ignore_dry_run:
        return ""
    try:
        return subprocess.run(
            shlex.split(cmd),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.error("%s failed with error code %d: `%s`", cmd, e.returncode, e.output)
        raise e


@lru_cache()
def get_hostname(ip: str) -> str:
    return run_command("hostname", ignore_dry_run=True, remote_host=ip)


class Host:
    def __init__(self, ip: str):
        self.ip = ip

    @property
    def hostname(self) -> str:
        return get_hostname(self.ip)

    def __str__(self) -> str:
        return ",".join([self.hostname, self.ip])

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, Host)
            and self.ip == other.ip
            and self.hostname == other.hostname
        )

    def __hash__(self) -> int:
        return hash((self.ip, self.hostname))

    def to_json(self) -> Dict[str, Any]:
        return self.__dict__

class LinstorPeer(Host):
    def __init__(self, ip: str, from_ip: str):
        super().__init__(ip)
        self.from_host = Host(from_ip)

    def __str__(self) -> str:
        return ",".join([super().__str__(), str(self.from_host)])

    # Intentionally not overloading eq and hash since the from_host
    #  does not change the peer


class ResourceStatus:
    def __init__(self, resource: str, peer: LinstorPeer, out_of_sync: int):
        self.resource = resource
        self.peer = peer
        self.out_of_sync = out_of_sync

    def __str__(self) -> str:
        return ",".join([self.resource, str(self.peer), str(self.out_of_sync)])

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, ResourceStatus)
            and self.resource == other.resource
            and self.peer == other.peer
            and self.out_of_sync == other.out_of_sync
        )

    def __hash__(self) -> int:
        return hash((self.resource, self.peer, self.out_of_sync))

    def to_json(self) -> Dict[str, Any]:
        return self.__dict__

JSON = Dict[str, Any]
StatusUnion = Union[List[ResourceStatus], Iterator[ResourceStatus]]


def get_peer_from_connection(connection: JSON) -> Optional[LinstorPeer]:
    for path in connection.get("paths", []):
        if path.get("established"):
            remote = path.get("remote_host", {}).get("address")
            local = path.get("this_host", {}).get("address")
            if remote and local:
                return LinstorPeer(remote, local)
    return None


def get_bad_resources_from_status(
    status: List[JSON],
    lazy: bool = False,
    oos_only: bool = False,
    resource_name: str = "",
) -> StatusUnion:
    statuses = (
        ResourceStatus(
            resource=resource.get("name", ""),
            peer=get_peer_from_connection(connection),  # type: ignore
            out_of_sync=peer_device.get("out-of-sync", 0),
        )
        for resource in status
        for connection in resource.get("connections", [])
        for peer_device in connection.get("peer_devices", [])
        if (
            (not oos_only or peer_device.get("out-of-sync", 0) > 0)
            and (not resource_name or resource.get("name", "") == resource_name)
            and get_peer_from_connection(connection)
        )
    )
    return statuses if lazy else list(statuses)


def get_peers_from_status(status: List[JSON]) -> Set[LinstorPeer]:
    peers = set()
    for resource in status:
        for connection in resource.get("connections", []):
            peer = get_peer_from_connection(connection)
            if peer:
                peers.add(peer)
    return peers


def get_status(remote_host: str = "") -> List[JSON]:
    return json.loads(
        run_command(DRBD_STATUS, ignore_dry_run=True, remote_host=remote_host)
    )


def get_oos_statuses(
    lazy: bool = False,
    oos_only: bool = False,
    resource: str = "",
    remote_host: str = "",
) -> StatusUnion:
    return get_bad_resources_from_status(
        get_status(remote_host),
        lazy=lazy,
        oos_only=oos_only,
        resource_name=resource,
    )


def verify_resource(status: ResourceStatus, remote_host: str = "") -> None:
    run_command(
        DRBD_VERIFY.format(resource=status.resource, peer=status.peer.hostname),
        remote_host=remote_host,
    )
    run_command(
        DRBD_WAIT_SYNC.format(resource=status.resource),
        remote_host=remote_host,
    )


def resync_resource(
    status: ResourceStatus, local: bool = False, remote_host: str = ""
) -> None:
    cmd = DRBD_INVALIDATE if local else DRBD_INVALIDATE_REMOTE
    run_command(
        cmd.format(resource=status.resource, peer=status.peer.hostname),
        remote_host=remote_host,
    )
    run_command(DRBD_WAIT_SYNC.format(resource=status.resource,))


def get_all_hosts() -> Set[LinstorPeer]:
    hosts = get_peers_from_status(get_status())
    local_ip = next(iter(hosts)).from_host.ip
    hosts.add(LinstorPeer(local_ip, local_ip))
    return hosts


def main(
    resource: str = "",
    print_report: bool = False,
    verify_only: bool = False
) -> None:
    hosts = get_all_hosts()
    all_statuses: Dict[str, List[ResourceStatus]] = {}
    reports: List[ResourceStatus] = []
    for host in hosts:
        remote_host = host.ip if host.ip != host.from_host.ip else ""
        statuses = get_oos_statuses(lazy=True, resource=resource, remote_host=remote_host)

        for status in statuses:
            verify_resource(status, remote_host=remote_host)

        statuses = get_oos_statuses(lazy=False, oos_only=True, resource=resource, remote_host=remote_host)

        for status in statuses:
            logging.info(
                "resource `%s` on `%s` is out of sync by %s",
                status.resource, status.peer.hostname, status.out_of_sync
            )
            all_statuses.setdefault(status.resource, []).append(status)

    for resource_name, statuses in all_statuses.items():
        msg = f"{resource} is reported out-of-sync by:"
        counts: Dict[ResourceStatus, int] = {}
        for status in statuses:
            counts[status] = counts.get(status, 0) + 1
            msg += "\n\t{} on {} by {}".format(
                status.peer.from_host.hostname, status.peer.hostname, status.out_of_sync
            )
        logger.info(msg)

        max_count = max(counts.values())
        top_resources = [s for s, c in counts.items() if c == max_count]

        if len(top_resources) > 1:
            logger.error(
                "Tie detected: multiple hosts with highest reports (%d): %s. Skipping.",
                max_count,
                ", ".join([s.peer.hostname for s in top_resources]),
            )
            continue

        top_resource = top_resources[0]
        logger.info(
            "Designed %s as the host with the corrupted %s",
            top_resource.peer.hostname, top_resource.resource
        )
        reports.append(top_resource)

        if verify_only:
            continue

        resync_resource(
            top_resource,
            remote_host=top_resource.peer.from_host.ip,
        )

    if print_report:
        print(json.dumps(reports, default=lambda o: o.to_json()))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--print", action="store_true")
    parser.add_argument("--resource", type=str)
    parser.add_argument(
        "--log-level",
        type=lambda x: x.upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
    )

    args = parser.parse_args()
    DRY_RUN = args.dry_run

    pid = os.getpid()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), None),
        format=f"%(asctime)s {SCRIPT_NAME}: [{pid}] %(levelname)-8s %(message)s",
        datefmt="%b %e %H:%M:%S"
    )
    logger = logging.getLogger(__name__)

    main(
        resource=args.resource,
        print_report=args.print,
        verify_only = args.verify_only
    )
