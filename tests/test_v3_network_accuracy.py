from __future__ import annotations

import socket
from types import SimpleNamespace

from app.monitoring.network import NetworkCollector


def sock(status, pid, *, remote=("127.0.0.1", 49000), local_port=50000, kind=socket.SOCK_STREAM):
    return SimpleNamespace(
        type=kind,
        status=status,
        pid=pid,
        laddr=("127.0.0.1", local_port),
        raddr=remote,
    )


def psutil_with(connections, names=None):
    names = names or {}
    return SimpleNamespace(
        net_io_counters=lambda: SimpleNamespace(bytes_sent=1, bytes_recv=1),
        net_connections=lambda kind: list(connections),
        Process=lambda pid: SimpleNamespace(name=lambda: names.get(pid, f"p{pid}.exe")),
    )


def windows_like_sockets():
    # Windows reports closed TCP endpoints in TIME_WAIT under PID 0, and lists many
    # of them before the established connections they crowd out.
    time_wait = [sock("TIME_WAIT", 0, local_port=40000 + i) for i in range(300)]
    established = [
        sock("ESTABLISHED", 1200, remote=("203.0.113.5", 443), local_port=51000 + i)
        for i in range(40)
    ]
    listening = [sock("LISTEN", 1300, remote=(), local_port=8765)]
    udp = [sock("NONE", 1400, remote=(), local_port=5353, kind=socket.SOCK_DGRAM)]
    closing = [sock("CLOSE_WAIT", 1200, remote=("203.0.113.6", 443), local_port=52000)]
    return time_wait + established + listening + udp + closing


def test_closed_sockets_are_not_counted_as_active():
    collector = NetworkCollector(psutil_with(windows_like_sockets()), clock=lambda: 1)

    sample, _ = collector.collect()

    # ESTABLISHED, LISTEN, UDP and CLOSE_WAIT are open; TIME_WAIT is not.
    assert sample.active_connections == 43
    assert sample.closing_connections == 300
    assert sample.established_connections == 40


def test_pid_zero_is_not_labelled_as_the_system_process():
    collector = NetworkCollector(
        psutil_with([sock("TIME_WAIT", 0), sock("ESTABLISHED", 4)], {4: "System"}),
        clock=lambda: 1,
    )

    _, details = collector.collect()

    names = {item.pid: item.process_name for item in details}
    assert names[0] == "No owner (PID 0)"
    assert names[4] == "System"


def test_ownership_counts_and_rankings_cover_open_sockets_only():
    collector = NetworkCollector(psutil_with(windows_like_sockets()), clock=lambda: 1)

    collector.collect()

    assert collector.connection_counts_by_pid == {1200: 41, 1300: 1, 1400: 1}
    rankings = {row.pid: row for row in collector.connection_rankings}
    assert 0 not in rankings
    assert rankings[1200].connection_count == 41
    assert rankings[1200].established_count == 40


def test_bounded_detail_rows_keep_established_connections_ahead_of_time_wait():
    collector = NetworkCollector(
        psutil_with(windows_like_sockets()), clock=lambda: 1, detail_limit=50
    )

    _, details = collector.collect()

    statuses = [item.status for item in details]
    assert len(details) == 50
    assert statuses[:40] == ["ESTABLISHED"] * 40
    # Other open TCP states, then listeners, then UDP endpoints, then closed sockets.
    assert statuses[40:43] == ["CLOSE_WAIT", "LISTEN", "NONE"]
    assert statuses[43:] == ["TIME_WAIT"] * 7


def test_closed_sockets_do_not_feed_destination_or_lifecycle_metrics():
    # Independent review reproduction: 5 TIME_WAIT + 2 established sockets.
    time_wait = [
        sock("TIME_WAIT", 0, remote=(f"198.51.100.{i}", 443), local_port=40000 + i)
        for i in range(5)
    ]
    established = [
        sock("ESTABLISHED", 1200, remote=(f"203.0.113.{i}", 443), local_port=51000 + i)
        for i in range(2)
    ]
    collector = NetworkCollector(psutil_with(time_wait + established), clock=lambda: 1)

    sample, details = collector.collect()

    assert sample.active_connections == 2
    assert sample.closing_connections == 5
    assert sample.unique_remote_destinations == 2
    assert sample.new_connections == 2
    assert sample.disappeared_connections == 0
    assert 0 not in collector.remote_endpoints_by_pid
    assert collector.remote_endpoints_by_pid[1200] == ["203.0.113.0:443", "203.0.113.1:443"]
    flags = {item.status: item.is_new for item in details}
    assert flags == {"ESTABLISHED": True, "TIME_WAIT": False}


def test_closing_a_connection_counts_one_disappearance_and_no_new_connection():
    remote = ("203.0.113.5", 443)
    samples = [
        [sock("ESTABLISHED", 1200, remote=remote, local_port=51000)],
        # Windows moves the closed endpoint to PID 0 while it waits out TIME_WAIT.
        [sock("TIME_WAIT", 0, remote=remote, local_port=51000)],
        [],
    ]
    connections: list = []
    provider = psutil_with(connections)
    collector = NetworkCollector(provider, clock=lambda: 1)

    observed = []
    for batch in samples:
        connections[:] = batch
        sample, _ = collector.collect()
        observed.append(
            (sample.new_connections, sample.disappeared_connections,
             sample.unique_remote_destinations, len(collector.recently_closed))
        )

    assert observed == [(1, 0, 1, 0), (0, 1, 0, 1), (0, 0, 0, 1)]
    assert collector.recently_closed[0].pid == 1200

