import base64
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import avadoweb  # noqa: E402
import gate  # noqa: E402
import main  # noqa: E402

LOGIN = ("lnd", "pw-secret-123", 8332)
ZMQ = {"pubrawblock": 28332, "pubrawtx": 28333}
CERT = """-----BEGIN CERTIFICATE-----
MIIBnzCCAUWgAwIBAgIUPQ7gQ+ywVfbTwFhDIgqU7ZQ2yAAwCgYIKoZIzj0EAwIw
FjEUMBIGA1UEAwwLZXhhbXBsZS5jb20wHhcNMjYwMTAxMDAwMDAwWhcNMzYwMTAx
MDAwMDAwWjAWMRQwEgYDVQQDDAtleGFtcGxlLmNvbTBZMBMGByqGSM49AgEGCCqG
SM49AwEHA0IABHbYxw1Jr6O0SjVqzK1o2HIxj0iO2xn2T0Wv8Lr8q8bQyQ2dKpYb
sR0b+fM1pP0E3V1t3tCw6QwJk9Vx8b4pPTSjbzBtMB0GA1UdDgQWBBQb3d4z4b2C
D9w0lD6hD0Y1j2q2mDAfBgNVHSMEGDAWgBQb3d4z4b2CD9w0lD6hD0Y1j2q2mDAP
BgNVHRMBAf8EBTADAQH/MBoGA1UdEQQTMBGCD2xpZ2h0bmluZy5teS5hdmEwCgYI
KoZIzj0EAwIDSAAwRQIhAKm3z0n4Yb8yP3l0l9Q2lZr8V1rG0+3tq0p7k7A9b2zC
AiBz8b7cV0x2kq1k5o4l1QxN7oG2Yt7rO5c1l3o5J9p2Qw==
-----END CERTIFICATE-----
"""

WORDS = ["abandon"] * 23 + ["art"]


def config(**env):
    return main.Config(env)


def node(**kw):
    base = {"chain": "main", "blocks": 900000, "headers": 900000, "verificationprogress": 1.0,
            "initialblockdownload": False, "pruned": False}
    base.update(kw)
    return base


def info(**kw):
    base = {"nodeInstalled": True, "loginError": None, "node": node(), "nodeError": None, "zmq": ZMQ,
            "running": True, "lastExit": None, "lndState": "SERVER_ACTIVE",
            "lndInfo": {"synced_to_chain": True, "version": "0.21.6-beta commit=v0.21.6-beta", "alias": "AVADO",
                        "identity_pubkey": "02" + "ab" * 32, "block_height": 900000, "num_peers": 3,
                        "num_active_channels": 1, "num_pending_channels": 0},
            "balances": {"onchain": {"confirmed_balance": "150000"},
                         "channels": {"local_balance": {"sat": "50000"}}},
            "walletExists": True, "passwordFile": True, "seedPending": False, "wordsDone": True,
            "backupSha": "aa", "backupDownloadedSha": "aa", "phoneShown": True, "phoneLogin": True,
            "tailscale": False, "lanIp": "192.168.1.20", "restored": False}
    base.update(kw)
    return base


def main_words(model):
    """Words on the main screen: the hero, the facts and the set-up steps (not "More options")."""
    parts = [model.get("headline", "")] + list(model.get("text") or [])
    for fact in model.get("facts") or []:
        parts += [fact["k"], fact["v"]]
    for section in model.get("sections") or []:
        if section.get("id") == "more":
            parts.append(section.get("advancedTitle") or "")
            continue
        parts += [section.get("title") or ""] + list(section.get("text") or [])
        for step in section.get("checklist") or []:
            parts.append(step["title"])
            if not step.get("done"):
                parts.append(step.get("text") or "")
                parts += [b["label"] for b in (step.get("buttons") or []) + (step.get("links") or [])]
        parts += [b["label"] for b in (section.get("buttons") or []) + (section.get("links") or [])]
    return len(" ".join(parts).split())


class ConfTest(unittest.TestCase):
    def test_conf_uses_node_login_and_zmq(self):
        conf = main.render_conf(config(), "main", LOGIN, ZMQ, "192.168.1.20", "/data/avado/wallet-password")
        host = "DAppNodePackage-bitcoincore.avado.dnp.dappnode.eth"
        self.assertIn("bitcoin.mainnet=true", conf)
        self.assertIn("bitcoin.node=bitcoind", conf)
        self.assertIn("bitcoind.rpchost=%s:8332" % host, conf)
        self.assertIn("bitcoind.zmqpubrawblock=tcp://%s:28332" % host, conf)
        self.assertIn("bitcoind.zmqpubrawtx=tcp://%s:28333" % host, conf)
        self.assertIn("wallet-unlock-password-file=/data/avado/wallet-password", conf)
        self.assertIn("wallet-unlock-allow-create=true", conf)
        self.assertIn("tlsextraip=192.168.1.20", conf)
        self.assertIn("tlsextradomain=lightning.my.ava.do", conf)

    def test_lnd_listens_on_loopback_only(self):
        conf = main.render_conf(config(), "main", LOGIN, ZMQ, None, None)
        self.assertIn("restlisten=127.0.0.1:8081", conf)
        self.assertIn("rpclisten=127.0.0.1:10009", conf)
        self.assertNotIn("restlisten=0.0.0.0", conf)
        self.assertNotIn("tlsextraip", conf)
        # Without the password file LND must not be told to use one (it would stop).
        self.assertNotIn("wallet-unlock", conf)

    def test_networks(self):
        for chain, flag in (("regtest", "regtest"), ("signet", "signet"), ("testnet4", "testnet4")):
            conf = main.render_conf(config(), chain, LOGIN, ZMQ, None, None)
            self.assertIn("bitcoin.%s=true" % flag, conf)
            self.assertNotIn("bitcoin.mainnet", conf)

    def test_alias_is_cleaned(self):
        self.assertEqual(config(ALIAS="My node\n;x=1").alias, "My nodex1")
        self.assertEqual(config(ALIAS="").alias, "AVADO")


class ZmqTest(unittest.TestCase):
    def test_both_needed_on_different_ports(self):
        notes = [{"type": "pubhashblock", "address": "tcp://0.0.0.0:28332"},
                 {"type": "pubrawblock", "address": "tcp://0.0.0.0:28332"},
                 {"type": "pubrawtx", "address": "tcp://0.0.0.0:28333"}]
        self.assertEqual(main.zmq_ports(notes), ZMQ)
        self.assertIsNone(main.zmq_ports(notes[:2]))
        self.assertIsNone(main.zmq_ports([]))
        same = [{"type": "pubrawblock", "address": "tcp://0.0.0.0:28332"},
                {"type": "pubrawtx", "address": "tcp://0.0.0.0:28332"}]
        self.assertIsNone(main.zmq_ports(same))


class WordsTest(unittest.TestCase):
    def test_24_words(self):
        self.assertEqual(main.parse_words("  " + " ".join(WORDS).upper() + "\n"), WORDS)
        self.assertEqual(main.parse_words("1. abandon 2. abandon " + " ".join(WORDS[2:])), WORDS)
        with self.assertRaises(avadoweb.UserError):
            main.parse_words("abandon abandon")


class PhoneTest(unittest.TestCase):
    def test_lndconnect_url(self):
        url = main.lndconnect_url("192.168.1.20", 8080, CERT, "0201abcdef")
        self.assertTrue(url.startswith("lndconnect://192.168.1.20:8080?cert="))
        query = url.split("?", 1)[1]
        params = dict(p.split("=", 1) for p in query.split("&"))
        self.assertNotIn("=", params["cert"] + params["macaroon"])  # base64url without padding
        self.assertNotIn("+", params["cert"])
        self.assertNotIn("/", params["cert"])
        mac = base64.urlsafe_b64decode(params["macaroon"] + "=" * (-len(params["macaroon"]) % 4))
        self.assertEqual(mac.hex(), "0201abcdef")

    def test_phone_cannot_make_logins(self):
        perms = set(main.PHONE_PERMISSIONS)
        self.assertNotIn(("macaroon", "generate"), perms)
        self.assertNotIn(("macaroon", "write"), perms)
        self.assertIn(("offchain", "write"), perms)
        self.assertIn(("invoices", "write"), perms)

    @unittest.skipUnless(shutil.which("qrencode"), "qrencode is in the image")
    def test_qr_is_png(self):
        uri = main.qr_data_uri("lndconnect://x:8080?macaroon=AA")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1])[:4], b"\x89PNG")

    def test_reveal_texts(self):
        r = main.phone_reveal("lndconnect://x", "data:image/png;base64,AA", "tailscale", None)
        self.assertIn("Tailscale", r["text"][0])
        r = main.phone_reveal("lndconnect://x", "data:", "home", "192.168.1.20")
        self.assertIn("home Wi-Fi", r["text"][0])
        self.assertIn("own phone", r["warn"])


class ModelTest(unittest.TestCase):
    def test_pruned_node_plain_page(self):
        m = main.build_model(info(node=node(pruned=True), running=False, lndState=None), config())
        self.assertEqual(m["headline"], "Lightning needs the full Bitcoin history")
        self.assertEqual(m["text"][0], "Switch Bitcoin Node to Full in its settings.")
        self.assertEqual(m["sections"][0]["links"][0]["href"], "http://bitcoincore.my.ava.do/#connect")
        self.assertNotIn("setup", [s.get("id") for s in m["sections"]])

    def test_no_bitcoin_node(self):
        m = main.build_model(info(nodeInstalled=False), config())
        self.assertEqual(m["headline"], "The Bitcoin Node is not running")

    def test_old_bitcoin_node(self):
        m = main.build_model(info(loginError={"code": 404}, node={}), config())
        self.assertEqual(m["headline"], "Update the Bitcoin Node app")
        m = main.build_model(info(zmq=None), config())
        self.assertEqual(m["headline"], "Update the Bitcoin Node app")

    def test_new_wallet(self):
        m = main.build_model(info(lndState="NON_EXISTING", walletExists=False, lndInfo=None, balances=None,
                                  wordsDone=False, backupSha=None, backupDownloadedSha=None, phoneShown=False,
                                  phoneLogin=False), config())
        self.assertEqual(m["headline"], "Create your Lightning wallet")
        self.assertIn("Keep only small amounts here.", m["text"][0])
        steps = next(s for s in m["sections"] if s["id"] == "setup")["checklist"]
        self.assertEqual([s["done"] for s in steps], [False, False, False, False])
        self.assertEqual(steps[0]["buttons"][0]["action"], "create-wallet")
        self.assertIn("power cut", steps[0]["text"])
        for s in steps[1:]:
            self.assertFalse(s.get("buttons") or s.get("links"))
        more = next(s for s in m["sections"] if s["id"] == "more")
        self.assertIn("Restore a wallet", [x["title"] for x in more["more"]])
        self.assertLessEqual(main_words(m), 120)

    def test_words_waiting(self):
        m = main.build_model(info(seedPending=True, wordsDone=False, backupDownloadedSha=None,
                                  phoneShown=False), config())
        steps = next(s for s in m["sections"] if s["id"] == "setup")["checklist"]
        self.assertEqual([s["done"] for s in steps], [True, False, False, False])
        self.assertEqual(steps[1]["buttons"][0]["action"], "show-words")
        self.assertEqual(steps[2]["links"][0]["href"], "api/download/channel-backup")
        self.assertEqual(steps[3]["buttons"][0]["action"], "show-phone")
        self.assertLessEqual(main_words(m), 120)

    def test_backup_changed_unticks(self):
        m = main.build_model(info(backupSha="bb", backupDownloadedSha="aa"), config())
        steps = next(s for s in m["sections"] if s["id"] == "setup")["checklist"]
        self.assertFalse(steps[2]["done"])
        self.assertIn("changed", steps[2]["text"])

    def test_ready(self):
        m = main.build_model(info(), config())
        self.assertEqual(m["state"], "ok")
        self.assertEqual(m["headline"], "Your Lightning wallet is ready")
        self.assertEqual([f["v"] for f in m["facts"]], ["150,000 sats", "50,000 sats", "1 open"])
        steps = next(s for s in m["sections"] if s["id"] == "setup")["checklist"]
        self.assertTrue(all(s["done"] for s in steps))
        self.assertLessEqual(main_words(m), 120)
        more = next(s for s in m["sections"] if s["id"] == "more")
        titles = [x["title"] for x in more["more"]]
        self.assertIn("Lost a phone?", titles)
        self.assertNotIn("Restore a wallet", titles)
        self.assertNotIn("pw-secret", json.dumps(m))

    def test_node_still_downloading(self):
        m = main.build_model(info(node=node(verificationprogress=0.42, initialblockdownload=True),
                                  lndInfo={"synced_to_chain": False}, balances=None), config())
        self.assertEqual(m["headline"], "Waiting for your Bitcoin node to finish downloading")
        self.assertEqual(m["progress"]["percent"], 42.0)

    def test_locked_without_password(self):
        m = main.build_model(info(lndState="LOCKED", passwordFile=False), config())
        self.assertEqual(m["headline"], "Your Lightning wallet is locked")

    def test_tailscale_switches_the_other_option(self):
        m = main.build_model(info(tailscale=True), config())
        more = next(s for s in m["sections"] if s["id"] == "more")
        self.assertIn("Zeus on your home Wi-Fi only", [x["title"] for x in more["more"]])
        m = main.build_model(info(tailscale=False), config())
        more = next(s for s in m["sections"] if s["id"] == "more")
        self.assertIn("Zeus away from home", [x["title"] for x in more["more"]])

    def test_lnd_stopped(self):
        m = main.build_model(info(running=False, lastExit={"code": 1, "at": time.time(), "why": None,
                                                           "pause": 20}), config())
        self.assertEqual(m["headline"], "Lightning is starting again")


class ExitTest(unittest.TestCase):
    def test_reasons(self):
        self.assertEqual(main.explain_exit(["write: No space left on device"]), "disk")
        self.assertEqual(main.explain_exit(["unable to unlock wallet: invalid passphrase"]), "password")
        self.assertIsNone(main.explain_exit(["Shutdown complete"]))


class GateTest(unittest.TestCase):
    def test_ready_keeps_callers_out(self):
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(4)
        port = target.getsockname()[1]
        state = {"open": False}
        g = gate.Gate(0, port, host="127.0.0.1", ready=lambda: state["open"])
        g.listen_port = 0
        g.start()
        gport = g.sock.getsockname()[1]
        c = socket.create_connection(("127.0.0.1", gport), timeout=3)
        self.assertEqual(c.recv(10), b"")  # closed at once
        c.close()
        state["open"] = True
        c = socket.create_connection(("127.0.0.1", gport), timeout=3)
        target.settimeout(3)
        conn, _ = target.accept()
        c.sendall(b"hi")
        self.assertEqual(conn.recv(10), b"hi")
        for s in (c, conn, target, g.sock):
            s.close()

    def test_sources(self):
        self.assertTrue(gate.allowed_source("192.168.1.5"))
        self.assertTrue(gate.allowed_source("172.33.0.9"))
        self.assertTrue(gate.allowed_source("100.101.102.103"))
        self.assertFalse(gate.allowed_source("8.8.8.8"))


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "index.html"), "w") as handle:
            handle.write("<html></html>")

        def backup():
            return "avado-lightning-channel-backup-2026-11-05.backup", b"\x00\x01backup"

        def missing():
            raise avadoweb.UserError("There is no channel backup yet.")

        web = avadoweb.WebApp(self.tmp, lambda: {}, downloads={"channel-backup": backup, "none": missing})
        self.server = web.serve(port=0, host="127.0.0.1")
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def get(self, path, host="lightning.my.ava.do"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": host})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_download(self):
        resp, body = self.get("/api/download/channel-backup")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"\x00\x01backup")
        self.assertIn('filename="avado-lightning-channel-backup-2026-11-05.backup"',
                      resp.getheader("Content-Disposition"))
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_download_refuses_foreign_host(self):
        resp, _ = self.get("/api/download/channel-backup", host="evil.example.com")
        self.assertEqual(resp.status, 403)

    def test_download_missing(self):
        resp, body = self.get("/api/download/none")
        self.assertEqual(resp.status, 409)
        resp, _ = self.get("/api/download/other")
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
