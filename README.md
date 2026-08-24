# Swiggy FeedMe Bot

Intent-driven food ordering over WhatsApp, on top of Swiggy's MCP servers.
Send a message ("chicken bowl under ₹300, fast") or a voice note, get back
two options (Safe Pick / Mood Pick), confirm, and it places the order.

Phase 1 scope: personal use only (builder's own Swiggy account), one
WhatsApp thread, official WhatsApp Cloud API for ingestion. Multi-user is
an explicit future phase, not phase 1.

## Status

Pre-build. Design and decisions are being tracked in the working plan —
see the project owner's local plan file for the current state of scope,
locked decisions, and verified Swiggy MCP tool behavior.

## Stack (planned)

- WhatsApp Cloud API (official Meta) for message ingestion — not Baileys
- FastAPI backend
- Groq (Whisper for STT, Llama for intent parsing + ranking)
- Swiggy MCP (`https://mcp.swiggy.com`) for search/cart/checkout/tracking

## Why Cloud API over Baileys

No spare number for an unofficial client to ride on a burner SIM; Meta's
Business Platform accepts virtual/VOIP numbers and provides a free test
number, which turned out to be the easier path here, not just the safer
one. Trade-off: requires a public HTTPS webhook, so this isn't pure local
execution — see plan notes on Cloudflare Tunnel as the zero-cost way to
satisfy that.
