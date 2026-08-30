# The desk side of the Kite session bridge (M58)

Two things live on the momentum desk (`desk@65.0.226.77`) so that the Baskfy box can borrow the
Kite session the desk's morning login produces. **Neither is application code** — the trading app
is untouched, which is the root `CLAUDE.md` safety rail "the desk must be able to rebalance on any
Friday" taken literally.

## 1. `emit-kite-token` → `~desk/bin/emit-kite-token`, mode 0500

Reads one field out of `data/.kite_token.json` and writes it to stdout. It cannot write the token,
refresh it, or reach the trading app.

## 2. One line in `~desk/.ssh/authorized_keys`

    restrict,command="/home/desk/bin/emit-kite-token",from="3.108.148.38" ssh-ed25519 AAAA... baskfy-box kite-session pull (forced command only)

`restrict` disables pty allocation and agent, port and X11 forwarding. `command=` replaces
whatever the client asks for, so the key cannot open a shell — verified adversarially: asking it
to run `id; cat .../.env` returns the token instead. `from=` pins it to the box's egress IP.

The public key comes from `tools/deploy/install-kite-session.sh`, which generates the pair **on
the box** and prints only the public half. There is deliberately no step that copies a private key
anywhere.

## Rotating

Delete `/opt/baskfy/secrets/ssh/kite-session` on the box, re-run the installer, and replace the
line above with the new public key. The old line is the whole of the revocation.

## If the box's IP changes

The `from=` clause stops matching and the pull fails — visibly, in the pipeline's stderr, with
the night continuing on the bhavcopy. Update the clause and the Kite app's IP whitelist together;
they are the same fact written in two places.
