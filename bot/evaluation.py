import asyncio
import logging
import random
import re
import time
from datetime import date, datetime

from bot.models import canonical_model
from bot.text import normalize

log = logging.getLogger(__name__)

CATEGORY = 84
ATTR_BRAND, ATTR_MODEL, ATTR_MEMORY, ATTR_COLOR, ATTR_CONDITION = 110618, 110617, 110615, 110616, 121588
APPLE = 469735
CONDITIONS = {"Отличное": 2850685, "Хорошее": 2850686, "Удовлетворительное": 2850687, "Требуется ремонт": 2850688}
EVAL_CONDITIONS = ("Отличное", "Хорошее")
NEIGHBORS = {"Отличное": ("Хорошее",), "Хорошее": ("Отличное",)}
SHORT = {"Отличное": "отл", "Хорошее": "хор", "Удовлетворительное": "удовл", "Требуется ремонт": "ремонт"}
LAYOUT = "/web/1/imv/goods/landing/layout"
RETRIES, RETRY_WAIT, PAUSE, COOLDOWN = 5, 120, (3.0, 5.0), 1800
RATE_LIMITED = (429, 439)
PREDICTION = "/web/1/imv/goods/landing/prediction"


def storage_from_label(label: str) -> int:
    s = normalize(label)
    m = re.search(r"(\d+)\s*(тб|tb)", s)
    if m:
        return int(m.group(1)) * 1024
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else 0


def eval_price(low: int, high: int, basis: str = "avg") -> int:
    return {"max": high, "min": low}.get(basis, (low + high) // 2)


def eval_condition(condition: str) -> str:
    c = normalize(condition)
    if "нов" in c or "отличн" in c:
        return "Отличное"
    return "Хорошее"


def _field(layout: dict, attr: int) -> dict | None:
    for block in layout.get("layout", []):
        for f in block.get("fields", []):
            if f.get("attrId") == attr:
                return f
    return None


def _params(**values: int) -> list[dict]:
    ids = {"memory": ATTR_MEMORY, "model": ATTR_MODEL, "brand": ATTR_BRAND, "condition": ATTR_CONDITION,
           "color": ATTR_COLOR}
    return [{"attrId": ids[k], "valueId": v} for k, v in values.items() if v]


class AvitoEvaluation:
    def __init__(self, avito, store, city: str):
        self.avito, self.store, self.city = avito, store, city
        self.refresh_since: datetime | None = None
        self.fresh_since: datetime | None = None
        self.cooldown_until = 0.0
        self._day = None
        self._location = None
        self._models: dict[str, int] = {}
        self._options: dict[str, tuple[dict[int, int], int]] = {}
        self._patient = False

    def _reset_daily(self):
        if self._day != date.today():
            self._day = date.today()
            self._models.clear()
            self._options.clear()

    async def _call(self, path: str, body: dict | None = None, patient: bool = False):
        for attempt in range(RETRIES + 1):
            await asyncio.sleep(random.uniform(*PAUSE))
            data = await self.avito.api(path, body)
            limited = isinstance(data, dict) and data.get("__status") in RATE_LIMITED
            if limited and not patient:
                self.cooldown_until = time.monotonic() + COOLDOWN
                raise RuntimeError(f"{path}: {data['__status']}, следующая попытка через {COOLDOWN // 60} мин")
            if limited and attempt < RETRIES:
                log.info("Калькулятор Авито: слишком часто (%s), жду %s с", data["__status"], RETRY_WAIT)
                await asyncio.sleep(RETRY_WAIT)
                continue
            if not isinstance(data, dict) or "__status" in data:
                raise RuntimeError(f"{path}: {data.get('__status') if isinstance(data, dict) else data}")
            return data

    async def location_id(self) -> int:
        key = f"eval_location:{self.city}"
        if not self._location:
            self._location = int(self.store.get_state(key, "0")) or None
        if not self._location:
            data = await self._call(f"/web/1/slocations?q={self.city}", patient=self._patient)
            locations = data.get("result", {}).get("locations", [])
            exact = [x for x in locations if x.get("names", {}).get("1") == self.city]
            self._location = (exact or locations)[0]["id"]
            self.store.set_state(key, self._location)
        return self._location

    async def _model_ids(self) -> dict[str, int]:
        if not self._models:
            layout = await self._call(LAYOUT, {"categoryId": CATEGORY, "params": _params(brand=APPLE)},
                                      patient=self._patient)
            for o in (_field(layout, ATTR_MODEL) or {}).get("options", []):
                model = canonical_model(o["label"])
                if model:
                    self._models[model] = o["valueId"]
        return self._models

    async def _model_options(self, model: str, model_id: int) -> tuple[dict[int, int], int]:
        if model not in self._options:
            layout = await self._call(LAYOUT, {"categoryId": CATEGORY, "params": _params(brand=APPLE, model=model_id)},
                                      patient=self._patient)
            memory = {storage_from_label(o["label"]): o["valueId"]
                      for o in (_field(layout, ATTR_MEMORY) or {}).get("options", [])}
            colors = (_field(layout, ATTR_COLOR) or {}).get("options", [])
            self._options[model] = ({k: v for k, v in memory.items() if k}, colors[0]["valueId"] if colors else 0)
        return self._options[model]

    def pending(self, models: list[str]) -> bool:
        return not self._models or any(m in self._models and not self._fresh(m) for m in models)

    def _fresh(self, model: str) -> bool:
        storages = list(self._options[model][0]) if model in self._options else self.store.eval_storages(model)
        return bool(storages) and all(self._is_fresh(model, s, c) for s in storages for c in EVAL_CONDITIONS)

    def _is_fresh(self, model: str, storage: int, cond: str) -> bool:
        since = self.refresh_since or self.fresh_since
        hours = (datetime.now() - since).total_seconds() / 3600 if since else 24
        return self.store.eval_fresh(model, storage, cond, hours)

    async def step(self, models: list[str], budget: int = 15, patient: bool = False) -> int:
        self._reset_daily()
        done = 0
        if not patient and time.monotonic() < self.cooldown_until:
            return 0
        self._patient = patient
        try:
            location = await self.location_id()
            ids = await self._model_ids()
            for model in models:
                if model not in ids or self._fresh(model):
                    continue
                memory, color = await self._model_options(model, ids[model])
                for storage, mem_id in sorted(memory.items()):
                    for cond in EVAL_CONDITIONS:
                        if self._is_fresh(model, storage, cond):
                            continue
                        if done >= budget:
                            return done
                        body = {"categoryId": CATEGORY, "locationID": location,
                                "params": _params(memory=mem_id, model=ids[model], brand=APPLE,
                                                  condition=CONDITIONS[cond], color=color)}
                        pred = await self._call(PREDICTION, body, patient=patient)
                        done += 1
                        ok = pred.get("isPredicted") and pred.get("min") and pred.get("max")
                        self.store.save_eval(model, storage, cond, int(pred["min"]) if ok else 0,
                                             int(pred["max"]) if ok else 0)
        except Exception as e:
            log.warning("Калькулятор Авито: %s", e)
        finally:
            if done:
                log.info("Калькулятор Авито: обновлено %s оценок, всего %s", done, self.store.evals_count())
        return done
