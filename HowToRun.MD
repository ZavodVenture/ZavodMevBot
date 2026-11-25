# **ZavodMevBot Guide** (Automated Solana On-Chain Arbitrage Bot)

## What you need

- **A Linux server** (x86-64). The bot needs little CPU and memory.
- **A private Solana RPC.** Public RPCs will not work.
- **A wallet** with SOL for transaction fees and tips.

## Step 1: Download

Download the latest release archive from GitHub Releases and unpack it into a folder on the server, or clone this repository:

```bash
git clone https://github.com/ZavodVenture/ZavodMevBot.git
cd ZavodMevBot
chmod +x zavod-mev-bot-rust-version-cli upgrade.sh
```

## Step 2: Create the config

```bash
cp config.toml.example config.toml
```

Open `config.toml` and fill in:

- `[rpc] url`: your private RPC
- `[wallet] private_key`: your wallet key

Both can be environment variables instead, for example `url = "$RPC_URL"`.

The example already has working activity windows with the spam and Jito senders. Everything else uses defaults.

**Optional: encrypt the wallet key.**

```bash
./zavod-mev-bot-rust-version-cli encrypt-key --config config.toml
```

The key is encrypted with your password into `wallet.enc`. Then remove `private_key` from `config.toml`: the bot asks for the password at startup. Keep an offline backup of the original key.

## Step 3: Set up the panel (optional, recommended)

The panel runs at `https://<server IP>:8787`. Run once, as root, with port 80 free and open from the internet:

```bash
./zavod-mev-bot-rust-version-cli panel issue-cert
./zavod-mev-bot-rust-version-cli panel set-password
```

The certificate is issued by Let's Encrypt for the server's public IP; the bot renews it by itself. Open port 8787 in your firewall.

Without a certificate the bot still runs, only the panel does not start. To serve the panel over plain HTTP instead, start the bot with `run --no-tls`.

## Step 4: Run

```bash
./zavod-mev-bot-rust-version-cli run
```

The bot reads `config.toml` from the current folder (another file: `run --config path/to/config.toml`).

To keep it running after you log out, start it inside `screen` or `tmux`. Stop it with `Ctrl+C`.

Then tune activity windows and fees on the panel's configuration page and restart the bot from the panel to apply them.

## Updating

```bash
./upgrade.sh            # latest release
./upgrade.sh 0.3.0      # a specific version
```

The script replaces the bot files in the current folder and keeps your `config.toml`, keys and data. Stop the bot before updating and start it again afterwards.

When the config format changes, the bot refuses to start and explains which keys were renamed or removed. Compare your `config.toml` with the new `config.toml.example`.

## Useful commands

| Command | What it does |
|---|---|
| `run` | Start the bot |
| `encrypt-key` | Encrypt the wallet key into `wallet.enc` |
| `panel issue-cert`, `panel set-password` | Panel certificate and password |
| `check-cashback`, `claim-cashback` | Check or claim Pump.fun AMM cashback |
| `close-all-empty-atas` | Close empty token accounts and return their rent |
| `wrap --amount <SOL>` | Wrap SOL into WSOL |

Commands that use the config take `--config <file>` (default `config.toml`). `--help` lists every command and option.

## Join Telegram

Join our Telegram group to learn how to boost your profit:
[https://t.me/ZavodMevBot](https://t.me/ZavodMevBot)
