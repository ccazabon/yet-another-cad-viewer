import asyncio
import atexit
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from threading import Thread
from typing import Optional, Dict, Union

import aiohttp_cors
from OCP.TopoDS import TopoDS_Shape
from aiohttp import web
from build123d import Shape, Axis
from dataclasses_json import dataclass_json, config

from mylogger import logger
from pubsub import BufferedPubSub
from tessellate import _hashcode, tessellate

FRONTEND_BASE_PATH = os.getenv('FRONTEND_BASE_PATH', '../dist')
UPDATES_API_PATH = '/api/updates'
OBJECTS_API_PATH = '/api/object'  # /{name}


@dataclass_json
@dataclass
class UpdatesApiData:
    """Data sent to the client through the updates API"""
    name: str
    """Name of the object. Should be unique unless you want to overwrite the previous object"""
    hash: str
    """Hash of the object, to detect changes without rebuilding the object"""
    obj: Optional[TopoDS_Shape] = field(default=None, metadata=config(exclude=lambda obj: True))
    """The OCCT object, if any (not serialized)"""
    kwargs: Optional[Dict[str, any]] = field(default=None, metadata=config(exclude=lambda obj: True))
    """The show_object options, if any (not serialized)"""


# noinspection PyUnusedLocal
async def _index_handler(request: web.Request) -> web.Response:
    return web.HTTPTemporaryRedirect(location='index.html')


class Server:
    app = web.Application()
    runner: web.AppRunner
    thread: Optional[Thread] = None
    do_shutdown = asyncio.Event()
    show_events = BufferedPubSub[UpdatesApiData]()
    object_events: Dict[str, BufferedPubSub[bytes]] = {}
    object_events_lock = asyncio.Lock()

    def __init__(self, *args, **kwargs):
        # --- Routes ---
        # - APIs
        self.app.router.add_route('GET', f'{UPDATES_API_PATH}', self._api_updates)
        self.app.router.add_route('GET', f'{OBJECTS_API_PATH}/{{name}}', self._api_object)
        # - Single websocket/objects/frontend entrypoint to ease client configuration
        self.app.router.add_get('/', self._entrypoint)
        # - Static files from the frontend
        self.app.router.add_get('/{path:(.*/|)}', _index_handler)  # Any folder -> index.html
        self.app.router.add_static('/', path=FRONTEND_BASE_PATH, name='static_frontend')
        # --- CORS ---
        cors = aiohttp_cors.setup(self.app, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        })
        for route in list(self.app.router.routes()):
            cors.add(route)
        # --- Misc ---
        self.loop = asyncio.new_event_loop()

    def start(self):
        """Starts the web server in the background"""
        assert self.thread is None, "Server currently running, cannot start another one"
        # Start the server in a separate daemon thread
        self.thread = Thread(target=self._run_server, name='yacv_server', daemon=True)
        signal.signal(signal.SIGINT | signal.SIGTERM, self.stop)
        atexit.register(self.stop)
        self.thread.start()

    # noinspection PyUnusedLocal
    def stop(self, *args):
        """Stops the web server"""
        if self.thread is None:
            print('Cannot stop server because it is not running')
            return
        # FIXME: Wait for at least one client to confirm ready before stopping in case we are too fast?
        self.loop.call_soon_threadsafe(lambda *a: self.do_shutdown.set())
        self.thread.join(timeout=12)
        self.thread = None
        if len(args) >= 1 and args[0] in (signal.SIGINT, signal.SIGTERM):
            sys.exit(0)  # Exit with success

    def _run_server(self):
        """Runs the web server"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._run_server_async())
        self.loop.stop()
        self.loop.close()

    async def _run_server_async(self):
        """Runs the web server (async)"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, os.getenv('YACV_HOST', 'localhost'), int(os.getenv('YACV_PORT', 32323)))
        await site.start()
        # print(f'Server started at {site.name}')
        # Wait for a signal to stop the server while running
        await self.do_shutdown.wait()
        # print('Shutting down server...')
        await runner.cleanup()

    async def _entrypoint(self, request: web.Request) -> web.StreamResponse:
        """Main entrypoint to the server, which automatically serves the frontend/updates/objects"""
        if request.headers.get('Upgrade', '').lower() == 'websocket':  # WebSocket -> updates API
            return await self._api_updates(request)
        elif request.query.get('api_object', '') != '':  # ?api_object={name} -> object API
            request.match_info['name'] = request.query['api_object']
            return await self._api_object(request)
        else:  # Anything else -> frontend index.html
            return await _index_handler(request)

    async def _api_updates(self, request: web.Request) -> web.WebSocketResponse:
        """Handles a publish-only websocket connection that send show_object events along with their hashes and URLs"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async def _send_api_updates():
            subscription = self.show_events.subscribe()
            try:
                async for data in subscription:
                    # noinspection PyUnresolvedReferences
                    await ws.send_str(data.to_json())
            finally:
                await subscription.aclose()

        # Start sending updates to the client automatically
        send_task = asyncio.create_task(_send_api_updates())
        try:
            logger.debug('Client connected: %s', request.remote)
            # Wait for the client to close the connection (or send a message)
            await ws.receive()
        finally:
            # Make sure to stop sending updates to the client and close the connection
            send_task.cancel()
            await ws.close()
            logger.debug('Client disconnected: %s', request.remote)

        return ws

    obj_counter = 0

    def _show_common(self, name: Optional[str], hash: str, start: float, obj: Optional[TopoDS_Shape] = None,
                     kwargs=None):
        name = name or f'object_{self.obj_counter}'
        self.obj_counter += 1
        precomputed_info = UpdatesApiData(name=name, hash=hash, obj=obj, kwargs=kwargs or {})
        self.show_events.publish_nowait(precomputed_info)
        logger.info('show_object(%s, %s) took %.3f seconds', name, hash, time.time() - start)
        return precomputed_info

    def show(self, any_object: Union[bytes, TopoDS_Shape, any], name: Optional[str] = None, **kwargs):
        """Publishes "any" object to the server"""
        if isinstance(any_object, bytes):
            self.show_gltf(any_object, name, **kwargs)
        else:
            self.show_cad(any_object, name, **kwargs)

    def show_gltf(self, gltf: bytes, name: Optional[str] = None, **kwargs):
        """Publishes any single-file GLTF object to the server (GLB format recommended)."""
        start = time.time()
        # Precompute the info and send it to the client as if it was a CAD object
        precomputed_info = self._show_common(name, _hashcode(gltf, **kwargs), start, kwargs=kwargs)
        # Also pre-populate the GLTF data for the object API
        publish_to = BufferedPubSub[bytes]()
        publish_to.publish_nowait(gltf)
        publish_to.publish_nowait(b'')  # Signal the end of the stream
        self.object_events[precomputed_info.name] = publish_to

    def show_cad(self, obj: Union[TopoDS_Shape, any], name: Optional[str] = None, **kwargs):
        """Publishes a CAD object to the server"""
        start = time.time()

        # Try to grab a shape if a different type of object was passed
        if not isinstance(obj, TopoDS_Shape):
            # Build123D
            if 'part' in dir(obj):
                obj = obj.part
            if 'sketch' in dir(obj):
                obj = obj.sketch
            if 'line' in dir(obj):
                obj = obj.line
            # Build123D & CadQuery
            while 'wrapped' in dir(obj) and not isinstance(obj, TopoDS_Shape):
                obj = obj.wrapped
            if not isinstance(obj, TopoDS_Shape):
                raise ValueError(f'Cannot show object of type {type(obj)} (submit issue?)')

        # Convert Z-up (OCCT convention) to Y-up (GLTF convention)
        obj = Shape(obj).rotate(Axis.X, -90).wrapped

        self._show_common(name, _hashcode(obj, **kwargs), start, obj, kwargs)

    async def _api_object(self, request: web.Request) -> web.Response:
        """Returns the object file with the matching name, building it if necessary."""

        # Export the object (or fail if not found)
        exported_glb = await self.export(request.match_info['name'])
        response = web.Response()
        try:
            # Create a new stream response with custom content type and headers
            response.content_type = 'model/gltf-binary'
            response.headers['Content-Disposition'] = f'attachment; filename="{request.match_info["name"]}.glb"'
            await response.prepare(request)

            # Stream the export data to the response
            response.body = exported_glb
        finally:
            # Close the response (if not an error)
            if response.prepared:
                await response.write_eof()
        return response

    async def export(self, name: str) -> bytes:
        """Export the given previously-shown object to a single GLB file, building it if necessary."""
        start = time.time()
        # Check that the object to build exists and grab it if it does
        found = False
        obj: Optional[TopoDS_Shape] = None
        kwargs: Optional[Dict[str, any]] = None
        subscription = self.show_events.subscribe(include_future=False)
        try:
            async for data in subscription:
                if data.name == name:
                    obj = data.obj
                    found = True  # Required because obj could be None
                    break
        finally:
            await subscription.aclose()
        if not found:
            raise web.HTTPNotFound(text=f'No object named {name} was previously shown')

        # Use the lock to ensure that we don't build the object twice
        async with self.object_events_lock:
            # If there are no object events for this name, we need to build the object
            if name not in self.object_events:
                # Prepare the pubsub for the object
                publish_to = BufferedPubSub[bytes]()
                self.object_events[name] = publish_to

                def _build_object():
                    # Build and publish the object (once)
                    gltf = tessellate(obj, tolerance=kwargs.get('tolerance', 0.1),
                                      angular_tolerance=kwargs.get('angular_tolerance', 0.1),
                                      faces=kwargs.get('faces', True),
                                      edges=kwargs.get('edges', True),
                                      vertices=kwargs.get('vertices', True))
                    glb_list_of_bytes = gltf.save_to_bytes()
                    publish_to.publish_nowait(b''.join(glb_list_of_bytes))
                    logger.info('export(%s) took %.3f seconds, %d parts', name, time.time() - start,
                                len(gltf.meshes[0].primitives))

                # We should build it fully even if we are cancelled, so we use a separate task
                # Furthermore, building is CPU-bound, so we use the default executor
                asyncio.get_running_loop().run_in_executor(None, _build_object)

        # In either case return the elements of a subscription to the async generator
        subscription = self.object_events[name].subscribe()
        try:
            return await anext(subscription)
        finally:
            await subscription.aclose()
