"""Unit tests for the pure version-comparison and transition logic.

Run from the version_monitor/ directory with:  python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import (  # noqa: E402
    SourceError,
    _fdroid_version,
    _metainfo_version,
    _newest_version,
    _packages_version,
)
from state import (  # noqa: E402
    ALL_CLEAR,
    INFO_GITHUB,
    INFO_STORE,
    WARNING,
    WARNING_REMINDER,
    WARNING_UPDATE,
    PlatformState,
    StateStore,
    evaluate,
)
from version_utils import compare_versions, deb_upstream_version, normalize_version  # noqa: E402

CMP = compare_versions


def kinds(result):
    return [e.kind for e in result.events]


def synced(github, store, warning=False, last_warned=None, store_high=None):
    return PlatformState(github, store, warning_active=warning, initialized=True,
                         last_warned_at=last_warned, store_high=store_high)


class VersionUtilsTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_version("v1.18.1"), "1.18.1")
        self.assertEqual(normalize_version("  2.15.3 "), "2.15.3")

    def test_deb_upstream(self):
        self.assertEqual(deb_upstream_version("1.17.17"), "1.17.17")
        self.assertEqual(deb_upstream_version("1:2.3.4-2"), "2.3.4")
        self.assertEqual(deb_upstream_version("1.4.3-1"), "1.4.3")

    def test_compare(self):
        self.assertEqual(CMP("1.18.1", "1.17.17"), 1)
        self.assertEqual(CMP("1.2.0", "1.10.0"), -1)
        self.assertEqual(CMP("2.15.3", "2.15.3"), 0)


class SeedTest(unittest.TestCase):
    def test_seed_in_sync_is_quiet(self):
        result = evaluate(None, "1.0.0", "1.0.0", CMP)
        self.assertEqual(kinds(result), [])
        self.assertFalse(result.warning_active)

    def test_seed_github_ahead_is_quiet(self):
        # GitHub ahead of store is the normal open-source state: no warning.
        result = evaluate(None, "1.1.0", "1.0.0", CMP)
        self.assertEqual(kinds(result), [])
        self.assertFalse(result.warning_active)

    def test_seed_store_ahead_warns(self):
        result = evaluate(None, "1.0.0", "1.1.0", CMP)
        self.assertEqual(kinds(result), [WARNING])
        self.assertTrue(result.events[0].initial)
        self.assertTrue(result.warning_active)


class TransitionTest(unittest.TestCase):
    def test_no_movement_is_quiet(self):
        result = evaluate(synced("1.0.0", "1.0.0"), "1.0.0", "1.0.0", CMP)
        self.assertEqual(kinds(result), [])

    def test_store_jumps_ahead_warns(self):
        result = evaluate(synced("1.0.0", "1.0.0"), "1.0.0", "1.1.0", CMP)
        self.assertEqual(kinds(result), [WARNING])
        self.assertTrue(result.warning_active)

    def test_store_advances_further_while_warning(self):
        prev = synced("1.0.0", "1.1.0", warning=True)
        result = evaluate(prev, "1.0.0", "1.2.0", CMP)
        self.assertEqual(kinds(result), [WARNING_UPDATE])
        self.assertTrue(result.warning_active)

    def test_github_catches_up_all_clear(self):
        prev = synced("1.0.0", "1.1.0", warning=True)
        result = evaluate(prev, "1.1.0", "1.1.0", CMP)
        self.assertEqual(kinds(result), [ALL_CLEAR])
        self.assertFalse(result.warning_active)

    def test_github_passes_store_all_clear(self):
        prev = synced("1.0.0", "1.1.0", warning=True)
        result = evaluate(prev, "1.2.0", "1.1.0", CMP)
        self.assertEqual(kinds(result), [ALL_CLEAR])
        self.assertFalse(result.warning_active)

    def test_github_advances_info(self):
        result = evaluate(synced("1.0.0", "1.0.0"), "1.1.0", "1.0.0", CMP)
        self.assertEqual(kinds(result), [INFO_GITHUB])
        self.assertFalse(result.warning_active)

    def test_store_advances_but_not_ahead_info(self):
        # GitHub at 1.2.0, store moves 1.0.0 -> 1.1.0: caught up but not ahead.
        result = evaluate(synced("1.2.0", "1.0.0"), "1.2.0", "1.1.0", CMP)
        self.assertEqual(kinds(result), [INFO_STORE])
        self.assertFalse(result.warning_active)

    def test_both_advance_store_ahead_warns_only(self):
        result = evaluate(synced("1.0.0", "1.0.0"), "1.1.0", "1.2.0", CMP)
        self.assertEqual(kinds(result), [WARNING])
        self.assertTrue(result.warning_active)

    def test_warning_persists_without_spam(self):
        prev = synced("1.0.0", "1.1.0", warning=True)
        result = evaluate(prev, "1.0.0", "1.1.0", CMP)
        self.assertEqual(kinds(result), [])
        self.assertTrue(result.warning_active)


class PackagesParseTest(unittest.TestCase):
    SAMPLE = (
        "Package: libfoo\nVersion: 9.9.9\n\n"
        "Package: session-desktop\nVersion: 1.17.17\nArchitecture: amd64\n\n"
        "Package: session-messenger-desktop\nVersion: 1.4.3-1\n"
    )

    def test_finds_exact_package(self):
        self.assertEqual(_packages_version(self.SAMPLE, "session-desktop"), "1.17.17")
        self.assertEqual(
            _packages_version(self.SAMPLE, "session-messenger-desktop"), "1.4.3-1"
        )

    def test_missing_package(self):
        self.assertIsNone(_packages_version(self.SAMPLE, "nope"))


class FdroidParseTest(unittest.TestCase):
    # An F-Droid index-v1.json maps each app id to a list of per-build entries
    # (one per ABI split); the live build is the highest integer versionCode.
    SAMPLE = {
        "packages": {
            "network.loki.messenger": [
                {"versionName": "1.33.4", "versionCode": 4519},
                {"versionName": "1.33.5", "versionCode": 4521},  # arm64
                {"versionName": "1.33.5", "versionCode": 4525},  # universal
                {"versionName": "1.33.5", "versionCode": 4522},  # armeabi
            ],
            "other.app": [{"versionName": "9.9.9", "versionCode": 1}],
        }
    }

    def test_highest_version_code_wins(self):
        self.assertEqual(
            _fdroid_version(self.SAMPLE, "network.loki.messenger"), "1.33.5"
        )

    def test_not_lexical_or_list_order(self):
        # 4525 (universal, listed 3rd) must win over the 4521 entry listed first.
        index = {"packages": {"app": [
            {"versionName": "2.0.0", "versionCode": 100},
            {"versionName": "10.0.0", "versionCode": 200},
        ]}}
        self.assertEqual(_fdroid_version(index, "app"), "10.0.0")

    def test_missing_app(self):
        self.assertIsNone(_fdroid_version(self.SAMPLE, "nope"))
        self.assertIsNone(_fdroid_version({"packages": {}}, "network.loki.messenger"))
        self.assertIsNone(_fdroid_version({}, "network.loki.messenger"))


class FlathubParseTest(unittest.TestCase):
    METAINFO = b"""<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>network.loki.Session</id>
  <releases>
    <release version="1.18.1" date="2026-07-09"/>
    <release version="1.18.2" date="2026-10-13"><description/></release>
    <release version="1.9.0" date="2022-01-01"/>
  </releases>
</component>
"""

    def test_metainfo_newest_regardless_of_order(self):
        self.assertEqual(_metainfo_version(self.METAINFO), "1.18.2")

    def test_metainfo_without_releases(self):
        self.assertIsNone(_metainfo_version(b"<component><id>x</id></component>"))

    def test_metainfo_malformed(self):
        with self.assertRaises(SourceError):
            _metainfo_version(b"<component>")

    def test_newest_version(self):
        self.assertEqual(_newest_version(["1.9.0", "v1.10.0", None, ""]), "1.10.0")
        self.assertIsNone(_newest_version([]))


class StateStoreIntegrationTest(unittest.TestCase):
    """Drive a full lifecycle through the real SQLite store round-trip."""

    def _step(self, store, platform, github, store_ver):
        prev = store.get(platform)
        result = evaluate(prev, github, store_ver, compare_versions)
        # Persist the TRUE fetched versions (no clamping) plus the watermark,
        # exactly as the bot does.
        store.upsert(platform, github, store_ver, result.warning_active,
                     result.last_warned_at, result.store_high)
        return kinds(result)

    def test_full_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(os.path.join(tmp, "state.db"))
            try:
                # Seed in sync -> quiet.
                self.assertEqual(self._step(store, "android", "1.0.0", "1.0.0"), [])
                # Store ships ahead -> warning, persisted.
                self.assertEqual(self._step(store, "android", "1.0.0", "1.1.0"), [WARNING])
                self.assertTrue(store.get("android").warning_active)
                # Steady state -> quiet, warning still latched.
                self.assertEqual(self._step(store, "android", "1.0.0", "1.1.0"), [])
                self.assertTrue(store.get("android").warning_active)
                # Store advances further while behind -> re-tag.
                self.assertEqual(
                    self._step(store, "android", "1.0.0", "1.2.0"), [WARNING_UPDATE]
                )
                # GitHub catches up -> all-clear, latch cleared.
                self.assertEqual(self._step(store, "android", "1.2.0", "1.2.0"), [ALL_CLEAR])
                self.assertFalse(store.get("android").warning_active)
                # GitHub advances ahead of store -> informational.
                self.assertEqual(
                    self._step(store, "android", "1.3.0", "1.2.0"), [INFO_GITHUB]
                )
                # Store catches up (not ahead) -> informational.
                self.assertEqual(self._step(store, "android", "1.3.0", "1.3.0"), [INFO_STORE])
            finally:
                store.close()

    def test_state_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.db")
            store = StateStore(path)
            self._step(store, "ios", "1.0.0", "1.1.0")  # -> warning latched
            store.close()
            # Reopen: latched warning must survive so no duplicate warning fires.
            store = StateStore(path)
            try:
                self.assertTrue(store.get("ios").warning_active)
                self.assertEqual(self._step(store, "ios", "1.0.0", "1.1.0"), [])
            finally:
                store.close()


class StrictSyncTest(unittest.TestCase):
    """The APT repo (strict_sync) must match GitHub in BOTH directions."""

    def test_behind_warns_under_strict_sync(self):
        # GitHub ahead of the repo: normally informational, but strict_sync warns.
        result = evaluate(synced("1.0.0", "1.0.0"), "1.1.0", "1.0.0", CMP,
                          strict_sync=True)
        self.assertEqual(kinds(result), [WARNING])
        self.assertTrue(result.warning_active)

    def test_behind_is_only_info_without_strict_sync(self):
        result = evaluate(synced("1.0.0", "1.0.0"), "1.1.0", "1.0.0", CMP,
                          strict_sync=False)
        self.assertEqual(kinds(result), [INFO_GITHUB])
        self.assertFalse(result.warning_active)

    def test_ahead_warns_under_strict_sync(self):
        result = evaluate(synced("1.0.0", "1.0.0"), "1.0.0", "1.1.0", CMP,
                          strict_sync=True)
        self.assertEqual(kinds(result), [WARNING])

    def test_behind_resolves_when_repo_catches_up(self):
        prev = synced("1.1.0", "1.0.0", warning=True, last_warned=100.0)
        result = evaluate(prev, "1.1.0", "1.1.0", CMP, strict_sync=True)
        self.assertEqual(kinds(result), [ALL_CLEAR])
        self.assertFalse(result.warning_active)
        self.assertIsNone(result.last_warned_at)

    def test_behind_divergence_grows(self):
        # Repo still behind and GitHub pulls further ahead -> re-tag.
        prev = synced("1.1.0", "1.0.0", warning=True, last_warned=100.0)
        result = evaluate(prev, "1.2.0", "1.0.0", CMP, strict_sync=True)
        self.assertEqual(kinds(result), [WARNING_UPDATE])
        self.assertTrue(result.warning_active)

    def test_seed_behind_warns_under_strict_sync(self):
        result = evaluate(None, "1.1.0", "1.0.0", CMP, strict_sync=True, now=50.0)
        self.assertEqual(kinds(result), [WARNING])
        self.assertTrue(result.warning_active)
        self.assertEqual(result.last_warned_at, 50.0)


class ReminderTest(unittest.TestCase):
    INTERVAL = 12 * 3600

    def test_reminder_fires_after_interval(self):
        prev = synced("1.0.0", "1.1.0", warning=True, last_warned=0.0)
        result = evaluate(prev, "1.0.0", "1.1.0", CMP,
                          now=self.INTERVAL, reminder_interval=self.INTERVAL)
        self.assertEqual(kinds(result), [WARNING_REMINDER])
        self.assertTrue(result.warning_active)
        self.assertEqual(result.last_warned_at, self.INTERVAL)  # timer reset

    def test_no_reminder_before_interval(self):
        prev = synced("1.0.0", "1.1.0", warning=True, last_warned=0.0)
        result = evaluate(prev, "1.0.0", "1.1.0", CMP,
                          now=self.INTERVAL - 1, reminder_interval=self.INTERVAL)
        self.assertEqual(kinds(result), [])
        self.assertEqual(result.last_warned_at, 0.0)  # timer untouched

    def test_worsening_resets_reminder_timer(self):
        prev = synced("1.0.0", "1.1.0", warning=True, last_warned=0.0)
        result = evaluate(prev, "1.0.0", "1.2.0", CMP,
                          now=5000.0, reminder_interval=self.INTERVAL)
        self.assertEqual(kinds(result), [WARNING_UPDATE])
        self.assertEqual(result.last_warned_at, 5000.0)

    def test_no_reminder_when_interval_unset(self):
        prev = synced("1.0.0", "1.1.0", warning=True, last_warned=0.0)
        result = evaluate(prev, "1.0.0", "1.1.0", CMP, now=self.INTERVAL * 10)
        self.assertEqual(kinds(result), [])


class StoreWatermarkTest(unittest.TestCase):
    """App stores are monotonic, but the APIs (Apple's iTunes lookup) are
    eventually consistent and briefly re-serve the previous version. A
    dip-and-recover must not re-fire the "updated" INFO_STORE, and the true
    version must still be persisted so a genuine pull stays visible."""

    def test_recover_to_prior_high_is_quiet(self):
        # Store already reached 2.15.4 (recorded as the high); it dipped to
        # 2.15.3 (persisted true) and now recovers. No duplicate INFO_STORE.
        prev = synced("2.15.4", "2.15.3", store_high="2.15.4")
        result = evaluate(prev, "2.15.4", "2.15.4", CMP)
        self.assertEqual(kinds(result), [])
        self.assertEqual(result.store_high, "2.15.4")

    def test_new_high_still_notifies(self):
        prev = synced("2.15.5", "2.15.4", store_high="2.15.4")
        result = evaluate(prev, "2.15.5", "2.15.5", CMP)
        self.assertEqual(kinds(result), [INFO_STORE])
        self.assertEqual(result.store_high, "2.15.5")

    def test_dip_is_quiet_and_persists_true_version(self):
        # A backward reading is silent, but the watermark holds and the true
        # (lower) version is what gets persisted -- so a real pull stays visible.
        prev = synced("2.15.4", "2.15.4", store_high="2.15.4")
        result = evaluate(prev, "2.15.4", "2.15.3", CMP)
        self.assertEqual(kinds(result), [])
        self.assertEqual(result.store_high, "2.15.4")


class AppleFlapIntegrationTest(unittest.TestCase):
    """The exact reported bug, driven through the real SQLite store."""

    def _step(self, store, platform, github, store_ver):
        prev = store.get(platform)
        result = evaluate(prev, github, store_ver, compare_versions)
        store.upsert(platform, github, store_ver, result.warning_active,
                     result.last_warned_at, result.store_high)
        return kinds(result)

    def test_flap_fires_info_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(os.path.join(tmp, "state.db"))
            try:
                # GitHub already at 2.15.4, App Store still 2.15.3: seed quiet.
                self.assertEqual(self._step(store, "ios", "2.15.4", "2.15.3"), [])
                # App Store catches up -> the (single, legitimate) INFO_STORE.
                self.assertEqual(
                    self._step(store, "ios", "2.15.4", "2.15.4"), [INFO_STORE]
                )
                # Apple flaps back to 2.15.3: silent, but the TRUE version is
                # persisted (a genuine pull would look exactly like this and
                # stays visible), while the watermark holds at 2.15.4.
                self.assertEqual(self._step(store, "ios", "2.15.4", "2.15.3"), [])
                self.assertEqual(store.get("ios").store_version, "2.15.3")
                self.assertEqual(store.get("ios").store_high, "2.15.4")
                # Apple recovers to 2.15.4: NO duplicate notification.
                self.assertEqual(self._step(store, "ios", "2.15.4", "2.15.4"), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
