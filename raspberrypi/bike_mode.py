# MIT License

# Copyright (c) 2021 jonyboi396825

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import datetime
import json
import math
import os
import subprocess
import sys
import threading
import time
import traceback
import typing as t
from copy import deepcopy

import Adafruit_SSD1306
import gps
import pytz
import RPi.GPIO as GPIO
import serial
from PIL import Image, ImageDraw, ImageFont

# config data sent to Arduino during setup and other cfg data
cfg_file = open("raspberrypi/cfg.json", 'r')
cfg_ard = json.load(cfg_file)
cfg = deepcopy(cfg_ard)
cur_tz = cfg["TMZ"]
del cfg_ard["24H"], cfg_ard["TMZ"]
cfg_file.close()

# data sent to Arduino during loop
send = {
    "GPS": [-1]*6,
    "LED": [0, 0],
    "B1RCV": False,
    "B2RCV": False
}

# data from GPS
curdata = {}

# tracking and buttons
tracking = 0  # 0 = stopped, 1 = paused, 2 = tracking
wastracking = False # continues tracking even if disconnected
prevbstate1 = False
prevbstate2 = False
prevTimeEpoch = 0

fileName = "ERROR"
msg = "ERROR"

# interval of plotting data during tracking
INTERVAL = 2

# what to put onto disp
disp_data_g = {
    "speed": 0,
    "unit": cfg_ard["UNT"],
    "datetime": datetime.datetime(1970, 1, 1, 0, 0, 0),
    "mode": 'D',
    "track": ''
}
# sync LED panel speed to OLED speed because OLED is slow and cannot do other way around
oled_speed = 0

# Arduino serial port
port_file = open("raspberrypi/port", 'r')
port = port_file.read().strip()
port_file.close()

def err(ex_type, value, tb):
    """
    Custom error (for debugging)
    """

    print(f"Exception occured at: {datetime.datetime.now()}")
    print(ex_type.__name__)
    traceback.print_tb(tb)


def get_gps_data() -> None:
    """
    Thread that gets positional, time, and speed data from GPS since it is blocking
    """

    global curdata

    session = gps.gps("localhost")
    session.stream(gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE)

    while True:
        try:
            report = session.next()
            if (report["class"] == "TPV"):
                curdata = report
                # print(curdata)
        except KeyError:
            pass
        except KeyboardInterrupt:
            quit()
        except StopIteration:
            session = None
            print("GPSD has terminated")


# converts dt as str to time zone
def _conv_tmz(dt: t.Union[str, datetime.datetime], fmt: t.Union[str, None], tmz: str) -> datetime.datetime:
    d_temp = None
    if (isinstance(dt, str)):
        d_temp = datetime.datetime.strptime(
            dt, fmt).replace(tzinfo=pytz.utc)
    else:
        d_temp = dt.replace(tzinfo=pytz.utc)

    timezone = pytz.timezone(tmz)
    d_localized = d_temp.astimezone(timezone)
    return d_localized

def new_track_file(tm: datetime.datetime, tmz: str) -> None:
    global fileName

    n_tm = _conv_tmz(tm, None, tmz)
    fileName = datetime.datetime.strftime(n_tm, "%Y-%m-%d_%H:%M:%S_track_path")

    print(f"creating new track file with time: {fileName}")


def tracker(lat: int, lng: int, tm: datetime.datetime) -> None:
    global tracking, fileName, prevTimeEpoch, msg

    # makes sure doesn't print "PAUSED" multiple times
    # print coordinates every 2 seconds
    if (tracking == 0 or (tracking == 1 and msg.strip().upper() == "PAUSED") or math.floor(time.time())-prevTimeEpoch < INTERVAL):
        return
    else:
        prevTimeEpoch = math.floor(time.time())

    if (tracking == 1):
        msg = "PAUSED\n"
    else:
        msg = str(lat) + "," + str(lng) + "\n"

    print(f"writing {msg[:-1]} to {fileName}")

    with open(os.path.join("tracking", fileName), 'a') as f:
        f.write(msg)


def conv_unit(val: int, unit: int) -> int:
    # given in m/s

    assert(isinstance(unit, int) and unit >= 0 and unit < 3)

    if (unit == 0): # mph
        return int((val/1.0)*2.237)
    elif (unit == 1): # km/h
        return int((val/1.0)*3.6)
    else: # m/s
        return int(val)


def draw_on_display(disp: Adafruit_SSD1306.SSD1306_128_64, img: Image.Image,
                    drawing: ImageDraw.ImageDraw, fonts: "list[ImageFont.ImageFont]", data: dict) -> None:
    """
    draws data on screen according to plan doc

    data: {
        "speed": x in m/s
        "unit": {0, 1, 2}; 0 = mph, 1 = km/h, 2 = m/s
        "datetime": datetime obj
        "mode": String of: {'D', '2', '3'}
        "track": String of: {'T', 'P', ''}
        refer to plan doc for what each means
    }
    """
    global oled_speed

    unit_to_str = ["mph", "km/h", "m/s"]

    mode_font = fonts[0]
    sp_font = fonts[1]
    unit_font = fonts[2]
    track_font = fonts[3]

    # dd-mm or mm-dd
    dayfmt = "%m/%d "
    if (cfg["DTM"] == 1):
        dayfmt = "%d-%m "

    # date_x is to shift to account for lack of AM/PM
    # 23:00 or 11:00PM
    tmfmt = "%I:%M%p"
    date_x = 30
    if (cfg["24H"] == 1):
        date_x = 40
        tmfmt = "%H:%M"

    # conv all to disp strings
    disp_speed = str(conv_unit(data["speed"], int(data["unit"])))
    disp_dt = str(datetime.datetime.strftime(data["datetime"], dayfmt + tmfmt))
    disp_mode = "M:" + str(data["mode"])
    disp_unit = str(unit_to_str[data["unit"]])
    disp_track = str(data["track"])

    # draw black rectangle the size of screen to clear the screen
    drawing.rectangle((0, 0, 128, 128), fill=0)

    drawing.text((0, 0), disp_mode, font=mode_font, fill=255)
    drawing.text((date_x, 0), disp_dt, font=mode_font, fill=255)
    drawing.text((0, 16), disp_speed, font=sp_font, fill=255)
    drawing.text((84, 16), disp_unit, font=unit_font, fill=255)
    drawing.text((84, 48), disp_track, font=track_font, fill=255)

    disp.image(img)

    disp.display()
    time.sleep(0.2)

    # this is the speed currently displayed on the oled
    oled_speed = data["speed"]

def disp_th() -> None:
    # writing to OLED causes delay so I'm putting it in a thread
    global disp_data_g

    display = Adafruit_SSD1306.SSD1306_128_64(rst=None)
    display.begin()
    time.sleep(2)

    display.clear()
    display.display()
    time.sleep(1)

    img = Image.new('1', (display.width, display.height))
    drawing = ImageDraw.Draw(img)

    FONTFILE = "raspberrypi/fonts/Gidole-Regular.ttf"
    fonts = [
        ImageFont.truetype(FONTFILE, 15),
        ImageFont.truetype(FONTFILE, 45),
        ImageFont.truetype(FONTFILE, 20),
        ImageFont.truetype(FONTFILE, 15)
    ]

    while True:
        try:
            draw_on_display(display, img, drawing, fonts, disp_data_g)
        except OSError:
            # ask user to reconnect by exiting out of program
            # refer to __main__.py under handle_bike_mode()
            print(f"OLED disconnected", file=sys.stderr)
            os._exit(1)

def main_ser_connect(ser: serial.Serial) -> None:
    global cfg_ard, send, curdata, tracking, prevbstate1, prevbstate2, disp_data_g, cur_tz, oled_speed, wastracking

    while True:
        while (ser.is_open):
            # what to put onto display
            display_dict = {
                "speed": 0,
                "unit": cfg_ard["UNT"],
                "datetime": datetime.datetime(1970, 1, 1, 0, 0, 0),
                "mode": 'D',
                "track": ''
            }

            # get data from Arduino
            rcv = {}
            if (ser.in_waiting):
                temp = ser.readline()
                if (temp != b"\r\n"):
                    rcv = json.loads(temp.decode('utf-8').rstrip())

            # get data from curdata (from thread) and alter send
            if ("mode" not in curdata or curdata["mode"] < 2):
                # disconnected
                send["GPS"] = [-1]*6
                send["LED"][1] = 2
                tracking = 0
            else:
                # GPS: [lat, long, speed, month, day, hr, min]
                # turn red LED off since there is communcation with GPS
                send["LED"][1] = 0

                # keep tracking if GPS was disconnected
                if (tracking == 0 and wastracking):
                    tracking = 2

                # time
                curtime = curdata["time"]
                d_localized = _conv_tmz(curtime[:-5], "%Y-%m-%dT%H:%M:%S", cur_tz)

                # speed, given in m/s
                speed = curdata["speed"]

                # send speed currently displayed on oled to panel so it is synced with oled
                send["GPS"] = [curdata["lat"], curdata["lon"], conv_unit(oled_speed, unit=display_dict["unit"]),
                               d_localized.month, d_localized.day, d_localized.hour, d_localized.minute]

                # update what to display
                t = ['', 'P', 'T']
                display_dict["speed"] = speed
                display_dict["mode"] = curdata["mode"]
                display_dict["datetime"] = d_localized
                display_dict["track"] = t[tracking]

            # TRACKING

            # if button 1 pressed
            if ("BUTTON1" in rcv and rcv["BUTTON1"] and not prevbstate1):
                if (tracking == 1 or tracking == 2):
                    wastracking = False
                    tracking = 0
                else:
                    tracking = 2

                    # create new file when button pressed
                    if (send["LED"][1] == 0):
                        wastracking = True
                        tm = datetime.datetime.strptime(
                            curdata["time"][:-5], "%Y-%m-%dT%H:%M:%S")
                        new_track_file(tm, cur_tz)

            # if button 2 pressed
            if ("BUTTON2" in rcv and rcv["BUTTON2"] and not prevbstate2 and tracking != 0):
                # switch between pausing it and resuming it (states 1 and 2)
                tracking = (tracking % 2)+1

            # adjust green LED
            # off = tracking stopped
            # flashing = tracking paused
            # on = tracking on
            send["LED"][0] = tracking

            # put tracking to file
            # make sure there is data from GPS since red LED would be on if no data
            if (send["LED"][1] == 0):
                tm = datetime.datetime.strptime(
                    curdata["time"][:-5], "%Y-%m-%dT%H:%M:%S")
                tracker(curdata["lat"], curdata["lon"], tm)

            # Process received data and prepare sending data
            send_str = ""
            if ("REQ" in rcv and rcv["REQ"] == 0):
                send_str = json.dumps(cfg_ard)
            elif ("REQ" in rcv and rcv["REQ"] == 1):
                # B1RCV/B2RCV alg
                if (rcv["BUTTON1"]):
                    send["B1RCV"] = True
                else:
                    send["B1RCV"] = False

                if (rcv["BUTTON2"]):
                    send["B2RCV"] = True
                else:
                    send["B2RCV"] = False

                send_str = json.dumps(send)

            send_str += "\n"

            # print what was received and what we are sending (debug)
            # if (rcv != {}):
            #     print(f"received: {rcv}")
            #     print(f"sending: {send_str}\n")

            # send data
            ser.write(send_str.encode("utf-8"))

            # display on OLED
            disp_data_g = display_dict

            prevbstate1 = send["B1RCV"]
            prevbstate2 = send["B2RCV"]

            time.sleep(0.01)

            assert(tracking < 3 and tracking >= 0)

        time.sleep(1)


def main() -> None:
    global cfg_ard, send, curdata, tracking, prevbstate1, prevbstate2, disp_data_g, cur_tz, port

    # debug
    # sys.excepthook = err

    # GPS command
    CMD = "sudo gpsd /dev/serial0 -F /var/run/gpsd.sock"
    subprocess.run(CMD.split())

    # threads
    th1 = threading.Thread(target=get_gps_data, name="gps_thread", daemon=True)
    th1.start()
    th2 = threading.Thread(target=disp_th, name="oled_thread", daemon=True)
    th2.start()

    # serial init
    ser = serial.Serial(port, 115200, timeout=1,
                        stopbits=2, parity=serial.PARITY_NONE)
    ser.flush()

    # main program
    main_ser_connect(ser)

    GPIO.cleanup()


if (__name__ == "__main__"):
    main()
