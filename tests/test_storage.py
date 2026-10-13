import copy
import json
import os
import shutil

import pytest
import yaml

from rednotebook import activities, storage, sync
from rednotebook.data import Month


KEY = "2026-09"


def legacy(directory, content=None):
    directory.mkdir(parents=True, exist_ok=True)
    content = content if content is not None else {1: {"text": "base", "Work": {"old": None}}}
    (directory / f"{KEY}.txt").write_text(yaml.safe_dump(content), encoding="utf-8")
    return storage.load_all_months_from_disk(directory)


def replicate(source, destination, reverse=False, rename=False):
    target = destination / sync.REVISION_DIR
    target.mkdir(parents=True, exist_ok=True)
    files = sorted((source / sync.REVISION_DIR).glob("*.json"), reverse=reverse)
    for path in files:
        name = f"Drive conflicted copy {path.name}" if rename else path.name
        shutil.copy2(path, target / name)


def edit(months, text=None, category=None):
    month = months[KEY]
    if text is not None:
        month.get_day(1).text = text
    if category is not None:
        month.get_day(1).content["Work"][category] = None
    month.edited = True


def content(months):
    return {key: storage._get_dict(month) for key, month in months.items()}


def test_identical_legacy_imports_have_same_root_before_returning(tmp_path):
    left = legacy(tmp_path / "left")
    right = legacy(tmp_path / "right")
    assert left[KEY]._sync_heads == right[KEY]._sync_heads
    assert (
        sync.read_graph(tmp_path / "left").records[left[KEY]._sync_heads[0]]["event"]
        == "legacy-import"
    )
    assert len(list((tmp_path / "left" / sync.REVISION_DIR).glob("*.json"))) == 1
    assert left[KEY]._sync_baseline == sync.normalize_snapshot(KEY, storage._get_dict(left[KEY]))
    assert not left[KEY]._sync_pending and left[KEY]._sync_durable
    assert not left[KEY]._sync_cache_failed
    header = (tmp_path / "left" / f"{KEY}.txt").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith(storage.CACHE_MAGIC)
    assert json.loads(header[len(storage.CACHE_MAGIC) :])["heads"] == list(left[KEY]._sync_heads)
    assert not storage.save_months_to_disk(left, tmp_path / "left")


def test_legacy_tags_and_activity_v1_migrate_to_stable_canonical_root(tmp_path):
    item = json.dumps(
        {
            "v": 1,
            "id": "legacy",
            "text": "Title",
            "start": "09:00",
            "end": "10:00",
            "alert": "08:55",
        }
    )
    original = {
        1: {"text": "base", "Tags": {"legacy tag": None}, activities.ACTIVITIES_KEY: {item: None}}
    }
    loaded = legacy(tmp_path, original)
    day = loaded[KEY].get_day(1)
    assert "legacy tag" in day.content and "Tags" not in day.content
    assert day.activities[0].title == "Title"
    assert not storage.save_months_to_disk(loaded, tmp_path)
    assert content(storage.load_all_months_from_disk(tmp_path)) == content(loaded)


def test_two_peers_merge_revisions_only_and_repeat_without_duplicates(tmp_path):
    directories = [tmp_path / name for name in ("left", "right")]
    peers = [legacy(directory) for directory in directories]
    for peer, directory, name in zip(peers, directories, ("A", "B")):
        edit(peer, category=name)
        assert storage.save_months_to_disk(peer, directory)
    replicate(directories[0], directories[1], reverse=True, rename=True)
    replicate(directories[1], directories[0], reverse=True)
    for peer, directory in zip(peers, directories):
        assert storage.save_months_to_disk(peer, directory)
    assert content(peers[0]) == content(peers[1])
    assert peers[0][KEY].get_day(1).content["Work"] == {"old": None, "A": None, "B": None}
    for peer, directory in zip(peers, directories):
        before = sorted((directory / sync.REVISION_DIR).glob("*.json"))
        assert not storage.save_months_to_disk(peer, directory)
        assert before == sorted((directory / sync.REVISION_DIR).glob("*.json"))
        assert content(storage.load_all_months_from_disk(directory)) == content(peer)


@pytest.mark.parametrize("conflicting", [False, True])
def test_two_peer_text_merge_and_causal_resolution(tmp_path, conflicting):
    directories = [tmp_path / name for name in ("left", "right")]
    original = {1: {"text": "one\ntwo\nthird\n"}}
    peers = [legacy(directory, original) for directory in directories]
    versions = (
        ("LEFT\ntwo\nthird\n", "RIGHT\ntwo\nthird\n")
        if conflicting
        else ("ONE\ntwo\nthird\n", "one\nTWO\nthird\n")
    )
    for peer, directory, text in zip(peers, directories, versions):
        edit(peer, text=text)
        storage.save_months_to_disk(peer, directory)
    replicate(directories[0], directories[1], reverse=True, rename=True)
    replicate(directories[1], directories[0], reverse=True)
    for peer, directory in zip(peers, directories):
        assert storage.save_months_to_disk(peer, directory)
    assert content(peers[0]) == content(peers[1])
    text = peers[0][KEY].get_day(1).text
    if conflicting:
        assert "sync conflict" in text
        assert all(version in text for version in versions)
    else:
        assert text == "ONE\nTWO\nthird\n"
    edit(peers[0], text="resolved")
    storage.save_months_to_disk(peers[0], directories[0])
    replicate(directories[0], directories[1], reverse=True)
    assert storage.save_months_to_disk(peers[1], directories[1])
    assert peers[1][KEY].get_day(1).text == "resolved"
    assert content(peers[0]) == content(peers[1])


def test_three_peers_offline_late_contributor_and_later_edit(tmp_path):
    directories = [tmp_path / name for name in ("A", "B", "C")]
    peers = [legacy(directory) for directory in directories]
    for peer, directory, name in zip(peers, directories, ("A", "B", "C")):
        edit(peer, category=name)
        storage.save_months_to_disk(peer, directory)
    replicate(directories[1], directories[0])
    storage.save_months_to_disk(peers[0], directories[0])
    del peers[0][KEY].get_day(1).content["Work"]["B"]
    edit(peers[0], category="later")
    storage.save_months_to_disk(peers[0], directories[0])
    for source in directories:
        for target in directories:
            if source != target:
                replicate(source, target, reverse=True)
    for peer, directory in zip(peers, directories):
        storage.save_months_to_disk(peer, directory)
    assert content(peers[0]) == content(peers[1]) == content(peers[2])
    assert peers[0][KEY].get_day(1).content["Work"] == {
        "old": None,
        "A": None,
        "C": None,
        "later": None,
    }


def test_local_branch_committed_before_unseen_remote_arrival(tmp_path):
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    left, right = legacy(left_dir), legacy(right_dir)
    observed = left[KEY]._sync_heads
    baseline = copy.deepcopy(left[KEY]._sync_baseline)
    edit(right, text="remote complete text")
    storage.save_months_to_disk(right, right_dir)
    remote_head = right[KEY]._sync_heads[0]
    edit(left, text="local complete text")
    replicate(right_dir, left_dir)
    assert storage.save_months_to_disk(left, left_dir)
    loaded = sync.read_graph(left_dir)
    local = [
        record
        for record in loaded.records.values()
        if record["content"]["1"]["text"] == "local complete text"
    ][0]
    assert local["parents"] == list(observed)
    assert local["base"] == baseline
    assert remote_head not in local["parents"]
    text = left[KEY].get_day(1).text
    assert "local complete text" in text and "remote complete text" in text
    assert "sync conflict" in text


def test_remote_poll_updates_existing_objects_and_discovers_new_months(tmp_path):
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    left, right = legacy(left_dir), legacy(right_dir)
    month, day = left[KEY], left[KEY].get_day(1)
    edit(right, category="remote")
    new_month = Month(2026, 10, {2: {"text": "new month"}})
    new_month.edited = True
    right["2026-10"] = new_month
    storage.save_months_to_disk(right, right_dir)
    replicate(right_dir, left_dir)
    assert not month.edited
    assert storage.save_months_to_disk(left, left_dir)
    assert left[KEY] is month and left[KEY].get_day(1) is day
    assert "remote" in day.content["Work"]
    assert left["2026-10"].get_day(2).text == "new month"
    assert not storage.save_months_to_disk(left, left_dir)


def test_load_months_arriving_only_as_revision_files(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    months = legacy(
        source, {1: {"text": "Unicode \u00e3 \u4e2d", "unknown": {"entries": [1, None]}}}
    )
    replicate(source, destination, reverse=True, rename=True)
    assert not (destination / f"{KEY}.txt").exists()
    loaded = storage.load_all_months_from_disk(destination)
    assert content(loaded) == content(months)
    assert (destination / f"{KEY}.txt").exists()


def test_restarting_with_stale_or_malformed_monthly_cache_uses_revisions(tmp_path):
    months = legacy(tmp_path)
    edit(months, text="authoritative")
    storage.save_months_to_disk(months, tmp_path)
    path = tmp_path / f"{KEY}.txt"
    path.write_text("not: [valid yaml", encoding="utf-8")
    loaded = storage.load_all_months_from_disk(tmp_path)
    assert loaded[KEY].get_day(1).text == "authoritative"
    assert yaml.safe_load(path.read_text(encoding="utf-8"))[1]["text"] == "authoritative"
    assert len(list((tmp_path / sync.REVISION_DIR).glob("*.json"))) == 2


def test_child_before_parent_never_seeds_stale_cache(tmp_path, caplog):
    source, destination = tmp_path / "source", tmp_path / "destination"
    months = legacy(source)
    initial_id = months[KEY]._sync_heads[0]
    edit(months, text="complete revision")
    storage.save_months_to_disk(months, source)
    destination.mkdir()
    cache = destination / f"{KEY}.txt"
    cache.write_text(yaml.safe_dump({1: {"text": "stale incompatible root"}}), encoding="utf-8")
    target = destination / sync.REVISION_DIR
    target.mkdir()
    paths = list((source / sync.REVISION_DIR).glob("*.json"))
    initial = next(path for path in paths if initial_id in path.name)
    child = next(path for path in paths if initial_id not in path.name)
    shutil.copy2(child, target / child.name)
    loaded = storage.load_all_months_from_disk(destination)
    assert list(loaded) == [KEY]
    assert loaded[KEY].days == {} and loaded[KEY]._sync_pending
    assert not loaded[KEY]._sync_durable and not hasattr(loaded[KEY], "_sync_baseline")
    assert storage.pending_revisions(destination)
    assert not storage.save_months_to_disk(loaded, destination)
    assert "stale incompatible root" in cache.read_text(encoding="utf-8")
    assert len(list(target.glob("*.json"))) == 1
    assert "Deferring month" in caplog.text
    shutil.copy2(initial, target / initial.name)
    assert storage.save_months_to_disk(loaded, destination)
    assert loaded[KEY].get_day(1).text == "complete revision"
    assert not loaded[KEY]._sync_pending and loaded[KEY]._sync_durable
    assert not storage.pending_revisions(destination)


def test_out_of_order_chain_waits_for_all_parents(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    months = legacy(source)
    ids = [months[KEY]._sync_heads[0]]
    for text in ("second", "third"):
        edit(months, text=text)
        storage.save_months_to_disk(months, source)
        ids.append(months[KEY]._sync_heads[0])
    target = destination / sync.REVISION_DIR
    target.mkdir(parents=True)
    peer = {}
    for index in (2, 0, 1):
        path = next((source / sync.REVISION_DIR).glob(f"*{ids[index]}.json"))
        shutil.copy2(path, target / path.name)
        changed = storage.save_months_to_disk(peer, destination)
        if index != 1:
            assert not changed and peer[KEY].days == {} and peer[KEY]._sync_pending
    assert peer[KEY].get_day(1).text == "third"


@pytest.mark.parametrize("filename", ["2026-09.partial.json", "unknown-drive-name.json"])
def test_partial_revision_blocks_legacy_migration_without_touching_it(tmp_path, filename):
    cache = tmp_path / f"{KEY}.txt"
    cache.write_text(yaml.safe_dump({1: {"text": "stale"}}), encoding="utf-8")
    directory = tmp_path / sync.REVISION_DIR
    directory.mkdir()
    partial = directory / filename
    partial.write_text('{"version":1,', encoding="utf-8")
    loaded = storage.load_all_months_from_disk(tmp_path)
    assert loaded[KEY].days == {} and loaded[KEY]._sync_pending
    assert storage.pending_revisions(tmp_path)
    assert partial.read_text(encoding="utf-8") == '{"version":1,'
    assert len(list(directory.glob("*.json"))) == 1
    assert "stale" in cache.read_text(encoding="utf-8")


def test_remote_activity_removal_bypasses_preserving_content_setter(tmp_path):
    item = activities.Activity("one", "Meeting", "", "09:00", "10:00")
    records = {activities.encode_activity(item): None}
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    original = {1: {"text": "base", activities.ACTIVITIES_KEY: records}}
    left, right = legacy(left_dir, original), legacy(right_dir, original)
    day = left[KEY].get_day(1)
    right[KEY].get_day(1).remove_activity("one")
    storage.save_months_to_disk(right, right_dir)
    replicate(right_dir, left_dir)
    assert storage.save_months_to_disk(left, left_dir)
    assert day.activities == []
    assert activities.ACTIVITIES_KEY not in day.content


def test_remote_day_deletion_keeps_editor_reference_safe_for_later_edit(tmp_path):
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    left, right = legacy(left_dir), legacy(right_dir)
    day = left[KEY].get_day(1)
    right[KEY].days.clear()
    right[KEY].edited = True
    storage.save_months_to_disk(right, right_dir)
    replicate(right_dir, left_dir)
    assert storage.save_months_to_disk(left, left_dir)
    assert day.empty and day.content == {"text": ""}
    assert left[KEY].get_day(1) is day
    day.text = "Edit after remote deletion"
    left[KEY].edited = True
    assert storage.save_months_to_disk(left, left_dir)
    assert storage.load_all_months_from_disk(left_dir)[KEY].get_day(1).text == day.text


@pytest.mark.parametrize("failure_kind", ["revision", "cache"])
def test_atomic_failure_retains_local_model_and_retry(tmp_path, monkeypatch, failure_kind):
    months = legacy(tmp_path)
    month = months[KEY]
    edit(months, text="local unsaved")
    old_cache = (tmp_path / f"{KEY}.txt").read_bytes()
    original = sync.os.replace

    def fail(source, target):
        if (str(target).endswith(".json")) == (failure_kind == "revision"):
            raise OSError("simulated replacement failure")
        return original(source, target)

    monkeypatch.setattr(sync.os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        storage.save_months_to_disk(months, tmp_path)
    assert month.edited and month.get_day(1).text == "local unsaved"
    assert not month._sync_pending
    assert month._sync_durable == (failure_kind == "cache")
    assert month._sync_cache_failed == (failure_kind == "cache")
    assert (tmp_path / f"{KEY}.txt").read_bytes() == old_cache
    assert not list(tmp_path.rglob("*.tmp"))
    monkeypatch.setattr(sync.os, "replace", original)
    storage.save_months_to_disk(months, tmp_path)
    assert not month.edited
    assert month._sync_durable and not month._sync_cache_failed
    assert storage.load_all_months_from_disk(tmp_path)[KEY].get_day(1).text == "local unsaved"
    assert len(list((tmp_path / sync.REVISION_DIR).glob("*.json"))) == 2


def test_edit_after_cache_failure_descends_from_durable_local_branch(tmp_path, monkeypatch):
    months = legacy(tmp_path)
    edit(months, text="first durable edit")
    original = sync.os.replace

    def fail_cache(source, target):
        if str(target).endswith(".txt"):
            raise OSError("cache unavailable")
        return original(source, target)

    monkeypatch.setattr(sync.os, "replace", fail_cache)
    with pytest.raises(OSError):
        storage.save_months_to_disk(months, tmp_path)
    durable = months[KEY]._sync_heads[0]
    edit(months, text="later edit")
    monkeypatch.setattr(sync.os, "replace", original)
    assert storage.save_months_to_disk(months, tmp_path)
    final = sync.read_graph(tmp_path).records[months[KEY]._sync_heads[0]]
    assert final["parents"] == [durable]
    assert months[KEY].get_day(1).text == "later edit"


def test_month_replacement_has_no_remove_target_gap(tmp_path, monkeypatch):
    months = legacy(tmp_path)
    edit(months, text="new")
    original = sync.os.replace
    observations = []

    def observe(source, target):
        if str(target).endswith(".txt"):
            observations.append(os.path.exists(target))
        return original(source, target)

    monkeypatch.setattr(sync.os, "replace", observe)
    storage.save_months_to_disk(months, tmp_path)
    assert observations == [True]


def test_save_as_initializes_destination_roots_and_baselines(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    months = legacy(source)
    edit(months, text="desired")
    storage.save_months_to_disk(months, source)
    source_head = months[KEY]._sync_heads[0]
    destination.mkdir()
    assert storage.save_months_to_disk(months, destination, saveas=True)
    loaded = sync.read_graph(destination)
    assert loaded.complete(KEY)
    assert all(record["parents"] == [] for record in loaded.records.values())
    assert source_head not in loaded.records
    assert content(storage.load_all_months_from_disk(destination)) == content(months)
    assert not storage.save_months_to_disk(months, destination)
    destination_root = months[KEY]._sync_heads[0]
    edit(months, text="destination edit")
    assert storage.save_months_to_disk(months, destination)
    loaded = sync.read_graph(destination)
    assert loaded.records[months[KEY]._sync_heads[0]]["parents"] == [destination_root]


def test_cache_only_repair_does_not_report_model_change(tmp_path):
    months = legacy(tmp_path)
    (tmp_path / f"{KEY}.txt").write_text("manual stale cache", encoding="utf-8")
    assert not storage.save_months_to_disk(months, tmp_path)
    assert months[KEY].get_day(1).text == "base"
    assert (
        yaml.safe_load((tmp_path / f"{KEY}.txt").read_text(encoding="utf-8"))[1]["text"] == "base"
    )


def test_pending_remote_month_retains_local_branch_and_later_reconciles(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    left, right = legacy(target), legacy(source)
    edit(right, category="remote one")
    storage.save_months_to_disk(right, source)
    parent = right[KEY]._sync_heads[0]
    edit(right, category="remote two")
    storage.save_months_to_disk(right, source)
    tip = right[KEY]._sync_heads[0]
    child = next((source / sync.REVISION_DIR).glob(f"*{tip}.json"))
    shutil.copy2(child, target / sync.REVISION_DIR / child.name)
    edit(left, category="local")
    assert storage.save_months_to_disk(left, target)
    assert left[KEY].edited
    assert left[KEY]._sync_pending and left[KEY]._sync_durable
    assert not left[KEY]._sync_cache_failed
    assert "local" in left[KEY].get_day(1).content["Work"]
    assert "remote two" not in left[KEY].get_day(1).content["Work"]
    intermediate = next((source / sync.REVISION_DIR).glob(f"*{parent}.json"))
    shutil.copy2(intermediate, target / sync.REVISION_DIR / intermediate.name)
    assert storage.save_months_to_disk(left, target)
    assert not left[KEY].edited
    assert not left[KEY]._sync_pending and left[KEY]._sync_durable
    assert set(left[KEY].get_day(1).content["Work"]) == {"old", "local", "remote one", "remote two"}


def test_failed_cache_with_unseen_remote_keeps_local_until_success(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    left, right = legacy(target), legacy(source)
    edit(right, category="remote")
    storage.save_months_to_disk(right, source)
    edit(left, category="local")
    replicate(source, target)
    replace = sync.os.replace

    def fail_cache(source, destination):
        if str(destination).endswith(".txt"):
            raise OSError("cache failure")
        return replace(source, destination)

    monkeypatch.setattr(sync.os, "replace", fail_cache)
    with pytest.raises(OSError):
        storage.save_months_to_disk(left, target)
    assert left[KEY].edited
    assert "local" in left[KEY].get_day(1).content["Work"]
    assert "remote" not in left[KEY].get_day(1).content["Work"]
    edit(left, category="later local")
    monkeypatch.setattr(sync.os, "replace", replace)
    assert storage.save_months_to_disk(left, target)
    assert set(left[KEY].get_day(1).content["Work"]) == {"old", "local", "later local", "remote"}


def test_notification_after_conflict_and_cache_failure_retries_without_duplicate_revisions(
    tmp_path, monkeypatch
):
    first = activities.Activity("one", "First", "", "09:00", "10:00", 5)
    original = {
        1: {"text": "base", activities.ACTIVITIES_KEY: {activities.encode_activity(first): None}}
    }
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    left, right = legacy(left_dir, original), legacy(right_dir, original)
    left[KEY].get_day(1).upsert_activity(
        activities.Activity("one", "Left", "", "09:00", "10:00", 5)
    )
    right[KEY].get_day(1).upsert_activity(
        activities.Activity("one", "Right", "", "09:00", "10:00", 5)
    )
    storage.save_months_to_disk(left, left_dir)
    storage.save_months_to_disk(right, right_dir)
    replicate(right_dir, left_dir)
    storage.save_months_to_disk(left, left_dir)
    left[KEY].get_day(1).mark_activity_notified("one", "2026-09-01T08:55")
    replace = sync.os.replace

    def fail_cache(source, destination):
        if str(destination).endswith(".txt"):
            raise OSError("cache failure")
        return replace(source, destination)

    monkeypatch.setattr(sync.os, "replace", fail_cache)
    with pytest.raises(OSError):
        storage.save_months_to_disk(left, left_dir)
    count = len(list((left_dir / sync.REVISION_DIR).glob("*.json")))
    with pytest.raises(OSError):
        storage.save_months_to_disk(left, left_dir)
    assert len(list((left_dir / sync.REVISION_DIR).glob("*.json"))) == count
    monkeypatch.setattr(sync.os, "replace", replace)
    storage.save_months_to_disk(left, left_dir)
    assert left[KEY].get_day(1).activities[0].notified_for == "2026-09-01T08:55"
    record = json.loads(next(iter(left[KEY].get_day(1).content[activities.ACTIVITIES_KEY])))
    assert sync.CONFLICT_KEY in record
    assert not storage.save_months_to_disk(left, left_dir)


def test_unbased_month_with_existing_revisions_is_not_seeded_from_stale_model(tmp_path):
    authoritative = legacy(tmp_path)
    stale = Month(2026, 9, {1: {"text": "stale cache model"}})
    stale.edited = True
    with pytest.raises(OSError, match="no observed sync baseline"):
        storage.save_months_to_disk({KEY: stale}, tmp_path)
    assert stale.edited and stale.get_day(1).text == "stale cache model"
    assert not stale._sync_durable and not stale._sync_pending
    assert len(list((tmp_path / sync.REVISION_DIR).glob("*.json"))) == 1
    assert (
        storage.load_all_months_from_disk(tmp_path)[KEY].get_day(1).text
        == authoritative[KEY].get_day(1).text
    )


def test_invalid_local_schema_retains_model_and_does_not_write(tmp_path):
    month = Month(2026, 9, {1: {"text": 5}})
    with pytest.raises(OSError, match="Invalid local month"):
        storage.save_months_to_disk({KEY: month}, tmp_path)
    assert month.edited
    assert month.get_day(1).content["text"] == 5
    assert not (tmp_path / sync.REVISION_DIR).exists()


def test_month_key_mismatch_cannot_write_content_under_wrong_date(tmp_path):
    month = Month(2026, 10, {1: {"text": "October"}})
    with pytest.raises(OSError, match="does not match its date"):
        storage.save_months_to_disk({KEY: month}, tmp_path)
    assert month.edited and not month._sync_durable
    assert month.get_day(1).text == "October"
    assert not (tmp_path / sync.REVISION_DIR).exists()


@pytest.mark.parametrize("edited_flag", [False, True])
def test_pending_startup_edit_is_refused_without_false_root_or_success(tmp_path, edited_flag):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(source)
    parent = original[KEY]._sync_heads[0]
    edit(original, text="remote")
    storage.save_months_to_disk(original, source)
    destination.mkdir()
    (destination / sync.REVISION_DIR).mkdir()
    child = next(
        path for path in (source / sync.REVISION_DIR).glob("*.json") if parent not in path.name
    )
    shutil.copy2(child, destination / sync.REVISION_DIR / child.name)
    months = storage.load_all_months_from_disk(destination)
    month = months[KEY]
    assert month._sync_pending and not month._sync_durable
    assert not month._sync_cache_failed and not hasattr(month, "_sync_baseline")
    month.get_day(1).text = "Typed before remote parents arrived"
    month.edited = edited_flag
    with pytest.raises(OSError, match="retained and were not saved"):
        storage.save_months_to_disk(months, destination)
    assert month.edited and month._sync_pending and not month._sync_durable
    assert month.get_day(1).text == "Typed before remote parents arrived"
    assert len(list((destination / sync.REVISION_DIR).glob("*.json"))) == 1
    assert not (destination / f"{KEY}.txt").exists()
    replicate(source, destination)
    with pytest.raises(OSError, match="no observed sync baseline"):
        storage.save_months_to_disk(months, destination)
    assert not month._sync_pending and not month._sync_durable
    assert month.get_day(1).text == "Typed before remote parents arrived"
    assert len(list((destination / sync.REVISION_DIR).glob("*.json"))) == 2


def test_unidentifiable_pending_revision_warns_even_without_months(tmp_path):
    directory = tmp_path / sync.REVISION_DIR
    directory.mkdir()
    partial = directory / "unknown.json"
    partial.write_text("partial JSON", encoding="utf-8")
    assert storage.load_all_months_from_disk(tmp_path) == {}
    assert storage.pending_revisions(tmp_path)
    month = Month(2026, 9, {1: {"text": "local"}})
    with pytest.raises(OSError, match="no observed sync baseline"):
        storage.save_months_to_disk({KEY: month}, tmp_path)
    assert month._sync_pending and not month._sync_durable
    assert month.edited and month.get_day(1).text == "local"
    assert list(directory.iterdir()) == [partial]


def test_pending_durable_revision_retry_does_not_write_again(tmp_path):
    months = legacy(tmp_path)
    unresolved = sync.make_revision(KEY, ["0" * 64], {}, {"1": {"text": "unresolved"}})
    sync.write_revision(tmp_path, unresolved)
    edit(months, text="local branch")
    assert storage.save_months_to_disk(months, tmp_path)
    assert months[KEY]._sync_pending and months[KEY]._sync_durable
    assert months[KEY].edited and not months[KEY]._sync_cache_failed
    files = list((tmp_path / sync.REVISION_DIR).glob("*.json"))
    assert not storage.save_months_to_disk(months, tmp_path)
    assert months[KEY]._sync_pending and months[KEY]._sync_durable
    assert list((tmp_path / sync.REVISION_DIR).glob("*.json")) == files


def test_category_conflict_metadata_is_stable_on_poll_and_restart(tmp_path):
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    original = {1: {"text": "base", "Work": {"entry": {"old": None}}}}
    left, right = legacy(left_dir, original), legacy(right_dir, original)
    left[KEY].get_day(1).content["Work"]["entry"] = None
    left[KEY].edited = True
    right[KEY].get_day(1).content["Work"]["entry"] = {"new": None}
    right[KEY].edited = True
    storage.save_months_to_disk(left, left_dir)
    storage.save_months_to_disk(right, right_dir)
    replicate(right_dir, left_dir)
    assert storage.save_months_to_disk(left, left_dir)
    day = left[KEY].get_day(1)
    assert day.content["Work"] == {"entry": {"new": None}}
    assert day.content[sync.CONFLICT_KEY]["version"] == 1
    before = content(left)
    assert not storage.save_months_to_disk(left, left_dir)
    assert content(left) == before
    assert content(storage.load_all_months_from_disk(left_dir)) == before


def test_empty_pending_placeholder_can_retry_cache_failure_after_parent_arrival(
    tmp_path, monkeypatch
):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(source)
    parent = original[KEY]._sync_heads[0]
    edit(original, text="complete remote")
    storage.save_months_to_disk(original, source)
    target = destination / sync.REVISION_DIR
    target.mkdir(parents=True)
    child = next(
        path for path in (source / sync.REVISION_DIR).glob("*.json") if parent not in path.name
    )
    shutil.copy2(child, target / child.name)
    months = storage.load_all_months_from_disk(destination)
    month = months[KEY]
    replicate(source, destination)
    replace = sync.os.replace

    def fail_cache(source, destination):
        if str(destination).endswith(".txt"):
            raise OSError("cache failed")
        return replace(source, destination)

    monkeypatch.setattr(sync.os, "replace", fail_cache)
    with pytest.raises(OSError, match="cache failed"):
        storage.save_months_to_disk(months, destination)
    assert not month._sync_pending and month._sync_cache_failed
    assert not month._sync_durable and not month.edited
    assert not hasattr(month, "_sync_baseline")
    assert storage._get_dict(month) == {}
    monkeypatch.setattr(sync.os, "replace", replace)
    assert storage.save_months_to_disk(months, destination)
    assert month.get_day(1).text == "complete remote"
    assert month._sync_durable and not month._sync_cache_failed
    assert not month._sync_pending and not month.edited


def test_cache_first_delivery_cannot_seed_root_or_resurrect_deleted_category(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(source)
    root_id = original[KEY]._sync_heads[0]
    destination.mkdir()
    shutil.copy2(source / f"{KEY}.txt", destination / f"{KEY}.txt")
    original[KEY].get_day(1).content.pop("Work")
    original[KEY].edited = True
    storage.save_months_to_disk(original, source)
    deletion_id = original[KEY]._sync_heads[0]
    peer = storage.load_all_months_from_disk(destination)
    assert peer[KEY]._sync_pending and peer[KEY].days == {}
    assert storage.pending_revisions(destination)
    assert not storage.save_months_to_disk(peer, destination)
    assert not (destination / sync.REVISION_DIR).exists()
    root_file = next((source / sync.REVISION_DIR).glob(f"*{root_id}.json"))
    target = destination / sync.REVISION_DIR
    target.mkdir()
    shutil.copy2(root_file, target / root_file.name)
    assert storage.save_months_to_disk(peer, destination)
    edit(peer, text="local independent edit")
    storage.save_months_to_disk(peer, destination)
    deletion = next((source / sync.REVISION_DIR).glob(f"*{deletion_id}.json"))
    shutil.copy2(deletion, target / deletion.name)
    assert storage.save_months_to_disk(peer, destination)
    assert "Work" not in peer[KEY].get_day(1).content
    assert peer[KEY].get_day(1).text == "local independent edit"
    records = sync.read_graph(destination).records
    assert [item["id"] for item in records.values() if not item["parents"]] == [root_id]


def test_cache_header_missing_tip_defers_even_when_old_root_is_complete(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(source)
    root_id = original[KEY]._sync_heads[0]
    edit(original, text="latest cache")
    storage.save_months_to_disk(original, source)
    destination.mkdir()
    shutil.copy2(source / f"{KEY}.txt", destination / f"{KEY}.txt")
    target = destination / sync.REVISION_DIR
    target.mkdir()
    initial = next((source / sync.REVISION_DIR).glob(f"*{root_id}.json"))
    shutil.copy2(initial, target / initial.name)
    assert sync.read_graph(destination).complete(KEY)
    peer = storage.load_all_months_from_disk(destination)
    assert peer[KEY]._sync_pending and peer[KEY].days == {}
    assert not storage.save_months_to_disk(peer, destination)
    assert "latest cache" in (destination / f"{KEY}.txt").read_text(encoding="utf-8")
    replicate(source, destination, reverse=True, rename=True)
    assert storage.save_months_to_disk(peer, destination)
    assert peer[KEY].get_day(1).text == "latest cache"
    assert not peer[KEY]._sync_pending


def test_cache_only_poll_discovers_pending_month_and_refuses_typing(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(source)
    destination.mkdir()
    peer = storage.load_all_months_from_disk(destination)
    shutil.copy2(source / f"{KEY}.txt", destination / f"{KEY}.txt")
    assert not storage.save_months_to_disk(peer, destination)
    assert peer[KEY]._sync_pending and not hasattr(peer[KEY], "_sync_baseline")
    peer[KEY].get_day(1).text = "typed while cache was waiting"
    peer[KEY].edited = True
    with pytest.raises(OSError, match="no observed sync baseline"):
        storage.save_months_to_disk(peer, destination)
    assert peer[KEY].get_day(1).text == "typed while cache was waiting"
    assert not (destination / sync.REVISION_DIR).exists()
    assert original[KEY]._sync_heads


@pytest.mark.parametrize("dirty", [False, True])
def test_missing_observed_head_never_rolls_back_and_restoration_merges(tmp_path, dirty):
    local_dir, remote_dir = tmp_path / "local", tmp_path / "remote"
    local, remote = legacy(local_dir), legacy(remote_dir)
    edit(local, text="latest local")
    storage.save_months_to_disk(local, local_dir)
    month = local[KEY]
    head = month._sync_heads[0]
    baseline = copy.deepcopy(month._sync_baseline)
    path = next((local_dir / sync.REVISION_DIR).glob(f"*{head}.json"))
    data = path.read_bytes()
    path.unlink()
    edit(remote, category="remote")
    storage.save_months_to_disk(remote, remote_dir)
    replicate(remote_dir, local_dir)
    if dirty:
        edit(local, text="unsaved after disappearance")
        with pytest.raises(OSError, match="previously observed revisions"):
            storage.save_months_to_disk(local, local_dir)
        assert month.edited and not month._sync_durable
    else:
        assert not storage.save_months_to_disk(local, local_dir)
    assert month._sync_pending and month._sync_heads == (head,)
    assert month._sync_baseline == baseline
    assert month.get_day(1).text == ("unsaved after disappearance" if dirty else "latest local")
    assert "remote" not in month.get_day(1).content["Work"]
    assert storage.load_all_months_from_disk(local_dir)[KEY]._sync_pending
    path.write_bytes(data)
    assert storage.save_months_to_disk(local, local_dir)
    assert "remote" in month.get_day(1).content["Work"]
    assert not month._sync_pending
    assert month.get_day(1).text == ("unsaved after disappearance" if dirty else "latest local")


@pytest.mark.parametrize("operation", ["load", "save"])
def test_invalid_utf8_monthly_cache_is_repaired_from_authoritative_json(tmp_path, operation):
    months = legacy(tmp_path)
    expected = content(months)
    cache = tmp_path / f"{KEY}.txt"
    header = cache.read_bytes().splitlines(keepends=True)[0]
    cache.write_bytes(header + b"\xff\xfe damaged cache")
    if operation == "load":
        months = storage.load_all_months_from_disk(tmp_path)
    else:
        assert not storage.save_months_to_disk(months, tmp_path)
    assert content(months) == expected
    assert yaml.safe_load(cache.read_text(encoding="utf-8")) == expected[KEY]
    assert cache.read_text(encoding="utf-8").startswith(storage.CACHE_MAGIC)


def test_failed_revision_write_cannot_pollute_shared_cached_graph(tmp_path, monkeypatch):
    months = legacy(tmp_path)
    cached = sync.read_graph(tmp_path)
    original_ids = set(cached.records)
    edit(months, text="unsaved phantom")
    attempted = []
    write_revision = sync.write_revision

    def capture_event(directory, record):
        attempted.append(record)
        return write_revision(directory, record)

    monkeypatch.setattr(sync, "write_revision", capture_event)
    replace = sync.os.replace

    def fail_json(source, target):
        if str(target).endswith(".json"):
            raise OSError("revision write failed")
        return replace(source, target)

    monkeypatch.setattr(sync.os, "replace", fail_json)
    with pytest.raises(OSError, match="revision write failed"):
        storage.save_months_to_disk(months, tmp_path)
    assert sync.read_graph(tmp_path) is cached
    assert len(attempted) == 1 and sync.validate_revision(attempted[0]) == attempted[0]
    assert set(cached.records) == original_ids and attempted[0]["id"] not in cached.records
    assert months[KEY].edited and not months[KEY]._sync_durable


def test_invalid_combined_schedule_survives_notifier_and_unrelated_descendants(tmp_path):
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    item = activities.Activity("one", "Work", "Notes", "09:00", "10:00", 5)
    initial = {
        1: {"text": "base", activities.ACTIVITIES_KEY: {activities.encode_activity(item): None}}
    }
    left, right = legacy(left_dir, initial), legacy(right_dir, initial)
    left[KEY].get_day(1).upsert_activity(
        activities.Activity("one", "Work", "Notes", "09:30", "10:00", 5)
    )
    right[KEY].get_day(1).upsert_activity(
        activities.Activity("one", "Work", "Notes", "09:00", "09:15", 5)
    )
    storage.save_months_to_disk(left, left_dir)
    storage.save_months_to_disk(right, right_dir)
    replicate(right_dir, left_dir, reverse=True)
    storage.save_months_to_disk(left, left_dir)
    expected = {("09:30", "10:00"), ("09:00", "09:15")}

    def variants(months):
        record = json.loads(next(iter(months[KEY].get_day(1).content[activities.ACTIVITIES_KEY])))
        return record, {
            (item["start"], item["end"]) for item in record[sync.CONFLICT_KEY]["variants"]
        }

    assert variants(left)[1] == expected
    left[KEY].get_day(1).mark_activity_notified("one", "2026-09-01T08:55")
    assert storage.save_months_to_disk(left, left_dir)
    assert variants(left)[1] == expected
    loaded = storage.load_all_months_from_disk(left_dir)
    assert variants(loaded)[1] == expected
    assert loaded[KEY].get_day(1).activities[0].notified_for == "2026-09-01T08:55"
    selected = loaded[KEY].get_day(1).activities[0]
    loaded[KEY].get_day(1).upsert_activity(
        activities.Activity(
            "one",
            selected.title,
            "Unrelated notes edit",
            selected.start,
            selected.end,
            5,
            selected.notified_for,
        )
    )
    assert storage.save_months_to_disk(loaded, left_dir)
    assert variants(loaded)[1] == expected
    replicate(left_dir, right_dir, reverse=True, rename=True)
    assert storage.save_months_to_disk(right, right_dir)
    assert variants(right)[1] == expected
    assert variants(storage.load_all_months_from_disk(right_dir))[1] == expected


def test_save_as_refuses_incomplete_destination_cache(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(source)
    destination.mkdir()
    shutil.copy2(source / f"{KEY}.txt", destination / f"{KEY}.txt")
    with pytest.raises(OSError, match="Destination month.*waiting"):
        storage.save_months_to_disk(original, destination, saveas=True)
    assert not (destination / sync.REVISION_DIR).exists()


def test_partial_cache_header_retains_tip_hint_and_never_rolls_back(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(source)
    root_id = original[KEY]._sync_heads[0]
    edit(original, text="latest")
    storage.save_months_to_disk(original, source)
    tip = original[KEY]._sync_heads[0]
    destination.mkdir()
    cache = destination / f"{KEY}.txt"
    cache.write_bytes((storage.CACHE_MAGIC + '{"heads":["' + tip + '",').encode("ascii"))
    target = destination / sync.REVISION_DIR
    target.mkdir()
    initial = next((source / sync.REVISION_DIR).glob(f"*{root_id}.json"))
    shutil.copy2(initial, target / initial.name)
    peer = storage.load_all_months_from_disk(destination)
    assert peer[KEY]._sync_pending and storage._get_dict(peer[KEY]) == {}
    assert not storage.save_months_to_disk(peer, destination)
    replicate(source, destination)
    assert storage.save_months_to_disk(peer, destination)
    assert peer[KEY].get_day(1).text == "latest"
    assert cache.read_text(encoding="utf-8").startswith(storage.CACHE_MAGIC)


def test_bom_annotated_cache_is_not_a_legacy_root(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    legacy(source)
    destination.mkdir()
    (destination / f"{KEY}.txt").write_bytes(b"\xef\xbb\xbf" + (source / f"{KEY}.txt").read_bytes())
    peer = storage.load_all_months_from_disk(destination)
    assert peer[KEY]._sync_pending and not (destination / sync.REVISION_DIR).exists()


def test_remembered_cache_tips_survive_an_older_cache_overwrite(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(source)
    old_cache = (source / f"{KEY}.txt").read_bytes()
    root_id = original[KEY]._sync_heads[0]
    edit(original, text="new cache tip")
    storage.save_months_to_disk(original, source)
    destination.mkdir()
    cache = destination / f"{KEY}.txt"
    shutil.copy2(source / f"{KEY}.txt", cache)
    peer = storage.load_all_months_from_disk(destination)
    cache.write_bytes(old_cache)
    target = destination / sync.REVISION_DIR
    target.mkdir()
    initial = next((source / sync.REVISION_DIR).glob(f"*{root_id}.json"))
    shutil.copy2(initial, target / initial.name)
    assert not storage.save_months_to_disk(peer, destination)
    assert peer[KEY]._sync_pending and storage._get_dict(peer[KEY]) == {}
    replicate(source, destination)
    assert storage.save_months_to_disk(peer, destination)
    assert peer[KEY].get_day(1).text == "new cache tip"


def test_private_observed_head_protects_model_when_cache_is_older(tmp_path):
    months = legacy(tmp_path)
    old_cache = (tmp_path / f"{KEY}.txt").read_bytes()
    edit(months, text="observed latest")
    storage.save_months_to_disk(months, tmp_path)
    months = storage.load_all_months_from_disk(tmp_path)
    month = months[KEY]
    head = month._sync_heads[0]
    path = next((tmp_path / sync.REVISION_DIR).glob(f"*{head}.json"))
    body = path.read_bytes()
    path.unlink()
    (tmp_path / f"{KEY}.txt").write_bytes(old_cache)
    assert not storage.save_months_to_disk(months, tmp_path)
    assert month._sync_pending and month.get_day(1).text == "observed latest"
    assert month._sync_heads == (head,)
    path.write_bytes(body)
    assert not storage.save_months_to_disk(months, tmp_path)
    assert not month._sync_pending and month.get_day(1).text == "observed latest"


def test_observed_blank_branch_persists_only_local_input_while_cache_heads_are_pending(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    original = legacy(
        source, {1: {"text": "remote contribution", "Work": {"deleted cache entry": None}}}
    )
    stale_cache = (source / f"{KEY}.txt").read_bytes()
    original[KEY].get_day(1).content.pop("Work")
    original[KEY].edited = True
    storage.save_months_to_disk(original, source)
    destination.mkdir()
    month = Month(2026, 9)
    peer = {KEY: month}
    assert not storage.save_months_to_disk(peer, destination)
    assert month._sync_baseline == {} and month._sync_heads == ()
    month.get_day(1).text = "typed in a genuinely observed blank branch"
    month.edited = True
    (destination / f"{KEY}.txt").write_bytes(stale_cache)
    assert storage.save_months_to_disk(peer, destination)
    assert month._sync_pending and month._sync_durable
    local = sync.read_graph(destination).records[month._sync_heads[0]]
    assert local["parents"] == [] and local["base"] == {}
    assert local["event"] != "legacy-import"
    assert local["content"] == {"1": {"text": "typed in a genuinely observed blank branch"}}
    assert (destination / f"{KEY}.txt").read_bytes() == stale_cache
    replicate(source, destination, reverse=True, rename=True)
    assert storage.save_months_to_disk(peer, destination)
    assert not month._sync_pending
    assert "Work" not in month.get_day(1).content
    assert "typed in a genuinely observed blank branch" in month.get_day(1).text
    assert "remote contribution" in month.get_day(1).text


def test_native_root_identical_to_remote_history_survives_unseen_deletion(tmp_path):
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    left_dir.mkdir()
    right_dir.mkdir()
    left = {KEY: Month(2026, 9)}
    right = {KEY: Month(2026, 9)}
    assert not storage.save_months_to_disk(left, left_dir)
    assert not storage.save_months_to_disk(right, right_dir)
    edit(left, text="hello")
    edit(right, text="hello")
    assert storage.save_months_to_disk(right, right_dir)
    historical_root = right[KEY]._sync_heads[0]
    right[KEY].days.clear()
    right[KEY].edited = True
    assert storage.save_months_to_disk(right, right_dir)
    deletion = right[KEY]._sync_heads[0]
    replicate(right_dir, left_dir, reverse=True)
    assert storage.save_months_to_disk(left, left_dir)
    assert left[KEY].get_day(1).text == "hello"
    loaded_graph = sync.read_graph(left_dir)
    native = [
        record
        for record in loaded_graph.records.values()
        if not record["parents"] and record["id"] != historical_root
    ]
    assert len(native) == 1
    assert native[0]["content"] == {"1": {"text": "hello"}}
    assert native[0]["event"] != loaded_graph.records[historical_root]["event"]
    assert native[0]["id"] not in loaded_graph.ancestors[deletion]
    assert content(storage.load_all_months_from_disk(left_dir)) == content(left)
    files = list((left_dir / sync.REVISION_DIR).glob("*.json"))
    assert not storage.save_months_to_disk(left, left_dir)
    assert list((left_dir / sync.REVISION_DIR).glob("*.json")) == files
    replicate(left_dir, right_dir, reverse=True, rename=True)
    assert storage.save_months_to_disk(right, right_dir)
    assert right[KEY].get_day(1).text == "hello"
    assert content(storage.load_all_months_from_disk(right_dir)) == content(left)


@pytest.mark.parametrize("deletion", [False, True])
def test_identical_nonroot_edit_survives_unseen_remote_followup(tmp_path, deletion):
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    left, right = legacy(left_dir), legacy(right_dir)
    observed_heads = left[KEY]._sync_heads
    observed_base = copy.deepcopy(left[KEY]._sync_baseline)
    edit(left, text="hello")
    edit(right, text="hello")
    storage.save_months_to_disk(right, right_dir)
    historical_edit = right[KEY]._sync_heads[0]
    if deletion:
        right[KEY].days.clear()
        right[KEY].edited = True
    else:
        edit(right, text="remote replacement")
    storage.save_months_to_disk(right, right_dir)
    followup = right[KEY]._sync_heads[0]
    replicate(right_dir, left_dir, reverse=True)
    assert storage.save_months_to_disk(left, left_dir)
    graph = sync.read_graph(left_dir)
    local = [
        record
        for record in graph.records.values()
        if record["content"].get("1", {}).get("text") == "hello" and record["id"] != historical_edit
    ]
    assert len(local) == 1
    assert local[0]["parents"] == list(observed_heads) and local[0]["base"] == observed_base
    assert local[0]["event"] != graph.records[historical_edit]["event"]
    assert local[0]["id"] not in graph.ancestors[followup]
    text = left[KEY].get_day(1).text
    assert "hello" in text
    if deletion:
        assert text == "hello"
    else:
        assert "remote replacement" in text and "sync conflict" in text
    replicate(left_dir, right_dir, reverse=True, rename=True)
    assert storage.save_months_to_disk(right, right_dir)
    assert content(right) == content(left)
    assert content(storage.load_all_months_from_disk(left_dir)) == content(left)
    assert content(storage.load_all_months_from_disk(right_dir)) == content(left)


def test_identical_native_roots_materialize_once_without_collapsing_events(tmp_path):
    left_dir, right_dir = tmp_path / "left", tmp_path / "right"
    left_dir.mkdir()
    right_dir.mkdir()
    left = {KEY: Month(2026, 9, {1: {"text": "hello", "Work": {"entry": None}}})}
    right = {KEY: Month(2026, 9, {1: {"text": "hello", "Work": {"entry": None}}})}
    storage.save_months_to_disk(left, left_dir)
    storage.save_months_to_disk(right, right_dir)
    assert left[KEY]._sync_heads != right[KEY]._sync_heads
    replicate(right_dir, left_dir, reverse=True)
    replicate(left_dir, right_dir, reverse=True, rename=True)
    storage.save_months_to_disk(left, left_dir)
    storage.save_months_to_disk(right, right_dir)
    assert content(left) == content(right) == {KEY: {1: {"text": "hello", "Work": {"entry": None}}}}
    assert len(sync.read_graph(left_dir).records) == 2
    assert len(left[KEY]._sync_heads) == 2
    assert not storage.save_months_to_disk(left, left_dir)
    assert not storage.save_months_to_disk(right, right_dir)


def test_durable_native_event_retry_and_restart_do_not_duplicate(tmp_path, monkeypatch):
    month = Month(2026, 9)
    months = {KEY: month}
    assert not storage.save_months_to_disk(months, tmp_path)
    edit(months, text="hello")
    replace = sync.os.replace

    def fail_cache(source, destination):
        if str(destination).endswith(".txt"):
            raise OSError("cache failure after durable event")
        return replace(source, destination)

    monkeypatch.setattr(sync.os, "replace", fail_cache)
    with pytest.raises(OSError, match="durable event"):
        storage.save_months_to_disk(months, tmp_path)
    graph = sync.read_graph(tmp_path)
    assert len(graph.records) == 1 and month._sync_durable
    head = month._sync_heads[0]
    event = graph.records[head]["event"]
    with pytest.raises(OSError, match="durable event"):
        storage.save_months_to_disk(months, tmp_path)
    assert len(sync.read_graph(tmp_path).records) == 1
    assert sync.read_graph(tmp_path).records[head]["event"] == event
    monkeypatch.setattr(sync.os, "replace", replace)
    assert not storage.save_months_to_disk(months, tmp_path)
    assert month._sync_heads == (head,) and month.get_day(1).text == "hello"
    restarted = storage.load_all_months_from_disk(tmp_path)
    assert restarted[KEY]._sync_heads == (head,) and restarted[KEY].get_day(1).text == "hello"
    assert not storage.save_months_to_disk(restarted, tmp_path)


def test_durable_local_commit_is_retained_when_parent_disappears_during_write(
    tmp_path, monkeypatch
):
    months = legacy(tmp_path)
    parent = next((tmp_path / sync.REVISION_DIR).glob("*.json"))
    parent_bytes = parent.read_bytes()
    edit(months, text="durable local branch")
    replace = sync.os.replace

    def remove_parent_after_commit(source, target):
        replace(source, target)
        if str(target).endswith(".json"):
            parent.unlink()

    monkeypatch.setattr(sync.os, "replace", remove_parent_after_commit)
    assert storage.save_months_to_disk(months, tmp_path)
    assert months[KEY]._sync_pending and months[KEY]._sync_durable
    assert months[KEY].get_day(1).text == "durable local branch"
    head = months[KEY]._sync_heads[0]
    assert head not in sync.read_graph(tmp_path).records
    monkeypatch.setattr(sync.os, "replace", replace)
    parent.write_bytes(parent_bytes)
    assert not storage.save_months_to_disk(months, tmp_path)
    assert not months[KEY]._sync_pending and not months[KEY].edited
    assert (
        storage.load_all_months_from_disk(tmp_path)[KEY].get_day(1).text == "durable local branch"
    )
