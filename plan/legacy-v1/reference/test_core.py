"""计划核心的反例/不变量测试；不需要真实模型、FastAPI 或 Docker。"""
import unittest

from contracts import Capability, MemorySample, ModelSpec, Outcome, State, Waiter
from core import Book, Conflict, StaleOperation, waiter_key


class CoreTests(unittest.TestCase):
    def make_book(self, *, concurrency=1, ttl=10):
        specs = {
            mid: ModelSpec(mid, f"http://127.0.0.1:{10001+i}", frozenset({Capability.CHAT}),
                           100, max_concurrency=concurrency, ttl_seconds=ttl)
            for i, mid in enumerate(("a", "b", "c"))
        }
        book = Book(specs, model_budget=300, free_floor=20, margin=0, half_life=10)
        for mid in specs:
            book.bootstrap_stopped(mid)
        return book

    def load(self, book, mid, now=0):
        operation = book.begin_load(mid, MemorySample(1000, 900, now), now)
        book.loaded(operation, now)
        return operation

    def test_batch_reservation_blocks_second_candidate(self):
        book = self.make_book()
        self.load(book, "a")
        self.load(book, "b")
        operations = book.begin_eviction(["a", "b"])
        # 模拟stop(a)正在锁外等待；b已经不能获准新请求。
        with self.assertRaises(Conflict):
            book.acquire_ready("b", "incoming", 1)
        book.stopped(operations[0])
        self.assertEqual(book.runtime["b"].state, State.EVICTING)
        self.assertEqual(book.committed, 100)

    def test_batch_validation_is_atomic(self):
        book = self.make_book()
        self.load(book, "a")
        self.load(book, "b")
        book.acquire_ready("b", "busy", 0)
        with self.assertRaises(Conflict):
            book.begin_eviction(["a", "b"])
        self.assertEqual(book.runtime["a"].state, State.READY)
        self.assertIsNone(book.runtime["a"].operation_id)

    def test_failed_unload_keeps_budget_and_unsent_is_recoverable(self):
        book = self.make_book()
        self.load(book, "a")
        self.load(book, "b")
        a, b = book.begin_eviction(["a", "b"])
        book.failed(a, "stop_timeout")
        book.rollback_unsent(b)
        self.assertEqual(book.runtime["a"].state, State.ERROR)
        self.assertEqual(book.runtime["b"].state, State.READY)
        self.assertEqual(book.committed, 200)

    def test_duplicate_release_does_not_release_other_request(self):
        book = self.make_book(concurrency=2)
        self.load(book, "a")
        first = book.acquire_ready("a", "first", 0)
        second = book.acquire_ready("a", "second", 0)
        self.assertTrue(book.release(first, Outcome.SUCCESS, 1, 10))
        self.assertFalse(book.release(first, Outcome.SUCCESS, 1, 10))
        self.assertEqual(list(book.runtime["a"].leases), [second.lease_id])

    def test_cancel_blocks_new_requests_but_preserves_existing(self):
        book = self.make_book(concurrency=2)
        self.load(book, "a")
        first = book.acquire_ready("a", "first", 0)
        second = book.acquire_ready("a", "second", 0)
        book.release(first, Outcome.ABORTED, 1)
        with self.assertRaises(Conflict):
            book.acquire_ready("a", "new", 1)
        with self.assertRaises(Conflict):
            book.begin_cleanup("a")
        book.release(second, Outcome.SUCCESS, 2)
        self.assertEqual(book.runtime["a"].state, State.ERROR)
        self.assertEqual(book.committed, 100)
        cleanup = book.begin_cleanup("a")
        book.stopped(cleanup)
        self.assertEqual(book.committed, 0)

    def test_load_timeout_keeps_reservation_and_late_success_is_stale(self):
        book = self.make_book()
        operation = book.begin_load("a", MemorySample(1000, 900, 0), 0)
        book.failed(operation, "load_timeout")
        self.assertEqual(book.committed, 100)
        with self.assertRaises(StaleOperation):
            book.loaded(operation, 901)
        self.assertFalse(book.can_load("a", MemorySample(1000, 900, 901), 901))

    def test_old_generation_cannot_mutate_new_load(self):
        book = self.make_book()
        old = self.load(book, "a")
        book.stopped(book.begin_eviction(["a"])[0])
        new = book.begin_load("a", MemorySample(1000, 900, 10), 10)
        self.assertGreater(new.generation, old.generation)
        with self.assertRaises(StaleOperation):
            book.failed(old, "late_failure")
        self.assertEqual(book.runtime["a"].state, State.LOADING)

    def test_live_memory_and_budget_are_both_required(self):
        book = self.make_book()
        self.assertFalse(book.can_load("a", MemorySample(1000, 119, 0), 0))
        self.assertTrue(book.can_load("a", MemorySample(1000, 120, 0), 0))
        book.model_budget = 150
        self.load(book, "a")
        self.assertFalse(book.can_load("b", MemorySample(1000, 900, 0), 0))

    def test_stale_or_future_samples_rejected(self):
        book = self.make_book()
        self.assertFalse(book.can_load("a", MemorySample(1000, 900, 0), 3))
        self.assertFalse(book.can_load("a", MemorySample(1000, 900, 10), 0))

    def test_ready_admission_does_not_double_reserve(self):
        book = self.make_book(concurrency=2)
        self.load(book, "a")
        book.acquire_ready("a", "1", 0)
        book.acquire_ready("a", "2", 0)
        self.assertEqual(book.committed, 100)
        with self.assertRaises(Conflict):
            book.acquire_ready("a", "3", 0)

    def test_ttl_starts_on_last_release(self):
        book = self.make_book(concurrency=2)
        self.load(book, "a")
        first = book.acquire_ready("a", "1", 5)
        second = book.acquire_ready("a", "2", 6)
        book.release(first, Outcome.SUCCESS, 20)
        self.assertFalse(book.ttl_due("a", 100))
        book.release(second, Outcome.SUCCESS, 100)
        self.assertFalse(book.ttl_due("a", 109.99))
        self.assertTrue(book.ttl_due("a", 110))

    def test_heat_decays_before_new_request_without_double_count(self):
        book = self.make_book()
        self.load(book, "a")
        lease = book.acquire_ready("a", "1", 0)
        book.release(lease, Outcome.SUCCESS, 0, 0)
        self.assertAlmostEqual(book.heat("a", 10), 0.5)
        book.acquire_ready("a", "2", 10)
        self.assertAlmostEqual(book.heat("a", 10), 1.5)

    def test_priority_aging_and_fifo_tie(self):
        old = Waiter("old", "a", 0, 1, 0, 1800)
        new = Waiter("new", "b", 5, 2, 180, 1980)
        self.assertLess(waiter_key(old, 180), waiter_key(new, 180))
        tied = Waiter("tie", "c", 0, 3, 0, 1800)
        self.assertLess(waiter_key(old, 180), waiter_key(tied, 180))

    def test_duplicate_eviction_candidate_rejected_without_mutation(self):
        book = self.make_book()
        self.load(book, "a")
        with self.assertRaises(Conflict):
            book.begin_eviction(["a", "a"])
        self.assertEqual(book.runtime["a"].state, State.READY)

    def test_recovery_preserves_lease_invalidates_old_operations_and_requires_proof(self):
        book = self.make_book()
        self.load(book, "a")
        lease = book.acquire_ready("a", "running", 0)
        pending = book.begin_load("b", MemorySample(1000, 900, 0), 0)
        epoch = book.begin_recovery()
        self.assertEqual(book.begin_recovery(), epoch)
        with self.assertRaises(StaleOperation):
            book.loaded(pending, 1)
        with self.assertRaises(Conflict):
            book.finish_recovery(epoch, frozenset(book.specs))
        self.assertEqual(book.committed, 200)
        book.release(lease, Outcome.SUCCESS, 1)
        with self.assertRaises(Conflict):
            book.finish_recovery(epoch, frozenset({"a"}))
        book.finish_recovery(epoch, frozenset(book.specs))
        self.assertEqual(book.committed, 0)
        self.assertTrue(all(r.state == State.UNLOADED for r in book.runtime.values()))
        self.assertTrue(book.can_load("a", MemorySample(1000, 900, 2), 2))

    def test_unknown_usage_differs_from_real_zero_and_does_not_double_count(self):
        book = self.make_book()
        self.load(book, "a")
        zero = book.acquire_ready("a", "zero", 0)
        book.release(zero, Outcome.SUCCESS, 1, tokens=0)
        unknown = book.acquire_ready("a", "unknown", 2)
        book.release(unknown, Outcome.SUCCESS, 3, tokens=None)
        book.release(unknown, Outcome.SUCCESS, 3, tokens=None)
        rejected = book.acquire_ready("a", "rejected", 4)
        book.release(rejected, Outcome.REJECTED, 5)
        self.assertEqual(book.runtime["a"].total_requests, 3)
        self.assertEqual(book.runtime["a"].usage_unknown_requests, 1)
        self.assertEqual(book.runtime["a"].total_tokens, 0)


if __name__ == "__main__":
    unittest.main()
