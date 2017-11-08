#/usr/bin/env python3

import os
import dbus
import sys
import subprocess
import multiprocessing
import logging
import socket
import signal

from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

EV_TABLET_MODE = b" 40D1BF71-A82D-4E"

class BasicEventHandler:
    def __init__(self, logger):
        self.logger = logger
        self.initialize();

        self.is_tablet = False

    def initialize(self):        
        pass

    def on_mode_change(self):
        self.is_tablet = not self.is_tablet
        if self.is_tablet:
            self.on_tablet_mode()
        else:
            self.on_laptop_mode()

    def on_tablet_mode(self):
        raise NotImplemented()

    def on_laptop_mode(self):
        raise NotImplemented()

    def on_rotate(self, orientation):
        raise NotImplemented()

    def on_stylus_event(self, status):
        raise NotImplemented()

class DefaultEventHandler(BasicEventHandler):
    xrandr_orientation_map = {
        'right-up': 'right',
        'normal': 'normal',
        'bottom-up': 'inverted',
        'left-up': 'left',
    }

    wacom_orientation_map = {
        'right-up': 'cw',
        'normal': 'none',
        'bottom-up': 'half',
        'left-up': 'ccw',
    }
    
    def initialize(self):
        self.wacom = [ i.decode().split('\t')[0] for i in
                  filter(lambda x:bool(x),
                         subprocess.check_output(['xsetwacom', '--list', 'devices']).split(b'\n'))]

        for i in self.wacom:
            self.logger.info("Wacom device detected: %s", i)

        xinput = subprocess.check_output(['xinput', '--list', '--name-only']).decode().split('\n')
        
        self.stylus = next(filter(lambda x: "stylus" in x, xinput))
        self.finger_touch = next(filter(lambda x: "Finger touch" in x, xinput))
        self.trackpoint = next(filter(lambda x: "TrackPoint" in x, xinput))
        self.touchpad = next(filter(lambda x: "TouchPad" in x, xinput))
        
        self.logger.info("xinput devices detected: stylus = %s, finger touch = %s, trackpoint = %s, touchpad = %s", self.stylus, self.finger_touch, self.trackpoint, self.touchpad)

    def on_tablet_mode(self):
        self.logger.debug('on tablet mode')

        for i in [self.trackpoint, self.touchpad]:
            subprocess.call(["xinput", "disable", i])

        try:
            self.onboard = subprocess.Popen(['nohup', 'onboard'],
                                                stdout=open('/dev/null', 'w'),
                                                preexec_fn=os.setpgrp)
        except:
            self.logger.warn("exception while starting onboard: ", exc_info=True)

    def on_laptop_mode(self):
        self.logger.debug('on laptop mode')

        for i in [self.trackpoint, self.touchpad]:
            subprocess.call(["xinput", "enable", i])

        try:
            os.kill(self.onboard.pid, signal.SIGTERM)
        except:
            self.logger.warn("exception while terminating onboard: ", exc_info=True)

    def on_rotate(self, orientation):
        self.logger.debug('on rotate: %s', orientation)
        subprocess.call(["xrandr", "-o", DefaultEventHandler.xrandr_orientation_map[orientation]])
        for i in self.wacom:
            subprocess.call(["xsetwacom", "--set", i, "rotate", DefaultEventHandler.wacom_orientation_map[orientation]])            

    def on_stylus_event(self, status):
        self.logger.debug('on stylus event: %s', status)

class SocketWrapper:
    def __init__(self, sock):
        self.buffer = b""
        self.sock = sock

    def read_line(self):
        while b'\n' not in self.buffer:
            self.buffer += self.sock.recv(4096)

        ret, self.buffer = self.buffer.split(b'\n', 1)
        return ret    

def process_wrapper(name, target, argtuple):
    logger.info('%s started', name)
    args, kwargs = argtuple
    try:
        target(*args, **kwargs)
    except:
        exc_info = sys.exc_info()
        logger.error('uncaught exception from process %s:', exc_info = exc_info)
        try:
            message_queue.put(('exit', [name, 'uncaught-exception',exc_info]))
        except:
            pass

def spawn_process(name, target, *args, **kwargs):
    ret = multiprocessing.Process(target=process_wrapper, args=(name, target, (args, kwargs)))
    ret.start()
    return ret

def acpi_events_watcher():
    acpi_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    acpi_socket.connect("/var/run/acpid.socket")
    acpi_socket = SocketWrapper(acpi_socket)

    while True:
        event = acpi_socket.read_line()
        logger.debug("acpid event: %s", event)
        if event.startswith(EV_TABLET_MODE):
            logger.debug('mode change event')
            message_queue.put(('mode-change', []))

def dbus_events_watcher():
    def sensor_proxy_signal_handler(source, changedProperties, invalidatedProperties, **kwargs):
        if 'AccelerometerOrientation' not in changedProperties:
            return
        
        orientation = changedProperties['AccelerometerOrientation']
        logger.debug('dbus signal: orientation change: %s', orientation)
    
        message_queue.put(('rotate', [orientation]))

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    proxy = bus.get_object('net.hadess.SensorProxy', '/net/hadess/SensorProxy')
    props = dbus.Interface(proxy, 'org.freedesktop.DBus.Properties')
    props.connect_to_signal('PropertiesChanged', sensor_proxy_signal_handler, sender_keyword='sender')
    ifce = dbus.Interface(proxy, 'net.hadess.SensorProxy')
    ifce.ClaimAccelerometer()

    loop = GLib.MainLoop()
    loop.run()

def stylus_events_watcher():
    lines = subprocess.check_output(['xinput','--list', '--name-only']).decode().split('\n')

    try:
        stylus = next(x for x in lines if "stylus" in x)
    except:
        raise RuntimeError('stylus not found')
    
    logger.info("found stylus %s", stylus)    
    
    xinput_pipe = subprocess.Popen(['xinput', 'test', '-proximity', stylus], stdout=subprocess.PIPE)
    for line in xinput_pipe.stdout:
        logger.debug('xinput stdout: %s', line)
        if not line.startswith(b'proximity'):
            continue
        status = line.split(b' ')[1]
        message_queue.put(('stylus-event', [status]))

def run(handler_type=DefaultEventHandler):
    global logger
    logger = logging.getLogger()
    logger.addHandler(logging.StreamHandler())
    logger.level = logging.INFO

    global message_queue
    message_queue = multiprocessing.Queue()
    
    handler = handler_type(logger)

    acpi_watcher = spawn_process('acpi_events_watcher', acpi_events_watcher)
    dbus_watcher = spawn_process('dbus_events_watcher', dbus_events_watcher)
    stylus_watcher = spawn_process('stylus_events_watcher', stylus_events_watcher)

    while True:
        event, args = message_queue.get()
        logger.debug('received event %s', event)
        
        if event == 'exit':
            name, reason, *args = args
            logger.info('received child exit event from %s caused by %s', name, reason)

        elif event == 'mode-change':
            handler.on_mode_change()

        elif event == 'rotate':
            orientation, = args
            handler.on_rotate(orientation)

        elif event == 'stylus-event':
            status, = args
            handler.on_stylus_event(status)

if __name__ == '__main__':
    run()
