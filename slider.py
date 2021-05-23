#!/bin/python3

import serial
import logging
import argparse
from datetime import datetime
import time
import os
import subprocess
import shutil
import re
import sys

SERIAL_BAUDRATE         = 115200
SERIAL_TIMEOUT_READ     = 0.5
SERIAL_TIMEOUT_WRITE    = 0.5
SERIAL_PORT_GRBL        = "/dev/tty.wchusbserial14210"
SERIAL_PORT_TRIGGER     = "/dev/ttyAMA0"

FILE_EXTENSION          = ".arw"
GPHOTO_DIRECTORY        = "/home/pi/storage"

FEEDRATE                = 2000 

# INTERVAL MODE
PRE_CAPTURE_WAIT        = 0.5
POST_CAPTURE_WAIT       = 0.1

MODE_INTERVAL           = "interval"
MODE_CONT               = "continuous"

def _send_command(ser, cmd, param=None):
    response = ""

    try:
        full_cmd = None
        if param is None:
            full_cmd = cmd
        else:
            full_cmd = "{} {}".format(cmd, param)

        log.debug("serial send: {}".format(full_cmd))

        ser.write(bytearray(full_cmd, "utf-8"))
        ser.write(bytearray("\n", "utf-8"))

        response = ser.read(100)
        response = response.decode("utf-8") 

        # remove every non-alphanumeric / non-underscore / non-space / non-decimalpoint character
        response = re.sub("[^a-zA-Z0-9_ .]", '', response)

        log.debug("serial receive: {}".format(response))

        if response is None or len(response) == 0:
            log.debug("empty response".format())
            raise Exception("empty response or timeout")

        if not response.startswith("ok"):
            log.debug("serial error, non ok response: {}".format(response))
            raise Exception("serial error, non ok response: {}".format(response))

        if len(response) > 1:
            return response[3:]
        else: 
            return None

    except serial.serialutil.SerialException as se:
        log.error("comm failed, SerialException: {}".format(se))
        raise se

    except Exception as e:
        log.error("comm failed, unknown exception: {}".format(e))
        raise e


def _acquire_filename(path):
    filename = None

    for i in range(0, 9999):
        name = i
        name = str(name).zfill(4)
        testname = name + FILE_EXTENSION
        if not os.path.exists(os.path.join(path, testname)):
            filename = testname
            break

    log.debug("acquired filename: {}".format(filename))

    return (path, filename)


def global_except_hook(exctype, value, traceback):
    close_ports()
    sys.__excepthook__(exctype, value, traceback)


def close_ports():

    log.info("closing serial connections")

    if not ser_grbl is None:
        ser_grbl.close()

    if not ser_trigger is None:
        ser_trigger.close()


log = logging.getLogger()

if __name__ == "__main__":

    global ser_grbl
    global ser_trigger

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "command",
        default=MODE_INTERVAL,
        choices=[MODE_INTERVAL, MODE_CONT], 
        help=""
    )

    ap.add_argument("-x", type=int, help="X axis units [mm]")
    ap.add_argument("-y", type=int, help="Y axis units [mm]")
    ap.add_argument("-z", type=int, help="Z axis units [mm]")
    ap.add_argument("-s", "--shutter-count", type=int, help="shutter trigger count")
    ap.add_argument("-t", "--time", type=int, help="total movement time from start to end [ms]")
    ap.add_argument("-d", "--delay", type=int, default=1, help="delay [ms]")
    ap.add_argument("-e", "--external-trigger", help="use an external USB trigger board")
    ap.add_argument("--debug", action="store_true", default=False, help="print debug messages")
    args = vars(ap.parse_args())
    
    input_shutter = args["shutter_count"]
    input_duration = args["time"]
    input_delay = args["delay"]

    log.info("init")

    # create logger
    log.handlers = [] # remove externally inserted handlers (systemd?)
    if args["debug"]:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)

    # create formatter
    formatter = logging.Formatter("%(asctime)s | %(name)-7s | %(levelname)-7s | %(message)s")

    # console handler and set level to debug
    consoleHandler = logging.StreamHandler()
    consoleHandler.setLevel(logging.DEBUG)
    consoleHandler.setFormatter(formatter)
    log.addHandler(consoleHandler)

    # global exception hook for killing the serial connection
    sys.excepthook = global_except_hook

    ser_grbl = serial.Serial(
        SERIAL_PORT_GRBL, SERIAL_BAUDRATE, 
        timeout=SERIAL_TIMEOUT_READ, 
        write_timeout=SERIAL_TIMEOUT_WRITE)
    time.sleep(2.0)
    response = ser_grbl.read(100) # get rid of init message "Grbl 1.1h ['$' for help]"

    ser_trigger = None

    if args["external_trigger"]:
        ser_trigger = serial.Serial(
            SERIAL_PORT_SHUTTER, SERIAL_BAUDRATE, 
            timeout=SERIAL_TIMEOUT_READ, 
            write_timeout=SERIAL_TIMEOUT_WRITE)
    else:
        if os.uname().nodename == "raspberrypi":
            try:
                os.makedir(GPHOTO_DIRECTORY)
            except OSError as e:
                log.debug("creating directory {} failed".format(GPHOTO_DIRECTORY))

    # GRBL setup

    grbl_setup_commands = [
        # "G91",                      # relative positioning
        "G90",                      # absolute positioning
        "G10 P0 L20 X0 Y0 Z0",      # set current pos as zero
        "G21",                      # set units to millimeters
        "G1 F{}".format(FEEDRATE)   # set feedrate to _ mm/min
    ]

    for cmd in grbl_setup_commands:
        resp = _send_command(ser_grbl, cmd)

    # modes

    if args["command"] == MODE_INTERVAL: # INTERVAL MODE 

        steps = []
        step_size = [0, 0, 0]

        if not args["x"] is None:
            step_size[0] = float(args["x"])/(input_shutter-1)

        if not args["y"] is None:
            step_size[1] = float(args["y"])/(input_shutter-1)

        if not args["z"] is None:
            step_size[2] = float(args["z"])/(input_shutter-1)

        for i in range(0, input_shutter+1):
            steps.append([step_size[0] * i, step_size[1] * i, step_size[2] * i])
        
        for i in range(0, input_shutter):

            log.info("INTERVAL: {}/{}: X: {:5.2f} Y:{:5.2f} Z:{:5.2f}".format(
                i+1, input_shutter, *steps[i]))

            while(True):
                try:
                    resp = _send_command(ser_grbl, "G4 P0")
                except Exception as e:
                    pass
                else:
                    log.debug("grbl command buffer done")
                    break

            # EXT SHUTTER:

            # # start timer
            # start = datetime.now()

            # # trigger
            # if ser_trigger is not None:
            #     pass
            # else:
            #     raise Exception("shutter not found")
 
            # # wait till timer ends
            # while (datetime.now() - (start + args["delay"])).total_seconds() < 0:
            #     time.sleep(0.1)
            #     print("sleep")

            # GPHOTO

            log.debug("TRIGGER [{}/{}]".format(i+1, input_shutter))

            # time.sleep(PRE_CAPTURE_WAIT)

            # temp_file = "capt0000{}".format(FILE_EXTENSION)
            # filename = _acquire_filename(GPHOTO_DIRECTORY)

            # if filename is None:
            #     raise Exception("could not acquire filename")

            # subprocess.call("gphoto2 --capture-image-and-download --force-overwrite", shell=True)
            # if not os.path.exists(temp_file):
            #     raise Exception("captured RAW file missing")
            # shutil.move(temp_file, os.path.join(*filename))

            # log.debug("camera save done: {}".format(filename[1]))

            # time.sleep(POST_CAPTURE_WAIT)

            # move
            cmd = "G1 X{} Y{} Z{}".format(*steps[i])
            _send_command(ser_grbl, cmd)


    elif args["command"] == MODE_CONT: # TIME MODE
        pass
    else:
        pass

    close_ports()
    log.info("done.")