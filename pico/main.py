# Pi Pico W: leest barcodes van een UART-scanner en stuurt ze via HTTP naar de barcoder-server.
import network
import socket
import time
from machine import UART, Pin

import config

led = Pin("LED", Pin.OUT)
uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1))

# Sommige scanners sluiten af met CR/LF, andere niet: na deze stilte is de scan compleet
SCAN_IDLE_MS = 80


def blink(times, ms=80):
    for _ in range(times):
        led.on()
        time.sleep_ms(ms)
        led.off()
        time.sleep_ms(ms)


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    # Power save uit: anders kan de eerste request na een tijdje stilte merkbaar vertragen
    wlan.config(pm=getattr(network.WLAN, "PM_NONE", 0xA11140))
    if not wlan.isconnected():
        print("Verbinden met WiFi...")
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        while not wlan.isconnected():
            led.toggle()
            time.sleep_ms(250)
    led.off()
    print("WiFi verbonden:", wlan.ifconfig())
    return wlan


def send(code):
    addr = socket.getaddrinfo(config.SERVER_HOST, config.SERVER_PORT)[0][-1]
    body = code.encode()
    header = (
        "POST /scan HTTP/1.0\r\n"
        "Host: {}\r\n"
        "Content-Type: text/plain\r\n"
        "Content-Length: {}\r\n\r\n"
    ).format(config.SERVER_HOST, len(body))
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(addr)
        s.send(header.encode() + body)
        return s.recv(16)[8:10] == b" 2"  # "HTTP/1.x 2xx"
    finally:
        s.close()


def read_scans():
    buf = b""
    last = time.ticks_ms()
    while True:
        if uart.any():
            buf += uart.read()
            last = time.ticks_ms()
        elif buf and (b"\r" in buf or b"\n" in buf or time.ticks_diff(time.ticks_ms(), last) > SCAN_IDLE_MS):
            for part in buf.replace(b"\r", b"\n").split(b"\n"):
                code = part.decode("utf-8", "ignore").strip()
                if code:
                    yield code
            buf = b""
        else:
            time.sleep_ms(5)


def main():
    wlan = connect_wifi()
    for code in read_scans():
        print("Barcode:", code)
        led.on()
        if not wlan.isconnected():
            wlan = connect_wifi()
        try:
            ok = send(code)
        except OSError as e:
            print("Versturen mislukt:", e)
            ok = False
        led.off()
        if not ok:
            blink(5)


while True:
    try:
        main()
    except KeyboardInterrupt:
        break
    except Exception as e:
        print("Fout, herstart over 2s:", e)
        time.sleep(2)
