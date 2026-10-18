from bot.db import Store, iqr_filter
from bot.models import ALL_MODELS


def test_market_manual_iqr_regions(tmp_path):
    db = Store(tmp_path / "t.sqlite")
    assert db.tracked() == ALL_MODELS
    for i, p in enumerate([30000, 31000, 32000, 33000, 34000, 5000]):
        db.save_listing(f"s{i}", "u", "13 Pro", 256, p, "Ставропольский край", True)
    r = db.market("13 Pro", 256, "Ставропольский край", min_samples=5, discount=0.9)
    assert r["price"] == int(32000 * 0.9) and r["samples"] == 5 and r["source"] == "по городу"
    r = db.market("13 Pro", 256, "Краснодарский край", min_samples=5, discount=0.9)
    assert r["source"] == "по всем городам"
    db.set_manual_market("13 Pro", 256, 27000)
    assert db.market("13 Pro", 256)["price"] == 27000
    db.set_manual_market("13 Pro", 256, 0)
    assert db.market("13 Pro", 256)["source"] != "ручная цена"


def test_sent_dup_checked(tmp_path):
    db = Store(tmp_path / "t.sqlite")
    db.mark_sent("1", "13", 128, 20000, "Иван", [(-100, 5)])
    assert db.find_duplicate("Иван", "13", 128, 20000)
    assert db.get_sent("1") == (20000, [(-100, 5)])
    db.mark_checked("2", 15000)
    assert db.was_checked("2", 15000) and not db.was_checked("2", 14000)
    assert iqr_filter([1, 2]) == [1, 2]


def test_summary(tmp_path):
    db = Store(tmp_path / "t.sqlite")
    for i, p in enumerate([30000, 32000, 34000]):
        db.save_listing(f"s{i}", "u", "13", 128, p, "Ставрополь", True)
    assert db.summary(["13"], discount=1.0) == [("13", 128, 32000, 30000, 34000, 3)]


def test_save_listing_updates_clean(tmp_path):
    from bot.db import Store
    s = Store(tmp_path / "t.sqlite")
    s.save_listing("1", "u", "13", 128, 30000, "Краснодар", False)
    assert s.model_samples("13") == 0
    s.save_listing("1", "u", "13", 128, 29000, "Краснодар", True)
    assert s.model_samples("13") == 1
