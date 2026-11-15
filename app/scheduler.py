from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler

from app import create_app
from app.services.email_notifications import run_daily_notifications


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    timezone = pytz.timezone(app.config["MAIL_TIMEZONE"])
    scheduler = BlockingScheduler(timezone=timezone)

    def run_in_context() -> None:
        with app.app_context():
            run_daily_notifications()

    scheduler.add_job(
        run_in_context,
        "cron",
        hour=app.config["MAIL_SEND_HOUR"],
        minute=0,
        timezone=timezone,
        id="daily-document-notifications",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_in_context,
        "date",
        run_date=datetime.now(timezone) + timedelta(seconds=20),
        id="notification-startup-catchup",
        misfire_grace_time=3600,
    )
    logging.getLogger(__name__).info(
        "Ежедневные уведомления запланированы на %02d:00 (%s).",
        app.config["MAIL_SEND_HOUR"],
        app.config["MAIL_TIMEZONE"],
    )
    scheduler.start()


if __name__ == "__main__":
    main()
