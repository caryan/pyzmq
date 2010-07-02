#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""A level-triggered I/O loop for non-blocking sockets."""

import bisect
import errno
import os
try:
    import fcntl
except ImportError:
    if os.name != 'posix':
        import socket
    else:
        raise
import logging
import socket
import time

from _zmq import (
    Poller,
    POLLIN, POLLOUT, POLLERR,
    ZMQError
)


class Pipe(object):
    """Create an OS independent asynchronous pipe"""
    def __init__(self):
        self.reader = None
        self.writer = None
        self.reader_fd = -1
        if os.name == 'posix':
            self.reader_fd, w = os.pipe()
            IOLoop.set_nonblocking(self.reader_fd)
            IOLoop.set_nonblocking(w)
            IOLoop.set_close_exec(self.reader_fd)
            IOLoop.set_close_exec(w)
            self.reader = os.fdopen(self.reader_fd, "r", 0)
            self.writer = os.fdopen(w, "w", 0)
        else:
            # Based on Zope async.py: http://svn.zope.org/zc.ngi/trunk/src/zc/ngi/async.py

            self.writer = socket.socket()
            # Disable buffering -- pulling the trigger sends 1 byte,
            # and we want that sent immediately, to wake up asyncore's
            # select() ASAP.
            self.writer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            count = 0
            while 1:
                count += 1
                # Bind to a local port; for efficiency, let the OS pick
                # a free port for us.
                # Unfortunately, stress tests showed that we may not
                # be able to connect to that port ("Address already in
                # use") despite that the OS picked it.  This appears
                # to be a race bug in the Windows socket implementation.
                # So we loop until a connect() succeeds (almost always
                # on the first try).  See the long thread at
                # http://mail.zope.org/pipermail/zope/2005-July/160433.html
                # for hideous details.
                a = socket.socket()
                a.bind(("127.0.0.1", 0))
                connect_address = a.getsockname()  # assigned (host, port) pair
                a.listen(1)
                try:
                    self.writer.connect(connect_address)
                    break    # success
                except socket.error, detail:
                    if detail[0] != errno.WSAEADDRINUSE:
                        # "Address already in use" is the only error
                        # I've seen on two WinXP Pro SP2 boxes, under
                        # Pythons 2.3.5 and 2.4.1.
                        raise
                    # (10048, 'Address already in use')
                    # assert count <= 2 # never triggered in Tim's tests
                    if count >= 10:  # I've never seen it go above 2
                        a.close()
                        self.writer.close()
                        raise BindError("Cannot bind trigger!")
                    # Close `a` and try again.  Note:  I originally put a short
                    # sleep() here, but it didn't appear to help or hurt.
                    a.close()

            self.reader, addr = a.accept()
            self.reader.setblocking(0)
            self.writer.setblocking(0)
            a.close()
            self.reader_fd = self.reader.fileno()

    def read(self):
        try:
            return self.reader.recv(1)
        except socket.error, ex:
            if ex.args[0] == errno.EWOULDBLOCK:
                raise IOError
            raise

    def write(self, data):
        return self.writer.send(data)


class IOLoop(object):
    """A level-triggered I/O loop.

    We use epoll if it is available, or else we fall back on select(). If
    you are implementing a system that needs to handle 1000s of simultaneous
    connections, you should use Linux and either compile our epoll module or
    use Python 2.6+ to get epoll support.

    Example usage for a simple TCP server:

        import errno
        import functools
        import ioloop
        import socket

        def connection_ready(sock, fd, events):
            while True:
                try:
                    connection, address = sock.accept()
                except socket.error, e:
                    if e[0] not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    return
                connection.setblocking(0)
                handle_connection(connection, address)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(0)
        sock.bind(("", port))
        sock.listen(128)

        io_loop = ioloop.IOLoop.instance()
        callback = functools.partial(connection_ready, sock)
        io_loop.add_handler(sock.fileno(), callback, io_loop.READ)
        io_loop.start()

    """
    # Use the zmq events masks
    NONE = 0
    READ = POLLIN
    WRITE = POLLOUT
    ERROR = POLLERR

    def __init__(self, impl=None):
        self._impl = impl or Poller()
        if hasattr(self._impl, 'fileno'):
            self.set_close_exec(self._impl.fileno())
        self._handlers = {}
        self._events = {}
        self._callbacks = set()
        self._timeouts = []
        self._running = False
        self._stopped = False

        # Create a pipe that we send bogus data to when we want to wake
        # the I/O loop when it is idle
        trigger = Pipe()
        self._waker_reader = self._waker_writer = trigger
        self.add_handler(trigger.reader_fd, self._read_waker, self.READ)

    @classmethod
    def instance(cls):
        """Returns a global IOLoop instance.

        Most single-threaded applications have a single, global IOLoop.
        Use this method instead of passing around IOLoop instances
        throughout your code.

        A common pattern for classes that depend on IOLoops is to use
        a default argument to enable programs with multiple IOLoops
        but not require the argument for simpler applications:

            class MyClass(object):
                def __init__(self, io_loop=None):
                    self.io_loop = io_loop or IOLoop.instance()
        """
        if not hasattr(cls, "_instance"):
            cls._instance = cls()
        return cls._instance

    @classmethod
    def initialized(cls):
        return hasattr(cls, "_instance")

    def add_handler(self, fd, handler, events):
        """Registers the given handler to receive the given events for fd."""
        self._handlers[fd] = handler
        self._impl.register(fd, events | self.ERROR)

    def update_handler(self, fd, events):
        """Changes the events we listen for fd."""
        self._impl.modify(fd, events | self.ERROR)

    def remove_handler(self, fd):
        """Stop listening for events on fd."""
        self._handlers.pop(fd, None)
        self._events.pop(fd, None)
        try:
            self._impl.unregister(fd)
        except (OSError, IOError):
            logging.debug("Error deleting fd from IOLoop", exc_info=True)

    def start(self):
        """Starts the I/O loop.

        The loop will run until one of the I/O handlers calls stop(), which
        will make the loop stop after the current event iteration completes.
        """
        if self._stopped:
            self._stopped = False
            return
        self._running = True
        while True:
            # Never use an infinite timeout here - it can stall epoll
            poll_timeout = 0.2

            # Prevent IO event starvation by delaying new callbacks
            # to the next iteration of the event loop.
            callbacks = list(self._callbacks)
            for callback in callbacks:
                # A callback can add or remove other callbacks
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
                    self._run_callback(callback)

            if self._callbacks:
                poll_timeout = 0.0

            if self._timeouts:
                now = time.time()
                while self._timeouts and self._timeouts[0].deadline <= now:
                    timeout = self._timeouts.pop(0)
                    self._run_callback(timeout.callback)
                if self._timeouts:
                    milliseconds = self._timeouts[0].deadline - now
                    poll_timeout = min(milliseconds, poll_timeout)

            if not self._running:
                break

            try:
                event_pairs = self._impl.poll(poll_timeout)
            except ZMQError, e:
                raise
            except Exception, e:
                if e.errno == errno.EINTR:
                    logging.warning("Interrupted system call", exc_info=1)
                    continue
                else:
                    raise

            # Pop one fd at a time from the set of pending fds and run
            # its handler. Since that handler may perform actions on
            # other file descriptors, there may be reentrant calls to
            # this IOLoop that update self._events
            self._events.update(event_pairs)
            while self._events:
                fd, events = self._events.popitem()
                try:
                    self._handlers[fd](fd, events)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except (OSError, IOError), e:
                    if e[0] == errno.EPIPE:
                        # Happens when the client closes the connection
                        pass
                    else:
                        logging.error("Exception in I/O handler for fd %d",
                                      fd, exc_info=True)
                except:
                    logging.error("Exception in I/O handler for fd %d",
                                  fd, exc_info=True)
        # reset the stopped flag so another start/stop pair can be issued
        self._stopped = False

    def stop(self):
        """Stop the loop after the current event loop iteration is complete.
        If the event loop is not currently running, the next call to start()
        will return immediately.

        To use asynchronous methods from otherwise-synchronous code (such as
        unit tests), you can start and stop the event loop like this:
          ioloop = IOLoop()
          async_method(ioloop=ioloop, callback=ioloop.stop)
          ioloop.start()
        ioloop.start() will return after async_method has run its callback,
        whether that callback was invoked before or after ioloop.start.
        """
        self._running = False
        self._stopped = True
        self._wake()

    def running(self):
        """Returns true if this IOLoop is currently running."""
        return self._running

    def add_timeout(self, deadline, callback):
        """Calls the given callback at the time deadline from the I/O loop."""
        timeout = _Timeout(deadline, callback)
        bisect.insort(self._timeouts, timeout)
        return timeout

    def remove_timeout(self, timeout):
        self._timeouts.remove(timeout)

    def add_callback(self, callback):
        """Calls the given callback on the next I/O loop iteration."""
        self._callbacks.add(callback)
        self._wake()

    def remove_callback(self, callback):
        """Removes the given callback from the next I/O loop iteration."""
        self._callbacks.remove(callback)

    def _wake(self):
        try:
            self._waker_writer.write("x")
        except IOError:
            pass

    def _run_callback(self, callback):
        try:
            callback()
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handle_callback_exception(callback)

    def handle_callback_exception(self, callback):
        """This method is called whenever a callback run by the IOLoop
        throws an exception.

        By default simply logs the exception as an error.  Subclasses
        may override this method to customize reporting of exceptions.

        The exception itself is not passed explicitly, but is available
        in sys.exc_info.
        """
        logging.error("Exception in callback %r", callback, exc_info=True)

    def _read_waker(self, fd, events):
        try:
            while True:
                self._waker_reader.read()
        except IOError:
            pass

    @staticmethod
    def set_nonblocking(fd):
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @staticmethod
    def set_close_exec(fd):
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


class _Timeout(object):
    """An IOLoop timeout, a UNIX timestamp and a callback"""
    def __init__(self, deadline, callback):
        self.deadline = deadline
        self.callback = callback

    def __cmp__(self, other):
        return cmp((self.deadline, id(self.callback)),
                   (other.deadline, id(other.callback)))


class PeriodicCallback(object):
    """Schedules the given callback to be called periodically.

    The callback is called every callback_time milliseconds.
    """
    def __init__(self, callback, callback_time, io_loop=None):
        self.callback = callback
        self.callback_time = callback_time
        self.io_loop = io_loop or IOLoop.instance()
        self._running = True

    def start(self):
        timeout = time.time() + self.callback_time / 1000.0
        self.io_loop.add_timeout(timeout, self._run)

    def stop(self):
        self._running = False

    def _run(self):
        if not self._running: return
        try:
            self.callback()
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            logging.error("Error in periodic callback", exc_info=True)
        self.start()