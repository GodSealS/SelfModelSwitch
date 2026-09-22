from __future__ import annotations

import pytest

from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Observation, Operation, Outcome, Presence, State
from model_scheduler.contracts_v2 import Envelope
from model_scheduler.contracts_v2 import ModelSpec as RegisteredModelSpec
from model_scheduler.contracts_v2 import physical_reserved_bytes_from_peak, reserved_bytes_from_peak
from model_scheduler.control_protocol_v1 import InstanceIdentity
from model_scheduler.model_registry import STOP_RESAMPLE_GRACE_SECONDS, Book, Conflict, StaleOperation


def make_book(concurrency: int = 1) -> Book:
    specs = {
        name: ModelSpec(name, f"http://127.0.0.1:{10001 + index}", frozenset({Capability.CHAT}), 100, max_concurrency=concurrency)
        for index, name in enumerate(("a", "b", "c"))
    }
    book = Book(specs, model_budget=300, free_floor=20, margin=0, half_life=10)
    for model_id in specs:
        book.bootstrap_stopped(model_id)
    return book


def load(book: Book, model_id: str, now: float = 0) -> None:
    operation = book.begin_load(model_id, MemorySample(1000, 900, now), now)
    book.loaded(operation, now)


def test_atomic_batch_eviction_blocks_every_selected_model() -> None:
    book = make_book()
    load(book, "a")
    load(book, "b")

    operations = book.begin_eviction(["a", "b"])

    with pytest.raises(Conflict):
        book.acquire_ready("b", "new-request", 1)
    book.stopped(operations[0])
    assert book.runtime["b"].state is State.EVICTING
    assert book.committed == 100


def test_failed_stop_keeps_budget_and_unsent_batch_member_recovers() -> None:
    book = make_book()
    load(book, "a")
    load(book, "b")
    first, second = book.begin_eviction(["a", "b"])

    book.failed(first, "stop_timeout")
    book.rollback_unsent(second)

    assert book.runtime["a"].state is State.ERROR
    assert book.runtime["b"].state is State.READY
    assert book.committed == 200


def test_duplicate_release_cannot_release_another_request() -> None:
    book = make_book(concurrency=2)
    load(book, "a")
    first = book.acquire_ready("a", "first", 0)
    second = book.acquire_ready("a", "second", 0)

    assert book.release(first, Outcome.SUCCESS, 1, tokens=4) is True
    assert book.release(first, Outcome.SUCCESS, 1, tokens=4) is False
    assert set(book.runtime["a"].leases) == {second.lease_id}


def test_aborted_request_isolates_model_without_harming_other_lease() -> None:
    book = make_book(concurrency=2)
    load(book, "a")
    first = book.acquire_ready("a", "first", 0)
    second = book.acquire_ready("a", "second", 0)

    book.release(first, Outcome.ABORTED, 1)

    assert book.runtime["a"].state is State.ERROR
    assert second.lease_id in book.runtime["a"].leases
    with pytest.raises(Conflict):
        book.acquire_ready("a", "third", 1)


def test_stale_load_operation_cannot_overwrite_new_generation() -> None:
    book = make_book()
    old = book.begin_load("a", MemorySample(1000, 900, 0), 0)
    book.failed(old, "load_timeout")

    with pytest.raises(StaleOperation):
        book.loaded(old, 901)
    assert book.committed == 100


def test_ttl_begins_only_after_last_lease_releases() -> None:
    specs = {"a": ModelSpec("a", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, max_concurrency=2, ttl_seconds=10)}
    book = Book(specs, model_budget=300, free_floor=20, margin=0); book.bootstrap_stopped("a")
    load(book, "a", 0)
    first, second = book.acquire_ready("a", "first", 1), book.acquire_ready("a", "second", 2)
    book.release(first, Outcome.SUCCESS, 5)
    assert not book.ttl_due("a", 100)
    book.release(second, Outcome.SUCCESS, 100)
    assert not book.ttl_due("a", 109.9)
    assert book.ttl_due("a", 110)


def test_ready_admission_requires_a_fresh_sample_and_free_floor() -> None:
    book = make_book()
    load(book, "a", 0)
    assert book.can_admit_ready("a", MemorySample(1_000, 20, 0), 1)
    assert not book.can_admit_ready("a", MemorySample(1_000, 19, 0), 1)
    assert not book.can_admit_ready("a", MemorySample(1_000, 900, -10), 1)


def test_switch_freeze_blocks_new_leases_without_revoking_existing_lease() -> None:
    book = make_book(concurrency=2)
    load(book, "a")
    active = book.acquire_ready("a", "active", 0)

    book.freeze_for_switch(["a"])

    assert active.lease_id in book.runtime["a"].leases
    with pytest.raises(Conflict):
        book.acquire_ready("a", "new", 1)
    book.unfreeze_switch(["a"])
    assert book.acquire_ready("a", "new", 1).model_id == "a"


# ---------------------------------------------------------------------------
# P08: the unified spec boundary and the two C02 ledgers.
# ---------------------------------------------------------------------------


def registered(model_id: str, *, measured_peak: int, physical_peak: int | None, port: int = 18081, parallel: int = 1) -> RegisteredModelSpec:
    return RegisteredModelSpec(
        model_id=model_id,
        runtime_id="llama-cpp-gguf-v1",
        capabilities=("chat",),
        assets=(),
        port=port,
        envelope=Envelope(
            ctx_size=4096,
            max_input_tokens=2048,
            max_output_tokens=512,
            max_parallel=parallel,
            max_image_tokens=0,
            max_image_edge_pixels=0,
            max_images=0,
        ),
        timeout_seconds=600,
        reserved_bytes=reserved_bytes_from_peak(measured_peak),
        measured=physical_peak is not None,
        measurement_ref="a" * 64 if physical_peak is not None else None,
        physical_resident_peak_bytes=physical_peak,
    )


def test_v1_legacy_margin_is_applied_exactly_once() -> None:
    specs = {"a": ModelSpec("a", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, max_concurrency=2)}
    book = Book(specs, model_budget=1000, free_floor=20, margin=0.15)

    assert book.required("a") == 115  # ceil(100 * 1.15), never multiplied twice
    assert book.ledger_spec("a").effective_reserved_bytes == 115
    assert book.committed == 115
    assert book.ledger_spec("a").physical_reserved_bytes is None
    assert book.physical_enforced is False
    assert book.ledger_spec("a").max_concurrency == 2


def test_v2_registration_is_accounted_without_a_second_margin() -> None:
    spec = registered("lab-a", measured_peak=1000, physical_peak=2000)
    book = Book({"lab-a": spec}, model_budget=10_000, free_floor=20, margin=0.15)

    assert spec.reserved_bytes == reserved_bytes_from_peak(1000) == 1150
    assert book.required("lab-a") == 1150
    assert book.physical_required("lab-a") == physical_reserved_bytes_from_peak(2000) == 2300
    assert book.physical_enforced is True


def test_both_ledgers_hold_unknown_error_and_unterminated_launches() -> None:
    spec = registered("lab-a", measured_peak=1000, physical_peak=2000, parallel=3)
    book = Book({"lab-a": spec}, model_budget=10_000, free_floor=0, margin=0)

    assert book.runtime["lab-a"].state is State.UNKNOWN
    assert (book.committed, book.physical_committed) == (1150, 2300)  # never observed: still on both books

    book.bootstrap_stopped("lab-a")
    assert (book.committed, book.physical_committed) == (0, 0)  # an observed stop releases both

    operation = book.begin_load("lab-a", MemorySample(10_000, 9_000, 0), 0)
    assert book.runtime["lab-a"].state is State.LOADING
    assert (book.committed, book.physical_committed) == (1150, 2300)  # an unterminated launch keeps both

    book.failed(operation, "load_timeout")
    assert book.runtime["lab-a"].state is State.ERROR
    assert (book.committed, book.physical_committed) == (1150, 2300)  # an error keeps both

    cleanup = book.begin_cleanup(["lab-a"])[0]
    book.stopped(cleanup)
    assert (book.committed, book.physical_committed) == (0, 0)  # only a proven stop releases both


def test_a_failed_stop_keeps_both_ledgers() -> None:
    specs = {"lab-a": registered("lab-a", measured_peak=1000, physical_peak=2000)}
    book = Book(specs, model_budget=10_000, free_floor=0, margin=0)
    book.bootstrap_stopped("lab-a")
    operation = book.begin_load("lab-a", MemorySample(10_000, 9_000, 0), 0)
    book.loaded(operation, 0)
    eviction = book.begin_eviction(["lab-a"])[0]

    book.failed(eviction, "stop_timeout")  # a cancelled task is not evidence that work stopped

    assert book.runtime["lab-a"].state is State.ERROR
    assert (book.committed, book.physical_committed) == (1150, 2300)


def test_model_budget_equality_and_one_byte_boundary() -> None:
    def two_models(second: int) -> Book:
        specs = {
            "a": ModelSpec("a", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, max_concurrency=1),
            "b": ModelSpec("b", "http://127.0.0.1:10002", frozenset({Capability.CHAT}), second, max_concurrency=1),
        }
        book = Book(specs, model_budget=200, free_floor=20, margin=0)
        for model_id in specs:
            book.bootstrap_stopped(model_id)
        load(book, "a")
        return book

    exact = two_models(100)
    assert exact.committed + exact.required("b") == exact.model_budget
    assert exact.can_load("b", MemorySample(1_000, 120, 0), 0.5)
    assert not exact.can_load("b", MemorySample(1_000, 119, 0), 0.5)  # one byte short of N + F

    over = two_models(101)
    assert over.committed + over.required("b") == over.model_budget + 1
    assert not over.can_load("b", MemorySample(1_000, 999, 0), 0.5)  # one byte over B


def test_physical_ledger_equality_and_one_byte_boundary() -> None:
    def two_models(budget: int) -> Book:
        specs = {
            "lab-a": registered("lab-a", measured_peak=1000, physical_peak=2000),
            "lab-b": registered("lab-b", measured_peak=1000, physical_peak=2000, port=18082),
        }
        book = Book(specs, model_budget=budget, free_floor=0, margin=0)
        for model_id in specs:
            book.bootstrap_stopped(model_id)
        operation = book.begin_load("lab-a", MemorySample(10_000, 9_000, 0), 0)
        book.loaded(operation, 0)
        return book

    exact = two_models(4600)  # 2300 + 2300 == B, while the model ledger only commits 1150 + 1150
    assert exact.committed + exact.required("lab-b") == 2300
    assert exact.physical_committed + 2300 == exact.model_budget
    assert exact.can_load("lab-b", MemorySample(10_000, 10_000, 0), 0.5)

    tight = two_models(4599)
    assert tight.committed + tight.required("lab-b") <= tight.model_budget
    assert not tight.can_load("lab-b", MemorySample(10_000, 10_000, 0), 0.5)  # the physical gate is one byte over


def test_an_unmeasured_model_is_refused_once_the_physical_ledger_governs() -> None:
    specs = {
        "lab-a": registered("lab-a", measured_peak=1000, physical_peak=2000),
        "lab-b": registered("lab-b", measured_peak=1000, physical_peak=None, port=18082),
    }
    book = Book(specs, model_budget=100_000, free_floor=0, margin=0)
    for model_id in specs:
        book.bootstrap_stopped(model_id)

    assert book.physical_enforced is True
    assert book.physical_required("lab-b") is None
    assert book.can_load("lab-a", MemorySample(10_000, 10_000, 0), 0.5)
    assert not book.can_load("lab-b", MemorySample(10_000, 10_000, 0), 0.5)


def test_sample_age_two_second_boundary_and_future_timestamps() -> None:
    book = make_book()
    load(book, "a", 0)
    sample = MemorySample(1_000, 900, 10.0)

    assert book.sample_valid(sample, 12.0)
    assert book.can_load("b", sample, 12.0)
    assert not book.sample_valid(sample, 12.0001)
    assert not book.can_load("b", sample, 12.0001)
    assert not book.sample_valid(sample, 9.999)  # a future timestamp is not a sample
    assert not book.can_load("b", sample, 9.999)


def test_a_proven_stop_needs_a_sample_newer_than_the_stop() -> None:
    book = make_book()
    load(book, "a")
    operations = book.begin_eviction(["a"])
    book.stopped(operations[0], now=50.0)

    assert book.runtime["a"].state is State.UNLOADED
    assert book.committed == 0
    assert book.runtime["a"].stopped_at == 50.0
    assert not book.can_load("a", MemorySample(1_000, 900, 49.5), 50.6)  # taken before the stop
    assert book.can_load("a", MemorySample(1_000, 900, 50.5), 51.0)
    assert book.stop_settled("a", 50.0 + STOP_RESAMPLE_GRACE_SECONDS)
    assert not book.stop_settled("a", 50.0 + STOP_RESAMPLE_GRACE_SECONDS - 0.001)


# ---------------------------------------------------------------------------
# P10: cleanup runs once, and a late stop write-back cannot release twice.
# ---------------------------------------------------------------------------


def test_a_cleanup_batch_runs_once_and_a_late_stop_cannot_release_again() -> None:
    book = make_book()
    load(book, "a")
    operations = book.begin_cleanup(["a"])

    with pytest.raises(Conflict):
        book.begin_cleanup(["a"])  # the same model is stopped by exactly one batch

    book.stopped(operations[0], now=5.0)
    assert book.committed == 0

    with pytest.raises(StaleOperation):
        book.stopped(operations[0], now=6.0)  # a late duplicate proof has no effect
    assert book.committed == 0


def test_a_cancelling_lease_keeps_its_budget_until_the_terminal_proof() -> None:
    book = make_book()
    load(book, "a")
    lease = book.acquire_ready("a", "req-1", 1.0)

    assert book.begin_cancel(lease) is True
    assert book.is_cancelling(lease)
    assert book.cancelling_leases("a") == (lease,)
    assert book.committed == 100  # a cancel acknowledgement is not a stop
    with pytest.raises(Conflict):
        book.begin_eviction(["a"])

    assert book.release(lease, Outcome.ABORTED, 2.0) is True
    assert book.cancelling_leases("a") == ()
    assert book.committed == 100  # the aborted model is ERROR: the reservation stays
    assert book.runtime["a"].state is State.ERROR

    operations = book.begin_cleanup(["a"])
    book.stopped(operations[0], now=3.0)
    assert book.committed == 0


def test_a_stale_operation_cannot_write_back_a_load_or_a_stop() -> None:
    book = make_book()
    stale_load = book.begin_load("a", MemorySample(1_000, 900, 0), 0)
    book.failed(stale_load, "load_timeout")
    cleanup = book.begin_cleanup(["a"])[0]
    book.stopped(cleanup, now=1.0)

    fresh_load = book.begin_load("a", MemorySample(1_000, 900, 1.0), 1.0)
    book.loaded(fresh_load, 1.0)

    with pytest.raises(StaleOperation):
        book.loaded(stale_load, 2.0)  # a late success cannot resurrect the failed operation

    eviction = book.begin_eviction(["a"])[0]
    with pytest.raises(StaleOperation):
        book.stopped(stale_load, 3.0)
    book.stopped(eviction, 3.0)
    assert book.committed == 0


def test_a_cancel_is_refused_for_a_lease_this_model_no_longer_holds() -> None:
    book = make_book()
    load(book, "a")
    lease = book.acquire_ready("a", "req-1", 1.0)
    assert book.begin_cancel(lease) is True
    assert book.release(lease, Outcome.SUCCESS, 2.0) is True

    assert book.begin_cancel(lease) is False  # released: nothing left to cancel
    assert book.is_cancelling(lease) is False


# --- K2/RP01: the book is the single owner of the accepted instance identity ---

def identity(container_id: str = "container-a", model_id: str = "a") -> InstanceIdentity:
    return InstanceIdentity(
        container_id=container_id,
        started_at="2026-09-22T00:00:00+00:00",
        deployment_id="deployment-1",
        model_id=model_id,
        runtime_id="runtime-1",
        candidate_digest="a" * 64,
        image_digest="b" * 64,
    )


def make_unknown_book() -> Book:
    specs = {"a": ModelSpec("a", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100)}
    return Book(specs, model_budget=300, free_floor=20, margin=0, half_life=10)


def load_identified(book: Book, model_id: str, now: float = 0) -> None:
    operation = book.begin_load(model_id, MemorySample(1_000, 900, now), now)
    book.loaded(operation, now, instance=identity(f"container-{model_id}", model_id))


def test_observation_tail_fields_default_to_none_and_keep_the_original_meaning() -> None:
    legacy = Observation(Presence.RUNNING, "container-a:2026-09-22T00:00:00+00:00", True, 12.5, "detail")

    assert (legacy.presence, legacy.instance_id, legacy.healthy, legacy.observed_at, legacy.detail_code) == (
        Presence.RUNNING, "container-a:2026-09-22T00:00:00+00:00", True, 12.5, "detail")
    assert legacy.instance is None
    assert legacy.valid_until is None


def test_observation_carries_the_identity_and_the_deadline_of_its_verdict() -> None:
    rich = Observation(Presence.RUNNING, None, True, 4.0, None, identity(), 6.0)

    assert rich.instance == identity()
    assert rich.valid_until == 6.0
    assert rich.healthy is True  # the two new fields add nothing to the original four


def test_a_current_load_writes_ready_and_the_full_identity_in_one_move() -> None:
    book = make_book()
    operation = book.begin_load("a", MemorySample(1_000, 900, 0), 0)
    assert book.instance("a") is None  # nothing is accepted before the load is proven

    book.loaded(operation, 0, instance=identity())

    assert book.runtime["a"].state is State.READY
    assert book.instance("a") == identity()


def test_a_stale_operation_and_a_conflicting_state_write_no_identity() -> None:
    book = make_book()
    operation = book.begin_load("a", MemorySample(1_000, 900, 0), 0)
    stale = Operation(operation.operation_id, "a", operation.generation, book.epoch + 1)
    with pytest.raises(StaleOperation):
        book.loaded(stale, 0, instance=identity())
    assert book.instance("a") is None
    assert book.runtime["a"].state is State.LOADING

    book.loaded(operation, 0, instance=identity())
    cleanup = book.begin_cleanup(["a"])[0]
    with pytest.raises(Conflict):
        book.loaded(cleanup, 1, instance=identity("container-other"))
    assert book.instance("a") == identity()  # a refused write leaves no partial identity
    assert book.runtime["a"].state is State.EVICTING


def test_a_proven_stop_clears_the_identity_while_a_failure_keeps_it() -> None:
    book = make_book()
    load_identified(book, "a")
    load_identified(book, "b")
    eviction = book.begin_eviction(["a"])[0]
    failure = book.begin_eviction(["b"])[0]

    book.stopped(eviction, 5.0)
    book.failed(failure, "stop_timeout")

    assert book.instance("a") is None
    assert book.instance("b") == identity("container-b", "b")  # an unproven stop keeps the identity


def test_bootstrap_stopped_and_a_finished_recovery_leave_no_identity() -> None:
    unknown = make_unknown_book()
    unknown.runtime["a"].instance = identity()
    unknown.bootstrap_stopped("a")
    assert unknown.instance("a") is None

    book = make_book()
    load_identified(book, "b")
    epoch = book.begin_recovery()
    assert book.instance("b") == identity("container-b", "b")  # starting recovery proves nothing

    book.finish_recovery(epoch, frozenset({"a", "b", "c"}))
    assert book.instance("b") is None


def test_an_aborted_lease_keeps_the_identity_until_a_stop_is_proven() -> None:
    book = make_book()
    load_identified(book, "a")
    lease = book.acquire_ready("a", "req-1", 0)

    assert book.release(lease, Outcome.ABORTED, 1.0) is True

    assert book.runtime["a"].state is State.ERROR
    assert book.instance("a") == identity()  # an abort is a failure, never a stop
