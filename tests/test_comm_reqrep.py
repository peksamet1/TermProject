"""REQ-REP request / reply primitive (Phase 3, step 4).

Mechanism only: bodies are opaque strings, the words are requester and
target. Every wait is bounded by a timeout, so a broken implementation
fails a test instead of hanging the suite (Architecture 4.3).
"""

import os
import time

import pytest
import zmq

from microgrid.comm.network import PeerNetwork, max_sockets_needed
from microgrid.comm.node import ERROR_REPLY_PREFIX, CommError, is_error_reply
from microgrid.config import CommConfig

FD_SETTLE_TIMEOUT_S = 2.0


@pytest.fixture(params=["tcp", "inproc"])
def comm_config(request) -> CommConfig:
    return CommConfig(transport=request.param)


def _echo(target_id: int, body: str) -> str:
    return f"{target_id}:{body}"


def _open_fds() -> int | None:
    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    return None


def _all_to_all(ids) -> list[tuple[int, int, str]]:
    return [(a, b, f"from {a} to {b}") for a in ids for b in ids if a != b]


def test_single_request_gets_the_echo(comm_config):
    with PeerNetwork([0, 1], comm_config) as net:
        [result] = net.negotiation_rounds([(0, 1, "hello")], _echo)
        assert result.answered
        assert result.reply == "1:hello"
        assert result.rtt_ms is not None and result.rtt_ms >= 0
        stats = net.stats()
        assert (stats.requests, stats.replies, stats.served, stats.timeouts) == (1, 1, 1, 0)
        assert stats.rtt_ms == [result.rtt_ms]
        assert len(stats.req_churn_ms) == 1


def test_split_send_serve_collect_on_nodes(comm_config):
    # The three rounds by hand: no blocking recv anywhere in between.
    with PeerNetwork([0, 1], comm_config) as net:
        pending = net[0].send_request(1, "ping")
        assert net[0].open_requests == 1
        assert net[1].serve_requests(lambda body: body.upper(), timeout_s=1.0) == 1
        assert net[0].collect_reply(pending, timeout_s=1.0) == "PING"
        assert net[0].open_requests == 0
        assert net[0].collect_reply(pending) == "PING"  # finished: no second count
        assert net[0].stats.replies == 1


@pytest.mark.parametrize("num_nodes", [3, 10])
def test_everyone_asks_everyone_in_three_rounds(comm_config, num_nodes):
    ids = list(range(num_nodes))
    requests = _all_to_all(ids)
    with PeerNetwork(ids, comm_config) as net:
        start = time.perf_counter()
        results = net.negotiation_rounds(requests, _echo)
        elapsed = time.perf_counter() - start
        assert elapsed < comm_config.request_timeout_s  # never ran into the deadline
        # Input order kept, and each reply came from the chosen target:
        # a REQ round-robined over several REPs would mix these up.
        assert [(r.requester, r.target, r.body) for r in results] == requests
        assert all(r.reply == f"{r.target}:{r.body}" for r in results)
        stats = net.stats()
        n = len(requests)
        assert (stats.requests, stats.replies, stats.served, stats.timeouts) == (n, n, n, 0)
        assert len(stats.rtt_ms) == n and len(stats.req_churn_ms) == n
        assert all(node.open_requests == 0 for node in net.nodes.values())


def test_unserved_peer_times_out_without_hanging(comm_config):
    timeout_s = 0.2
    with PeerNetwork([0, 1], comm_config) as net:
        start = time.perf_counter()
        # Nobody serves node 1's REP while node 0 waits: a blocking recv
        # would hang here for good.
        assert net[0].request(1, "anyone?", timeout_s=timeout_s) is None
        elapsed = time.perf_counter() - start
        assert timeout_s <= elapsed < timeout_s + 0.5
        assert net[0].stats.timeouts == 1
        assert net[0].stats.replies == 0
        assert net[0].open_requests == 0


def test_stale_request_does_not_break_the_next_round(comm_config):
    # The timed-out request still sits on the target's REP. Answering it
    # goes into a closed REQ, and counting it must not make round 2 stop
    # one short of this round's requests.
    with PeerNetwork([0, 1, 2], comm_config) as net:
        assert net[0].request(1, "stale", timeout_s=0.05) is None
        assert net[1].rep.poll(1000, zmq.POLLIN)  # the stale one has arrived
        # One request owed: a drain that reads only the stale one meets the
        # count while "fresh" may still be on the wire (tcp).
        [result] = net.negotiation_rounds([(2, 1, "fresh")], _echo)
        assert result.reply == "1:fresh"
        assert net.stats().timeouts == 1
        assert net[1].stats.served == 2  # the stale one was answered too
        [result] = net.negotiation_rounds([(0, 1, "again")], _echo)
        assert result.reply == "1:again"


def test_handler_exception_becomes_an_error_reply(comm_config):
    def handler(target_id: int, body: str) -> str:
        if body == "boom":
            raise ValueError("bad body")
        return body

    with PeerNetwork([0, 1, 2], comm_config) as net:
        results = net.negotiation_rounds([(0, 1, "boom"), (2, 1, "fine")], handler)
        assert is_error_reply(results[0].reply)
        assert results[0].reply.startswith(ERROR_REPLY_PREFIX + "ValueError")
        assert results[1].reply == "fine"
        stats = net.stats()
        assert stats.handler_errors == 1 and stats.served == 2 and stats.timeouts == 0


def test_custom_error_reply_and_non_str_reply(comm_config):
    # Phase 4 hooks a NACK in here; a non-str reply is a handler error too.
    with PeerNetwork([0, 1], comm_config) as net:
        [result] = net.negotiation_rounds(
            [(0, 1, "x")],
            lambda target_id, body: 42,
            error_reply=lambda body, exc: f"rejected {body}",
        )
        assert result.reply == "rejected x"
        assert net.stats().handler_errors == 1


def test_two_requests_to_one_target_are_both_answered(comm_config):
    with PeerNetwork([0, 1, 2], comm_config) as net:
        requests = [(0, 2, "a"), (1, 2, "b"), (0, 2, "c")]  # node 0 asks twice
        results = net.negotiation_rounds(requests, _echo)
        assert [r.reply for r in results] == ["2:a", "2:b", "2:c"]
        assert net[2].stats.served == 3
        # Node 0's two requests to 2 were in flight together: two REQs, and
        # both stay pooled for the next round.
        assert net[0].stats.req_opened == 2
        assert net[0].req_sockets == 2


def test_req_sockets_are_pooled_per_target(comm_config):
    # Over tcp a socket per request is a TCP connection per request, each
    # leaving a port in TIME_WAIT; the pool opens one per pair, once.
    ids = [0, 1, 2]
    pairs = len(_all_to_all(ids))
    with PeerNetwork(ids, comm_config) as net:
        for _ in range(5):
            results = net.negotiation_rounds(_all_to_all(ids), _echo)
            assert all(r.reply == f"{r.target}:{r.body}" for r in results)
        stats = net.stats()
        assert stats.requests == 5 * pairs
        assert stats.req_opened == pairs
        assert sum(node.req_sockets for node in net.nodes.values()) == pairs
        assert len(stats.req_churn_ms) == 5 * pairs  # one sample per request


def test_timed_out_req_is_closed_not_pooled(comm_config):
    # A timed-out REQ still waits for its reply; reused, it would refuse to
    # send, or hand the late reply to the next request.
    with PeerNetwork([0, 1], comm_config) as net:
        net.negotiation_rounds([(0, 1, "warm")], _echo)
        assert net[0].req_sockets == 1
        assert net[0].request(1, "unanswered", timeout_s=0.05) is None
        assert net[0].req_sockets == 0
        [result] = net.negotiation_rounds([(0, 1, "next")], _echo)
        assert result.reply == "1:next"  # not the late "1:unanswered"
        assert net[0].stats.req_opened == 2


def test_no_req_in_flight_or_fd_growth_after_requests(comm_config):
    ids = [0, 1, 2]
    with PeerNetwork(ids, comm_config) as net:
        net.negotiation_rounds(_all_to_all(ids), _echo)  # fills the pool
        fds_before = _open_fds()
        for _ in range(5):
            net.negotiation_rounds(_all_to_all(ids), _echo)
        net[0].request(1, "unanswered", timeout_s=0.05)
        assert all(node.open_requests == 0 for node in net.nodes.values())
        if fds_before is not None:
            # tcp teardown runs on the io thread, so the count settles
            # shortly after close; bounded wait, never a fixed sleep.
            deadline = time.perf_counter() + FD_SETTLE_TIMEOUT_S
            while _open_fds() > fds_before and time.perf_counter() < deadline:
                net[1].serve_requests(lambda body: body, timeout_s=0.01)
            assert _open_fds() <= fds_before


def test_max_sockets_covers_a_full_round():
    # Every node asking every peer pools N * (N - 1) REQs.
    assert max_sockets_needed(50) >= 3 * 50 + 2 * 50 * 49
    with PeerNetwork(range(40), CommConfig(transport="inproc")) as net:
        results = net.negotiation_rounds(_all_to_all(range(40)), _echo)
        assert all(r.answered for r in results)


def test_requests_refuse_misuse(comm_config):
    with PeerNetwork([0, 1], comm_config) as net:
        with pytest.raises(ValueError):
            net.negotiation_rounds([(0, 7, "x")], _echo)
        with pytest.raises(ValueError):
            net[0].send_request(7, "x")
        with pytest.raises(TypeError):
            net[0].send_request(1, b"bytes")
        assert net[0].open_requests == 0
    with pytest.raises(CommError):
        net.negotiation_rounds([(0, 1, "x")], _echo)
    with pytest.raises(CommError):
        net[0].send_request(1, "x")
