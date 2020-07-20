"""Helper function to launch httpstan server.

Configure the server and schedule startup and shutdown tasks.
"""
import asyncio
import logging

import aiohttp.web

import httpstan.routes

try:
    from uvloop import EventLoopPolicy
except ImportError:
    EventLoopPolicy = asyncio.DefaultEventLoopPolicy


logger = logging.getLogger("httpstan")


async def _warn_unfinished_operations(app: aiohttp.web.Application) -> None:
    """Warn if tasks (e.g., operations) are unfinished.

    Called immediately before tasks are cancelled.

    """
    for name, operation in app["operations"].items():
        if not operation["done"]:
            logger.critical(f"Operation `{name}` cancelled before finishing.")


def make_app() -> aiohttp.web.Application:
    """Assemble aiohttp Application.

    Returns:
        aiohttp.web.Application: assembled aiohttp application.

    """
    app = aiohttp.web.Application()
    httpstan.routes.setup_routes(app)
    # startup and shutdown tasks
    app["operations"] = {}
    app.on_cleanup.append(_warn_unfinished_operations)
    return app
