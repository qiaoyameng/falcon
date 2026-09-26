.. _sse_heartbeat_recipe:

Keeping SSE Connections Alive with Heartbeats
=============================================

Server-Sent Event (SSE) streams may remain idle for a long time when no
business events are available. Proxies and load balancers often close such
seemingly idle connections, forcing clients to reconnect.

For ASGI applications, Falcon can automatically emit SSE comment frames
(``: ping``) as heartbeats whenever no event has been sent for a
configurable number of seconds. Heartbeats are disabled by default; enable
them app-wide via
:attr:`falcon.ResponseOptions.sse_heartbeat_interval`, and override or
disable the interval for individual responses via
:attr:`falcon.asgi.Response.sse_heartbeat_interval`:

.. literalinclude:: ../../../examples/recipes/sse_heartbeat_app.py
    :language: python

Heartbeats are only emitted while waiting for the next event from the
emitter; the emitter is neither cancelled nor restarted, and no events are
dropped or prefetched. When the client is slow to consume the stream,
heartbeat frames are not queued up either, so a stalled connection does
not lead to unbounded buffering.
