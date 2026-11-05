# AVADO Lightning

An AVADO package (`lightning.avado.dnp.dappnode.eth`, title "Lightning") that runs
[LND](https://github.com/lightningnetwork/lnd), a Lightning node, on the AVADO Bitcoin Node, with a
plain-language page at `http://lightning.my.ava.do`. It is meant for everyday payments with the
[Zeus](https://zeusln.com) app on a phone. It is a hot wallet: keep only small amounts here.

| Package | Repository | Needs |
|---|---|---|
| Bitcoin Node (`bitcoincore.avado.dnp.dappnode.eth`) | AVADO-DNP-BitcoinCore | nothing |
| Lightning (`lightning.avado.dnp.dappnode.eth`) | this one | Bitcoin Node in full mode, with ZMQ (it has it) |

## For the owner: set up in 4 steps

The page shows these steps as a checklist and ticks each one off by itself.

1. **Create your wallet.** One button. Your AVADO makes the wallet and its 24 recovery words.
2. **Write down your 24 words.** They show once, with the warning to keep only small amounts here.
   Press "I have written them down" and your AVADO deletes them.
3. **Download the channel backup.** One button. Download it again when the step un-ticks (after a
   channel opens or closes).
4. **Connect Zeus on your phone.** Press "Show the QR code" and scan it in Zeus (Advanced set-up,
   connect your own node). With the AVADO Tailscale app logged in, the code works at home and away;
   otherwise it uses the home address and works on the home Wi-Fi.

If the Bitcoin Node keeps only the newest blocks (space-saving mode), the page says only:
"Lightning needs the full Bitcoin history. Switch Bitcoin Node to Full in its settings." LND does not
start until then.

Behind **More options**: restore a wallet from 24 words (and a channel backup file), the other QR
code (home Wi-Fi only, or `lightning.my.ava.do` for Tailscale, AVADO Remote Access and the AVADO
Wi-Fi), "Disconnect all phones", how the wallet opens by itself, and the technical details.

## Page PIN

The 24 words, the phone QR code ("Show the QR code", "Connect another phone"), "Disconnect all
phones", creating or restoring the wallet and the channel-backup download ask for a page PIN
(6 to 12 digits), chosen the first time one of them is pressed. Same rules as AVADO AI Connect
(`build/app/pin.py`): scrypt in `/data/avado/pin.json`; an HttpOnly, SameSite=Strict session cookie
for 30 minutes at most, 10 minutes without use; per device 5 tries, then waits that double from
1 minute to 15 minutes; for the whole box, after more than 20 wrong PINs in an hour every try waits
3 seconds. Forgot it: Restart Lightning in the AVADO Admin (a new container of the same version)
removes the PIN; the wallet, words and phones stay. A reboot, power cut or update does not.
Note: everyone coming in through AVADO Remote Access shares one address, so for the device count
they are one device.

## How it works

- **Bitcoin Node login.** The app fetches its own RPC login from the Bitcoin Node
  (`GET /internal/rpc-login/lnd`, answered only to this app's container). The Bitcoin Node lets this
  login call only what LND needs (`rpcwhitelist`, checked with `-debug=rpc` on regtest). Before it
  starts LND, the app checks with `getblockchaininfo` that the node is not pruned and with
  `getzmqnotifications` that it publishes raw blocks and raw transactions on two addresses
  (28332 and 28333).
- **Wallet in the browser.** "Create my wallet" calls LND's `GenSeed` and `InitWallet` from inside
  the container. The 24 words are kept in a private file (`/data/avado/seed-once.json`, mode 0600)
  only until the owner confirms them; then the file is overwritten and deleted. They are never logged.
- **Opens by itself.** On the first start the app makes a random 43-character wallet password and
  keeps it in the app's volume (`/data/avado/wallet-password`, 0600). LND gets it through
  `wallet-unlock-password-file`, so the wallet opens after every restart or power cut. Plainly:
  whoever has the AVADO can open the wallet, which is why the page says to keep only small amounts.
- **Wallet creation cannot be hijacked from the network.** `wallet-unlock-allow-create` lets LND wait
  for a new wallet without a login, so LND listens on `127.0.0.1` only, and the published phone port
  8080 is a gate (`gate.py`) that stays shut until the wallet exists.
- **Phone (Zeus).** The QR code is an [lndconnect](https://github.com/LN-Zap/lndconnect) link to
  LND's REST port with the TLS certificate and a phone login (macaroon) baked for it: everything the
  admin login may do except making or deleting logins. "Disconnect all phones" deletes that login's
  root key, so every phone that scanned the code loses access; the next QR code has a new login.
  The QR code is made only when the owner presses the button (an `X-Avado` POST), never in the
  status the page polls.
- **Who reaches LND.** Port 8080 (REST, TLS) is published for phones on the home network and lets in
  only the home network, VPNs (Tailscale's 100.64/10), the AVADO Wi-Fi/Remote Access and AVADO apps
  (172.33/16); anyone else is cut off even when the router forwards the port. Port 9735 (Lightning
  peers) is published as usual. gRPC (10009) stays inside the container (`docker exec ... lncli`
  works for support).
- **Tailscale.** The app reads the AVADO Tailscale app's short status (`state: running`). If it runs,
  the QR code points at `lightning.my.ava.do:8080`, which a phone with Tailscale (route and DNS steps
  of the Tailscale app done) reaches anywhere.
- **Channel backup.** `GET /api/download/channel-backup` serves LND's `channel.backup`. The app keeps
  the SHA-256 of the last download, and the step un-ticks when LND writes a new backup.
- **TLS.** LND's own self-signed certificate, with `lightning.my.ava.do` and the home address in it,
  no container addresses (`tlsdisableautofill`), renewed by LND (`tlsautorefresh`).
- **Supervisor.** Restarts LND with a growing pause and stops it cleanly (SIGTERM) on shutdown.

The page is the same generic page as the other AVADO Bitcoin apps (Python standard library only,
strict Content-Security-Policy, `/api/*` only for requests addressed to the box, buttons need the
`X-Avado: 1` header). This copy adds a checklist, a small form, downloads and "reveal" cards.

Settings (Admin, package page): `ALIAS` (the node's public name, default `AVADO`).

## No web UI (RTL / ThunderHub)

Left out on purpose. Zeus does everything an owner needs (receive, pay, open and close channels,
on-chain); a second web app would add its own password, a much bigger image and more attack surface
on a hot wallet. It can come later behind "More options" if owners ask for it.

## Image

`build/Dockerfile` downloads the official `lnd-linux-amd64-<version>.tar.gz` from the LND GitHub
release and checks its SHA-256 against the release's `manifest-<version>.txt`, whose signatures were
verified with gpg against the keys in the LND repository (`scripts/keys`). Debian bookworm slim,
pinned by digest (the same as the Bitcoin Node), plus `qrencode` for the QR code. The page and
supervisor are Python standard library only; their unit tests run during the image build. Everything
runs as an unprivileged user.

## Licence and reuse

GPL-3.0 (see LICENSE). LND is MIT-licensed.

[dappnode/DAppNodePackage-LightningNetwork](https://github.com/dappnode/DAppNodePackage-LightningNetwork)
(GPL-3.0, Copyright DAppNode Association and contributors) was studied: its ports (9735, REST 8080),
the ZMQ addresses on the Bitcoin package (28332/28333) and the rule that the Bitcoin node must not
be pruned follow it. No code was copied: it pins LND 0.14 (2022) with RTL and runs `--noseedbackup`,
so its wallet has no recovery words. The page, `avadoweb.py`, `bitcoinrpc.py` and `wamp.py` come
from the AVADO Bitcoin apps, `gate.py` from the Electrum Server.

## Development

```
cd build/app && python3 -m unittest discover -s tests
docker buildx build --platform linux/amd64 --load -t lightning.avado.dnp.dappnode.eth:0.1.0 -f build/Dockerfile .
```

End to end on regtest: run the Bitcoin Node image with `NETWORK=regtest MODE=full
APP_HOSTS_LND=<this container's name>` and this image with `BITCOIN_HOST=<the node's container name>`
on one Docker network.
