# TG Multi Manager Bot

Multi-account Telegram automation:
- Session import: hex, string, Telethon/Pyrogram, .session, .json, .zip
- Auto reaction + view on any channel (up to 15)
- Auto VC join using PyTgCalls (mic muted, silent WAV)
- Indian name/DP randomizer from ZIP
- Paid plans: per-user quota for reactions, views, VC joins, channels, accounts
- Owner panel, admin tier, pseudo-owner tier
- Broadcast, health check, spam check via @SpamBot

## Quick Start
```bash
git clone https://github.com/nexapayz99-del/Multi-tg
cd tg-multi-manager
cp .env.example .env
# fill .env
docker-compose up -d
