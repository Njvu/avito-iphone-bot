import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median, quantiles

from bot.models import ALL_MODELS

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY, url TEXT, model TEXT, storage_gb INTEGER, price INTEGER,
    region TEXT, clean INTEGER, seller TEXT, seen_at TEXT);
CREATE INDEX IF NOT EXISTS ix_listings_ms ON listings(model, storage_gb, seen_at);
CREATE TABLE IF NOT EXISTS sent (
    id TEXT PRIMARY KEY, model TEXT, storage_gb INTEGER, price INTEGER, seller TEXT,
    chat_id INTEGER, message_id INTEGER, sent_at TEXT);
CREATE TABLE IF NOT EXISTS sent_posts (id TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY(id, chat_id));
CREATE TABLE IF NOT EXISTS checked (id TEXT PRIMARY KEY, price INTEGER, checked_at TEXT);
CREATE TABLE IF NOT EXISTS tracked (model TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS repair (model TEXT PRIMARY KEY, screen INTEGER, back INTEGER);
CREATE TABLE IF NOT EXISTS manual_market (model TEXT, storage_gb INTEGER, price INTEGER,
    PRIMARY KEY(model, storage_gb));
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS shop_sellers (seller TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS avito_eval (model TEXT, storage_gb INTEGER, condition TEXT, min INTEGER, max INTEGER,
    updated_at TEXT, PRIMARY KEY(model, storage_gb, condition));
"""
EVAL_HOURS = 48


def iqr_filter(prices: list[int]) -> list[int]:
    if len(prices) < 4:
        return prices
    q1, _, q3 = quantiles(prices, n=4)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return [p for p in prices if lo <= p <= hi]


class Store:
    def __init__(self, path, regions: tuple[str, ...] = (), reseller_listings: int = 3):
        self.regions = tuple(regions)
        self.reseller_listings = reseller_listings
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        if "avito_mark" not in {r[1] for r in self.db.execute("PRAGMA table_info(listings)")}:
            self.db.execute("ALTER TABLE listings ADD COLUMN avito_mark TEXT DEFAULT ''")
        if self.get_state("tracked_init") is None:
            self.db.executemany("INSERT OR IGNORE INTO tracked VALUES(?)", [(m,) for m in ALL_MODELS])
            self.set_state("tracked_init", "1")
        if self.get_state("tracked_v2") is None:
            self.db.executemany("INSERT OR IGNORE INTO tracked VALUES(?)",
                                [(m,) for m in ("X", "XR", "XS", "XS Max", "11", "11 Pro", "11 Pro Max")])
            self.set_state("tracked_v2", "1")
        if self.get_state("tracked_v3") is None:
            self.db.executemany("INSERT OR IGNORE INTO tracked VALUES(?)", [(m,) for m in ALL_MODELS])
            self.set_state("tracked_v3", "1")
        self.db.commit()

    def _one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def _do(self, sql, *args):
        self.db.execute(sql, args)
        self.db.commit()

    def get_state(self, key, default=None):
        row = self._one("SELECT value FROM state WHERE key=?", key)
        return row[0] if row else default

    def set_state(self, key, value):
        self._do("INSERT OR REPLACE INTO state VALUES(?,?)", key, str(value))

    def save_listing(self, id, url, model, storage_gb, price, region, clean, seller="", avito_mark=""):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO listings(id, url, model, storage_gb, price, region, clean, seller, seen_at, avito_mark)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (id, url, model, storage_gb, price, region, int(clean), seller, datetime.now().isoformat(), avito_mark))
        if not cur.rowcount:
            self.db.execute("UPDATE listings SET price=?, clean=?, avito_mark=? WHERE id=?",
                            (price, int(clean), avito_mark, id))
        self.db.commit()
        return bool(cur.rowcount)

    def save_eval(self, model, storage_gb, condition, low, high):
        self._do("INSERT OR REPLACE INTO avito_eval VALUES(?,?,?,?,?,?)",
                 model, storage_gb, condition, low, high, datetime.now().isoformat())

    def eval_fresh(self, model, storage_gb, condition, hours=24):
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        return bool(self._one("SELECT 1 FROM avito_eval WHERE model=? AND storage_gb=? AND condition=? AND updated_at>=?",
                              model, storage_gb, condition, since))

    def get_eval(self, model, storage_gb, condition, hours=EVAL_HOURS):
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        row = self._one("SELECT min, max FROM avito_eval WHERE model=? AND storage_gb=? AND condition=? "
                        "AND updated_at>=? AND min>0", model, storage_gb, condition, since)
        return (row[0], row[1]) if row else None

    def evals_for(self, model):
        return self.db.execute("SELECT storage_gb, condition, min, max FROM avito_eval WHERE model=? AND min>0 "
                               "ORDER BY storage_gb", (model,)).fetchall()

    def eval_storages(self, model):
        return sorted(r[0] for r in self.db.execute("SELECT DISTINCT storage_gb FROM avito_eval WHERE model=?", (model,)))

    def evals_updated(self):
        return self._one("SELECT MAX(updated_at) FROM avito_eval")[0]

    def seen_at(self, id) -> str:
        row = self._one("SELECT seen_at FROM listings WHERE id=?", id)
        return row[0] if row else ""

    def allowed_defects(self, default: frozenset[str]) -> frozenset[str]:
        raw = self.get_state("allowed_defects")
        return default if raw is None else frozenset(x for x in raw.split(",") if x)

    def set_allowed_defects(self, allowed) -> None:
        self.set_state("allowed_defects", ",".join(sorted(allowed)))

    def recent_sent(self, limit: int = 5) -> list[tuple]:
        return [tuple(r) for r in self.db.execute(
            "SELECT s.id, s.model, s.storage_gb, s.price, s.sent_at, l.url FROM sent s LEFT JOIN listings l ON l.id=s.id "
            "ORDER BY s.sent_at DESC LIMIT ?", (limit,))]

    def admins(self) -> dict[int, str]:
        raw = self.get_state("admins") or ""
        pairs = (item.split(":", 1) for item in raw.split(";") if item)
        return {int(uid): name for uid, name in pairs}

    def admin_ids(self, owners: set[int]) -> set[int]:
        return set(owners) | set(self.admins())

    def add_admin(self, uid: int, name: str = "") -> bool:
        current = self.admins()
        added = uid not in current
        current[uid] = name.replace(";", " ").replace(":", " ")
        self.set_state("admins", ";".join(f"{u}:{n}" for u, n in current.items()))
        return added

    def remove_admin(self, uid: int) -> bool:
        current = self.admins()
        if uid not in current:
            return False
        del current[uid]
        self.set_state("admins", ";".join(f"{u}:{n}" for u, n in current.items()))
        return True

    def keep_evals(self, conditions):
        marks = ",".join("?" * len(conditions))
        self._do(f"DELETE FROM avito_eval WHERE condition NOT IN ({marks})", *conditions)

    def evals_since(self, since: datetime) -> int:
        return self._one("SELECT COUNT(*) FROM avito_eval WHERE updated_at>=?", since.isoformat())[0]

    def evals_count(self):
        return self._one("SELECT COUNT(*) FROM avito_eval WHERE min>0")[0]

    def evals_stale(self, models, hours=EVAL_HOURS) -> bool:
        if not self.evals_count():
            return True
        if not models:
            return False
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        marks = ",".join("?" * len(models))
        return bool(self._one(f"SELECT 1 FROM avito_eval WHERE model IN ({marks}) AND updated_at<? LIMIT 1",
                              *models, since))

    def add_shop(self, seller, reason):
        if seller:
            self._do("INSERT OR IGNORE INTO shop_sellers VALUES(?,?,?)", seller, reason, datetime.now().isoformat())

    def remove_shop(self, seller):
        cur = self.db.execute("DELETE FROM shop_sellers WHERE seller=?", (seller,))
        self.db.commit()
        return bool(cur.rowcount)

    def is_shop(self, seller):
        return bool(seller) and bool(self._one("SELECT 1 FROM shop_sellers WHERE seller=?", seller))

    def shops_count(self):
        return self._one("SELECT COUNT(*) FROM shop_sellers")[0]

    def listings_count(self):
        return self._one("SELECT COUNT(*) FROM listings")[0]

    def was_checked(self, id, price):
        row = self._one("SELECT price FROM checked WHERE id=?", id)
        return bool(row) and row[0] <= price

    def mark_checked(self, id, price):
        self._do("INSERT OR REPLACE INTO checked VALUES(?,?,?)", id, price, datetime.now().isoformat())

    def get_sent(self, id) -> tuple[int, list[tuple[int, int]]] | None:
        row = self._one("SELECT price, chat_id, message_id FROM sent WHERE id=?", id)
        if not row:
            return None
        posts = [tuple(r) for r in self.db.execute("SELECT chat_id, message_id FROM sent_posts WHERE id=? ORDER BY rowid", (id,))]
        if not posts and row[1] and row[2]:
            posts = [(row[1], row[2])]
        return row[0], posts

    def find_duplicate(self, seller, model, storage_gb, price, hours=24):
        if not seller:
            return False
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        return bool(self._one(
            "SELECT 1 FROM sent WHERE seller=? AND model=? AND storage_gb=? AND price=? AND sent_at>=?",
            seller, model, storage_gb, price, since))

    def mark_sent(self, id, model, storage_gb, price, seller, posts: list[tuple[int, int]]):
        chat_id, message_id = posts[0] if posts else (0, 0)
        self.db.execute("INSERT OR REPLACE INTO sent VALUES(?,?,?,?,?,?,?,?)",
                        (id, model, storage_gb, price, seller, chat_id, message_id, datetime.now().isoformat()))
        self.db.executemany("INSERT OR REPLACE INTO sent_posts VALUES(?,?,?)", [(id, c, m) for c, m in posts])
        self.db.commit()

    def channels(self, default: int = 0) -> list[int]:
        raw = self.get_state("chat_ids")
        if raw is None:
            first = int(self.get_state("group_chat_id") or default or 0)
            return [first] if first else []
        return [int(x) for x in raw.split(",") if x]

    def add_channel(self, chat_id: int, default: int = 0) -> bool:
        current = self.channels(default)
        if chat_id in current:
            return False
        self.set_state("chat_ids", ",".join(str(c) for c in current + [chat_id]))
        return True

    def remove_channel(self, chat_id: int, default: int = 0) -> bool:
        current = self.channels(default)
        if chat_id not in current:
            return False
        self.set_state("chat_ids", ",".join(str(c) for c in current if c != chat_id))
        return True

    def replace_channel(self, old: int, new: int, default: int = 0) -> bool:
        current = self.channels(default)
        if old not in current:
            return False
        self.set_state("chat_ids", ",".join(dict.fromkeys(str(new if c == old else c) for c in current)))
        return True

    def sent_count(self, hours):
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        return self._one("SELECT COUNT(*) FROM sent WHERE sent_at>=?", since)[0]

    def update_sent_price(self, id, price):
        self._do("UPDATE sent SET price=? WHERE id=?", price, id)

    def unmark_sent(self, id):
        self.db.execute("DELETE FROM sent WHERE id=?", (id,))
        self.db.execute("DELETE FROM sent_posts WHERE id=?", (id,))
        self.db.commit()

    def tracked(self):
        order = {m: i for i, m in enumerate(ALL_MODELS)}
        return sorted((r[0] for r in self.db.execute("SELECT model FROM tracked")), key=lambda m: order.get(m, 99))

    def add_tracked(self, model):
        self._do("INSERT OR IGNORE INTO tracked VALUES(?)", model)

    def remove_tracked(self, model):
        self._do("DELETE FROM tracked WHERE model=?", model)

    def set_repair(self, model, screen, back):
        self._do("INSERT OR REPLACE INTO repair VALUES(?,?,?)", model, screen, back)

    def get_repair(self, model):
        row = self._one("SELECT screen, back FROM repair WHERE model=?", model)
        return row if row else None

    def set_manual_market(self, model, storage_gb, price):
        if price:
            self._do("INSERT OR REPLACE INTO manual_market VALUES(?,?,?)", model, storage_gb, price)
        else:
            self._do("DELETE FROM manual_market WHERE model=? AND storage_gb=?", model, storage_gb)

    def storages_seen(self, model):
        rows = self.db.execute(
            "SELECT DISTINCT storage_gb FROM listings WHERE model=? AND storage_gb>0 "
            "UNION SELECT storage_gb FROM manual_market WHERE model=?",
            (model, model))
        return sorted(r[0] for r in rows)

    def _market_where(self, model, since, storage_gb=None, region=None, exclude_id=None):
        sql = "model=? AND clean=1 AND seen_at>=? AND price>0 AND storage_gb>0"
        args = [model, since]
        if storage_gb:
            sql += " AND storage_gb=?"
            args.append(storage_gb)
        if region:
            sql += " AND region=?"
            args.append(region)
        elif self.regions:
            sql += f" AND region IN ({','.join('?' * len(self.regions))})"
            args += self.regions
        if exclude_id:
            sql += " AND id!=?"
            args.append(exclude_id)
        if self.reseller_listings:
            sql += (" AND seller NOT IN (SELECT seller FROM listings WHERE seller!='' AND seen_at>=? "
                    "GROUP BY seller HAVING COUNT(*)>=?)")
            args += [(datetime.now() - timedelta(hours=24)).isoformat(), self.reseller_listings]
        return sql, args

    def _prices(self, model, storage_gb, since, region=None, exclude_id=None):
        sql, args = self._market_where(model, since, storage_gb, region, exclude_id)
        return [r[0] for r in self.db.execute(f"SELECT price FROM listings WHERE {sql}", args)]

    def model_samples(self, model, hours=6, exclude_id=None):
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        sql, args = self._market_where(model, since, exclude_id=exclude_id)
        return self._one(f"SELECT COUNT(*) FROM listings WHERE {sql}", *args)[0]

    def avito_market(self, model, storage_gb, hours=24, discount=0.9, exclude_id=None):
        if not storage_gb:
            return None
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        sql = ("SELECT price FROM listings WHERE model=? AND storage_gb=? AND clean=1 AND avito_mark='market' "
               "AND seen_at>=? AND price>0 AND id!=?")
        args = [model, storage_gb, since, exclude_id or ""]
        if self.regions:
            sql += f" AND region IN ({','.join('?' * len(self.regions))})"
            args += self.regions
        prices = [r[0] for r in self.db.execute(sql, args)]
        if not prices:
            return None
        mid = int(median(prices))
        return {"price": int(mid * discount), "median": mid, "samples": len(prices), "source": "рынок Авито"}

    def market(self, model, storage_gb, region=None, hours=6, min_samples=5, discount=0.9, exclude_id=None):
        if not storage_gb:
            for storage in self.storages_seen(model):
                m = self.market(model, storage, region, hours, min_samples, discount, exclude_id)
                if m["price"]:
                    return {**m, "source": f"{m['source']}, для {storage} ГБ"}
            return {"price": None, "median": None, "samples": 0, "source": "нет данных"}
        row = self._one("SELECT price FROM manual_market WHERE model=? AND storage_gb=?", model, storage_gb)
        if row:
            return {"price": row[0], "median": row[0], "samples": 0, "source": "ручная цена"}
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        for reg, label in ((region, "по городу"), (None, "по всем городам")):
            if (reg is None and region is None and label == "по городу") or (reg is None and self.regions == (region,)):
                continue
            prices = iqr_filter(self._prices(model, storage_gb, since, reg, exclude_id))
            if prices and len(prices) >= min_samples:
                return {"price": int(median(prices) * discount), "median": int(median(prices)),
                        "samples": len(prices), "source": label}
        return {"price": None, "median": None, "samples": 0, "source": "нет данных"}

    def summary(self, models, hours=6, discount=0.9):
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        out = []
        for model in models:
            for (storage,) in self.db.execute(
                    "SELECT DISTINCT storage_gb FROM listings WHERE model=? AND clean=1 AND seen_at>=? ORDER BY storage_gb",
                    (model, since)).fetchall():
                prices = iqr_filter(self._prices(model, storage, since))
                if prices:
                    out.append((model, storage, int(median(prices) * discount), min(prices), max(prices), len(prices)))
        return out
