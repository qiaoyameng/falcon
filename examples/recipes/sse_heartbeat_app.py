"""Self-contained ASGI app streaming Server-Sent Events with heartbeats.

Serve with any ASGI server, for example:

    uvicorn sse_heartbeat_app:app

or simply run this file directly:

    python sse_heartbeat_app.py

Then stream events with curl (no external services are required):

    curl -N http://127.0.0.1:8000/clock
    curl -N http://127.0.0.1:8000/slow
"""

import asyncio
from datetime import datetime, timezone

import falcon.asgi


class ClockResource:
    """Streams the current UTC time as an SSE event once per second."""

    def __init__(self, tick_interval=1.0):
        self._tick_interval = tick_interval

    async def on_get(self, req, resp):
        resp.sse = self._ticks()

    async def _ticks(self):
        while True:
            now = datetime.now(timezone.utc).isoformat()
            yield falcon.asgi.SSEvent(text=f'tick: {now}')
            await asyncio.sleep(self._tick_interval)


class SlowResource:
    """Emits business events only rarely, relying on heartbeats in between."""

    def __init__(self, event_interval=60.0, heartbeat_interval=5.0):
        self._event_interval = event_interval
        self._heartbeat_interval = heartbeat_interval

    async def on_get(self, req, resp):
        # Override the app-level heartbeat interval for this response only
        #   (or set it to None to disable heartbeats for this response).
        resp.sse_heartbeat_interval = self._heartbeat_interval
        resp.sse = self._events()

    async def _events(self):
        while True:
            await asyncio.sleep(self._event_interval)
            yield falcon.asgi.SSEvent(text='a slow business event')


def create_app(heartbeat_interval=15.0):
    app = falcon.asgi.App()

    # Emit an SSE comment frame (": ping") whenever no event has been sent
    #   for this many seconds, so that proxies and load balancers do not
    #   close idle SSE connections. The default is None (heartbeats
    #   disabled).
    app.resp_options.sse_heartbeat_interval = heartbeat_interval

    app.add_route('/clock', ClockResource())
    app.add_route('/slow', SlowResource())
    return app


app = create_app()


if __name__ == '__main__':
    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            'No ASGI server found; install one, e.g.: pip install uvicorn'
        )

    uvicorn.run(app, host='127.0.0.1', port=8000)
