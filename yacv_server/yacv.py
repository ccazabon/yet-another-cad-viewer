import atexit
import copy
import inspect
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from threading import Thread
from typing import Optional, Dict, Union, Callable, List

from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
# noinspection PyProtectedMember
from build123d import Shape, Axis, Location, Vector
from dataclasses_json import dataclass_json

from myhttp import HTTPHandler
from yacv_server.cad import get_shape, grab_all_cad, CADCoreLike, CADLike
from yacv_server.mylogger import logger
from yacv_server.pubsub import BufferedPubSub
from yacv_server.tessellate import _hashcode, tessellate


@dataclass_json
@dataclass
class UpdatesApiData:
    """Data sent to the client through the updates API"""
    name: str
    """Name of the object. Should be unique unless you want to overwrite the previous object"""
    hash: str
    """Hash of the object, to detect changes without rebuilding the object"""
    is_remove: bool
    """Whether to remove the object from the scene"""


YACVSupported = Union[bytes, CADCoreLike]


class UpdatesApiFullData(UpdatesApiData):
    obj: YACVSupported
    """The OCCT object, if any (not serialized)"""
    kwargs: Optional[Dict[str, any]]
    """The show_object options, if any (not serialized)"""

    def __init__(self, obj: YACVSupported, name: str, _hash: str, is_remove: bool = False,
                 kwargs: Optional[Dict[str, any]] = None):
        self.name = name
        self.hash = _hash
        self.is_remove = is_remove
        self.obj = obj
        self.kwargs = kwargs

    def to_json(self) -> str:
        # noinspection PyUnresolvedReferences
        return super().to_json()


class YACV:
    server_thread: Optional[Thread]
    server: Optional[ThreadingHTTPServer]
    startup_complete: threading.Event
    show_events: BufferedPubSub[UpdatesApiFullData]
    build_events: Dict[str, BufferedPubSub[bytes]]
    object_events_lock: threading.Lock

    def __init__(self):
        self.server_thread = None
        self.server = None
        self.startup_complete = threading.Event()
        self.at_least_one_client = threading.Event()
        self.show_events = BufferedPubSub()
        self.build_events = {}
        self.object_events_lock = threading.Lock()
        self.frontend_lock = threading.Lock()
        logger.info('Using yacv-server v%s', version('yacv-server'))

    def start(self):
        """Starts the web server in the background"""
        assert self.server_thread is None, "Server currently running, cannot start another one"
        assert self.startup_complete.is_set() is False, "Server already started"
        # Start the server in a separate daemon thread
        self.server_thread = Thread(target=self._run_server, name='yacv_server', daemon=True)
        signal.signal(signal.SIGINT | signal.SIGTERM, self.stop)
        atexit.register(self.stop)
        self.server_thread.start()
        logger.info('Server started (requested)...')
        # Wait for the server to be ready before returning
        while not self.startup_complete.wait():
            time.sleep(0.01)
        logger.info('Server started (received)...')

    # noinspection PyUnusedLocal
    def stop(self, *args):
        """Stops the web server"""
        if self.server_thread is None:
            logger.error('Cannot stop server because it is not running')
            return

        graceful_secs_connect = float(os.getenv('YACV_GRACEFUL_SECS_CONNECT', 12.0))
        graceful_secs_request = float(os.getenv('YACV_GRACEFUL_SECS_REQUEST', 5.0))
        # Make sure we can hold the lock for more than 100ms (to avoid exiting too early)
        logger.info('Stopping server (waiting for at least one frontend request first, cancel with CTRL+C)...')
        start = time.time()
        try:
            while not self.at_least_one_client.wait(
                    graceful_secs_connect / 10) and time.time() - start < graceful_secs_connect:
                time.sleep(0.01)
        except KeyboardInterrupt:
            pass

        logger.info('Stopping server (waiting for no more frontend requests)...')
        start = time.time()
        try:
            while time.time() - start < graceful_secs_request:
                if self.frontend_lock.locked():
                    start = time.time()
                time.sleep(0.01)
        except KeyboardInterrupt:
            pass

        # Stop the server in the background
        self.server.shutdown()
        logger.info('Stopping server (sent)...')

        # Wait for the server to stop gracefully
        self.server_thread.join(timeout=30)
        self.server_thread = None
        logger.info('Stopping server (confirmed)...')
        if len(args) >= 1 and args[0] in (signal.SIGINT, signal.SIGTERM):
            sys.exit(0)  # Exit with success

    def _run_server(self):
        """Runs the web server"""
        logger.info('Starting server...')
        self.server = ThreadingHTTPServer(
            (os.getenv('YACV_HOST', 'localhost'), int(os.getenv('YACV_PORT', 32323))),
            lambda a, b, c: HTTPHandler(a, b, c, yacv=self))
        # noinspection HttpUrlsUsage
        logger.info(f'Serving at http://{self.server.server_name}:{self.server.server_port}')
        self.startup_complete.set()
        self.server.serve_forever()

    def show(self, *objs: List[YACVSupported], names: Optional[Union[str, List[str]]] = None, **kwargs):
        # Prepare the arguments
        start = time.time()
        names = names or [_find_var_name(obj) for obj in objs]
        if isinstance(names, str):
            names = [names]
        assert len(names) == len(objs), 'Number of names must match the number of objects'

        # Handle auto clearing of previous objects
        if kwargs.get('auto_clear', True):
            self.clear(except_names=names)

        # Remove a previous object event with the same name
        for old_event in self.show_events.buffer():
            if old_event.name in names:
                self.show_events.delete(old_event)
                if old_event.name in self.build_events:
                    del self.build_events[old_event.name]

        # Publish the show event
        for obj, name in zip(objs, names):
            if not isinstance(obj, bytes):
                obj = _preprocess_cad(obj, **kwargs)
            _hash = _hashcode(obj, **kwargs)
            event = UpdatesApiFullData(name=name, _hash=_hash, obj=obj, kwargs=kwargs or {})
            self.show_events.publish(event)

        logger.info('show %s took %.3f seconds', names, time.time() - start)

    def show_cad_all(self, **kwargs):
        """Publishes all CAD objects in the current scope to the server"""
        all_cad = grab_all_cad()
        self.show(*[cad for _, cad in all_cad], names=[name for name, _ in all_cad], **kwargs)

    def remove(self, name: str):
        """Removes a previously-shown object from the scene"""
        show_events = self._show_events(name)
        if len(show_events) > 0:
            # Ensure only the new remove event remains for this name
            for old_show_event in show_events:
                self.show_events.delete(old_show_event)

            # Delete any cached object builds
            with self.object_events_lock:
                if name in self.build_events:
                    del self.build_events[name]

            # Publish the remove event
            show_event = copy.copy(show_events[-1])
            show_event.is_remove = True
            self.show_events.publish(show_event)

    def clear(self, except_names: List[str] = None):
        """Clears all previously-shown objects from the scene"""
        if except_names is None:
            except_names = []
        for event in self.show_events.buffer():
            if event.name not in except_names:
                self.remove(event.name)

    def shown_object_names(self, apply_removes: bool = True) -> List[str]:
        """Returns the names of all objects that have been shown"""
        res = []
        for obj in self.show_events.buffer():
            if not obj.is_remove or not apply_removes:
                res.append(obj.name)
            else:
                res.remove(obj.name)
        return res

    def _show_events(self, name: str, apply_removes: bool = True) -> List[UpdatesApiFullData]:
        """Returns the show events with the given name"""
        res = []
        for event in self.show_events.buffer():
            if event.name == name:
                if not event.is_remove or not apply_removes:
                    res.append(event)
                else:
                    # Also remove the previous events
                    for old_event in res:
                        if old_event.name == event.name:
                            res.remove(old_event)
        return res

    def export(self, name: str) -> Optional[bytes]:
        """Export the given previously-shown object to a single GLB file, building it if necessary."""
        start = time.time()

        # Check that the object to build exists and grab it if it does
        events = self._show_events(name)
        if len(events) == 0:
            logger.warning('Object %s not found', name)
            return None
        event = events[-1]

        # Use the lock to ensure that we don't build the object twice
        with self.object_events_lock:
            # If there are no object events for this name, we need to build the object
            if name not in self.build_events:
                logger.debug('Building object %s with hash %s', name, event.hash)

                # Prepare the pubsub for the object
                publish_to = BufferedPubSub[bytes]()
                self.build_events[name] = publish_to

                # Build and publish the object (once)
                if isinstance(event.obj, bytes):  # Already a GLTF
                    publish_to.publish(event.obj)
                else:  # CAD object to tessellate and convert to GLTF
                    gltf = tessellate(event.obj, tolerance=event.kwargs.get('tolerance', 0.1),
                                      angular_tolerance=event.kwargs.get('angular_tolerance', 0.1),
                                      faces=event.kwargs.get('faces', True),
                                      edges=event.kwargs.get('edges', True),
                                      vertices=event.kwargs.get('vertices', True))
                    glb_list_of_bytes = gltf.save_to_bytes()
                    publish_to.publish(b''.join(glb_list_of_bytes))
                    logger.info('export(%s) took %.3f seconds, %d parts', name, time.time() - start,
                                len(gltf.meshes[0].primitives))

            # In either case return the elements of a subscription to the async generator
            subscription = self.build_events[name].subscribe()
            try:
                return next(subscription)
            finally:
                subscription.close()

    def export_all(self, folder: str,
                   export_filter: Callable[[str, Optional[CADCoreLike]], bool] = lambda name, obj: True):
        """Export all previously-shown objects to GLB files in the given folder"""
        os.makedirs(folder, exist_ok=True)
        for name in self.shown_object_names():
            if export_filter(name, self._show_events(name)[-1].obj):
                with open(os.path.join(folder, f'{name}.glb'), 'wb') as f:
                    f.write(self.export(name))


# noinspection PyUnusedLocal
def _preprocess_cad(obj: CADLike, **kwargs) -> CADCoreLike:
    # Get the shape of a CAD-like object
    obj = get_shape(obj)

    # Convert Z-up (OCCT convention) to Y-up (GLTF convention)
    if isinstance(obj, TopoDS_Shape):
        obj = Shape(obj).rotate(Axis.X, -90).wrapped
    elif isinstance(obj, TopLoc_Location):
        tmp_location = Location(obj)
        tmp_location.position = Vector(tmp_location.position.X, tmp_location.position.Z,
                                       -tmp_location.position.Y)
        tmp_location.orientation = Vector(tmp_location.orientation.X - 90, tmp_location.orientation.Y,
                                          tmp_location.orientation.Z)
        obj = tmp_location.wrapped

    return obj


_find_var_name_count = 0


def _find_var_name(obj: any) -> str:
    """A hacky way to get a stable name for an object that may change over time"""
    global _find_var_name_count
    for frame in inspect.stack():
        for key, value in frame.frame.f_locals.items():
            if value is obj:
                return key
    _find_var_name_count += 1
    return 'unknown_var_' + str(_find_var_name_count)
