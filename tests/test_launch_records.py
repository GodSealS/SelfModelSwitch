"""K4/RP18: the boot-scoped launch provenance the observers are wired to.

`stopped_is_proven` accepts a stop only when the launch dimension is settled, and
the fixed control protocol cannot prove that a launch ended. The one fact this
process can prove is its own dispatch record: a fresh boot has launched nothing,
so an unknown target resolves to "no launch"; once a load dispatches, the record
stays `starting` until a terminal verdict (a witnessed RUNNING or a proven
STOPPED) settles it, so a lost instance can never be read as a proven stop.

(RP17 found the real consequence of getting this wrong: without any launch
source the production composition could never witness STOPPED and the service
refused to start with `unproven_stop`.)
"""
from __future__ import annotations

import pytest

from model_scheduler.backend_control import BootLaunchRecords
from model_scheduler.control_protocol_v1 import Fence
from model_scheduler.ports_v3 import ObservationTarget, stopped_is_proven

DEPLOYMENT = "orin-lab"
OTHER_DEPLOYMENT = "orin-lab-2"
FENCE = Fence("boot-1", "chat", 1, "op-1", None, None)


def target(deployment_id: str = DEPLOYMENT) -> ObservationTarget:
    return ObservationTarget(deployment_id=deployment_id)


def test_a_fresh_boot_positively_never_launched_anything() -> None:
    records = BootLaunchRecords(DEPLOYMENT)

    assert records.launch_lookup("chat")(target()) is None  # "no launch", and it was really looked up
    # ...which is exactly what lets a clean deployment prove a stop (K4)
    assert stopped_is_proven(container_absent=True, launch_operation_terminal=True,
                             subprocess_exited=True, port_listening=False) is True


def test_one_models_launch_lookup_never_answers_for_another_deployment() -> None:
    records = BootLaunchRecords(DEPLOYMENT)

    with pytest.raises(ValueError):
        records.launch_lookup("chat")(target(OTHER_DEPLOYMENT))


def test_a_dispatch_is_unsettled_until_a_terminal_verdict_settles_it() -> None:
    records = BootLaunchRecords(DEPLOYMENT, now=lambda: 100.0)

    records.dispatched("chat", FENCE)
    unsettled = records.launch_lookup("chat")(target())
    assert unsettled is not None and unsettled.is_terminal is False
    assert unsettled.fence == FENCE and unsettled.started_at_monotonic == 100.0
    # an unsettled launch can never carry a proven stop, even with every other fact in place
    assert stopped_is_proven(container_absent=True, launch_operation_terminal=unsettled.is_terminal,
                             subprocess_exited=True, port_listening=False) is False

    records.settled("chat")
    settled = records.launch_lookup("chat")(target())
    assert settled is not None and settled.is_terminal is True and settled.terminal_at_monotonic == 100.0
    assert stopped_is_proven(container_absent=True, launch_operation_terminal=settled.is_terminal,
                             subprocess_exited=True, port_listening=False) is True


def test_dispatching_or_settling_twice_never_restarts_or_rewrites_the_record() -> None:
    records = BootLaunchRecords(DEPLOYMENT, now=lambda: 5.0)
    records.dispatched("chat", FENCE)
    first = records.launch_lookup("chat")(target())

    records.dispatched("chat", FENCE)  # a second dispatch is not a new launch
    assert records.launch_lookup("chat")(target()) == first

    records.settled("chat")
    settled = records.launch_lookup("chat")(target())
    records.settled("chat")  # and a second verdict cannot move the terminal stamp
    assert records.launch_lookup("chat")(target()) == settled


def test_settling_never_invents_a_launch_this_boot_never_dispatched() -> None:
    records = BootLaunchRecords(DEPLOYMENT)

    records.settled("chat")

    assert records.launch_lookup("chat")(target()) is None  # still "never launched", never fabricated


def test_one_models_record_never_leaks_into_another() -> None:
    records = BootLaunchRecords(DEPLOYMENT, now=lambda: 1.0)
    records.dispatched("chat", FENCE)

    assert records.launch_lookup("chat")(target()) is not None
    assert records.launch_lookup("embedding")(target()) is None


@pytest.mark.parametrize("bad", ["", None, 7])
def test_the_records_refuse_a_missing_deployment_or_model(bad) -> None:
    with pytest.raises(ValueError):
        BootLaunchRecords(bad)

    records = BootLaunchRecords(DEPLOYMENT)
    with pytest.raises(ValueError):
        records.dispatched(bad, FENCE)
    with pytest.raises(ValueError):
        records.launch_lookup(bad)
