#!/usr/bin/env python3
"""AVADO Lightning: runs LND on the AVADO Bitcoin Node, and its page.

- Gets its own RPC login from the Bitcoin Node app (which hands it out only to
  this app's container) and checks that the node keeps the full history and
  sends new blocks and transactions over ZMQ. On a space-saving (pruned) node
  LND does not start and the page says what to do.
- Makes the wallet in the browser: LND makes the 24 recovery words, the page
  shows them until the owner says they are written down, then they are deleted
  from the box.
- Opens the wallet by itself after every restart: the wallet password is a
  random one made on the box and kept in this app's volume (LND's
  wallet-unlock-password-file).
- Offers the channel backup as a download, and a QR code (lndconnect) for the
  Zeus phone wallet, over Tailscale when the Tailscale app runs.
- LND's REST port is reached through gate.py (home network, VPNs and AVADO apps
  only), and only after the wallet exists.
"""

import base64
import collections
import datetime
import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import ssl
import subprocess
import sys
import threading
import time

import avadoweb
import gate
import wamp
from avadoweb import UserError, log
from bitcoinrpc import RPC_IN_WARMUP, BitcoinRpc, RpcError, fetch_login
from lnd import Lnd, LndError

_HERE = os.path.dirname(os.path.abspath(__file__))
_VERSION_FILE = os.path.join(_HERE, "package-version")
VERSION = open(_VERSION_FILE).read().strip() if os.path.exists(_VERSION_FILE) else "dev"

DATA_DIR = os.environ.get("DATA_DIR", "/data")
LND_DIR = os.path.join(DATA_DIR, "lnd")
STATE_DIR = os.path.join(DATA_DIR, "avado")
CONF_FILE = os.path.join(LND_DIR, "lnd.conf")
CERT_FILE = os.path.join(LND_DIR, "tls.cert")
PASSWORD_FILE = os.path.join(STATE_DIR, "wallet-password")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PUBLIC_DIR = os.path.join(_HERE, "public")

# Bitcoin Core's chain name -> LND's network option and folder name
CHAINS = {"main": "mainnet", "test": "testnet", "testnet4": "testnet4", "signet": "signet", "regtest": "regtest"}
CHAIN_NAMES = {"main": "Bitcoin", "test": "testnet3", "testnet4": "testnet4", "signet": "signet", "regtest": "regtest"}

# What the phone's login (macaroon) may do: everything the admin login may, except
# making or deleting logins. "Disconnect all phones" deletes its root key.
PHONE_PERMISSIONS = [
    ("onchain", "read"), ("onchain", "write"), ("offchain", "read"), ("offchain", "write"),
    ("address", "read"), ("address", "write"), ("message", "read"), ("message", "write"),
    ("peers", "read"), ("peers", "write"), ("info", "read"), ("info", "write"),
    ("invoices", "read"), ("invoices", "write"), ("signer", "read"), ("signer", "generate"),
    ("macaroon", "read"),
]
PHONE_ROOT_KEY_BASE = 7300

LINKS = {
    "bitcoin": "http://bitcoincore.my.ava.do/#connect",
    "store": "http://my.ava.do/#/installer",
    "tailscale": "http://tailscale.my.ava.do",
    "zeus": "https://zeusln.com",
}


def env_int(name, default, low=None, high=None, env=os.environ):
    try:
        value = int(str(env.get(name, default)).strip())
    except ValueError:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


class Config:
    def __init__(self, env=os.environ):
        self.bitcoin_host = env.get("BITCOIN_HOST") or "DAppNodePackage-bitcoincore.avado.dnp.dappnode.eth"
        self.tailscale_host = env.get("TAILSCALE_HOST") or "DAppNodePackage-tailscale.avado.dnp.dappnode.eth"
        self.wamp_host = env.get("WAMP_HOST") or "wamp.my.ava.do"
        self.page_host = env.get("PAGE_HOST") or "lightning.my.ava.do"
        alias = re.sub(r"[^A-Za-z0-9 ._-]", "", env.get("ALIAS") or "").strip()[:32]
        self.alias = alias or "AVADO"
        self.p2p_port = env_int("P2P_PORT", 9735, 1, 65535, env=env)
        self.rest_port = env_int("REST_PORT", 8080, 1, 65535, env=env)  # published, behind gate.py
        self.lnd_rest_port = 8081  # LND itself, 127.0.0.1 only
        self.lnd_rpc_port = 10009  # gRPC, 127.0.0.1 only (lncli for support)
        self.lnd = env.get("LND", "/usr/local/bin/lnd")


def network_of(chain):
    return CHAINS.get(chain or "main", "mainnet")


def chain_dir(chain):
    return os.path.join(LND_DIR, "data", "chain", "bitcoin", network_of(chain))


def render_conf(config, chain, login, zmq, lan_ip, unlock_file):
    """lnd.conf, written before every start. login = (user, password, rpc port);
    zmq = {"pubrawblock": port, "pubrawtx": port} from the node's getzmqnotifications."""
    user, password, rpc_port = login
    host = config.bitcoin_host
    lines = [
        "# Written by the AVADO Lightning app at every start: changes here are lost.",
        "[Application Options]",
        "alias=%s" % config.alias,
        "color=#F7931A",
        "listen=0.0.0.0:%d" % config.p2p_port,
        "rpclisten=127.0.0.1:%d" % config.lnd_rpc_port,
        "restlisten=127.0.0.1:%d" % config.lnd_rest_port,
        # The phone connects by this name (Tailscale, AVADO Wi-Fi, Remote Access) or the home address.
        "tlsextradomain=%s" % config.page_host,
        "tlsextradomain=DAppNodePackage-lightning.avado.dnp.dappnode.eth",
        # No container addresses in the certificate: they change at every update.
        "tlsdisableautofill=true",
        "tlsautorefresh=true",
        "logging.file.max-files=3",
        "logging.file.max-file-size=10",
    ]
    if lan_ip:
        lines.append("tlsextraip=%s" % lan_ip)
    if unlock_file:
        lines += [
            # The wallet opens by itself with the password kept in this app's volume.
            "wallet-unlock-password-file=%s" % unlock_file,
            # Until the wallet exists LND waits for it to be made. Its REST port is then
            # reachable only from inside this container (gate.py stays shut).
            "wallet-unlock-allow-create=true",
        ]
    lines += [
        "",
        "[Bitcoin]",
        "bitcoin.%s=true" % network_of(chain),
        "bitcoin.node=bitcoind",
        "",
        "[Bitcoind]",
        "bitcoind.rpchost=%s:%d" % (host, rpc_port),
        "bitcoind.rpcuser=%s" % user,
        "bitcoind.rpcpass=%s" % password,
        "bitcoind.zmqpubrawblock=tcp://%s:%d" % (host, zmq["pubrawblock"]),
        "bitcoind.zmqpubrawtx=tcp://%s:%d" % (host, zmq["pubrawtx"]),
        "bitcoind.estimatemode=CONSERVATIVE",
    ]
    return "\n".join(lines) + "\n"


def zmq_ports(notifications):
    """{"pubrawblock": port, "pubrawtx": port} from getzmqnotifications, or None
    when the node does not send both (LND needs both, on different addresses)."""
    found = {}
    for item in notifications or []:
        kind = item.get("type")
        match = re.search(r":(\d+)$", str(item.get("address") or ""))
        if kind in ("pubrawblock", "pubrawtx") and match:
            found[kind] = int(match.group(1))
    if set(found) != {"pubrawblock", "pubrawtx"} or found["pubrawblock"] == found["pubrawtx"]:
        return None
    return found


def b64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def lndconnect_url(host, port, cert_pem, macaroon_hex):
    """The lndconnect link Zeus scans: lndconnect://host:port?cert=...&macaroon=..."""
    der = ssl.PEM_cert_to_DER_cert(cert_pem)
    return "lndconnect://%s:%d?cert=%s&macaroon=%s" % (host, port, b64url(der), b64url(bytes.fromhex(macaroon_hex)))


def qr_data_uri(text):
    """QR code as a PNG data URI (qrencode reads the text from stdin, never from its command line)."""
    out = subprocess.run(["qrencode", "-t", "PNG", "-l", "L", "-m", "2", "-s", "6", "-o", "-"],
                         input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         check=True, timeout=20).stdout
    return "data:image/png;base64," + base64.b64encode(out).decode("ascii")


def parse_words(text):
    words = re.findall(r"[a-z]+", (text or "").lower())
    if len(words) != 24:
        raise UserError("Type all 24 words, in order, with a space between them (you typed %d)." % len(words))
    return words


def sats(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def fmt_sats(n):
    return "{:,} sats".format(int(n))


def fmt_int(n):
    return "{:,}".format(int(n))


def wipe(path):
    """Overwrites a small secret file once, then deletes it."""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as handle:
            handle.write(secrets.token_bytes(max(size, 1)))
            handle.flush()
            os.fsync(handle.fileno())
        os.remove(path)
    except FileNotFoundError:
        pass


def tailscale_running(host, timeout=4):
    """True when the AVADO Tailscale app is installed and logged in (its short status)."""
    conn = http.client.HTTPConnection(host, 80, timeout=timeout)
    try:
        conn.request("GET", "/api/status", headers={"Host": "tailscale.my.ava.do"})
        resp = conn.getresponse()
        data = json.loads(resp.read(65536).decode("utf-8")) if resp.status == 200 else {}
        return isinstance(data, dict) and data.get("state") == "running"
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()


class LndProcess:
    """Keeps LND running while it should run."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.proc = None
        self.lines = collections.deque(maxlen=80)
        self.started_at = None
        self.last_exit = None
        self.restarts = 0
        self.next_start = 0
        self.conf = None  # the lnd.conf this process started with

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _reader(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            self.lines.append(line)
            print(line, flush=True)

    def start(self, conf):
        with self.lock:
            if self.running():
                return
            avadoweb.write_text(CONF_FILE, conf, mode=0o600)
            args = [self.config.lnd, "--lnddir=%s" % LND_DIR, "--configfile=%s" % CONF_FILE]
            log("starting LND")
            self.lines.clear()
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.started_at = time.time()
            self.conf = conf
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def stop(self, timeout=120):
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self.proc = None
                return
            log("stopping LND")
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        with self.lock:
            if self.proc is proc:
                self.proc = None
                self.next_start = 0

    def reap(self):
        """Notices a stop; returns True when it is time to start (again)."""
        with self.lock:
            if self.proc is not None and self.proc.poll() is not None:
                code = self.proc.returncode
                uptime = time.time() - (self.started_at or time.time())
                self.restarts = 0 if uptime > 600 else self.restarts + 1
                pause = min(300, 10 * (2 ** min(self.restarts, 5)))
                why = explain_exit(list(self.lines))
                self.last_exit = {"code": code, "at": time.time(), "why": why, "pause": pause}
                log("LND stopped (exit %s, %s); next try in %d s" % (code, why or "no reason given", pause))
                self.proc = None
                self.next_start = time.time() + pause
            return self.proc is None and time.time() >= self.next_start


def explain_exit(lines):
    text = "\n".join(lines[-40:])
    if re.search(r"No space left on device|disk full", text, re.I):
        return "disk"
    if re.search(r"invalid passphrase|wallet password|unable to unlock", text, re.I):
        return "password"
    if re.search(r"unable to (?:connect|reach)|connection refused|chain backend|zmq", text, re.I):
        return "node"
    return None


class App:
    def __init__(self, config):
        self.config = config
        for d in (LND_DIR, STATE_DIR):
            os.makedirs(d, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
        self.state = avadoweb.read_json(STATE_FILE, {}) or {}
        self.lnd = LndProcess(config)
        self.lock = threading.RLock()  # wallet actions one at a time
        self.login = None
        self.login_at = 0
        self.login_error = None
        self.node = {}
        self.node_error = None
        self.zmq = None
        self.lan_ip = self.state.get("lanIp")
        self.lan_ip_at = 0
        self.tailscale = False
        self.tailscale_at = 0
        self.lnd_state = None
        self.lnd_info = None
        self.balances = None
        self.balances_at = 0
        self.cache = {"at": 0, "model": None}
        self.cache_lock = threading.Lock()

    # -- small helpers
    def save_state(self):
        avadoweb.write_json(STATE_FILE, self.state)

    def chain(self):
        return self.node.get("chain") or self.state.get("chain") or "main"

    def wallet(self):
        """What the app remembers about the wallet of the current network (a regtest test wallet
        and the mainnet wallet live side by side in LND's folder, so they are kept apart here too)."""
        return self.state.setdefault("wallets", {}).setdefault(network_of(self.chain()), {})

    def seed_file(self):
        return os.path.join(STATE_DIR, "seed-once-%s.json" % network_of(self.chain()))

    def phone_macaroon(self):
        return os.path.join(STATE_DIR, "phone-%s.macaroon" % network_of(self.chain()))

    def client(self):
        mac = os.path.join(chain_dir(self.chain()), "admin.macaroon")
        return Lnd(self.config.lnd_rest_port, CERT_FILE, mac)

    def wallet_exists(self):
        return os.path.exists(os.path.join(chain_dir(self.chain()), "wallet.db"))

    def backup_file(self):
        return os.path.join(chain_dir(self.chain()), "channel.backup")

    def ensure_password(self):
        """A random wallet password, made once before the wallet exists."""
        if not os.path.exists(PASSWORD_FILE) and not self.wallet_exists():
            avadoweb.write_text(PASSWORD_FILE, secrets.token_urlsafe(32), mode=0o600)
        return PASSWORD_FILE if os.path.exists(PASSWORD_FILE) else None

    def read_password(self):
        with open(PASSWORD_FILE, "r", encoding="utf-8") as handle:
            return handle.read().strip()

    # -- the Bitcoin Node
    def refresh_login(self, force=False):
        if self.login and not force and time.time() - self.login_at < 300:
            return self.login
        try:
            login = fetch_login(self.config.bitcoin_host, "lnd")
            if self.login is not None and self.login != login:
                log("the Bitcoin Node app gave a new login; LND restarts with it")
            self.login, self.login_at, self.login_error = login, time.time(), None
        except RpcError as err:
            self.login_error = {"code": err.code}
            if err.code in (403, 404):
                log("the Bitcoin Node app refused the login:", err)
        return self.login

    def check_node(self):
        login = self.refresh_login()
        if not login:
            self.node, self.node_error, self.zmq = {}, "unreachable", None
            return
        rpc = BitcoinRpc(self.config.bitcoin_host, login[2], login[0], login[1], timeout=8)
        try:
            self.node = rpc.call("getblockchaininfo")
            self.node_error = None
            self.zmq = zmq_ports(rpc.call("getzmqnotifications"))
            if self.node.get("chain") and self.state.get("chain") != self.node["chain"]:
                self.state["chain"] = self.node["chain"]
                self.save_state()
        except RpcError as err:
            self.node, self.zmq = {}, None
            if err.code == RPC_IN_WARMUP:
                self.node_error = "warmup"
            elif err.code == 401:
                self.node_error = "login"
                self.refresh_login(force=True)
            elif err.code == 403:
                self.node_error = "old"  # a Bitcoin Node without getzmqnotifications for Lightning
            else:
                self.node_error = "unreachable"

    def should_run(self):
        return bool(self.login) and bool(self.node) and not self.node.get("pruned") and self.zmq is not None

    def box_ip(self):
        if time.time() - self.lan_ip_at > 600 or (self.lan_ip is None and time.time() - self.lan_ip_at > 60):
            self.lan_ip_at = time.time()
            ip = wamp.box_lan_ip(host=self.config.wamp_host)
            if ip and ip != self.lan_ip:
                self.lan_ip = ip
                self.state["lanIp"] = ip
                self.save_state()
        return self.lan_ip

    def tailscale_on(self):
        if time.time() - self.tailscale_at > 60:
            self.tailscale_at = time.time()
            self.tailscale = (bool(avadoweb.resolve_ips(self.config.tailscale_host))
                              and tailscale_running(self.config.tailscale_host))
        return self.tailscale

    # -- the loop
    def loop(self):
        while True:
            try:
                self.check_node()
                if self.should_run():
                    conf = render_conf(self.config, self.chain(), self.login, self.zmq, self.box_ip(),
                                       self.ensure_password())
                    if self.lnd.running() and self.lnd.conf != conf:
                        log("LND settings changed (Bitcoin Node login, network or home address): restarting LND")
                        self.lnd.stop()
                    if self.lnd.reap() and not self.lnd.running():
                        self.lnd.start(conf)
                elif self.node.get("pruned") and self.lnd.running():
                    log("the Bitcoin node keeps only the newest blocks: Lightning cannot run on it")
                    self.lnd.stop()
                else:
                    self.lnd.reap()
                self.check_lnd()
            except Exception as err:  # keep the loop alive
                log("check failed:", repr(err))
            time.sleep(10)

    def check_lnd(self):
        if not self.lnd.running():
            self.lnd_state, self.lnd_info = None, None
            return
        client = self.client()
        try:
            self.lnd_state = client.state()
        except LndError:
            self.lnd_state = None
            return
        if self.lnd_state in ("RPC_ACTIVE", "SERVER_ACTIVE"):
            try:
                self.lnd_info = client.getinfo()
            except LndError:
                self.lnd_info = None
            if self.lnd_info and time.time() - self.balances_at > 30:
                try:
                    self.balances = {"onchain": client.onchain_balance(), "channels": client.channel_balance()}
                    self.balances_at = time.time()
                except LndError:
                    pass
        else:
            self.lnd_info = None

    def phone_port_open(self):
        """gate.py lets phones in only once the wallet exists (creating one needs no login)."""
        return self.lnd_state in ("LOCKED", "UNLOCKED", "RPC_ACTIVE", "SERVER_ACTIVE") and self.wallet_exists()

    # -- actions
    def action_create_wallet(self, _body):
        with self.lock:
            if self.lnd_state != "NON_EXISTING" or self.wallet_exists():
                raise UserError("This AVADO already has a Lightning wallet.")
            client = self.client()
            password = self.read_password()
            words = client.genseed()
            # Kept until the owner confirms they wrote them down; never logged.
            avadoweb.write_json(self.seed_file(), {"words": words, "at": int(time.time())}, mode=0o600)
            try:
                client.initwallet(password, words)
            except LndError:
                wipe(self.seed_file())
                raise
            self.wallet().update({"walletAt": int(time.time()), "restored": False})
            self.lnd_state = "UNLOCKED"  # until the loop reads it again: no second "Create" button
            self.save_state()
            log("wallet created")
            return {"message": "Your wallet is made. Write down your 24 words now.", "reveal": seed_reveal(words)}

    def action_show_words(self, _body):
        seed = avadoweb.read_json(self.seed_file(), None)
        if not seed or not seed.get("words"):
            raise UserError("Your AVADO no longer has your 24 words: they were deleted after you wrote them down.")
        return {"reveal": seed_reveal(seed["words"])}

    def action_words_done(self, _body):
        wipe(self.seed_file())
        self.wallet()["wordsDoneAt"] = int(time.time())
        self.save_state()
        log("the owner wrote down the 24 words; they are deleted from the box")
        return {"message": "Done. Your AVADO has deleted the 24 words. Keep your paper safe."}

    def action_restore_wallet(self, body):
        with self.lock:
            if self.lnd_state != "NON_EXISTING" or self.wallet_exists():
                raise UserError("This AVADO already has a Lightning wallet.")
            words = parse_words(body.get("words"))
            backup = None
            if body.get("backup"):
                try:
                    backup = base64.b64decode(str(body["backup"]), validate=True)
                except ValueError:
                    raise UserError("The channel backup file could not be read.")
                if not backup or len(backup) > 48 * 1024:
                    raise UserError("That does not look like a channel backup file.")
            try:
                self.client().initwallet(self.read_password(), words, multi_chan_backup=backup, recovery_window=2500)
            except LndError as err:
                if "mnemonic" in str(err).lower() or "checksum" in str(err).lower() or "word" in str(err).lower():
                    raise UserError("These 24 words are not a Lightning (LND) wallet. Check each word and try again.")
                if backup and "backup" in str(err).lower():
                    raise UserError("The channel backup does not belong to these 24 words.")
                raise
            self.wallet().update({"walletAt": int(time.time()), "restored": True, "wordsDoneAt": int(time.time())})
            self.lnd_state = "UNLOCKED"
            self.save_state()
            log("wallet restored from 24 words%s" % (" and a channel backup" if backup else ""))
            return {"message": "Your wallet is restored. Finding your old payments takes a while after your Bitcoin "
                               "node is up to date."}

    def phone_host(self, via):
        if via == "home" and self.box_ip():
            return self.box_ip()
        return self.config.page_host

    def action_show_phone(self, body):
        with self.lock:
            if self.lnd_state not in ("RPC_ACTIVE", "SERVER_ACTIVE"):
                raise UserError("Lightning is still starting. Try again in a minute.")
            via = body.get("via")
            if via not in ("tailscale", "home"):
                via = "tailscale" if self.tailscale_on() else "home"
            mac = None
            if os.path.exists(self.phone_macaroon()):
                with open(self.phone_macaroon(), "r", encoding="ascii") as handle:
                    mac = handle.read().strip()
            if not mac:
                key_id = PHONE_ROOT_KEY_BASE + int(self.wallet().get("phoneKeys", 0))
                try:
                    mac = self.client().bake_macaroon(PHONE_PERMISSIONS, key_id)
                except LndError:
                    raise UserError("Lightning is still starting. Try again when your Bitcoin node is up to date.")
                avadoweb.write_text(self.phone_macaroon(), mac, mode=0o600)
            with open(CERT_FILE, "r", encoding="ascii") as handle:
                cert = handle.read()
            url = lndconnect_url(self.phone_host(via), self.config.rest_port, cert, mac)
            self.wallet()["phoneAt"] = int(time.time())
            self.save_state()
            return {"reveal": phone_reveal(url, qr_data_uri(url), via, self.box_ip())}

    def action_forget_phones(self, _body):
        with self.lock:
            if self.lnd_state not in ("RPC_ACTIVE", "SERVER_ACTIVE"):
                raise UserError("Lightning is still starting. Try again in a minute.")
            key_id = PHONE_ROOT_KEY_BASE + int(self.wallet().get("phoneKeys", 0))
            try:
                self.client().delete_macaroon_id(key_id)
            except LndError as err:
                if "not found" not in str(err).lower():
                    raise
            wipe(self.phone_macaroon())
            self.wallet()["phoneKeys"] = int(self.wallet().get("phoneKeys", 0)) + 1
            self.wallet().pop("phoneAt", None)
            self.save_state()
            log("all phone logins deleted")
            return {"message": "Done. Phones connected so far can no longer use your wallet. Show a new QR code to "
                               "connect again."}

    def download_backup(self):
        path = self.backup_file()
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            raise UserError("There is no channel backup yet. It appears when Lightning has started.")
        self.wallet().update({"backupSha": hashlib.sha256(data).hexdigest(), "backupAt": int(time.time())})
        self.save_state()
        return "avado-lightning-channel-backup-%s.backup" % datetime.date.today().isoformat(), data

    def backup_sha(self):
        try:
            with open(self.backup_file(), "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return None

    # -- page
    def gather(self):
        info = {
            "nodeInstalled": bool(avadoweb.resolve_ips(self.config.bitcoin_host)),
            "loginError": self.login_error, "node": self.node, "nodeError": self.node_error,
            "zmq": self.zmq, "running": self.lnd.running(), "lastExit": self.lnd.last_exit,
            "lndState": self.lnd_state, "lndInfo": self.lnd_info, "balances": self.balances,
            "walletExists": self.wallet_exists(), "passwordFile": os.path.exists(PASSWORD_FILE),
            "seedPending": os.path.exists(self.seed_file()), "wordsDone": bool(self.wallet().get("wordsDoneAt")),
            "backupSha": self.backup_sha(), "backupDownloadedSha": self.wallet().get("backupSha"),
            "phoneShown": bool(self.wallet().get("phoneAt")), "phoneLogin": os.path.exists(self.phone_macaroon()),
            "tailscale": self.tailscale_on(), "lanIp": self.lan_ip, "restored": bool(self.wallet().get("restored")),
        }
        return info

    def status(self):
        with self.cache_lock:
            if self.cache["model"] is not None and time.time() - self.cache["at"] < 4:
                return self.cache["model"]
        model = build_model(self.gather(), self.config)
        with self.cache_lock:
            self.cache = {"at": time.time(), "model": model}
        return model

    def run(self):
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)
        gate.Gate(self.config.rest_port, self.config.lnd_rest_port, ready=self.phone_port_open).start()
        actions = {
            "create-wallet": self.action_create_wallet, "show-words": self.action_show_words,
            "words-done": self.action_words_done, "restore-wallet": self.action_restore_wallet,
            "show-phone": self.action_show_phone, "forget-phones": self.action_forget_phones,
        }

        def invalidate(handler):
            def run(body):
                try:
                    return handler(body)
                finally:
                    with self.cache_lock:
                        self.cache["at"] = 0
            return run

        avadoweb.WebApp(PUBLIC_DIR, self.status, actions={k: invalidate(v) for k, v in actions.items()},
                        downloads={"channel-backup": self.download_backup},
                        version=VERSION, title="Lightning").serve(port=env_int("PORT", 80))
        threading.Thread(target=self.loop, daemon=True).start()
        while True:
            time.sleep(3600)

    def shutdown(self, *_args):
        log("shutting down")
        self.lnd.stop(timeout=60)
        sys.exit(0)


# ---------------------------------------------------------------- page model

HOT_WALLET = "Keep only small amounts here."


def seed_reveal(words):
    return {
        "id": "words",
        "title": "Your 24 words",
        "warn": "Write them on paper, in order. Anyone with these words can take your money. Never type them "
                "into a website and never take a photo of them.",
        "words": list(words),
        "text": ["With these words and the channel backup you get your money back, even if this AVADO breaks. "
                 "Your AVADO deletes them when you press the button below."],
        "buttons": [{"action": "words-done", "label": "I have written them down", "style": "primary",
                     "confirm": "Did you write down all 24 words, in order? Your AVADO deletes them now and can never "
                                "show them again."}],
    }


def phone_reveal(url, qr, via, lan_ip):
    if via == "tailscale":
        where = ("Works at home and away, with Tailscale switched on in your phone (and the AVADO's DNS set in "
                 "Tailscale, step 3 of the Tailscale app).")
    elif lan_ip:
        where = "Works when your phone is on your home Wi-Fi."
    else:
        where = "Works when your phone is on the AVADO Wi-Fi or uses AVADO Remote Access."
    return {
        "id": "phone",
        "title": "Connect Zeus",
        "qr": qr,
        "steps": ["Install Zeus on your phone (App Store or Google Play).",
                  "In Zeus, choose to connect your own node (Advanced set-up), then press the scan button.",
                  "Scan this code."],
        "text": [where],
        "warn": "This code gives full use of your Lightning wallet. Show it only to your own phone.",
        "copy": {"label": "Copy the link", "value": url},
    }


def checklist(info, ready):
    """The four set-up steps, ticked live."""
    lnd_state = info.get("lndState")
    has_wallet = bool(info.get("walletExists")) and lnd_state != "NON_EXISTING"
    steps = []

    s1 = {"title": "Create your wallet", "done": has_wallet}
    if not has_wallet:
        s1["text"] = "It opens by itself after a power cut: your AVADO keeps its password."
        if lnd_state == "NON_EXISTING":
            s1["buttons"] = [{"action": "create-wallet", "label": "Create my wallet", "style": "primary",
                              "confirm": "Your AVADO makes a new Lightning wallet and shows its 24 recovery words "
                                         "once. Have pen and paper ready. " + HOT_WALLET + " Continue?"}]
    steps.append(s1)

    words_done = bool(info.get("wordsDone")) or (has_wallet and not info.get("seedPending")
                                                 and bool(info.get("restored")))
    s2 = {"title": "Write down your 24 words", "done": has_wallet and words_done}
    if has_wallet and info.get("seedPending"):
        s2["buttons"] = [{"action": "show-words", "label": "Show my 24 words", "style": "primary"}]
    steps.append(s2)

    sha, got = info.get("backupSha"), info.get("backupDownloadedSha")
    s3 = {"title": "Download the channel backup", "done": bool(sha) and sha == got}
    if has_wallet and not sha:
        s3["text"] = "Possible when Lightning has started."
    elif has_wallet and sha:
        if got and sha != got:
            s3["text"] = "Your channels changed: download it again."
        elif not got:
            s3["text"] = "Download it again after you open or close a channel."
        if sha != got:
            s3["links"] = [{"label": "Download", "href": "api/download/channel-backup", "download": True,
                            "style": "primary" if words_done else None}]
    steps.append(s3)

    s4 = {"title": "Connect Zeus on your phone", "done": bool(info.get("phoneShown")) and has_wallet}
    if has_wallet and ready:
        s4["buttons"] = [{"action": "show-phone", "label": "Show the QR code",
                          "style": "primary" if words_done and s3["done"] else None}]
    elif has_wallet:
        s4["text"] = "Possible when Lightning has started."
    steps.append(s4)
    return {"id": "setup", "title": "Set up", "checklist": steps}


def node_progress(node):
    progress = float(node.get("verificationprogress") or 0)
    ibd = bool(node.get("initialblockdownload")) or progress < 0.9999
    return progress, ibd


def build_model(info, config):
    node = info.get("node") or {}
    model = {"title": "Lightning", "facts": [], "sections": [], "refresh": 10}
    login_code = (info.get("loginError") or {}).get("code")
    lnd_state = info.get("lndState")
    lnd_info = info.get("lndInfo") or {}

    if not info.get("nodeInstalled"):
        # Docker's DNS knows a container only while it runs: not installed and stopped look the same.
        model.update(state="error", headline="The Bitcoin Node is not running",
                     text=["Lightning works on top of the Bitcoin Node app. Install \"Bitcoin Node\" from the AVADO "
                           "store, or start it in the AVADO Admin if it is stopped. This page then updates by itself."])
        model["sections"].append({"id": "node", "links": [
            {"label": "Open the AVADO store", "href": LINKS["store"], "style": "primary"}]})
        return model
    if login_code == 404 or info.get("nodeError") == "old":
        model.update(state="error", headline="Update the Bitcoin Node app",
                     text=["This Bitcoin Node app is too old for Lightning. Update it in the AVADO Admin; this page "
                           "then updates by itself."])
        return model
    if login_code == 403:
        model.update(state="error", headline="The Bitcoin Node does not let Lightning in",
                     text=["Restart the Bitcoin Node app and then this app in the AVADO Admin. If this stays, contact "
                           "AVADO support."])
        return model
    if node.get("pruned"):
        model.update(state="warn", headline="Lightning needs the full Bitcoin history",
                     text=["Switch Bitcoin Node to Full in its settings."])
        if info.get("walletExists"):
            model["text"].append("Your Lightning wallet stays safe on this AVADO and starts again by itself then.")
        model["sections"].append({"id": "node", "links": [
            {"label": "Open Bitcoin Node", "href": LINKS["bitcoin"], "style": "primary"}]})
        return model
    if not node:
        model.update(state="working", headline="Waiting for your Bitcoin node",
                     text=["Your Bitcoin node is starting. Lightning begins as soon as it answers."])
        return model
    if info.get("zmq") is None:
        model.update(state="error", headline="Update the Bitcoin Node app",
                     text=["Your Bitcoin node does not pass new blocks on to Lightning. Update the Bitcoin Node app "
                           "in the AVADO Admin and restart it."])
        return model

    progress, ibd = node_progress(node)
    ready = lnd_state in ("RPC_ACTIVE", "SERVER_ACTIVE")
    if not info.get("running"):
        last = info.get("lastExit") or {}
        if last.get("why") == "disk":
            model.update(state="error", headline="Lightning stopped: the disk is full",
                         text=["Make room on your AVADO by removing apps you no longer use. It tries again by itself."])
        elif last:
            wait = max(0, int(last.get("at", 0) + last.get("pause", 0) - time.time()))
            model.update(state="error", headline="Lightning is starting again",
                         text=["It stopped unexpectedly and starts again in %s." % avadoweb.human_duration(wait)])
        else:
            model.update(state="working", headline="Lightning is starting", text=["This takes a moment."])
        return model
    if lnd_state in (None, "WAITING_TO_START"):
        model.update(state="working", headline="Lightning is starting", text=["This takes a moment."])
        return model
    if lnd_state == "LOCKED" and not info.get("passwordFile"):
        model.update(state="error", headline="Your Lightning wallet is locked",
                     text=["Its password is not on this AVADO. Contact AVADO support; your money is safe."])
        return model

    if lnd_state == "NON_EXISTING":
        model.update(state="working", headline="Create your Lightning wallet",
                     text=["For quick, cheap payments with your phone. " + HOT_WALLET])
    elif not ready:
        model.update(state="working", headline="Opening your wallet", text=["This takes a moment."])
    elif ibd:
        model.update(state="working", headline="Waiting for your Bitcoin node to finish downloading",
                     text=["Lightning starts when it is done. You can set up everything below already."])
        model["progress"] = {"percent": round(progress * 100, 2), "left": "Bitcoin node: %.1f%% done" % (progress * 100)}
    elif not lnd_info.get("synced_to_chain"):
        model.update(state="working", headline="Lightning is catching up with new blocks",
                     text=["This takes a few minutes. " + HOT_WALLET])
    else:
        test = "" if node.get("chain") == "main" else " (test network: %s)" % CHAIN_NAMES.get(node.get("chain"))
        model.update(state="ok", headline="Your Lightning wallet is ready" + test,
                     text=["Use it with Zeus on your phone. " + HOT_WALLET])

    balances = info.get("balances") or {}
    if ready and lnd_info and balances:
        onchain = sats((balances.get("onchain") or {}).get("confirmed_balance"))
        local = sats(((balances.get("channels") or {}).get("local_balance") or {}).get("sat"))
        model["facts"] += [
            {"k": "In your wallet", "v": fmt_sats(onchain)},
            {"k": "In channels", "v": fmt_sats(local)},
            {"k": "Channels", "v": "%d open" % int(lnd_info.get("num_active_channels") or 0)},
        ]

    model["sections"].append(checklist(info, ready))
    model["sections"].append(more_options(info, config, ready))
    return model


def more_options(info, config, ready):
    node = info.get("node") or {}
    lnd_info = info.get("lndInfo") or {}
    more = []
    has_wallet = bool(info.get("walletExists")) and info.get("lndState") != "NON_EXISTING"
    if not has_wallet and info.get("lndState") == "NON_EXISTING":
        more.append({"title": "Restore a wallet",
                     "text": "Have 24 words from an earlier AVADO or LND wallet? Type them here, and add your channel "
                             "backup file if you have one.",
                     "form": {"action": "restore-wallet", "submit": "Restore my wallet",
                              "confirm": "Your AVADO restores the wallet from these 24 words. Continue?",
                              "fields": [{"name": "words", "type": "textarea", "label": "Your 24 words",
                                          "placeholder": "word1 word2 word3 ..."},
                                         {"name": "backup", "type": "file", "label": "Channel backup (optional)"}]}})
    if has_wallet and ready:
        other = "home" if info.get("tailscale") else "tailscale"
        if other == "home":
            more.append({"title": "Zeus on your home Wi-Fi only",
                         "text": "A QR code with your AVADO's home address, for a phone without Tailscale.",
                         "buttons": [{"action": "show-phone", "label": "Show the home QR code",
                                      "body": {"via": "home"}}]})
        else:
            more.append({"title": "Zeus away from home",
                         "text": "Install the Tailscale app on your AVADO and your phone, then come back here: the QR "
                                 "code then works anywhere. With AVADO Remote Access or the AVADO Wi-Fi, use this one.",
                         "links": [{"label": "Open Tailscale", "href": LINKS["tailscale"]}],
                         "buttons": [{"action": "show-phone", "label": "Show the QR code for lightning.my.ava.do",
                                      "body": {"via": "tailscale"}}]})
        if info.get("phoneLogin"):
            more.append({"title": "Lost a phone?",
                         "text": "Disconnect every phone at once. Then show a new QR code for the phones you keep.",
                         "buttons": [{"action": "forget-phones", "label": "Disconnect all phones", "style": "danger",
                                      "confirm": "Every phone connected to this wallet loses access now. Continue?"}]})
    more.append({"title": "How your wallet opens by itself",
                 "text": "The wallet is locked with a long random password that your AVADO made and keeps for this "
                         "app. That is how it opens again by itself after a restart or a power cut. Someone who takes "
                         "your AVADO could open it too, so keep only small amounts here, like cash in a wallet. Your 24 "
                         "words are what bring your money back on a new device; the password is not needed for that."})
    more.append({"title": "Channel backup",
                 "text": "Lightning money sits in channels with other nodes. The backup file lists them. With your 24 "
                         "words and this file, the money in your channels comes back to your wallet if this AVADO "
                         "breaks. Keep it next to your words; it is encrypted."})
    adv = [
        {"k": "LND version", "v": str(lnd_info.get("version") or "Unknown").split(" ")[0]},
        {"k": "Network", "v": CHAIN_NAMES.get(node.get("chain"), node.get("chain") or "Unknown")},
        {"k": "Node name", "v": str(lnd_info.get("alias") or config.alias)},
    ]
    if lnd_info.get("identity_pubkey"):
        adv.append({"k": "Node ID", "v": lnd_info["identity_pubkey"]})
    if lnd_info.get("block_height"):
        adv.append({"k": "Block", "v": fmt_int(lnd_info["block_height"])})
    if lnd_info:
        adv.append({"k": "Lightning peers", "v": str(lnd_info.get("num_peers") or 0)})
        adv.append({"k": "Channels open / waiting", "v": "%s / %s" % (lnd_info.get("num_active_channels") or 0,
                                                                     lnd_info.get("num_pending_channels") or 0)})
    adv += [
        {"k": "Port for other Lightning nodes", "v": str(config.p2p_port)},
        {"k": "Port for phones (REST)", "v": "%d (home network, VPNs and AVADO apps only)" % config.rest_port},
        {"k": "Tailscale", "v": "On" if info.get("tailscale") else "Not installed or not logged in"},
    ]
    return {"id": "more", "more": more, "advanced": adv, "advancedTitle": "More options"}


if __name__ == "__main__":
    App(Config()).run()
