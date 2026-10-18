import asyncio
import logging
import sys
from datetime import datetime

from bot.avito import EVAL_PAGE, Avito
from bot.config import REGIONS, ROOT, settings
from bot.db import Store
from bot.evaluation import AvitoEvaluation



async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(ROOT / "data" / "bot.log", encoding="utf-8")])
    store = Store(settings.db_path, tuple(REGIONS.values()), settings.market_reseller_listings)
    avito = Avito()
    await avito.start()
    try:
        await avito.fetch(EVAL_PAGE)
        evaluation = AvitoEvaluation(avito, store, settings.avito_eval_city)
        if "--force" in sys.argv:
            evaluation.refresh_since = datetime.now()
        while await evaluation.step(store.tracked(), budget=50, patient=True):
            pass
        logging.info("Готово: оценок калькулятора Авито в базе %s", store.evals_count())
    finally:
        await avito.close()


if __name__ == "__main__":
    asyncio.run(main())
