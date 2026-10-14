"""Command line and MCP entry points of the translation engine.

``translator-sidecar types | scan | translate | status | stop | mcp`` (or
``python -m cli …`` from source).  Everything goes through
``core.engine_client``, so a task started here is an ordinary task of the
running app: it shows in the task center and can be stopped there.  No
module here knows a translation type by name; the type descriptors
(``GET /api/surfaces``) drive detection, options and output.
"""
