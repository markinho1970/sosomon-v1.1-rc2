"""
APScheduler — runs agent jobs on a cron schedule.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
import asyncio


scheduler = BackgroundScheduler()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def run_scout():
    from agents.scout import run_all_indexes
    logger.info("Scheduler: starting Scout agent run")
    _run_async(run_all_indexes())


def run_rebalancer():
    from agents.rebalancer import check_and_propose_rebalances
    logger.info("Scheduler: starting Rebalancer agent run")
    _run_async(check_and_propose_rebalances())


def run_narrator():
    from agents.narrator import generate_weekly_content
    logger.info("Scheduler: starting Narrator agent run")
    _run_async(generate_weekly_content())


def run_deposit_monitor_mainnet():
    from database import SessionLocal
    from services.deposit_monitor import check_deposits_mainnet
    db = SessionLocal()
    try:
        _run_async(check_deposits_mainnet(db))
    finally:
        db.close()


def run_deposit_monitor_testnet():
    from database import SessionLocal
    from services.deposit_monitor import check_deposits_testnet
    db = SessionLocal()
    try:
        _run_async(check_deposits_testnet(db))
    finally:
        db.close()


def run_nav_updater():
    from services.nav_updater import update_all_navs
    logger.info("Scheduler: starting NAV Updater")
    _run_async(update_all_navs())


def run_fee_manager():
    from services.fee_manager import run_monthly_fees
    logger.info("Scheduler: starting Fee Manager (monthly collection)")
    _run_async(run_monthly_fees(dry_run=False))


def run_announcement_fetcher():
    from database import SessionLocal
    from services.announcement_fetcher import sync_announcements
    logger.info("Scheduler: starting Announcement Fetcher")
    db = SessionLocal()
    try:
        _run_async(sync_announcements(db))
    finally:
        db.close()


def run_universe_refresh():
    from database import SessionLocal
    from services.universe import refresh_universe
    logger.info("Scheduler: starting Universe refresh")
    db = SessionLocal()
    try:
        count = _run_async(refresh_universe(db))
        logger.info(f"Scheduler: Universe refresh done, {count} assets")
    except Exception as e:
        logger.error(f"Scheduler: Universe refresh failed: {e}")
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(run_scout, CronTrigger(hour=6, minute=0),
                      id="scout_daily", replace_existing=True)

    # Rebalancer: apenas drift check a cada 4h — weekly automático removido (modelo passivo)
    scheduler.add_job(run_rebalancer, CronTrigger(hour="*/4", minute=30),
                      id="rebalancer_drift_check", replace_existing=True)

    scheduler.add_job(run_narrator, CronTrigger(day_of_week="sun", hour=18, minute=0),
                      id="narrator_weekly", replace_existing=True)

    # Dois monitores simultâneos — mainnet e testnet independentes
    scheduler.add_job(run_deposit_monitor_mainnet, IntervalTrigger(minutes=2),
                      id="deposit_monitor_mainnet", replace_existing=True)
    scheduler.add_job(run_deposit_monitor_testnet, IntervalTrigger(minutes=2),
                      id="deposit_monitor_testnet", replace_existing=True)

    scheduler.add_job(run_nav_updater, IntervalTrigger(hours=1),
                      id="nav_updater", replace_existing=True)

    # Fee Manager: dia 1 de cada mês às 08:00 UTC — management fee + performance fee
    scheduler.add_job(run_fee_manager, CronTrigger(day=1, hour=8, minute=0),
                      id="fee_manager_monthly", replace_existing=True)

    # Announcement Fetcher: busca anúncios SoDEX a cada 2h
    scheduler.add_job(run_announcement_fetcher, IntervalTrigger(hours=2),
                      id="announcement_fetcher", replace_existing=True)

    # Universe Refresh: 05:00 UTC diário — antes do Scout (06:00)
    scheduler.add_job(run_universe_refresh, CronTrigger(hour=5, minute=0),
                      id="universe_refresh_daily", replace_existing=True)

    scheduler.start()
    logger.info("Scheduler started: Scout(06:00), Rebalancer(drift/4h), Narrator(Sun 18:00), DepositMonitor Mainnet+Testnet(2min), NAVUpdater(1h), FeeManager(dia1 08:00), AnnouncementFetcher(2h), UniverseRefresh(05:00)")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
