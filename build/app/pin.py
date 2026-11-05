"""The page PIN (Python standard library only). Same rules as the AVADO AI Connect page PIN.

The owner chooses it the first time something secret is asked for: creating or restoring the
wallet (the 24 words show right after), showing the words, the phone QR code, "Disconnect all
phones" and the channel-backup download. The rest of the page needs no PIN.

- Stored as scrypt (N=2^15, r=8, p=1, random salt) in /data/avado/pin.json (mode 600).
- A right PIN gives a session cookie (random 256 bits, only its SHA-256 kept, in memory only, so a
  restart locks the page): HttpOnly, SameSite=Strict, Path=/api, 30 minutes at most, 10 minutes
  without use.
- Wrong PINs are counted per device (the caller's address): 5 tries, then a wait that doubles from
  1 minute up to 15 minutes, so one guessing device never locks the owner's other devices out. For
  the whole box: after more than 20 wrong PINs in an hour every try (right or wrong, any device)
  waits 3 seconds first. Both counts are kept on disk, so a restart does not help a guesser. At most
  two PINs are hashed at once.
- Forgot the PIN: Restart Lightning in the AVADO Admin. That makes a new container from the same
  version, which removes the PIN (the wallet, the words and the phones stay). A power cut, a box
  reboot, a crash or Start after Pause keep the old container, and an update brings a new version,
  so neither removes it. Detected like AI Connect: a marker in the container's own files (gone in
  a new container) plus the last version run, kept in the volume.
"""

import hashlib
import hmac
import os
import re
import secrets
import threading
import time

import avadoweb

PIN_RE = re.compile(r"[0-9]{6,12}")
COOKIE = "ln_session"
SESSION_MAX = 30 * 60
SESSION_IDLE = 10 * 60
FREE_TRIES = 5
FIRST_WAIT = 60
MAX_WAIT = 15 * 60
BOX_SLOW_AFTER = 20
BOX_WINDOW = 60 * 60
BOX_DELAY = 3
MAX_DEVICES = 500
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "dklen": 32}


class PinError(avadoweb.HttpError):
    """status 401: a PIN is needed ("pin": "set" or "enter"); 429: wait retryAfter seconds; 400: bad PIN."""

    def __init__(self, message, status, need=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.need = need
        self.retry_after = retry_after

    def payload(self):
        body = {"error": str(self)}
        if self.need:
            body["pin"] = self.need
        if self.retry_after:
            body["retryAfter"] = int(self.retry_after)
        return body


def _hash(pin, salt):
    return hashlib.scrypt(pin.encode("ascii"), salt=bytes.fromhex(salt), maxmem=64 * 1024 * 1024, **SCRYPT).hex()


def owner_restarted(marker, version_file, version):
    """True when the AVADO Admin restarted this app: a new container (no marker) of the same version."""
    first_in_container = not os.path.exists(marker)
    last = None
    try:
        with open(version_file, "r", encoding="utf-8") as handle:
            last = handle.read().strip()
    except OSError:
        pass
    try:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("%d\n" % time.time())
        avadoweb.write_text(version_file, version + "\n", mode=0o600)
    except OSError:
        pass
    return first_in_container and last == version


class Pin:
    def __init__(self, path, now=time.time, sleep=time.sleep):
        self.path = path
        self.now = now
        self.sleep = sleep
        self.lock = threading.Lock()
        self.hashing = threading.BoundedSemaphore(2)
        self.sessions = {}  # sha256(token) -> {"at": created, "used": last use}
        self.data = avadoweb.read_json(path, None)
        if not isinstance(self.data, dict) or not self.data.get("hash"):
            self.data = None

    def is_set(self):
        return self.data is not None

    def reset(self):
        """Removes the PIN (after an Admin restart). Keeps the guess counts: they are about devices."""
        with self.lock:
            if self.data is None:
                return False
            counts = {"devices": self.data.get("devices", {}), "recentWrong": self.data.get("recentWrong", [])}
            self.data = None
            self.sessions.clear()
            avadoweb.write_json(self.path, counts)
            return True

    def _save(self):
        avadoweb.write_json(self.path, self.data)

    # -- sessions
    def _new_session(self):
        token = secrets.token_urlsafe(32)
        now = self.now()
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {"at": now, "used": now}
        return token

    def cookie(self, token):
        return "%s=%s; HttpOnly; SameSite=Strict; Path=/api; Max-Age=%d" % (COOKIE, token, SESSION_MAX)

    def unlocked(self, cookie_header):
        token = None
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                token = value
        if not token:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            s = self.sessions.get(key)
            now = self.now()
            if not s or now - s["at"] > SESSION_MAX or now - s["used"] > SESSION_IDLE:
                self.sessions.pop(key, None)
                return False
            s["used"] = now
            return True

    def require(self, cookie_header):
        if self.data is None:
            raise PinError("Choose a PIN for this page first.", 401, need="set")
        if not self.unlocked(cookie_header):
            raise PinError("Enter your PIN.", 401, need="enter")

    # -- choosing and entering
    def choose(self, pin, again):
        if not PIN_RE.fullmatch(pin or ""):
            raise PinError("Use 6 to 12 digits.", 400)
        if pin != again:
            raise PinError("The two PINs are not the same.", 400)
        salt = secrets.token_hex(16)
        digest = _hash(pin, salt)
        with self.lock:
            if self.data is not None:
                raise PinError("A PIN is already set.", 409)
            old = avadoweb.read_json(self.path, {}) or {}
            self.data = {"v": 1, "salt": salt, "hash": digest, "setAt": int(self.now()),
                         "devices": old.get("devices", {}), "recentWrong": old.get("recentWrong", [])}
            self._save()
            return self._new_session()

    def enter(self, device, pin):
        with self.lock:
            if self.data is None:
                raise PinError("Choose a PIN for this page first.", 401, need="set")
            now = self.now()
            dev = self.data.setdefault("devices", {}).get(device) or {"failures": 0, "waitUntil": 0}
            if dev.get("waitUntil", 0) > now:
                raise PinError("Too many wrong PINs from this device. Try again later.", 429,
                               retry_after=dev["waitUntil"] - now)
            recent = [t for t in self.data.get("recentWrong", []) if now - t < BOX_WINDOW]
            slow = len(recent) > BOX_SLOW_AFTER
            salt, want = self.data["salt"], self.data["hash"]
        if slow:
            self.sleep(BOX_DELAY)
        if not PIN_RE.fullmatch(pin or ""):
            ok = False
        else:
            with self.hashing:
                ok = hmac.compare_digest(_hash(pin, salt), want)
        with self.lock:
            if self.data is None:  # reset while checking
                raise PinError("Choose a PIN for this page first.", 401, need="set")
            devices = self.data.setdefault("devices", {})
            now = self.now()
            if ok:
                devices.pop(device, None)
                self._save()
                return self._new_session()
            dev = devices.get(device) or {"failures": 0, "waitUntil": 0}
            dev["failures"] = int(dev.get("failures", 0)) + 1
            if dev["failures"] >= FREE_TRIES:
                dev["waitUntil"] = now + min(MAX_WAIT, FIRST_WAIT * 2 ** (dev["failures"] - FREE_TRIES))
            dev["at"] = now
            devices[device] = dev
            if len(devices) > MAX_DEVICES:
                for old in sorted(devices, key=lambda d: devices[d].get("at", 0))[:len(devices) - MAX_DEVICES]:
                    devices.pop(old, None)
            self.data["recentWrong"] = [t for t in self.data.get("recentWrong", []) if now - t < BOX_WINDOW] + [now]
            self._save()
            left = FREE_TRIES - dev["failures"]
            if left > 0:
                raise PinError("Wrong PIN. %d tr%s left before a wait." % (left, "y" if left == 1 else "ies"), 401,
                               need="enter")
            raise PinError("Wrong PIN. Wait %s before the next try." % avadoweb.human_duration(dev["waitUntil"] - now),
                           429, retry_after=dev["waitUntil"] - now)
