"""Minimal client for LND's REST API on 127.0.0.1 (Python standard library only).

LND's TLS certificate is self-signed; the client trusts exactly that
certificate (read from LND's folder at every call, as LND renews it), so it
needs no host name check. Calls that need a login send the admin macaroon in
the Grpc-Metadata-macaroon header. Nothing here logs a password, seed or
macaroon.
"""

import base64
import http.client
import json
import ssl


class LndError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def b64(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


class Lnd:
    def __init__(self, port, cert_path, macaroon_path, host="127.0.0.1", timeout=15):
        self.host = host
        self.port = port
        self.cert_path = cert_path
        self.macaroon_path = macaroon_path
        self.timeout = timeout

    def _context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        try:
            ctx.load_verify_locations(cafile=self.cert_path)
        except (OSError, ssl.SSLError) as err:
            raise LndError("LND has no TLS certificate yet") from err
        return ctx

    def _macaroon(self):
        try:
            with open(self.macaroon_path, "rb") as handle:
                return handle.read().hex()
        except OSError:
            return None

    def call(self, method, path, body=None, auth=True, timeout=None):
        headers = {"Accept": "application/json"}
        if auth:
            mac = self._macaroon()
            if not mac:
                raise LndError("No login for LND yet")
            headers["Grpc-Metadata-macaroon"] = mac
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout or self.timeout,
                                           context=self._context())
        try:
            conn.request(method, path, payload, headers)
            resp = conn.getresponse()
            raw = resp.read(8 * 1024 * 1024)
        except (OSError, http.client.HTTPException, ssl.SSLError) as err:
            raise LndError("Cannot reach LND: %s" % err) from err
        finally:
            conn.close()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError as err:
            raise LndError("Unexpected answer from LND (HTTP %d)" % resp.status, resp.status) from err
        if resp.status != 200:
            message = data.get("message") or data.get("error") if isinstance(data, dict) else None
            raise LndError(message or "LND answered HTTP %d" % resp.status, resp.status)
        return data

    # -- before the wallet is open (no login)
    def state(self):
        """NON_EXISTING, LOCKED, UNLOCKED, RPC_ACTIVE, SERVER_ACTIVE or WAITING_TO_START."""
        return self.call("GET", "/v1/state", auth=False, timeout=5).get("state") or "WAITING_TO_START"

    def genseed(self):
        words = self.call("GET", "/v1/genseed", auth=False).get("cipher_seed_mnemonic") or []
        if len(words) != 24:
            raise LndError("LND did not make 24 words")
        return words

    def initwallet(self, password, words, multi_chan_backup=None, recovery_window=0):
        body = {"wallet_password": b64(password), "cipher_seed_mnemonic": list(words)}
        if recovery_window:
            body["recovery_window"] = int(recovery_window)
        if multi_chan_backup:
            body["channel_backups"] = {"multi_chan_backup": {"multi_chan_backup": b64(multi_chan_backup)}}
        return self.call("POST", "/v1/initwallet", body, auth=False, timeout=120)

    # -- with the admin macaroon
    def getinfo(self):
        return self.call("GET", "/v1/getinfo", timeout=8)

    def onchain_balance(self):
        return self.call("GET", "/v1/balance/blockchain", timeout=8)

    def channel_balance(self):
        return self.call("GET", "/v1/balance/channels", timeout=8)

    def bake_macaroon(self, permissions, root_key_id):
        body = {"permissions": [{"entity": e, "action": a} for e, a in permissions],
                "root_key_id": str(int(root_key_id))}
        mac = self.call("POST", "/v1/macaroon", body).get("macaroon")
        if not mac:
            raise LndError("LND did not make a login")
        return mac  # hex

    def delete_macaroon_id(self, root_key_id):
        return self.call("DELETE", "/v1/macaroon/%d" % int(root_key_id))
