import asyncio
import logging
import sys
import httpx
from bot.config import ROOT,load_config
from bot.db import Store
from bot.runtime import InstanceLock,AlreadyRunning,below_normal_priority,configure_logging
from bot.app import App


async def main():
    settings,campaigns,secrets = load_config()
    database = (ROOT/settings["database"]).resolve()
    if not database.is_relative_to(ROOT):
        raise ValueError("Database must be inside this project")
    store = Store(database)
    configure_logging(store)
    try:
        with InstanceLock():
            below_normal_priority()
            (ROOT/"data"/"stop.request").unlink(missing_ok=True)
            async with httpx.AsyncClient(timeout=45,limits=httpx.Limits(max_connections=4,max_keepalive_connections=2)) as client:
                await App(client,settings,campaigns,secrets,store).run()
    finally:
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AlreadyRunning:
        pass
    except Exception:
        if not logging.getLogger().handlers:
            configure_logging()
        logging.getLogger(__name__).exception("Marketing Manager could not start. Run the setup wizard; inspect logs/marketing.log.")
        sys.exit(1)
