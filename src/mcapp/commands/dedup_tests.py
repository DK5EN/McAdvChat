"""Built-in test suite for DedupMixin (src/mcapp/commands/dedup.py).

Standalone, no pytest — mirrors the house pattern in `router_tests.py`:
a `results: list[tuple[str, bool]]`, `✅ PASS | label` / `❌ FAIL | label`
lines, a `dedup: PASS/FAIL` summary, and `return all(...)`.

Time is injected, never slept: the module's clock (`dedup.time`) is swapped
for a `_FakeClock` whose `now` we advance by hand, then restored in a `finally`.

Run headless:
    uv run python -c "import sys; from mcapp.commands.dedup_tests import \
run_dedup_tests; sys.exit(0 if run_dedup_tests() else 1)"
"""

import hashlib

from . import dedup as dedup_module
from .constants import COMMAND_THROTTLING, DEFAULT_THROTTLE_TIMEOUT
from .dedup import CONTENT_HASH_LENGTH, MSG_ID_TIMEOUT_SECONDS, DedupMixin

_BASE_TIME = 1000.0


class _FakeClock:
    """Controllable stand-in for the `time` module: only `time()` is used by dedup."""

    def __init__(self, start: float = _BASE_TIME) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


class _DedupTestHarness(DedupMixin):
    """Minimal concrete DedupMixin instance wired to a controllable clock."""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self._init_dedup()

    def _reset(self) -> None:
        """Clear all dedup/throttle state between test groups."""
        self._init_dedup()

    # ── msg_id 300 s window ──────────────────────────────────────────────────
    def _check_msg_id_window(self) -> list[tuple[str, bool]]:
        self._reset()
        out: list[tuple[str, bool]] = []
        mid = "msg-42"

        self.clock.now = _BASE_TIME
        unseen = self._is_duplicate_msg_id(mid)
        out.append(("msg_id: unseen id is not a duplicate", not unseen))

        self._mark_msg_id_processed(mid)

        self.clock.now = _BASE_TIME + (MSG_ID_TIMEOUT_SECONDS - 1)
        within = self._is_duplicate_msg_id(mid)
        out.append(("msg_id: same id within 300 s window is a duplicate", within))

        self.clock.now = _BASE_TIME + (MSG_ID_TIMEOUT_SECONDS + 1)
        beyond = self._is_duplicate_msg_id(mid)
        out.append(("msg_id: same id after >300 s window is not a duplicate", not beyond))
        return out

    # ── content-hash: per-command-vs-full split ──────────────────────────────
    def _check_content_hash_split(self) -> list[tuple[str, bool]]:
        self._reset()
        out: list[tuple[str, bool]] = []

        # Throttled command (!time ∈ COMMAND_THROTTLING): args are stripped, so a
        # bare command and the same command + args collapse to one hash.
        h_time_bare = self._get_content_hash("A", "!time")
        h_time_args = self._get_content_hash("A", "!time OE5HWN-12")
        out.append(("hash: throttled !time collapses args (same hash)", h_time_bare == h_time_args))

        # Non-throttled command (!wx ∉ COMMAND_THROTTLING): full command+args is
        # hashed, so differing args yield different hashes.
        h_wx_bare = self._get_content_hash("A", "!wx")
        h_wx_args = self._get_content_hash("A", "!wx graz")
        out.append(("hash: non-throttled !wx keeps args (different hash)", h_wx_bare != h_wx_args))

        # Same split, but with a dst present (different content prefix branch).
        h_time_dst_x = self._get_content_hash("A", "!time x", "20")
        h_time_dst_y = self._get_content_hash("A", "!time y", "20")
        out.append(("hash: throttled !time collapses args with dst", h_time_dst_x == h_time_dst_y))
        h_wx_dst_x = self._get_content_hash("A", "!wx x", "20")
        h_wx_dst_y = self._get_content_hash("A", "!wx y", "20")
        out.append(("hash: non-throttled !wx keeps args with dst", h_wx_dst_x != h_wx_dst_y))

        # Pin the exact content format on both branches (collapse: src:!cmd ;
        # full: src:msg_text) so the distinction is asserted, not just observed.
        expected_collapse = hashlib.md5(b"A:!time", usedforsecurity=False).hexdigest()[
            :CONTENT_HASH_LENGTH
        ]
        out.append(("hash: throttled content == md5('A:!time')", h_time_args == expected_collapse))
        expected_full = hashlib.md5(b"A:!wx graz", usedforsecurity=False).hexdigest()[
            :CONTENT_HASH_LENGTH
        ]
        out.append(("hash: non-throttled content == md5('A:!wx graz')", h_wx_args == expected_full))

        out.append(
            ("hash: output length == CONTENT_HASH_LENGTH", len(h_time_bare) == CONTENT_HASH_LENGTH)
        )
        return out

    # ── content-hash throttle window ─────────────────────────────────────────
    def _check_throttle_window(self) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []

        # Default timeout (command=None → DEFAULT_THROTTLE_TIMEOUT).
        self._reset()
        h_default = "hash-default"
        self.clock.now = _BASE_TIME
        out.append(("throttle: unseen hash is not throttled", not self._is_throttled(h_default)))
        self._mark_content_processed(h_default, None)
        self.clock.now = _BASE_TIME + (DEFAULT_THROTTLE_TIMEOUT - 1)
        out.append(
            ("throttle: default entry throttled within timeout", self._is_throttled(h_default))
        )
        self.clock.now = _BASE_TIME + (DEFAULT_THROTTLE_TIMEOUT + 1)
        out.append(
            ("throttle: default entry expires after timeout", not self._is_throttled(h_default))
        )

        # Per-command timeout (!time → 5 s), the short-throttle branch.
        self._reset()
        h_cmd = "hash-cmd"
        time_timeout = COMMAND_THROTTLING["time"]
        self.clock.now = _BASE_TIME
        self._mark_content_processed(h_cmd, "time")
        self.clock.now = _BASE_TIME + (time_timeout - 1)
        out.append(("throttle: per-command !time throttled within 5 s", self._is_throttled(h_cmd)))
        self.clock.now = _BASE_TIME + (time_timeout + 1)
        out.append(("throttle: per-command !time expires after 5 s", not self._is_throttled(h_cmd)))
        return out

    # ── cleanup sweeps ───────────────────────────────────────────────────────
    def _check_cleanup_sweeps(self) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []

        # msg_id sweep: prunes entries older than the window, keeps fresh ones.
        self._reset()
        self.clock.now = _BASE_TIME
        self._mark_msg_id_processed("stale")
        self.clock.now = _BASE_TIME + 200.0
        self._mark_msg_id_processed("fresh")
        self._cleanup_msg_id_cache(_BASE_TIME + (MSG_ID_TIMEOUT_SECONDS + 1))
        out.append(
            (
                "cleanup: msg_id sweep prunes stale, keeps fresh",
                set(self.processed_msg_ids) == {"fresh"},
            )
        )

        # Throttle sweep: honors the per-entry timeout — a short !time entry is
        # evicted while a default-timeout entry of the same age survives.
        self._reset()
        self.clock.now = _BASE_TIME
        self._mark_content_processed("h-time", "time")
        self._mark_content_processed("h-default", None)
        self._cleanup_throttle_cache(_BASE_TIME + (COMMAND_THROTTLING["time"] + 5))
        out.append(
            (
                "cleanup: throttle sweep honors per-command timeout",
                set(self.command_throttle) == {"h-default"},
            )
        )
        return out

    def collect_results(self) -> list[tuple[str, bool]]:
        results: list[tuple[str, bool]] = []
        results.extend(self._check_msg_id_window())
        results.extend(self._check_content_hash_split())
        results.extend(self._check_throttle_window())
        results.extend(self._check_cleanup_sweeps())
        return results


def run_dedup_tests() -> bool:
    """Run the DedupMixin test suite. Returns True iff every case passes."""
    # getattr/setattr (not `.time`) below: dedup_module.time is the stdlib `time`
    # module, imported but not explicitly reexported, so direct attribute access
    # trips mypy's attr-defined under no_implicit_reexport; setattr also avoids
    # an incompatible-assignment error (Module vs _FakeClock).
    original_time = getattr(dedup_module, "time")  # noqa: B009
    results: list[tuple[str, bool]] = []
    try:
        clock = _FakeClock()
        setattr(dedup_module, "time", clock)  # noqa: B010
        results = _DedupTestHarness(clock).collect_results()  # type: ignore[abstract]  # partial test double for CommandHandler mixins
    finally:
        setattr(dedup_module, "time", original_time)  # noqa: B010

    print("Testing Dedup Logic:")
    print("=" * 50)
    for label, ok in results:
        print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    overall = all(ok for _, ok in results)
    print(f"dedup: {'PASS' if overall else 'FAIL'} ({passed}/{total})")
    return overall


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_dedup_tests() else 1)
