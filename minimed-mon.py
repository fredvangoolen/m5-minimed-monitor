###############################################################################
#
#  M5Stack Minimed Monitor
#
#  Description:
#
#  This is an application for the M5Stack Fire device. It implements a remote
#  monitor for the Medtronic Minimed 770G/780G insulin pump system to be used
#  by caregivers of Type-1 Diabetes patients wearing the pump.
#
#  Originally written for the M5Stack Core2 (UIFlow1 / m5stack_ui API).
#  Ported to run on the M5Stack Fire under the UIFlow2 MicroPython firmware
#  (M5 / Widgets API). See the "Fire port notes" comment block below for a
#  summary of what changed and why.
#
#  Dependencies:
#
#  At this stage, the M5Stack Minimed Monitor relies on an external instance
#  of the Carelink Python Client to provide the Pump data downloaded from the
#  Carelink Cloud.
#
#  Carelink Python Client
#  https://github.com/ondrej1024/carelink-python-client
#
#  Author:
#
#    Ondrej Wisniewski (ondrej.wisniewski *at* gmail.com)
#
#  Changelog:
#
#    28/06/2021 - Initial public release
#    20/11/2021 - Handle DST (quick'n'dirty)
#    21/11/2021 - Handle alarm notifications
#    03/04/2022 - Add AP mode for configuration
#    01/02/2022 - Improve error handling
#    23/02/2022 - Handle pump banner, shield state, device in range
#    27/03/2022 - Add sensor age icon
#    02/11/2022 - Fix DST handling
#    09/01/2023 - Improve alarm handling
#    12/02/2023 - Add configuration screen
#    12/02/2023 - Fix a regression in AP handling from 0.7 release
#    17/01/2025 - Adapt to new Carelink data format
#    21/01/2025 - Display system status message
#    11/02/2025 - Adapt alarm handling to new data format
#    31/03/2025 - Fix regression bug in DST handling
#    16/08/2026 - Port from M5Stack Core2 (UIFlow1) to M5Stack Fire (UIFlow2)
#
#  Fire port notes:
#
#  This firmware build (uiflow_micropython, https://github.com/m5stack/
#  uiflow_micropython) does not provide the legacy UIFlow1 modules the
#  original script was written against (m5stack, m5stack_ui, uiflow,
#  urequests, ntptime.client(), nvs). It was ported as follows:
#
#  * GUI: M5Screen/M5Img/M5Label/M5Btn/M5Msgbox (UIFlow1)
#         -> M5.Lcd + Widgets.Image/Widgets.Label (UIFlow2's simple,
#            non-LVGL drawing API). Fire's screen is 320x240, same as
#            Core2's, so the original absolute pixel layout is unchanged.
#  * Multi-screen: M5Screen.get_new_screen()/load_screen() (no equivalent)
#         -> Screen/TrackedLabel/TrackedImage helper classes below, which
#            group widgets per screen and redraw them on setVisible(True)
#            since Widgets don't repaint themselves automatically.
#  * Buttons: Core2 has no physical buttons; UIFlow1 simulated btnA/B/C via
#         touch zones -> Fire has *real* physical A/B/C buttons, wired up
#         via BtnA/BtnB/BtnC.setCallback(type=..., cb=...).
#  * Screen dimming: the original dimmed the screen and brightened it on
#         touch (touch.status()). Fire has no touchscreen, so brightening
#         is now triggered by any physical button press instead - closest
#         available proxy for "user is interacting with the device".
#  * Progress ring: lcd.arc(cx,cy,r,thick,a0,a1,fg,bg) -> M5.Lcd.fillArc(
#         cx,cy,r-thick,r,a0,a1,color=fg). Every original call used the
#         same color for fg/bg, so the second color was dropped.
#  * Speaker: speaker.playWAV(file, rate=...) -> M5.Speaker.playWavFile(file)
#  * HTTP: urequests (not present in this firmware) -> requests2, which has
#         the same .request(method=, url=, headers=) / .json() interface.
#         requests2 also accepts a timeout=, so the manual "urequests is
#         stuck" watchdog timer from the original is no longer needed.
#  * NTP: ntptime.client(host=, timezone=) (M5Stack-specific helper that
#         returned an object with .hour()/.minute()) -> standard MicroPython
#         ntptime.settime() (sets the RTC to UTC) plus a small local_now()
#         helper that applies the timezone/DST offset when reading the time.
#  * Config storage: nvs.write_str()/read_str() (not present) -> a plain
#         JSON file at /flash/minimed_config.json.
#  * wait_ms() (UIFlow1 global) -> time.sleep_ms().
#  * Timer scheduler (@timerSch.event('timerN')) -> manual elapsed-time
#         polling in the main loop using time.ticks_ms()/ticks_diff(), with
#         the same "set a flag, handle it once per loop iteration" pattern
#         the original used. M5.update() is called every iteration to pump
#         the physical button callbacks (UIFlow2 requirement).
#  * Fonts: FONT_MONT_20/22/26/28 don't exist as built-ins on this firmware
#         (only 12/14/16/18/24/40/44/48) -> rounded to the nearest available
#         size (20->18, 22->24, 26->24, 28->24).
#
#  Deployment note: copy the res/ folder to /flash/res/ on the Fire's flash
#  filesystem (same as before), and this script to /flash/apps/ (or run it
#  directly) - paths below assume /flash/res/... .
#
#  TODO:
#
#  * Integration of Carelink Client
#  * History graph for recent glucose data
#
#  Copyright 2021-2025, Ondrej Wisniewski
#
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with crelay.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################

import M5
from M5 import *
import ntptime
import time
import requests2 as requests
import json
import os
import network
import socket
import machine

VERSION = "1.2-fire"

# Constants
CONFIG_FILE = "/flash/minimed_config.json"
RES_DIR     = "/flash/res"

# Default configuration parameters
DEFAULT_NTP_SERVER = "pool.ntp.org"
DEFAULT_TIME_ZONE  = "1"
DEFAULT_PROXY_PORT = "8081"

# Access point parameters
API_URL     = "carelink/nohistory"
AP_SSID     = "M5_MINIMED_MON"
AP_ADDR     = "192.168.4.1"

# Gobal variables
dstDelta     = 0
lastUpdateTm = time.localtime(0)
lastAlarmId  = None
lastAlarmMsg = None
lastStatusMsg = None
lastApMsg    = None
runNtpsync        = False
runTimeupdate     = False
runPumpdataupdate = False
ntpSynced         = False

# Fault ID mapping
faultIdMapping = {
   "002": "002",
   "003": "002",
   "004": "002",
   "013": "002",
   "014": "002",
   "015": "002",
   "016": "002",
   "017": "002",
   "018": "002",
   "019": "002",
   "020": "002",
   "022": "002",
   "023": "002",
   "026": "002",
   "027": "002",
   "028": "002",
   "030": "002",
   "031": "002",
   "033": "002",
   "034": "002",
   "044": "002",
   "045": "002",
   "046": "002",
   "049": "002",
   "053": "002",
   "054": "002",
   "060": "002",
   "063": "002",
   "064": "002",
   "065": "002",
   "067": "002",
   "068": "002",
   "074": "002",
   "075": "002",
   "076": "002",
   "079": "002",
   "080": "002",
   "081": "002",
   "082": "002",
   "117": "117",
   "817": "817",
   "805": "805",
   "819": "819",
   "820": "819",
   "012": "012",
   "807": "807",
   "808": "807",
   "814": "814",
   "103": "103",
   "829": "829",
   "830": "829",
   "831": "829",
   "100": "100",
   "051": "051",
   "775": "775",
   "776": "776",
   "869": "869",
   "832": "832",
   "812": "812",
   "777": "777",
   "778": "777",
   "789": "777",
   "833": "833",
   "024": "024",
   "035": "024",
   "040": "024",
   "047": "024",
   "048": "024",
   "050": "024",
   "055": "024",
   "131": "024",
   "052": "052",
   "007": "007",
   "008": "007",
   "140": "140",
   "801": "801",
   "816": "816",
   "823": "823",
   "824": "823",
   "058": "058",
   "069": "069",
   "780": "780",
   "781": "780",
   "795": "795",
   "815": "815",
   "802": "802",
   "803": "803",
   "822": "822",
   "821": "821",
   "107": "107",
   "066": "066",
   "796": "796",
   "057": "057",
   "006": "006",
   "084": "084",
   "061": "061",
   "037": "037",
   "038": "037",
   "039": "037",
   "041": "037",
   "042": "037",
   "043": "037",
   "025": "025",
   "029": "029",
   "077": "077",
   "779": "779",
   "870": "870",
   "011": "011",
   "073": "011",
   "104": "104",
   "113": "113",
   "105": "105",
   "106": "105",
   "130": "130",
   "797": "797",
   "798": "797",
   "794": "794",
   "109": "109",
   "784": "784",
   "110": "110",
   "810": "810",
   "811": "810",
   "809": "809",
   "062": "062",
   "070": "062",
   "071": "062",
   "072": "062",
   "108": "062",
   "114": "062",
   "786": "062",
   "787": "062",
   "788": "062",
   "799": "062",
   "806": "062",
   "825": "062",
   "828": "062",
   "827": "827",
}

# Fault ID table
faultIdTable = {
   "002": "Pump Error. Delivery Stopped",
   "006": "Pump Battery Out Limit",
   "007": "Delivery Stopped. Check BG",
   "011": "Replace Pump Battery Now",
   "012": "Auto Suspend Limit Reached. Delivery Stopped",
   "024": "Critical Pump Error. Stop Pump Use. Use Other Treatment",
   "025": "Pump Power Error. Record Settings",
   "029": "Pump Restarted. Delivery Stopped",
   "037": "Pump Motor Error. Delivery Stopped",
   "051": "Bolus Stopped",
   "052": "Delivery Limit Exceeded. Check BG",
   "057": "Pump Battery Not Compatible",
   "058": "Insert A New AA Battery",
   "061": "Pump Button Error. Delivery Stopped",
   "062": "New Notification Received From Pump",
   "066": "No Reservoir Detected During Infusion Set Change",
   "069": "Loading Incomplete During Infusion Set Change",
   "073": "Replace Pump Battery Now",
   "077": "Pump Settings Error. Delivery Stopped",
   "084": "Pump Battery Removed. Replace Battery",
   "100": "Bolus Entry Timed Out Before Delivery",
   "103": "BG Check Reminder",
   "104": "Replace Pump Battery Soon",
   "105": "Reservoir Low. Change Reservoir Soon",
   "107": "Missed Meal Bolus Reminder",
   "109": "Set Change Reminder",
   "110": "Silenced Sensor Alert. Check Alarm History",
   "113": "Reservoir Empty. Change Reservoir Now",
   "117": "Active Insulin Cleared",
   "130": "Rewind Required. Delivery Stopped",
   "140": "Delivery Suspended. Connect Infusion Set",
   "775": "Calibrate Now",
   "776": "Calibration Error",
   "777": "Change Sensor",
   "779": "Recharge Transmitter Now",
   "780": "Lost Sensor Signal",
   "784": "SG Rising Rapidly",
   "794": "Sensor Expired. Change Sensor",
   "795": "Lost Sensor Signal. Check Transmitter",
   "796": "No Sensor Signal",
   "797": "Sensor Connected",
   "801": "Do Not Calibrate. Wait Up To 3 Hours",
   "802": "Low Sensor Glucose",
   "803": "Low Sensor Glucose. Check BG",
   "805": "Alert Before Low. Check BG",
   "807": "Basal Delivery Resumed. Check BG",
   "809": "Suspend On Low. Delivery Stopped. Check BG",
   "810": "Suspend Before Low. Delivery Stopped. Check BG",
   "812": "Call Emergency Assistance",
   "814": "Basal Resumed. SG Still Under Low Limit. Check BG",
   "815": "Low Limit Changed. Basal Manually Resumed. Check BG",
   "816": "High Sensor Glucose",
   "817": "Alert Before High. Check BG",
   "819": "Auto Mode Exit. Basal Delivery Started. BG Required",
   "821": "Minimum Delivery Timeout. BG Required",
   "822": "Maximum Delivery Timeout. BG Required",
   "823": "High Sensor Glucose For Over 1 Hour",
   "827": "Urgent Low Sensor Glucose. Check BG",
   "829": "BG Required",
   "832": "Calibration Required",
   "833": "Correction Bolus Recommended",
   "869": "Calibration Reminder",
   "870": "Recharge Transmitter Soon",
}


#################################################
#
# Widgets helpers (Fire port)
#
# Widgets.Image/Widgets.Label draw straight onto the LCD and don't repaint
# themselves. To emulate UIFlow1's multi-screen model (each M5Screen keeps
# its own framebuffer) we group widgets per logical screen and, when a
# screen becomes visible again, replay each widget's last known value so
# it actually gets redrawn onto the freshly cleared LCD.
#
#################################################

class TrackedLabel:
   # Note on the widget behaviour this class works around (confirmed by
   # reading mpy_m5widgets.cpp): Widgets.Label.setVisible(True) always
   # forces a real redraw, but setVisible(False) always does a real erase
   # (fillRect at the widget's *own* stored x/y) - even if it was already
   # hidden. Since screen 1/2/3 share overlapping coordinate space, calling
   # set_hidden(True) repeatedly on an already-hidden widget paints stray
   # black rectangles wherever that widget last happened to sit, which can
   # land on top of whatever screen is currently showing. set_hidden() is
   # therefore made idempotent below: it only touches the hardware on an
   # *actual* visible<->hidden transition. set_pos() is similarly guarded
   # so that periodic data updates (which run regardless of which screen
   # is currently visible) can't make a hidden widget reappear.
   def __init__(self, text, x, y, color, font, screen=None):
      self._font = font
      self._x, self._y = x, y
      self._w = Widgets.Label(text, x, y, 1.0, color, 0x000000, font)
      self._text = text
      self._hidden = False
      if screen is not None:
         screen.append(self)

   def set_text(self, text):
      # Widgets.Label stores a raw C pointer into the *previous* text's
      # Python string buffer (not a tracked mp_obj_t reference - confirmed
      # by reading mpy_m5widgets.cpp), and setText()'s internal erase step
      # measures that old string's width before switching to the new one.
      # self._text must therefore keep referencing the OLD string for the
      # full duration of this call (so MicroPython's GC can't reclaim it
      # out from under the widget mid-erase) - only overwrite it with the
      # new value *after* the call returns.
      if not self._hidden:
         self._w.setText(text)
      self._text = text

   def get_width(self):
      M5.Lcd.setFont(self._font)
      return M5.Lcd.textWidth(self._text)

   def set_pos(self, x, y):
      self._x, self._y = x, y
      if not self._hidden:
         self._w.setCursor(x, y)

   def set_hidden(self, hidden):
      if hidden == self._hidden:
         return  # already in that state - avoid a redundant erase/draw
      self._hidden = hidden
      if hidden:
         self._w.setVisible(False)
      else:
         self._w.setCursor(self._x, self._y)  # apply any pos set while hidden
         self._w.setVisible(True)

   def redraw(self):
      # Widgets draw straight onto the LCD with no persistent z-order -
      # whatever was painted most recently visually sits "on top", full
      # stop. The shield image is large enough to cover several labels
      # (confirmed: it's 190x190px), and images always redraw unconditionally
      # whenever their src is set, so any label that isn't also redrawn
      # after the shield gets buried by it. Call this after image updates
      # to put a label back on top regardless of whether its text changed.
      if not self._hidden:
         self._w.setVisible(True)  # unconditional redraw, no erase

   def delete(self):
      self.set_hidden(True)


class TrackedImage:
   # See the note on TrackedLabel above - the same idempotency and
   # hidden-guard logic applies to Widgets.Image.
   def __init__(self, path, x, y, screen=None):
      self._x, self._y = x, y
      self._w = Widgets.Image(path, x, y)
      self._path = path
      self._hidden = False
      if screen is not None:
         screen.append(self)

   def set_img_src(self, path):
      self._path = path
      if not self._hidden:
         self._w.setImage(path)

   def set_pos(self, x, y):
      self._x, self._y = x, y
      if not self._hidden:
         self._w.setCursor(x, y)

   def set_hidden(self, hidden):
      if hidden == self._hidden:
         return  # already in that state - avoid a redundant erase/draw
      self._hidden = hidden
      if hidden:
         self._w.setVisible(False)
      else:
         self._w.setCursor(self._x, self._y)  # apply any pos set while hidden
         self._w.setImage(self._path)  # setCursor's own redraw may be stale
         self._w.setVisible(True)

   def delete(self):
      self.set_hidden(True)


class Msgbox:
   """Minimal stand-in for UIFlow1's M5Msgbox: a text box that can be
   shown/updated/cleared on demand, used for transient status/alarm text."""

   def __init__(self, x, y, w=300, h=60, bg_c=0x2196F3, text_c=0xFFFFFF,
                font=Widgets.FONTS.Montserrat14):
      self._x, self._y, self._w_px, self._h_px = x, y, w, h
      self._bg_c = bg_c
      self._label = Widgets.Label("", x + 4, y + 4, 1.0, text_c, bg_c, font)
      self._label.setVisible(False)
      self._visible = False
      self._text = ""  # see set_text() below for why this is kept around

   def set_text(self, text):
      # Same hazard as TrackedLabel.set_text() (see its comment): keep the
      # previous text referenced until after setText()'s internal erase
      # step (which measures the *old* text) has run.
      M5.Lcd.fillRect(self._x, self._y, self._w_px, self._h_px, self._bg_c)
      self._label.setText(text)
      self._text = text
      self._label.setVisible(True)
      self._visible = True

   def delete(self):
      if self._visible:
         M5.Lcd.fillRect(self._x, self._y, self._w_px, self._h_px, 0x000000)
         self._label.setVisible(False)
         self._visible = False


#################################################
#
# Configuration storage (Fire port: JSON file instead of NVS)
#
#################################################

def nvs_write_config(cfg: dict):
   try:
      with open(CONFIG_FILE, "w") as f:
         json.dump(cfg, f)
   except OSError as e:
      print("Failed to write config file: %s" % e)


def nvs_read_config():
   try:
      with open(CONFIG_FILE) as f:
         return json.load(f)
   except (OSError, ValueError):
      return {}


def nvs_erase_config():
   try:
      os.remove(CONFIG_FILE)
   except OSError:
      pass


#################################################
#
# NTP time sync (Fire port: standard ntptime + manual tz/DST offset,
# instead of UIFlow1's ntptime.client(host=, timezone=) helper)
#
#################################################

def ntp_sync(ntpserver):
   try:
      ntptime.host = ntpserver
      ntptime.settime()  # sets the RTC to UTC
      return True
   except Exception as e:
      print("NTP sync failed: %s" % e)
      return False


def local_now(timezone, dst_delta):
   # RTC is kept in UTC (see ntp_sync); apply tz+DST offset when reading.
   return time.localtime(time.time() + (int(timezone) + dst_delta) * 3600)


#################################################
#
# WIFI Access Point functions
#
#################################################

def web_page_config(ntpserver,timezone,proxyport):
   html =  '<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd"> \n \
            <html><head><title>M5 Minimed Mon</title></head> \n \
            <body><table style="text-align: left; width: 400px; background-color: #2196F3; font-family: Helvetica,Arial,sans-serif; font-weight: bold; color: white;" border="0" cellpadding="2" cellspacing="2"> \n \
            <tbody><tr><td> \n \
            <span style="vertical-align: top; font-size: 48px;">M5 Minimed Mon</span><br> \n \
            <span style="font-size: 20px; color: rgb(204, 255, 255);">Configuration</span> \n \
            </td></tr></tbody></table><br> \n \
            <form action="/m5config"> \n \
            <table style="text-align: left; width: 400px; background-color: white; font-family: Helvetica,Arial,sans-serif; font-weight: bold; font-size: 14px;" border="0" cellpadding="2" cellspacing="3"><tbody> \n \
            <tr style="font-size: 18px; background-color: lightgrey"> \n \
            <td style="width: 200px;">Wifi parameters</td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">SSID<br><input type="text" id="fwifissid" name="fwifissid"></td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">Password<br><input type="text" id="fwifipass" name="fwifipass"></td> \n \
            </tbody></table><br> \n \
            <table style="text-align: left; width: 400px; background-color: white; font-family: Helvetica,Arial,sans-serif; font-weight: bold; font-size: 14px;" border="0" cellpadding="2" cellspacing="3"><tbody> \n \
            <tr style="font-size: 18px; background-color: lightgrey"> \n \
            <td style="width: 200px;">Time and date</td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">NTP server address<br><input type="text" id="fntpserver" name="fntpserver" value=%s></td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">Time Zone (h)<br><input type="text" id="ftimezone" name="ftimezone" value=%s></td> \n \
            </tbody></table><br> \n \
            <table style="text-align: left; width: 400px; background-color: white; font-family: Helvetica,Arial,sans-serif; font-weight: bold; font-size: 14px;" border="0" cellpadding="2" cellspacing="3"><tbody> \n \
            <tr style="font-size: 18px; background-color: lightgrey"> \n \
            <td style="width: 200px;">Carelink proxy</td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">IP address<br><input type="text" id="fproxyaddr" name="fproxyaddr"></td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">Port<br><input type="text" id="fproxyport" name="fproxyport" value=%s></td> \n \
            </tbody></table><br> \n \
            <input type="submit" value="Save"> \n \
            </form></body></html>' % (ntpserver,timezone,proxyport)
   return html


def web_page_success():
   html =  '<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd"> \n \
            <html><head><title>M5 Minimed Mon</title></head> \n \
            <body><table style="text-align: left; width: 400px; background-color: #2196F3; font-family: Helvetica,Arial,sans-serif; font-weight: bold; color: white;" border="0" cellpadding="2" cellspacing="2"> \n \
            <tbody><tr><td> \n \
            <span style="vertical-align: top; font-size: 48px;">M5 Minimed Mon</span><br> \n \
            <span style="font-size: 20px; color: rgb(204, 255, 255);">Configuration</span> \n \
            </td></tr></tbody></table><br> \n \
            <table style="text-align: left; width: 400px; background-color: rgb(230, 230, 255); font-family: Helvetica,Arial,sans-serif; font-weight: bold; font-size: 14px;" border="0" cellpadding="2" cellspacing="3"><tbody> \n \
            <tr><td style="color: green; font-size: 18px;">Parameters updated successfully</td> \n \
            <tr><td style="color: grey">Restarting device with new configuration ...</td> \n \
            </tbody></table></body></html>'
   return html


def get_url_param(url,param):
   try:
      value = url.split("?")[1].split(param+"=")[1].split("&")[0]
   except IndexError:
      value = None
   return value


def do_ap_msg(msg):
   global lastApMsg
   if lastApMsg != None:
      lastApMsg.delete()
      lastApMsg = None
   if msg:
      lastApMsg = Msgbox(x=0, y=100, w=320, h=80)
      lastApMsg.set_text(msg)
      sndfile = RES_DIR + "/sound_alert.wav"
      M5.Speaker.playWavFile(sndfile)


def do_access_point(ntpserver,timezone,proxyport):
   # Start access point
   ap = network.WLAN(network.AP_IF)
   ap.active(True)
   ap.config(essid=AP_SSID)
   ap.config(authmode=3, password='123456789')
   ap.config(max_clients=1)
   do_ap_msg("Device configuration needed\nConnect to WIFI network\n%s" %(AP_SSID))

   # Wait for client to connect
   while ap.isconnected() == False:
       pass
   do_ap_msg("WIFI connection established\nLoad address %s in web browser" % (AP_ADDR))

   # Get WIFI credentials via Web GUI
   s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
   s.bind((AP_ADDR, 80))
   s.listen(5)

   while True:
      # Get request
      conn,addr = s.accept()
      request = str(conn.recv(1024))
      rmethod  = request.split()[0]
      rurl     = request.split()[1]
      rheaders = request.split()[2]
      print("request: %s\n" % (request))
      #print("rmethod: %s\n" % (rmethod))
      print("rurl: %s\n" % (rurl))

      # Send response headers
      conn.send('HTTP/1.1 200 OK\n')
      conn.send('Content-Type: text/html\n')
      conn.send('Connection: close\n\n')

      if rurl.find("/m5config") != -1:
         # Get input parameters from request
         wifissid  = get_url_param(rurl, "fwifissid")
         wifipass  = get_url_param(rurl, "fwifipass")
         ntpserver = get_url_param(rurl, "fntpserver")
         timezone  = get_url_param(rurl, "ftimezone")
         proxyaddr = get_url_param(rurl, "fproxyaddr")
         proxyport = get_url_param(rurl, "fproxyport")
         if wifissid  != None and wifissid  != "" and \
            wifipass  != None and wifipass  != "" and \
            ntpserver != None and ntpserver != "" and \
            timezone  != None and timezone  != "" and \
            proxyaddr != None and proxyaddr != "" and \
            proxyport != None and proxyport != "":

            print("New configuration parameters received\n")
            # Send reboot page
            conn.sendall(web_page_success())
            conn.close()
            break

      # Send setup page
      conn.sendall(web_page_config(ntpserver,timezone,proxyport))
      conn.close()

   # Write WIFI credentials to config file
   nvs_write_config({
      "wifissid":  wifissid,
      "wifipass":  wifipass,
      "ntpserver": ntpserver,
      "timezone":  timezone,
      "proxyaddr": proxyaddr,
      "proxyport": proxyport,
   })
   print("New configuration parameters stored in config file\n")
   print("wifissid: %s, wifipass: %s, proxyaddr: %s, proxyport: %s, ntpserver: %s, timezone: %s\n" % (wifissid,wifipass,proxyaddr,proxyport,ntpserver,timezone))
   do_ap_msg("New configuration parameters stored\nResetting device ...")

   # Reset device
   time.sleep_ms(8000)
   machine.reset()


#################################################
#
# Access configuration parameters
#
#################################################

def read_config():
   # Try to read config from the config file
   cfg = nvs_read_config()
   wifissid  = cfg.get('wifissid')
   wifipass  = cfg.get('wifipass')
   ntpserver = cfg.get('ntpserver')
   timezone  = cfg.get('timezone')
   proxyaddr = cfg.get('proxyaddr')
   proxyport = cfg.get('proxyport')

   # Set default values
   if proxyport == None:
      proxyport = DEFAULT_PROXY_PORT
   if ntpserver == None:
      ntpserver = DEFAULT_NTP_SERVER
   if timezone == None:
      timezone  = DEFAULT_TIME_ZONE

   # To delete the config use nvs_erase_config()

   if wifissid == None or wifipass == None or proxyaddr == None:
      print("Needed configuration parameters not found\n")
      # Start access point for configuration
      do_access_point(ntpserver,timezone,proxyport)

   return (wifissid,wifipass,proxyaddr,proxyport,ntpserver,timezone)


#################################################
#
# Connect to network
#
#################################################

def wlan_connect(wifissid, wifipass, ntpserver, timezone, proxyport):
   # Try to connect to WIFI network
   wlan = network.WLAN(network.STA_IF)
   wlan.active(True)
   wlan.connect(wifissid, wifipass)
   ctimeout=0
   while not wlan.isconnected():
      time.sleep_ms(1000)
      ctimeout += 1
      if ctimeout > 5:
         break
   if not wlan.isconnected():
      wlan.active(False)
      print("Failed to connect to WIFI network %s\n" % (wifissid))
      # Start access point for configuration
      do_access_point(ntpserver,timezone,proxyport)


# Init M5 / screen
M5.begin()
M5.Lcd.fillScreen(0x000000)
M5.Lcd.setTextColor(0xFFFFFF)
M5.Lcd.setFont(Widgets.FONTS.Montserrat24)
M5.Lcd.drawString("Minimed Mon, ver %s" % (VERSION), 0, 0)
print("Minimed Mon, ver %s" % (VERSION))
M5.Lcd.setBrightness(40)
time.sleep_ms(3000)
M5.Lcd.fillScreen(0x000000)

# Read config from file
wifissid,wifipass,proxyaddr,proxyport,ntpserver,timezone = read_config()
print("wifissid: %s, wifipass: %s, proxyaddr: %s, proxyport: %s, ntpserver: %s, timezone: %s\n" % (wifissid,wifipass,proxyaddr,proxyport,ntpserver,timezone))

# Connect to network
wlan_connect(wifissid, wifipass, ntpserver, timezone, proxyport)

# Screen groups: widgets are appended to these lists as they are created,
# and shown/hidden together by show_screen() below.
scr1 = []
scr2 = []
scr3 = []
# None (not 1) until the first real show_screen() call: at this point no
# widgets exist yet, and every widget draws itself immediately when
# constructed regardless of "which screen" - so screen1/2/3's widgets all
# get drawn on top of each other during construction below. The first
# show_screen(1) call (see Init) is what cleans that up into an actual
# single-screen state. If this started as 1, show_screen(1)'s "already on
# this screen, do nothing" fast path (see show_screen()) would wrongly
# skip that cleanup, leaving screens 1+2+3 all overlaid at boot.
current_screen = None

# Load images on screen 1
imageBattery     = TrackedImage(RES_DIR+"/mm_batt_unk.png", 6, 0, scr1)
imageReservoir   = TrackedImage(RES_DIR+"/mm_tank_unk.png", 40, 0, scr1)
imageSensorConn  = TrackedImage(RES_DIR+"/mm_sensor_connection_nok.png", 68, 0, scr1)
imageDrop        = TrackedImage(RES_DIR+"/mm_drop_unk.png", 105, 8, scr1)
imageSage        = TrackedImage(RES_DIR+"/mm_sage_unk.png", 135, 0, scr1)
imageShield      = TrackedImage(RES_DIR+"/mm_shield_none.png", 65, 33, scr1)
imageBanner      = TrackedImage(RES_DIR+"/mm_banner_delivery_suspend.png", 40, 145, scr1)

# Init labels on screen 1
labelBglValue    = TrackedLabel('--', 140, 90, 0xffffff, Widgets.FONTS.Montserrat48, scr1)
labelBglUnit     = TrackedLabel('mg/dL', 137, 145, 0x89abeb, Widgets.FONTS.Montserrat16, scr1)
labelActInsValue = TrackedLabel('-- U', 261, 173, 0xffffff, Widgets.FONTS.Montserrat24, scr1)
# Montserrat24's fontHeight is 27px (measured on-device), so labelActInsValue
# (y=173) spans exactly to y=200 - any repositioning erase (triggered when
# its text width changes) would land right on top of labelActIns at y=200
# with zero margin, permanently wiping this static caption. Moved down for
# clearance.
labelActIns      = TrackedLabel('Act Insulin', 231, 204, 0xffffff, Widgets.FONTS.Montserrat16, scr1)
labelTime        = TrackedLabel('--:--', 250, 0, 0xffffff, Widgets.FONTS.Montserrat24, scr1)
labelLastData    = TrackedLabel('--', 120, 218, 0xffffff, Widgets.FONTS.Montserrat18, scr1)
labelSage        = TrackedLabel('', 144, 8, 0xffffff, Widgets.FONTS.Montserrat14, scr1)

# Init labels on screen 2
labelScreen2Title     = TrackedLabel('In target range (last 24h)', 9, 0, 0xffffff, Widgets.FONTS.Montserrat24, scr2)
labelAboveTarget      = TrackedLabel('Above target 180 mg/dl:', 9, 60, 0xffc418, Widgets.FONTS.Montserrat18, scr2)
labelInTarget         = TrackedLabel('In target:', 9, 90, 0x45db49, Widgets.FONTS.Montserrat18, scr2)
labelBelowTarget      = TrackedLabel('Below target 70 mg/dl:', 9, 119, 0xff0000, Widgets.FONTS.Montserrat18, scr2)
labelAgerageSg        = TrackedLabel('Average SG:', 9, 147, 0xa0a0a0, Widgets.FONTS.Montserrat18, scr2)
labelAboveTargetValue = TrackedLabel('-- %', 252, 60, 0xffffff, Widgets.FONTS.Montserrat18, scr2)
labelInTargetValue    = TrackedLabel('-- %', 252, 90, 0xffffff, Widgets.FONTS.Montserrat18, scr2)
labelBelowTargetValue = TrackedLabel('-- %', 252, 119, 0xffffff, Widgets.FONTS.Montserrat18, scr2)
labelAverageSgValue   = TrackedLabel('-- mg/dl', 231, 147, 0xffffff, Widgets.FONTS.Montserrat18, scr2)

# Init labels on screen 3

# Wifi settings
labelWifi        = TrackedLabel('WIFI', 31, 0, 0x09f31a, Widgets.FONTS.Montserrat18, scr3)
labelSsid        = TrackedLabel('SSID', 52, 25, 0xffffff, Widgets.FONTS.Montserrat14, scr3)
labelMySsid      = TrackedLabel(wifissid, 143, 25, 0xffffff, Widgets.FONTS.Montserrat14, scr3)

# Time and date settings
labelTimeAndDate = TrackedLabel('Time and Date', 31, 47, 0x09f31a, Widgets.FONTS.Montserrat18, scr3)
labelNtpServer   = TrackedLabel('NTP server', 52, 75, 0xffffff, Widgets.FONTS.Montserrat14, scr3)
labelMyNtpServer = TrackedLabel(ntpserver, 143, 75, 0xffffff, Widgets.FONTS.Montserrat14, scr3)
labelTimeZone    = TrackedLabel('Time zone', 52, 97, 0xffffff, Widgets.FONTS.Montserrat14, scr3)
labelMyTimeZone  = TrackedLabel(timezone, 143, 97, 0xffffff, Widgets.FONTS.Montserrat14, scr3)

# Carelink proxy settings
labelCarelinkProxy = TrackedLabel('Carelink Proxy', 31, 124, 0x09f31a, Widgets.FONTS.Montserrat18, scr3)
labelIpAddress   = TrackedLabel('IP address', 52, 151, 0xffffff, Widgets.FONTS.Montserrat14, scr3)
labelMyIpAddress = TrackedLabel(proxyaddr, 143, 151, 0xffffff, Widgets.FONTS.Montserrat14, scr3)
labelPort        = TrackedLabel('Port', 52, 173, 0xffffff, Widgets.FONTS.Montserrat14, scr3)
labelMyPort      = TrackedLabel(proxyport, 143, 173, 0xffffff, Widgets.FONTS.Montserrat14, scr3)

# M5Stack Fire device info
labelDevice      = TrackedLabel('M5STACK FIRE', 31, 193, 0x09f31a, Widgets.FONTS.Montserrat18, scr3)
labelBattery     = TrackedLabel('Battery', 52, 217, 0xffffff, Widgets.FONTS.Montserrat14, scr3)
labelMyBattery   = TrackedLabel('--', 143, 217, 0xffffff, Widgets.FONTS.Montserrat14, scr3)

# Note: holding BtnC (the same button used to navigate to this screen)
# resets the config - see btnC_wasHold() below. No on-screen hint for this
# (a rotated-text attempt at one broke display orientation firmware-wide -
# M5.Lcd.setRotation(0) is not actually this board's normal/boot rotation,
# so "restoring" to it left every screen rotated - reverted).


def reset_config():
   # delete stored parameters
   nvs_erase_config()
   # Restart
   machine.reset()


#################################################
#
# Screen switching
#
#################################################

def show_screen(n):
   global current_screen
   if n == current_screen:
      # Already showing this screen. fillScreen() below is unconditional,
      # but the "show" pass for a screen's own widgets is a no-op when
      # they're already marked visible (set_hidden() only acts on an
      # actual hidden<->visible transition - see TrackedLabel/TrackedImage).
      # Re-running this for the screen we're already on would therefore
      # wipe the display and then redraw nothing, leaving it blank until a
      # genuinely different screen is selected. Pressing the button for the
      # screen you're already on should just do nothing.
      return
   current_screen = n
   M5.Lcd.fillScreen(0x000000)
   screens = {1: scr1, 2: scr2, 3: scr3}
   # Hide every other screen's widgets *before* showing the target screen's
   # widgets. set_hidden() is idempotent (see TrackedLabel/TrackedImage) so
   # already-hidden screens are cheap no-ops here; the screen we're actually
   # leaving gets a real hide/erase, which must land before the target
   # screen draws or it stomps the freshly-drawn content.
   for i, widgets in screens.items():
      if i != n:
         for widget in widgets:
            widget.set_hidden(True)
   for widget in screens[n]:
      widget.set_hidden(False)


#################################################
#
# Button handlers
#
#################################################

def bump_brightness():
   # No touchscreen on Fire to detect user interaction, so any button
   # press is used instead to briefly brighten the screen (see timer4).
   M5.Lcd.setBrightness(100)
   global runBrightnessReset, brightnessResetAt
   brightnessResetAt = time.ticks_add(time.ticks_ms(), TIMER4_PERIOD_S*1000)
   runBrightnessReset = True


def btnA_wasClicked(state):
   global lastAlarmMsg
   if lastAlarmMsg != None:
      lastAlarmMsg.delete()
      lastAlarmMsg = None
   show_screen(1)
   bump_brightness()
BtnA.setCallback(type=BtnA.CB_TYPE.WAS_CLICKED, cb=btnA_wasClicked)


def btnB_wasClicked(state):
   show_screen(2)
   bump_brightness()
BtnB.setCallback(type=BtnB.CB_TYPE.WAS_CLICKED, cb=btnB_wasClicked)


def btnC_wasClicked(state):
   show_screen(3)
   bump_brightness()
BtnC.setCallback(type=BtnC.CB_TYPE.WAS_CLICKED, cb=btnC_wasClicked)


def btnC_wasHold(state):
   if current_screen == 3:
      reset_config()
BtnC.setCallback(type=BtnC.CB_TYPE.WAS_HOLD, cb=btnC_wasHold)


#################################################
#
# Helper functions
#
#################################################

def align_text(label,pos,y):
    w = label.get_width()
    if pos=="left":
        label.set_pos(x=0,y=y)
    elif pos=="center":
        label.set_pos(x=160-int(w/2),y=y)
    elif pos=="right":
        label.set_pos(x=320-w,y=y)


def time_delta(tm,now,timezone):
   if tm != None and now != None:
      delta_min  = now[4] - tm[4]
      if delta_min < 0:
         delta_min += 60
      #print("delta_min: "+str(delta_min))
      delta_hour = now[3] - tm[3]
      if delta_hour < 0:
         delta_hour += 24
      #print("delta_hour: "+str(delta_hour))

      if delta_min == 0 and delta_hour == 0:
         delta_txt = "Now"
      elif delta_min > 15 or delta_hour > 1:
         delta_txt = "No data"
      else:
         delta_txt = str(delta_min)+" min ago"
   else:
      delta_txt = "---"
   return delta_txt


def reservoir_level(lvl):
   if lvl > 150:
      img_lvl = 200  # green
   elif lvl > 80:
      img_lvl = 150  # yellow
   elif lvl > 1:
      img_lvl = 50   # red
   else:
      img_lvl = 0    # empty
   return img_lvl


def time_to_calib_progress(cfs,ttc,sst,cst):
   # TODO: check if screen 1 is active
   centerX = 112
   centerY = 17
   radius  = 16
   thick   = 4
   endposfull = 359
   endpos = int(360*((12-ttc)/12))
   endpos = min(endpos,endposfull)
   endpos = max(endpos,0)
   if (ttc == 255 or cst == "UNKNOWN") and not cfs: # unknown
      #print("unknown")
      # full blue circle, question mark
      imageDrop.set_img_src(RES_DIR+"/mm_drop_unk.png")
      imageDrop.set_pos(106, 8)
      M5.Lcd.fillArc(centerX, centerY, radius-thick, radius, 0, endposfull, color=0x00cccc)
   elif ttc >= 12:
      # full green circle, white drop
      imageDrop.set_img_src(RES_DIR+"/mm_drop_white.png")
      imageDrop.set_pos(105, 8)
      M5.Lcd.fillArc(centerX, centerY, radius-thick, radius, 0, endposfull, color=0x33cc00)
   elif ttc > 3:
      # decreasing green circle, white drop
      imageDrop.set_img_src(RES_DIR+"/mm_drop_white.png")
      imageDrop.set_pos(105, 8)
      M5.Lcd.fillArc(centerX, centerY, radius-thick, radius, 0, endposfull, color=0x33cc00)
      M5.Lcd.fillArc(centerX, centerY, radius-thick, radius, 0, endpos, color=0x000000)
   elif ttc > 0:
      # decreasing red circle, white drop
      imageDrop.set_img_src(RES_DIR+"/mm_drop_white.png")
      imageDrop.set_pos(105, 8)
      M5.Lcd.fillArc(centerX, centerY, radius-thick, radius, 0, endposfull, color=0xff0000)
      M5.Lcd.fillArc(centerX, centerY, radius-thick, radius, 0, endpos, color=0x000000)
   else:
      if sst == "CALIBRATION_REQUIRED":
         # no circle, red drop
         imageDrop.set_img_src(RES_DIR+"/mm_drop_red.png")
         imageDrop.set_pos(100, 0)
      else:
         # no circle, white drop
         imageDrop.set_img_src(RES_DIR+"/mm_drop_white.png")
         imageDrop.set_pos(105, 8)
      M5.Lcd.fillArc(centerX, centerY, radius-thick, radius, 0, endposfull, color=0x000000)


def sensor_age_text(rem_hours):
   if rem_hours == 255:
      text = ""
   elif rem_hours > 9:
      text = str(round(rem_hours/24))
   else:
      text = str(rem_hours)
   return text


def sensor_age_icon(rem_hours, sensor_state):
   if sensor_state == "CHANGE_SENSOR":
      icon = "expired"
   elif rem_hours == 255:
      icon = "unk"
   elif rem_hours > 9:
      icon = "green"
   else:
      icon = "red"
   return icon


def convert_datetimestr_to_epoch(datetimestr):
   # datetime string format is the following:
   # yyyy-mm-ddThh:mm:ss.000-00:00
   try:
      d  = datetimestr.split('.')[0].split('T')[0]
      t  = datetimestr.split('.')[0].split('T')[1]
      year = int(d.split('-')[0])
      mon  = int(d.split('-')[1])
      day  = int(d.split('-')[2])
      hour = int(t.split(':')[0])
      min  = int(t.split(':')[1])
      sec  = int(t.split(':')[2])
      #print("%d-%d-%d %d:%d:%d"%(year,mon,day,hour,min,sec))
      return time.mktime((year,mon,day,hour,min,sec,0,0))
   except:
      return 0


def getFaultStr(faultId):
   try:
      faultStr = faultIdTable[faultIdMapping[faultId]]
   except KeyError:
      faultStr = "Unknow error code %s" % faultId
   print("faultStr = %s" % faultStr)
   return faultStr


def handle_alarm(lastAlarm):
   TDELTA_S = 15*60 # 15 min in seconds
   global lastAlarmId
   global lastAlarmMsg

   # The popup (like the original Core2 script's M5Msgbox, which was
   # parented to scr1 there too) only ever touches the display while
   # screen 1 is current. Deleting/creating it while a different screen is
   # showing would paint stray pixels over whatever the user is actually
   # looking at (Msgbox isn't screen-tracked like TrackedLabel/TrackedImage
   # are). The sound and lastAlarmId bookkeeping below are NOT gated - this
   # is a safety-relevant medical alert, it should play regardless of which
   # screen happens to be on display.
   if current_screen == 1 and lastAlarmMsg != None:
      lastAlarmMsg.delete()
      lastAlarmMsg = None

   try:
      # Check for new alarm
      if lastAlarmId != lastAlarm["GUID"]:
         # Check if alarm is recent
         if convert_datetimestr_to_epoch(lastAlarm["dateTime"]) > (time.time() - TDELTA_S):
            # Play alarm sound
            if lastAlarm["type"] == "ALARM":
               sndfile = RES_DIR + "/sound_alarm.wav"
            else:
               sndfile = RES_DIR + "/sound_alert.wav"
            M5.Speaker.playWavFile(sndfile)

            # Show alarm message (screen 1 only, see above)
            if current_screen == 1:
               msg = getFaultStr(lastAlarm["faultId"])
               if lastAlarmMsg != None:
                  lastAlarmMsg.delete()
               lastAlarmMsg = Msgbox(x=0, y=100, w=320, h=80)
               lastAlarmMsg.set_text(msg)
         lastAlarmId = lastAlarm["GUID"]
   except:
      pass


#################################################
#
# Periodic task handlers
#
# (Fire port: the original UIFlow1 @timerSch.event('timerN') hardware
# timers are replaced by plain elapsed-time checks in the main loop below,
# using the same "set a flag, handle it once per loop pass" pattern.)
#
#################################################

def handle_ntpsync(ntpserver):
   global ntpSynced
   if ntp_sync(ntpserver):
      ntpSynced = True


def handle_timeupdate(timezone):
   if not ntpSynced:
      return
   try:
      now = local_now(timezone, dstDelta)
      timestr = ("%02d:%02d") % (now[3],now[4])
      labelTime.set_text(timestr)
      align_text(labelTime,"right",0)
      labelLastData.set_text(time_delta(lastUpdateTm,now,timezone))
      align_text(labelLastData,"center",218)
   except:
      pass

   try:
      labelMyBattery.set_text(str(M5.Power.getBatteryLevel())+" %")
   except:
      pass


def handle_pumpdataupdate(proxyaddr, proxyport):
   global lastStatusMsg
   global lastUpdateTm
   global dstDelta
   proxy_url = "http://%s:%s/%s" % (proxyaddr, proxyport, API_URL)

   # Update Minimed data

   # Get Minimed data from proxy via API
   try:
      r = requests.request(method='GET', url=proxy_url, headers={}, timeout=30)
   except (OSError, Exception):
      r = None

   # r.json() re-parses the *entire* cached response body from scratch on
   # every single call (only the raw bytes are cached, not the parsed
   # result) - calling it a dozen+ times per update, as the original code
   # did, allocates a dozen+ throwaway dicts every 60s. That garbage churn
   # is almost certainly what was triggering the GC-timing race described
   # in TrackedLabel.set_text() far more often than expected. Parse once
   # and reuse the result throughout.
   data = None
   if r != None and r.status_code == 200:
      try:
         data = r.json()
      except ValueError:
         data = None
      if data == "":
         data = None

   if data != None:
      # Timestamp/DST bookkeeping and alarm handling run unconditionally,
      # regardless of which screen is currently visible - alarm sound
      # (safety-relevant) and lastUpdateTm/lastAlarmId bookkeeping should
      # not depend on which screen happens to be on display right now.
      try:
         # Check for DST first: lastUpdateTm below must use the same tz+DST
         # offset as local_now() (see time_delta()), or the two will always
         # be ~1 hour apart and time_delta() will show "No data" forever.
         dstDelta = 1 if data["clientTimeZoneName"].lower().find("summer")>-1 else 0

         lastUpdateTm = time.localtime(int(data["lastConduitUpdateServerDateTime"]/1000) + (int(timezone)+dstDelta)*3600) #-NTPCONST)

         # Check for alarm notification (sound plays regardless of which
         # screen is showing - see handle_alarm() for why)
         handle_alarm(data["lastAlarm"])
      except:
         pass

      # Check conduit, medical device in range
      try:
         haveData = data["conduitInRange"] and data["conduitMedicalDeviceInRange"]
      except:
         haveData = False

      ##### Screen 1 #####
      # Gated to only run while screen 1 is actually visible. Widget
      # content updates (set_text/set_img_src) are already safe to call
      # while hidden (they check self._hidden internally and skip the
      # draw), but set_hidden() itself is a REAL visibility transition from
      # the widget's point of view - it doesn't know "hidden" currently
      # means "wrong screen" rather than "genuinely should not show".
      # Calling imageShield.set_hidden(False)/imageBanner.set_hidden(False)
      # while on screen 2/3 was drawing them right on top of whatever
      # screen was actually showing (screen 1 icons overlay bug). Worse, it
      # also corrupts TrackedImage's own _hidden bookkeeping: if pump data
      # flips it to "visible" while screen 1 is not current, then later
      # switching TO screen 1 sees no hidden->visible transition to act on
      # (it already thinks it's visible) and skips the redraw entirely,
      # even though fillScreen() just wiped it - leaving screen 1 blank for
      # that widget. Only touching these widgets while screen 1 is actually
      # current avoids both failure modes.
      if current_screen == 1:
         try:
            if haveData:
               imageBattery.set_img_src(RES_DIR+"/mm_batt"+str(data["pumpBatteryLevelPercent"])+".png")
               imageReservoir.set_img_src(RES_DIR+"/mm_tank"+str(reservoir_level(data["reservoirRemainingUnits"]))+".png")
               imageSage.set_img_src(RES_DIR+"/mm_sage_"+sensor_age_icon(data["sensorDurationHours"],data["sensorState"])+".png")
               labelSage.set_text(sensor_age_text(data["sensorDurationHours"]))
            else:
               imageBattery.set_img_src(RES_DIR+"/mm_batt_unk.png")
               imageReservoir.set_img_src(RES_DIR+"/mm_tank_unk.png")
               imageSage.set_img_src(RES_DIR+"/mm_sage_unk.png")
               labelSage.set_text("")

            if data["conduitSensorInRange"]:
               imageSensorConn.set_img_src(RES_DIR+"/mm_sensor_connection_ok.png")
            else:
               imageSensorConn.set_img_src(RES_DIR+"/mm_sensor_connection_nok.png")

            time_to_calib_progress(data["calFreeSensor"],data["timeToNextCalibHours"],data["sensorState"],data["calibStatus"])

            if not haveData or data["therapyAlgorithmState"]["autoModeShieldState"] == "FEATURE_OFF":
               imageShield.set_hidden(True)
            else:
               imageShield.set_img_src(RES_DIR+"/mm_shield_"+data["lastSGTrend"].lower()+".png")
               imageShield.set_hidden(False)
            lastSG = data["lastSG"]["sg"]
            labelBglValue.set_text(str(lastSG) if lastSG > 0 else "--")
            align_text(labelBglValue,"center",90)

            if haveData:
               labelActInsValue.set_text(str(round(data["activeInsulin"]["amount"],1))+" U")
            else:
               labelActInsValue.set_text("-- U")
            align_text(labelActInsValue,"right",173)

            # All screen-1 images for this cycle are now up to date. Since
            # images always redraw unconditionally and can visually bury
            # labels beneath them (no persistent z-order - see
            # TrackedLabel.redraw()), force every screen-1 label back on
            # top, not just the ones whose text happened to change.
            labelBglValue.redraw()
            labelBglUnit.redraw()
            labelActInsValue.redraw()
            labelActIns.redraw()
            labelTime.redraw()
            labelLastData.redraw()
            labelSage.redraw()
         except:
            pass

         try:
            systemStatus = data["systemStatusMessage"]
            if systemStatus == "NO_ERROR_MESSAGE" or systemStatus == None:
               raise Exception
            else:
               if lastStatusMsg == None:
                  lastStatusMsg = Msgbox(x=0, y=50, w=320, h=40)
               lastStatusMsg.set_text(systemStatus.replace("_"," "))
         except:
            if lastStatusMsg != None:
               lastStatusMsg.delete()
               lastStatusMsg = None

         try:
            pumpBanner = data["pumpBannerState"][0]["type"]
            imageBanner.set_img_src(RES_DIR+"/mm_banner_"+pumpBanner.lower()+".png")
            imageBanner.set_hidden(False)
         except:
            imageBanner.set_hidden(True)

      ##### Screen 2 #####
      try:
         labelAboveTargetValue.set_text(str(data["aboveHyperLimit"])+" %")
         labelInTargetValue.set_text(str(data["timeInRange"])+" %")
         labelBelowTargetValue.set_text(str(data["belowHypoLimit"])+" %")
         labelAverageSgValue.set_text(str(data["averageSG"])+" mg/dl")
      except:
         pass


def handle_brightness_reset():
   M5.Lcd.setBrightness(40)


#################################################
#
# Init
#
#################################################

# Every widget above draws itself immediately as it's constructed (there's
# no "inactive" state at construction time), so right now screens 1, 2 and
# 3 are all overlaid on top of each other. show_screen(1) must run before
# anything else - including the NTP wait below, which can take several
# seconds - or that overlay stays visible for the whole wait.
show_screen(1)

msgbox = None
msgbox = Msgbox(x=0, y=0, w=320, h=40)
msgbox.set_text("Trying to synch time and date ...")
while not ntpSynced:
   time.sleep_ms(1000)
   handle_ntpsync(ntpserver)
msgbox.delete()

print("Time and date successfully synched")

# Task periods (seconds)
TIMER0_PERIOD_S = 1200  # ntpsync
TIMER1_PERIOD_S = 10    # timeupdate
TIMER2_PERIOD_S = 60    # pumpdataupdate
TIMER4_PERIOD_S = 10    # brightness reset delay after a button press

runBrightnessReset = False
brightnessResetAt  = time.ticks_ms()

nextNtpsync  = time.ticks_add(time.ticks_ms(), TIMER0_PERIOD_S*1000)
nextTimeupdate = time.ticks_ms()
nextPumpdataupdate = time.ticks_ms()


#################################################
#
# Main loop
#
#################################################
while True:
   M5.update()  # pumps BtnA/B/C callbacks

   now_ms = time.ticks_ms()

   if time.ticks_diff(now_ms, nextPumpdataupdate) >= 0:
      handle_pumpdataupdate(proxyaddr, proxyport)
      nextPumpdataupdate = time.ticks_add(now_ms, TIMER2_PERIOD_S*1000)

   if time.ticks_diff(now_ms, nextNtpsync) >= 0:
      handle_ntpsync(ntpserver)
      nextNtpsync = time.ticks_add(now_ms, TIMER0_PERIOD_S*1000)

   if time.ticks_diff(now_ms, nextTimeupdate) >= 0:
      handle_timeupdate(timezone)
      nextTimeupdate = time.ticks_add(now_ms, TIMER1_PERIOD_S*1000)

   if runBrightnessReset and time.ticks_diff(now_ms, brightnessResetAt) >= 0:
      handle_brightness_reset()
      runBrightnessReset = False

   time.sleep_ms(50)
