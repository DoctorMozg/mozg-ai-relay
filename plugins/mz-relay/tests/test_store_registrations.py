"""Store: registrations, delivery binding choice, presence and context facts."""

from __future__ import annotations

import dataclasses
from typing import Any, cast

from mz_relay.envelope import Status
from mz_relay.roster import Harness
from mz_relay.store import ContextState, Registration, Store
from relay_rig import envelope, mid, token


def registration(token: str, *, name: str = "bob", **changes: object) -> Registration:
    base = Registration(
        token=token,
        name=name,
        harness=Harness.CLAUDE,
        client="mcp",
        delivery_mode="native",
        binding={"messaging_socket": f"/run/user/1000/cc-socks/{token}.sock", "pid": 4242},
        pid=4242,
        created_at=100.0,
        last_seen=100.0,
        online=True,
    )
    return dataclasses.replace(base, **cast("dict[str, Any]", changes))


def latest_token(store: Store, name: str = "bob") -> str | None:
    found = store.latest_delivery_registration(name)
    return None if found is None else found.token


class TestRegistrations:
    def test_upserted_registration_reads_back_unchanged(self, store: Store) -> None:
        reg = registration(token(1), binding={"socket": "/tmp/mzr/run/oc-7.sock", "pid": 7})

        store.upsert_registration(reg)

        assert store.registration(token(1)) == reg

    def test_unknown_token_reads_as_none(self, store: Store) -> None:
        assert store.registration(token(1)) is None

    def test_upsert_of_a_known_token_replaces_it(self, store: Store) -> None:
        store.upsert_registration(registration(token(1)))
        replaced = registration(token(1), delivery_mode=None, binding=None, last_seen=150.0)

        store.upsert_registration(replaced)

        assert store.registration(token(1)) == replaced

    def test_tools_only_registration_keeps_a_null_mode_and_binding(self, store: Store) -> None:
        reg = registration(token(1), harness=Harness.OPENCODE, delivery_mode=None, binding=None)

        store.upsert_registration(reg)

        assert store.registration(token(1)) == reg

    def test_set_online_flips_the_flag_and_stamps_last_seen(self, store: Store) -> None:
        store.upsert_registration(registration(token(1)))

        store.set_online(token(1), False, 300.0)

        stored = store.registration(token(1))
        assert stored is not None
        assert (stored.online, stored.last_seen) == (False, 300.0)

    def test_mark_all_offline_takes_every_registration_offline(self, store: Store) -> None:
        store.upsert_registration(registration(token(1)))
        store.upsert_registration(registration(token(2), name="carol"))

        store.mark_all_offline()

        assert store.online_names() == set()
        assert all(
            reg is not None and reg.online is False
            for reg in (store.registration(token(1)), store.registration(token(2)))
        )

    def test_online_names_include_tools_only_registrations(self, store: Store) -> None:
        store.upsert_registration(
            registration(token(1), name="alice", delivery_mode=None, binding=None)
        )
        store.upsert_registration(registration(token(2), name="bob", online=False))
        store.upsert_registration(registration(token(3), name="carol"))
        store.upsert_registration(registration(token(4), name="carol", online=False))

        assert store.online_names() == {"alice", "carol"}


class TestLatestDelivery:
    def test_native_binding_beats_a_newer_fallback_binding(self, store: Store) -> None:
        store.upsert_registration(registration(token(1), created_at=100.0))
        store.upsert_registration(
            registration(token(2), delivery_mode="fallback", created_at=200.0)
        )

        assert latest_token(store) == token(1)

    def test_fallback_binding_is_used_once_the_native_one_is_offline(self, store: Store) -> None:
        store.upsert_registration(registration(token(1), created_at=100.0))
        store.upsert_registration(
            registration(token(2), delivery_mode="fallback", created_at=200.0)
        )

        store.set_online(token(1), False, 250.0)

        assert latest_token(store) == token(2)

    def test_tools_only_registration_is_never_a_delivery_binding(self, store: Store) -> None:
        store.upsert_registration(
            registration(token(1), delivery_mode=None, binding=None, created_at=300.0)
        )

        assert latest_token(store) is None

    def test_newest_of_two_native_bindings_wins(self, store: Store) -> None:
        store.upsert_registration(registration(token(1), created_at=100.0))
        store.upsert_registration(registration(token(2), created_at=200.0))

        assert latest_token(store) == token(2)

    def test_socket_binding_is_preferred_over_fallback_too(self, store: Store) -> None:
        store.upsert_registration(
            registration(token(1), harness=Harness.PI, delivery_mode="socket", created_at=100.0)
        )
        store.upsert_registration(
            registration(token(2), harness=Harness.PI, delivery_mode="fallback", created_at=200.0)
        )

        assert latest_token(store) == token(1)

    def test_registrations_of_other_names_are_ignored(self, store: Store) -> None:
        store.upsert_registration(registration(token(1), name="carol"))

        assert latest_token(store, "bob") is None

    def test_offline_bindings_only_give_none(self, store: Store) -> None:
        store.upsert_registration(registration(token(1), online=False))
        store.upsert_registration(
            registration(token(2), delivery_mode="fallback", online=False, created_at=200.0)
        )

        assert latest_token(store) is None


class TestContexts:
    def test_context_reports_its_turns_with_no_cap_override_and_open(self, store: Store) -> None:
        first = envelope(mid(1), context_id="ctx-a", at=1.0)
        second = envelope(mid(2), context_id="ctx-a", at=2.0)
        store.insert_message(first, Status.QUEUED, 1.0, next_attempt_at=1.0)
        store.insert_message(second, Status.HELD, 2.0, next_attempt_at=None)

        assert store.context_state("ctx-a") == ContextState(turns=2, turn_cap=None, closed_by=None)

    def test_context_never_written_has_no_turns(self, store: Store) -> None:
        # A new message's context does not exist until its insert, and policy asks first.
        assert store.context_state("ctx-new") == ContextState(
            turns=0, turn_cap=None, closed_by=None
        )
